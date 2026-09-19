from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    complete_personal_terms_queryset,
    is_complete_personal_terms,
)
from apps.attendance.personal_offers import direct_personal_offer_payload, personal_offer_payload
from apps.attendance.services.enrollment import (
    ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES,
    _as_club_aware,
    _booking_time,
    _personal_session_group_name,
    _save_booking_event,
    _save_enrollment,
)
from apps.attendance.services.personal_terms import (
    clone_complete_personal_terms_snapshot,
    create_complete_personal_terms_snapshot,
    create_legacy_partial_personal_terms_snapshot,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import Club, Location
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate

DROP_IN_STUDENT_STATUSES = {
    Student.Status.LEAD,
    Student.Status.TRIAL,
    Student.Status.ACTIVE,
    Student.Status.AT_RISK,
    Student.Status.CHURNED,
}

LIVE_PAYMENT_STATUSES = (Payment.Status.PENDING,)
PERSONAL_DROP_IN_ATTENDANCE_REASON_MAX_LENGTH = 500
PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX = "Ручная корректировка посещения: "


@dataclass(frozen=True)
class PersonalDropInBookingResult:
    booking: PersonalDropInBooking
    schedule: Schedule
    enrollment: ScheduleEnrollment
    created: bool
    availability_slot_id: int | None = None


@dataclass(frozen=True)
class PersonalDropInAttendanceCorrectionResult:
    booking: PersonalDropInBooking
    checkin: Checkin
    group_session: GroupSession | None
    created: bool


@dataclass(frozen=True)
class PersonalTimeConflictScope:
    schedule_ids: tuple[int, ...]
    slot_ids: tuple[int, ...]
    reservation_ids: tuple[int, ...]


def _clean_idempotency_key(value: str | None, *, prefix: str) -> str:
    cleaned = (value or "").strip()
    return cleaned or f"{prefix}-{uuid4()}"


def _normalize_personal_drop_in_attendance_reason(reason: str) -> str:
    normalized = " ".join(reason.split())
    if not normalized:
        raise BusinessLogicError(
            "Укажите причину ручного подтверждения посещения",
            code="attendance_correction_reason_required",
        )
    if len(normalized) > PERSONAL_DROP_IN_ATTENDANCE_REASON_MAX_LENGTH:
        raise BusinessLogicError(
            "Причина ручного подтверждения не должна превышать 500 символов",
            code="attendance_correction_reason_too_long",
        )
    return normalized


def record_personal_drop_in_attendance_correction(
    *,
    club_id: int,
    booking_id: int,
    actor_user_id: int,
    reason: str,
) -> PersonalDropInAttendanceCorrectionResult:
    """Record an exact completed drop-in through the canonical batch cascade."""
    from apps.attendance.services.checkin import batch_checkin

    normalized_reason = _normalize_personal_drop_in_attendance_reason(reason)
    booking = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related(
            "checkin",
            "enrollment",
            "enrollment__schedule",
            "enrollment__schedule__training_type",
        )
        .filter(id=booking_id)
        .first()
    )
    if booking is None:
        raise BusinessLogicError(
            "Разовая персональная бронь не найдена",
            code="personal_drop_in_booking_not_found",
        )

    enrollment = booking.enrollment
    schedule = enrollment.schedule
    if (
        enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN
        or enrollment.starts_on is None
        or enrollment.starts_on != enrollment.ends_on
        or schedule.one_time_date != enrollment.starts_on
        or schedule.training_type.kind != TrainingType.Kind.PERSONAL
    ):
        raise BusinessLogicError(
            "Ручное подтверждение доступно только для точной разовой персональной брони",
            code="personal_drop_in_attendance_not_supported",
        )

    if booking.state == PersonalDropInBooking.State.ATTENDED:
        checkin = (
            Checkin.objects.for_club(club_id)
            .filter(
                id=booking.checkin_id,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .first()
        )
        if checkin is None:
            raise BusinessLogicError(
                "Посещение разовой персональной брони требует проверки",
                code="personal_drop_in_attendance_inconsistent",
            )
        group_session = (
            GroupSession.objects.for_club(club_id)
            .filter(schedule_id=schedule.id, date=schedule.one_time_date)
            .first()
        )
        return PersonalDropInAttendanceCorrectionResult(
            booking=booking,
            checkin=checkin,
            group_session=group_session,
            created=False,
        )
    if booking.state != PersonalDropInBooking.State.SCHEDULED:
        raise BusinessLogicError(
            "Отменённую или пропущенную бронь нельзя зачесть как посещённую",
            code="personal_drop_in_attendance_terminal",
        )

    batch_result = batch_checkin(
        club_id=club_id,
        schedule_id=schedule.id,
        checkin_date=schedule.one_time_date,
        present_student_ids=[enrollment.student_id],
        training_type_id=schedule.training_type_id,
        actor_user_id=actor_user_id,
        notes=f"{PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX}{normalized_reason}",
        _preserve_closed_session_on_idempotent_retry=True,
    )
    booking = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related("checkin", "enrollment", "enrollment__schedule")
        .get(id=booking_id)
    )
    if booking.state != PersonalDropInBooking.State.ATTENDED or booking.checkin is None:
        raise BusinessLogicError(
            "Посещение не было сохранено",
            code="personal_drop_in_attendance_inconsistent",
        )
    group_session = (
        GroupSession.objects.for_club(club_id)
        .filter(id=batch_result["group_session_id"])
        .first()
    )
    return PersonalDropInAttendanceCorrectionResult(
        booking=booking,
        checkin=booking.checkin,
        group_session=group_session,
        created=any(item["created"] for item in batch_result["checkins"]),
    )


def _drop_in_contract_error(message: str) -> None:
    raise BusinessLogicError(
        message,
        code="personal_drop_in_tariff_invalid",
    )


def validate_personal_drop_in_tariff_contract(
    *,
    club_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    tariff_id: int,
    lock: bool = False,
    allow_designated_offer: bool = False,
) -> tuple[Trainer, Location, TrainingType, Tariff]:
    """Validate the immutable one-credit contract required by pay-at-club booking."""
    trainer_qs = Trainer.objects.for_club(club_id)
    type_qs = TrainingType.objects.for_club(club_id)
    tariff_qs = Tariff.objects.for_club(club_id).select_related("location", "training_type")
    if lock:
        trainer_qs = trainer_qs.select_for_update(of=("self",))
        type_qs = type_qs.select_for_update(of=("self",))
        tariff_qs = tariff_qs.select_for_update(of=("self",))

    trainer = trainer_qs.filter(id=trainer_id, is_active=True).first()
    if trainer is None:
        raise BusinessLogicError("Trainer does not belong to this club", code="trainer_club_mismatch")
    location = Location.objects.filter(id=location_id, club_id=club_id).first()
    if location is None:
        raise BusinessLogicError("Location does not belong to this club", code="location_club_mismatch")
    if (
        not TrainerLocation.objects.for_club(club_id)
        .filter(
            trainer_id=trainer_id,
            location_id=location_id,
        )
        .exists()
    ):
        raise BusinessLogicError("Trainer is not assigned to this location", code="trainer_location_required")
    training_type = type_qs.filter(id=training_type_id, is_active=True).first()
    if training_type is None:
        raise BusinessLogicError("Training type does not belong to this club", code="training_type_club_mismatch")
    if training_type.kind != TrainingType.Kind.PERSONAL:
        _drop_in_contract_error("Drop-in requires an active personal training type")
    if (
        not allow_designated_offer
        and (training_type.drop_in_price is None or training_type.drop_in_price <= Decimal("0"))
    ):
        _drop_in_contract_error("Configure a positive personal drop-in price")

    tariff = tariff_qs.filter(id=tariff_id, is_active=True).first()
    if tariff is None:
        raise BusinessLogicError("Tariff does not belong to this club", code="tariff_club_mismatch")
    if tariff.training_type_id != training_type.id:
        _drop_in_contract_error("Tariff must belong to the selected personal training type")
    if tariff.scope == Tariff.Scope.LOCATION and tariff.location_id != location.id:
        _drop_in_contract_error("Tariff does not cover the selected location")
    if not allow_designated_offer and tariff.price != training_type.drop_in_price:
        _drop_in_contract_error("Tariff price must equal the personal drop-in price")
    if tariff.trainings_limit != 1:
        _drop_in_contract_error("Drop-in tariff must contain exactly one training")

    components_qs = TariffComponent.objects.for_club(club_id).filter(tariff_id=tariff.id, is_active=True)
    if lock:
        components_qs = components_qs.select_for_update(of=("self",))
    components = list(components_qs.select_related("location", "training_type"))
    if len(components) != 1:
        _drop_in_contract_error("Drop-in tariff must have exactly one active component")
    component = components[0]
    if (
        component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS
        or component.credits_total != 1
        or component.training_type_id != training_type.id
        or component.paid_amount_basis != tariff.price
        or component.trainer_payout_policy != Tariff.PayoutPolicy.ON_CHECKIN
    ):
        _drop_in_contract_error("Drop-in tariff component must be one paid on-check-in personal credit")
    if component.scope == Tariff.Scope.LOCATION and component.location_id != location.id:
        _drop_in_contract_error("Drop-in tariff component does not cover the selected location")
    if _effective_tariff_payout_policy(tariff=tariff) != Tariff.PayoutPolicy.ON_CHECKIN:
        _drop_in_contract_error("Drop-in tariff payout policy must be on check-in")
    if (
        not TrainerRate.objects.for_club(club_id)
        .filter(
            trainer_id=trainer.id,
            location_id=location.id,
            training_type_id=training_type.id,
        )
        .exists()
    ):
        raise BusinessLogicError("Trainer rate is required for this personal booking", code="trainer_rate_required")
    return trainer, location, training_type, tariff


def _effective_tariff_payout_policy(*, tariff: Tariff) -> str:
    return tariff.trainer_payout_policy or Tariff.PayoutPolicy.ON_CHECKIN


def _club_local_time_bounds(
    *, club_id: int, starts_at: datetime, ends_at: datetime
) -> tuple[datetime, datetime]:
    club = Club.objects.only("id", "timezone").get(id=club_id)
    return (
        _as_club_aware(starts_at, club=club),
        _as_club_aware(ends_at, club=club),
    )


def _preview_conflicting_schedule_ids(
    *, club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> list[int]:
    """Preview schedule inputs which can produce an effective occurrence."""
    local_starts_at, local_ends_at = _club_local_time_bounds(
        club_id=club_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    target_date = local_starts_at.date()
    preview_exception_schedule_ids = list(
        ScheduleException.objects.for_club(club_id)
        .filter(
            models.Q(date=target_date) | models.Q(new_date=target_date),
            schedule__is_active=True,
        )
        .values_list("schedule_id", flat=True)
    )
    direct_schedule_filter = models.Q(
        trainer_id=trainer_id,
        start_time__lt=_booking_time(local_ends_at),
        end_time__gt=_booking_time(local_starts_at),
    ) & (
        models.Q(one_time_date=target_date)
        | models.Q(one_time_date__isnull=True, day_of_week=target_date.weekday())
    )
    return list(
        Schedule.objects.for_club(club_id)
        .filter(is_active=True)
        .filter(direct_schedule_filter | models.Q(id__in=preview_exception_schedule_ids))
        .order_by("id")
        .values_list("id", flat=True)
    )


def _lock_conflicting_schedule_rows(
    *, club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> list[int]:
    """Lock the schedule inputs which can produce an effective trainer occurrence."""
    local_starts_at, _local_ends_at = _club_local_time_bounds(
        club_id=club_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    target_date = local_starts_at.date()
    preview_schedule_ids = _preview_conflicting_schedule_ids(
        club_id=club_id,
        trainer_id=trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    locked_schedule_ids = list(
        Schedule.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=preview_schedule_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    # Lock matching exception rows only after every known participating
    # schedule row. A just-created exception is still visible to the
    # effective-occurrence selector below, which will reject the booking.
    list(
        ScheduleException.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            models.Q(date=target_date) | models.Q(new_date=target_date),
            schedule__is_active=True,
        )
        .values_list("id", flat=True)
    )
    return locked_schedule_ids


def preview_personal_time_conflict_scope(
    *, club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> PersonalTimeConflictScope:
    """Preview every mutable time owner before the D12 financial suffix."""

    now = timezone.now()
    slot_ids = PersonalAvailabilitySlot.objects.for_club(club_id).filter(
        trainer_id=trainer_id,
        status__in=[
            PersonalAvailabilitySlot.Status.PUBLISHED,
            PersonalAvailabilitySlot.Status.HELD,
            PersonalAvailabilitySlot.Status.BOOKED,
            PersonalAvailabilitySlot.Status.BLOCKED,
        ],
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    ).values_list("id", flat=True)
    reservation_ids = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(
            trainer_id=trainer_id,
            status__in=ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .filter(
            models.Q(
                status__in=[
                    PersonalBookingPaymentReservation.Status.BOOKED,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ]
            )
            | models.Q(expires_at__gt=now)
        )
        .values_list("id", flat=True)
    )
    return PersonalTimeConflictScope(
        schedule_ids=tuple(
            _preview_conflicting_schedule_ids(
                club_id=club_id,
                trainer_id=trainer_id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        ),
        slot_ids=tuple(slot_ids),
        reservation_ids=tuple(reservation_ids),
    )


def _lock_overlapping_slots(
    *, club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> list[PersonalAvailabilitySlot]:
    return list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=[
                PersonalAvailabilitySlot.Status.PUBLISHED,
                PersonalAvailabilitySlot.Status.HELD,
                PersonalAvailabilitySlot.Status.BOOKED,
                PersonalAvailabilitySlot.Status.BLOCKED,
            ],
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .order_by("id")
    )


def _lock_live_reservations(
    *, club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> list[PersonalBookingPaymentReservation]:
    now = timezone.now()
    return list(
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .filter(
            models.Q(
                status__in=[
                    PersonalBookingPaymentReservation.Status.BOOKED,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ]
            )
            | models.Q(expires_at__gt=now)
        )
        .order_by("id")
    )


def _schedule_conflicts(
    *, schedule_ids: list[int], club_id: int, trainer_id: int, starts_at: datetime, ends_at: datetime
) -> bool:
    # An occurrence may enter this date or trainer only through a schedule
    # exception, so base candidate IDs cannot decide whether it conflicts.
    # Keep the argument because callers use it to lock known schedule rows;
    # the selector remains the source of truth for effective occurrences.
    del schedule_ids
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    local_starts_at, local_ends_at = _club_local_time_bounds(
        club_id=club_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    return any(
        occurrence.effective_start_time < _booking_time(local_ends_at)
        and occurrence.effective_end_time > _booking_time(local_starts_at)
        for occurrence in get_schedule_occurrences_for_date(
            club=club_id,
            target_date=local_starts_at.date(),
            trainer_id=trainer_id,
        )
    )


def _usable_personal_entitlement_exists(
    *, club: Club, student_id: int, training_type_id: int, location_id: int, target_date
) -> bool:
    day_start = timezone.make_aware(
        datetime.combine(target_date, datetime.min.time()),
        club_zoneinfo(club),
    )
    legacy_entitlement_exists = (
        Subscription.objects.for_club(club)
        .filter(
            student_id=student_id,
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.PENDING],
            deleted_at__isnull=True,
            tariff__training_type_id=training_type_id,
            components__isnull=True,
        )
        .filter(
            models.Q(status=Subscription.Status.PENDING)
            | models.Q(expires_at__isnull=True)
            | models.Q(expires_at__gt=day_start)
        )
        .filter(models.Q(trainings_left__isnull=True) | models.Q(trainings_left__gt=0))
        .filter(models.Q(scope=Tariff.Scope.CLUB) | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
        .exists()
    )
    if legacy_entitlement_exists:
        return True

    components = (
        SubscriptionComponent.objects.for_club(club)
        .select_related("subscription")
        .filter(
            subscription__student_id=student_id,
            subscription__status__in=[Subscription.Status.ACTIVE, Subscription.Status.PENDING],
            subscription__deleted_at__isnull=True,
            training_type_id=training_type_id,
            is_active=True,
        )
        .filter(
            models.Q(subscription__status=Subscription.Status.PENDING)
            | models.Q(subscription__expires_at__isnull=True)
            | models.Q(subscription__expires_at__gt=day_start)
        )
        .filter(models.Q(credits_left__isnull=True) | models.Q(credits_left__gt=0))
        .filter(models.Q(scope=Tariff.Scope.CLUB) | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
        .order_by("subscription_id", "id")
    )
    from apps.attendance.services.checkin import _component_has_weekly_capacity

    return any(
        component.subscription.status == Subscription.Status.PENDING
        or _component_has_weekly_capacity(
            component=component,
            checkin_date=target_date,
        )
        for component in components
    )


def _existing_drop_in_by_slot(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    starts_at: datetime,
    ends_at: datetime,
    lock: bool = True,
) -> PersonalDropInBooking | None:
    qs = PersonalDropInBooking.objects.for_club(club_id)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return (
        qs.select_related("enrollment", "enrollment__schedule")
        .filter(
            state__in=[
                PersonalDropInBooking.State.SCHEDULED,
                PersonalDropInBooking.State.ATTENDED,
            ],
            enrollment__student_id=student_id,
            enrollment__schedule__trainer_id=trainer_id,
            enrollment__schedule__location_id=location_id,
            enrollment__schedule__training_type_id=training_type_id,
            enrollment__schedule__one_time_date=starts_at.date(),
            enrollment__schedule__start_time=_booking_time(starts_at),
            enrollment__schedule__end_time=_booking_time(ends_at),
        )
        .first()
    )


def _booking_result(
    booking: PersonalDropInBooking, *, created: bool, availability_slot_id: int | None = None
) -> PersonalDropInBookingResult:
    enrollment = booking.enrollment
    return PersonalDropInBookingResult(
        booking=booking,
        schedule=enrollment.schedule,
        enrollment=enrollment,
        created=created,
        availability_slot_id=availability_slot_id,
    )


def _get_idempotent_drop_in_booking_or_raise(
    *,
    club_id: int,
    idempotency_key: str,
    student_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    starts_at: datetime,
    ends_at: datetime,
    tariff_id: int | None,
    ignore_tariff: bool = False,
) -> PersonalDropInBooking | None:
    booking = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("enrollment", "enrollment__schedule")
        .filter(idempotency_key=idempotency_key)
        .first()
    )
    if booking is None:
        return None

    schedule = booking.enrollment.schedule
    if (
        booking.enrollment.student_id != student_id
        or schedule.trainer_id != trainer_id
        or schedule.location_id != location_id
        or schedule.training_type_id != training_type_id
        or schedule.one_time_date != starts_at.date()
        or schedule.start_time != _booking_time(starts_at)
        or schedule.end_time != _booking_time(ends_at)
        or (not ignore_tariff and booking.tariff_id != tariff_id)
    ):
        raise BusinessLogicError(
            "Idempotency key is already used for another drop-in booking",
            code="personal_drop_in_idempotency_conflict",
        )
    return booking


def book_personal_drop_in(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    tariff_id: int | None,
    actor_user_id: int,
    availability_slot_id: int | None = None,
    offer_digest: str | None = None,
    discount_id: int | None = None,
    idempotency_key: str | None = None,
) -> PersonalDropInBookingResult:
    club = Club.objects.only("id", "timezone").filter(id=club_id).first()
    if club is None:
        raise BusinessLogicError("Club not found", code="club_not_found")
    starts_at = _as_club_aware(starts_at, club=club)
    ends_at = _as_club_aware(ends_at, club=club)
    if ends_at <= starts_at:
        raise BusinessLogicError(
            "Personal booking end time must be after start time", code="invalid_personal_booking_time"
        )
    if starts_at.date() != ends_at.date():
        raise BusinessLogicError(
            "Personal booking must start and end on the same date", code="personal_booking_crosses_date"
        )
    if starts_at <= timezone.now():
        raise BusinessLogicError("Drop-in booking requires a future slot", code="personal_drop_in_past_slot")
    cleaned_key = _clean_idempotency_key(idempotency_key, prefix="personal-drop-in")
    existing_complete_terms = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
            booking__idempotency_key=cleaned_key,
        )
    ).exists()
    # The feature flag permits accepting a new complete contract.  An already
    # persisted complete contract remains authoritative after a flag rollback.
    unified_personal_policy = is_unified_client_journey_enabled(club=club) or existing_complete_terms

    try:
        with transaction.atomic():
            offer = None
            if unified_personal_policy:
                # An exact replay already has immutable terms.  It must keep
                # working after a later default flip/removal without a new
                # catalog acceptance.
                existing_key = _get_idempotent_drop_in_booking_or_raise(
                    club_id=club_id,
                    idempotency_key=cleaned_key,
                    student_id=student_id,
                    trainer_id=trainer_id,
                    location_id=location_id,
                    training_type_id=training_type_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    tariff_id=None,
                    ignore_tariff=True,
                )
                if existing_key is not None:
                    return _booking_result(
                        existing_key,
                        created=False,
                        availability_slot_id=availability_slot_id,
                    )
                if not (offer_digest or "").strip():
                    raise BusinessLogicError(
                        "A displayed personal offer is required",
                        code="personal_offer_digest_required",
                    )
                # Catalog is the first D12 lock.  The client tariff is never
                # authority while the unified capability is enabled.
                offer = resolve_personal_booking_offer(
                    club_id=club_id,
                    trainer_id=trainer_id,
                    training_type_id=training_type_id,
                    location_id=location_id,
                    discount_id=discount_id,
                    lock=True,
                )
                tariff_id = offer.tariff.id
            if tariff_id is None:
                raise BusinessLogicError(
                    "A tariff is required for the compatibility booking path",
                    code="personal_drop_in_tariff_required",
                )
            existing_key = _get_idempotent_drop_in_booking_or_raise(
                club_id=club_id,
                idempotency_key=cleaned_key,
                student_id=student_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
                tariff_id=tariff_id,
                ignore_tariff=unified_personal_policy,
            )
            if existing_key is not None:
                return _booking_result(existing_key, created=False, availability_slot_id=availability_slot_id)

            # The global lock order is trainer -> student -> schedule/slot/reservation rows.
            trainer, location, training_type, tariff = validate_personal_drop_in_tariff_contract(
                club_id=club_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=tariff_id,
                lock=True,
                allow_designated_offer=unified_personal_policy,
            )
            student = (
                Student.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(id=student_id, deleted_at__isnull=True)
                .first()
            )
            if student is None:
                raise BusinessLogicError("Student does not belong to this club", code="student_club_mismatch")
            if student.status not in DROP_IN_STUDENT_STATUSES:
                raise BusinessLogicError("Student is not eligible for personal drop-in", code="student_ineligible")

            # A concurrent request may have missed the initial lookup and waited
            # on the trainer/student serialization locks. Re-read before slot
            # conflict checks so it becomes a replay instead of a false conflict.
            existing_key = _get_idempotent_drop_in_booking_or_raise(
                club_id=club_id,
                idempotency_key=cleaned_key,
                student_id=student_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
                tariff_id=tariff_id,
                ignore_tariff=unified_personal_policy,
            )
            if existing_key is not None:
                return _booking_result(existing_key, created=False, availability_slot_id=availability_slot_id)

            schedule_ids = _lock_conflicting_schedule_rows(
                club_id=club_id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            slots = _lock_overlapping_slots(
                club_id=club_id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            reservations = _lock_live_reservations(
                club_id=club_id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            if _schedule_conflicts(
                schedule_ids=schedule_ids,
                club_id=club_id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
            ):
                raise BusinessLogicError(
                    "Trainer already has a schedule in this time slot", code="personal_booking_slot_conflict"
                )
            if reservations:
                raise BusinessLogicError(
                    "Trainer already has a pending personal payment reservation in this time slot",
                    code="personal_payment_reservation_slot_conflict",
                )

            exact_slot = None
            if availability_slot_id is not None:
                exact_slot = next((slot for slot in slots if slot.id == availability_slot_id), None)
                if exact_slot is None:
                    raise BusinessLogicError(
                        "Personal availability slot not found", code="personal_availability_slot_not_found"
                    )
                if (
                    exact_slot.location_id != location.id
                    or exact_slot.training_type_id != training_type.id
                    or exact_slot.starts_at != starts_at
                    or exact_slot.ends_at != ends_at
                    or exact_slot.status != PersonalAvailabilitySlot.Status.PUBLISHED
                ):
                    raise BusinessLogicError(
                        "Personal availability slot is not available", code="personal_availability_slot_unavailable"
                    )
                if any(slot.id != exact_slot.id for slot in slots):
                    raise BusinessLogicError(
                        "Trainer already has an overlapping availability slot",
                        code="personal_availability_slot_conflict",
                    )
                if unified_personal_policy:
                    shown_offer = personal_offer_payload(slot=exact_slot, offer=offer)
                    if offer_digest != shown_offer["offer_digest"]:
                        error = BusinessLogicError(
                            "The personal offer changed; refresh the slot before booking",
                            code="personal_offer_changed",
                        )
                        error.safe_payload = {"current_offer": shown_offer}
                        raise error
            elif slots:
                raise BusinessLogicError(
                    "Trainer already has an overlapping availability slot", code="personal_availability_slot_conflict"
                )
            elif unified_personal_policy:
                shown_offer = direct_personal_offer_payload(
                    offer=offer,
                    trainer_id=trainer.id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    location_id=location.id,
                    training_type_id=training_type.id,
                )
                if offer_digest != shown_offer["offer_digest"]:
                    error = BusinessLogicError(
                        "The personal offer changed; refresh before booking",
                        code="personal_offer_changed",
                    )
                    error.safe_payload = {"current_offer": shown_offer}
                    raise error

            existing = _existing_drop_in_by_slot(
                club_id=club_id,
                student_id=student.id,
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            if existing is not None:
                raise BusinessLogicError(
                    "Client already has a drop-in booking for this personal slot",
                    code="personal_drop_in_booking_exists",
                )
            if _usable_personal_entitlement_exists(
                club=club,
                student_id=student.id,
                training_type_id=training_type.id,
                location_id=location.id,
                target_date=starts_at.date(),
            ):
                raise BusinessLogicError(
                    "Student already has a usable or pending personal entitlement",
                    code="personal_subscription_already_available",
                )

            schedule = Schedule.objects.create(
                club_id=club_id,
                day_of_week=starts_at.date().weekday(),
                start_time=_booking_time(starts_at),
                end_time=_booking_time(ends_at),
                group_name=_personal_session_group_name(student),
                trainer=trainer,
                location=location,
                training_type=training_type,
                one_time_date=starts_at.date(),
            )
            enrollment = _save_enrollment(
                ScheduleEnrollment(
                    club_id=club_id,
                    student=student,
                    schedule=schedule,
                    status=ScheduleEnrollment.Status.ACTIVE,
                    starts_on=starts_at.date(),
                    ends_on=starts_at.date(),
                    created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
                )
            )
            booking = PersonalDropInBooking(
                club_id=club_id,
                enrollment=enrollment,
                tariff=tariff,
                tariff_name_snapshot=tariff.name,
                price_snapshot=offer.payable_amount if offer is not None else tariff.price,
                created_by_id=actor_user_id,
                idempotency_key=cleaned_key,
            )
            booking.full_clean()
            booking.save()
            if unified_personal_policy:
                create_complete_personal_terms_snapshot(offer=offer, booking=booking)
            else:
                create_legacy_partial_personal_terms_snapshot(
                    tariff=tariff,
                    booking=booking,
                )
            _save_booking_event(
                ScheduleBookingEvent(
                    club_id=club_id,
                    enrollment=enrollment,
                    schedule=schedule,
                    student=student,
                    actor_id=actor_user_id,
                    event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
                    origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                    effective_date=starts_at.date(),
                    metadata={
                        "booking_kind": "drop_in",
                        "personal_drop_in_booking_id": booking.id,
                        "tariff_id": tariff.id,
                        "tariff_name_snapshot": tariff.name,
                        "base_amount": str(offer.base_amount) if offer is not None else str(tariff.price),
                        "discount_amount": str(offer.discount_amount) if offer is not None else "0.00",
                        "discount_id": offer.discount.id if offer is not None and offer.discount is not None else None,
                        "payable_amount": str(offer.payable_amount) if offer is not None else str(tariff.price),
                        "price_snapshot": str(offer.payable_amount) if offer is not None else str(tariff.price),
                        "idempotency_key": cleaned_key,
                        "availability_slot_id": exact_slot.id if exact_slot else None,
                    },
                )
            )
            if exact_slot is not None:
                exact_slot.status = PersonalAvailabilitySlot.Status.BOOKED
                exact_slot.booked_enrollment = enrollment
                exact_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
            return _booking_result(booking, created=True, availability_slot_id=exact_slot.id if exact_slot else None)
    except IntegrityError:
        existing = _existing_drop_in_by_slot(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
            lock=False,
        )
        if existing is not None:
            if existing.idempotency_key == cleaned_key:
                return _booking_result(existing, created=False, availability_slot_id=availability_slot_id)
            raise BusinessLogicError(
                "Client already has a drop-in booking for this personal slot",
                code="personal_drop_in_booking_exists",
            )
        raise


def book_personal_drop_in_from_frozen_reservation(
    *,
    club_id: int,
    reservation_id: int,
    source_terms_id: int,
    actor_user_id: int,
    idempotency_key: str,
    conflict_scope: PersonalTimeConflictScope,
) -> PersonalDropInBookingResult:
    """Reacquire a safely cancelled reservation's frozen time and terms.

    This is intentionally not a retry through ``book_personal_drop_in``:
    resolving today's designated tariff or discount could change a price that
    the original reservation already accepted.  The caller must have made the
    bank family terminal in the same outer transaction before entering here.
    """

    cleaned_key = _clean_idempotency_key(idempotency_key, prefix="personal-method-correction")
    with transaction.atomic():
        # The frozen price is immutable evidence, but the reservation family
        # and the exact slot remain mutable roots.  Take their D12 scope before
        # selecting either record so this helper is also safe when invoked
        # outside the replacement coordinator.
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        lock_complete_personal_scopes(
            club_id=club_id,
            reservation_ids=[reservation_id],
            extra_slot_ids=conflict_scope.slot_ids,
            extra_schedule_ids=conflict_scope.schedule_ids,
            extra_reservation_ids=conflict_scope.reservation_ids,
        )
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("tariff", "trainer", "location", "training_type")
            .filter(id=reservation_id)
            .first()
        )
        if reservation is None:
            raise BusinessLogicError(
                "Personal payment reservation was not found",
                code="personal_payment_reservation_not_found",
            )
        terms = (
            complete_personal_terms_queryset(
                PersonalServiceTermsSnapshot.objects.for_club(club_id)
                .filter(id=source_terms_id, reservation_id=reservation.id)
            )
            .first()
        )
        if terms is None or terms.terms_version != PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2:
            raise BusinessLogicError(
                "A complete frozen personal offer is required for replacement",
                code="personal_payment_replacement_terms_missing",
            )
        if (
            terms.tariff_id_snapshot != reservation.tariff_id
            or terms.training_type_id_snapshot != reservation.training_type_id
            or terms.payable_amount is None
            or terms.payable_amount <= 0
        ):
            raise BusinessLogicError(
                "Frozen personal terms do not match the reservation",
                code="personal_payment_replacement_terms_mismatch",
            )
        if reservation.status not in {
            PersonalBookingPaymentReservation.Status.CANCELLED,
            PersonalBookingPaymentReservation.Status.EXPIRED,
        }:
            raise BusinessLogicError(
                "The original payment reservation is not terminal",
                code="personal_payment_replacement_original_not_terminal",
            )

        existing = _get_idempotent_drop_in_booking_or_raise(
            club_id=club_id,
            idempotency_key=cleaned_key,
            student_id=reservation.student_id,
            trainer_id=reservation.trainer_id,
            location_id=reservation.location_id,
            training_type_id=reservation.training_type_id,
            starts_at=reservation.starts_at,
            ends_at=reservation.ends_at,
            tariff_id=reservation.tariff_id,
        )
        if existing is not None:
            return _booking_result(existing, created=False, availability_slot_id=reservation.availability_slot_id)

        # Catalog rows are only compatibility projections here. The complete
        # snapshot is the commercial authority, while the mutable D12 roots
        # were all locked before the reservation/financial suffix.
        trainer = Trainer.objects.for_club(club_id).filter(id=reservation.trainer_id).first()
        location = Location.objects.filter(
            id=reservation.location_id,
            club_id=club_id,
        ).first()
        training_type = TrainingType.objects.for_club(club_id).filter(
            id=reservation.training_type_id,
            kind=TrainingType.Kind.PERSONAL,
        ).first()
        tariff = Tariff.objects.for_club(club_id).filter(id=reservation.tariff_id).first()
        student = Student.objects.for_club(club_id).filter(
            id=reservation.student_id,
            deleted_at__isnull=True,
        ).first()
        if any(value is None for value in (trainer, location, training_type, tariff, student)):
            raise BusinessLogicError(
                "The frozen personal booking context is no longer available",
                code="personal_payment_replacement_context_missing",
            )
        if student.status not in DROP_IN_STUDENT_STATUSES:
            raise BusinessLogicError("Student is not eligible for personal drop-in", code="student_ineligible")

        slots = list(
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .filter(id__in=conflict_scope.slot_ids)
            .order_by("id")
        )
        exact_slot = next(
            (slot for slot in slots if slot.id == reservation.availability_slot_id),
            None,
        )
        if reservation.availability_slot_id is not None:
            if (
                exact_slot is None
                or exact_slot.status != PersonalAvailabilitySlot.Status.PUBLISHED
                or exact_slot.trainer_id != reservation.trainer_id
                or exact_slot.location_id != reservation.location_id
                or exact_slot.training_type_id != reservation.training_type_id
                or exact_slot.starts_at != reservation.starts_at
                or exact_slot.ends_at != reservation.ends_at
                or any(slot.id != exact_slot.id for slot in slots)
            ):
                raise BusinessLogicError(
                    "The original personal availability slot is no longer available",
                    code="personal_payment_replacement_slot_unavailable",
                )
        elif slots:
            raise BusinessLogicError(
                "The frozen personal time now overlaps an availability slot",
                code="personal_payment_replacement_slot_unavailable",
            )

        live_reservations = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(id__in=conflict_scope.reservation_ids)
            .exclude(id=reservation.id)
            .filter(
                status__in=ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES,
                starts_at__lt=reservation.ends_at,
                ends_at__gt=reservation.starts_at,
            )
            .exists()
        )
        if _schedule_conflicts(
            schedule_ids=list(conflict_scope.schedule_ids),
            club_id=club_id,
            trainer_id=trainer.id,
            starts_at=reservation.starts_at,
            ends_at=reservation.ends_at,
        ) or live_reservations:
            raise BusinessLogicError(
                "The original personal availability slot is no longer available",
                code="personal_payment_replacement_slot_unavailable",
            )
        if _existing_drop_in_by_slot(
            club_id=club_id,
            student_id=student.id,
            trainer_id=trainer.id,
            location_id=location.id,
            training_type_id=training_type.id,
            starts_at=reservation.starts_at,
            ends_at=reservation.ends_at,
        ) is not None:
            raise BusinessLogicError(
                "Client already has a drop-in booking for this personal slot",
                code="personal_drop_in_booking_exists",
            )

        club = Club.objects.only("id", "timezone").get(id=club_id)
        zone = club_zoneinfo(club)
        local_starts_at = timezone.localtime(reservation.starts_at, zone)
        local_ends_at = timezone.localtime(reservation.ends_at, zone)
        schedule = Schedule.objects.create(
            club_id=club_id,
            day_of_week=local_starts_at.date().weekday(),
            start_time=_booking_time(local_starts_at),
            end_time=_booking_time(local_ends_at),
            group_name=_personal_session_group_name(student),
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=local_starts_at.date(),
        )
        enrollment = _save_enrollment(
            ScheduleEnrollment(
                club_id=club_id,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=local_starts_at.date(),
                ends_on=local_starts_at.date(),
                created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
            )
        )
        booking = PersonalDropInBooking(
            club_id=club_id,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=terms.tariff_name_snapshot,
            price_snapshot=terms.payable_amount,
            created_by_id=actor_user_id,
            idempotency_key=cleaned_key,
        )
        booking.full_clean()
        booking.save()
        clone_complete_personal_terms_snapshot(source_terms=terms, booking=booking)
        _save_booking_event(
            ScheduleBookingEvent(
                club_id=club_id,
                enrollment=enrollment,
                schedule=schedule,
                student=student,
                actor_id=actor_user_id,
                event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                effective_date=local_starts_at.date(),
                metadata={
                    "booking_kind": "drop_in",
                    "personal_drop_in_booking_id": booking.id,
                    "tariff_id": reservation.tariff_id,
                    "tariff_name_snapshot": terms.tariff_name_snapshot,
                    "base_amount": str(terms.base_amount),
                    "discount_amount": str(terms.discount_amount),
                    "discount_id": terms.discount_id_snapshot,
                    "payable_amount": str(terms.payable_amount),
                    "price_snapshot": str(terms.payable_amount),
                    "idempotency_key": cleaned_key,
                    "availability_slot_id": exact_slot.id if exact_slot is not None else None,
                    "replacement_reservation_id": reservation.id,
                },
            )
        )
        if exact_slot is not None:
            exact_slot.status = PersonalAvailabilitySlot.Status.BOOKED
            exact_slot.booked_enrollment = enrollment
            exact_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
        return _booking_result(
            booking,
            created=True,
            availability_slot_id=exact_slot.id if exact_slot is not None else None,
        )


def get_active_personal_drop_in_booking(
    *, club_id: int, student_id: int, schedule_id: int, target_date
) -> PersonalDropInBooking | None:
    return (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related("enrollment", "enrollment__schedule", "tariff", "checkin", "debt")
        .filter(
            enrollment__student_id=student_id,
            enrollment__schedule_id=schedule_id,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            state__in=[PersonalDropInBooking.State.SCHEDULED, PersonalDropInBooking.State.ATTENDED],
        )
        .first()
    )


def get_locked_personal_drop_in_booking(
    *, club_id: int, student_id: int, schedule_id: int, target_date
) -> PersonalDropInBooking | None:
    return (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("enrollment", "enrollment__schedule", "tariff")
        .filter(
            enrollment__student_id=student_id,
            enrollment__schedule_id=schedule_id,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            state=PersonalDropInBooking.State.SCHEDULED,
        )
        .first()
    )


def _live_payment_link(*, booking: PersonalDropInBooking, lock: bool = False) -> PersonalDropInPaymentLink | None:
    qs = PersonalDropInPaymentLink.objects.for_club(booking.club_id)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return (
        qs.select_related("payment", "payment__subscription", "bank_payment_order")
        .filter(booking=booking, payment__status__in=LIVE_PAYMENT_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )


def _confirmed_payment_link(*, booking: PersonalDropInBooking, lock: bool = False) -> PersonalDropInPaymentLink | None:
    qs = PersonalDropInPaymentLink.objects.for_club(booking.club_id)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return (
        qs.select_related("payment", "payment__subscription")
        .filter(booking=booking, payment__status=Payment.Status.CONFIRMED)
        .order_by("-created_at", "-id")
        .first()
    )


def _has_live_bank_order_claim(
    *,
    booking: PersonalDropInBooking,
    excluding_order_id: int | None = None,
) -> bool:
    qs = BankPaymentOrder.objects.for_club(booking.club_id).filter(
        personal_drop_in_booking_id_snapshot=booking.id,
    ).exclude(
        status__in=[
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.EXPIRED,
            BankPaymentOrder.Status.CANCELLED,
        ]
    )
    if excluding_order_id is not None:
        qs = qs.exclude(id=excluding_order_id)
    return qs.exists()


def apply_personal_drop_in_checkin(
    *,
    booking: PersonalDropInBooking,
    checkin: Checkin,
    student: Student,
    club: Club | None = None,
    prelocked_links: list[PersonalDropInPaymentLink] | None = None,
    prelocked_subscription_component: SubscriptionComponent | None = None,
    prelocked_subscription_match: tuple[Subscription, SubscriptionComponent | None] | None = None,
    prelocked_has_matching_subscription_component: bool | None = None,
) -> Subscription | None:
    """Apply the booking's deterministic payment priority after creating check-in."""
    from apps.attendance.services.checkin import (
        _deduct_subscription,
        _find_checkin_subscription_component,
        _find_legacy_checkin_subscription,
    )

    if prelocked_links is None:
        confirmed_link = _confirmed_payment_link(booking=booking, lock=True)
        pending_link = _live_payment_link(booking=booking, lock=True)
    else:
        confirmed_link = next(
            iter(
                sorted(
                    (link for link in prelocked_links if link.payment.status == Payment.Status.CONFIRMED),
                    key=lambda link: (link.created_at, link.id),
                    reverse=True,
                )
            ),
            None,
        )
        pending_link = next(
            iter(
                sorted(
                    (link for link in prelocked_links if link.payment.status in LIVE_PAYMENT_STATUSES),
                    key=lambda link: (link.created_at, link.id),
                    reverse=True,
                )
            ),
            None,
        )
    resolved_club = club or Club.objects.only("id", "timezone").get(id=booking.club_id)
    complete_terms = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(booking.club_id).filter(booking_id=booking.id)
    ).first()
    # A pay-at-visit complete intent owns its exact price/debt.  An unrelated
    # entitlement that appeared after booking must not silently replace that
    # intent (or leave a spare entitlement plus an open debt).
    if complete_terms is not None and confirmed_link is None and pending_link is None:
        prelocked_subscription_match = None
        prelocked_subscription_component = None
        prelocked_has_matching_subscription_component = False
    if confirmed_link is not None and confirmed_link.payment.subscription_id:
        subscription = confirmed_link.payment.subscription
        if subscription is None or subscription.status != Subscription.Status.ACTIVE:
            raise BusinessLogicError(
                "Confirmed drop-in payment has no active subscription", code="drop_in_subscription_missing"
            )
        resolved = _deduct_subscription(
            student=student,
            club_id=booking.club_id,
            schedule=booking.enrollment.schedule,
            club=resolved_club,
            training_type_id=checkin.training_type_id,
            location=checkin.location,
            checkin=checkin,
            checkin_date=checkin.date,
            subscription=subscription,
            subscription_component=prelocked_subscription_component,
            pending_manual_admission_payments=[],
        )
    elif pending_link is not None:
        from apps.billing.service_modules.debts import debt_state

        debt = Debt(
            club_id=booking.club_id,
            student=student,
            checkin=checkin,
            tariff_price=booking.price_snapshot,
            required_tariff_id=booking.tariff_id,
            reason="personal_drop_in",
        )
        previous_debt_state = debt_state(debt)
        debt.settlement_payment_id = pending_link.payment_id
        debt.full_clean()
        debt.save()
        from apps.billing.models import DebtLifecycleEvent, DebtSettlementEvent
        from apps.billing.service_modules.debts import (
            record_debt_lifecycle_event,
            record_debt_settlement_events,
        )

        record_debt_lifecycle_event(
            club_id=booking.club_id,
            debt=debt,
            event_type=DebtLifecycleEvent.EventType.RESERVED,
            previous_state=previous_debt_state,
            new_state="reserved",
            actor_user_id=pending_link.created_by_id,
            reason="personal_drop_in_pending_payment",
            payment_id=pending_link.payment_id,
            subscription_id=pending_link.payment.subscription_id,
        )
        record_debt_settlement_events(
            club_id=booking.club_id,
            payment=pending_link.payment,
            debt_ids=[debt.id],
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )
        checkin.is_debt = True
        checkin.save(update_fields=["is_debt", "updated_at"])
        booking.debt = debt
        resolved = None
    else:
        subscription_match = prelocked_subscription_match
        if subscription_match is None and prelocked_links is None:
            subscription_match = _find_checkin_subscription_component(
                student=student,
                club_id=booking.club_id,
                training_type_id=checkin.training_type_id,
                location=checkin.location,
                checkin_date=checkin.date,
            )
        if subscription_match is not None:
            resolved = _deduct_subscription(
                student=student,
                club_id=booking.club_id,
                schedule=booking.enrollment.schedule,
                club=resolved_club,
                training_type_id=checkin.training_type_id,
                location=checkin.location,
                checkin=checkin,
                checkin_date=checkin.date,
                subscription=subscription_match[0],
                subscription_component=subscription_match[1],
                pending_manual_admission_payments=[],
                has_matching_subscription_component=prelocked_has_matching_subscription_component,
            )
        else:
            legacy_subscription = None
            if prelocked_links is None:
                legacy_subscription = _find_legacy_checkin_subscription(
                    student=student,
                    club_id=booking.club_id,
                    training_type_id=checkin.training_type_id,
                    location=checkin.location,
                    checkin_date=checkin.date,
                )
            if legacy_subscription is not None:
                resolved = _deduct_subscription(
                    student=student,
                    club_id=booking.club_id,
                    schedule=booking.enrollment.schedule,
                    club=resolved_club,
                    training_type_id=checkin.training_type_id,
                    location=checkin.location,
                    checkin=checkin,
                    checkin_date=checkin.date,
                    subscription=legacy_subscription,
                    subscription_component=None,
                    pending_manual_admission_payments=[],
                )
            else:
                resolved = None
        if resolved is None:
            debt = Debt(
                club_id=booking.club_id,
                student=student,
                checkin=checkin,
                tariff_price=booking.price_snapshot,
                required_tariff_id=booking.tariff_id,
                reason="personal_drop_in",
            )
            debt.full_clean()
            debt.save()
            checkin.is_debt = True
            checkin.save(update_fields=["is_debt", "updated_at"])
            booking.debt = debt
    booking.checkin = checkin
    booking.state = PersonalDropInBooking.State.ATTENDED
    booking.full_clean()
    booking.save(update_fields=["checkin", "debt", "state", "updated_at"])
    if complete_terms is not None:
        from apps.leads.services import convert_lead_after_personal_attendance

        payment_id = None
        if confirmed_link is not None:
            payment_id = confirmed_link.payment_id
        elif pending_link is not None:
            payment_id = pending_link.payment_id
        convert_lead_after_personal_attendance(
            club_id=booking.club_id,
            student_id=student.id,
            booking_id=booking.id,
            checkin_id=checkin.id,
            payment_id=payment_id,
            actor_user_id=(pending_link or confirmed_link).created_by_id
            if (pending_link or confirmed_link) is not None
            else booking.created_by_id,
        )
    return resolved


def _assert_booking_payment_actionable(
    *,
    booking: PersonalDropInBooking,
    allowed_bank_order_id: int | None = None,
) -> None:
    if booking.state in {PersonalDropInBooking.State.CANCELLED, PersonalDropInBooking.State.NO_SHOW}:
        raise BusinessLogicError("Terminal drop-in booking cannot accept payment", code="personal_drop_in_terminal")
    if _live_payment_link(booking=booking, lock=True) is not None:
        raise BusinessLogicError(
            "Drop-in booking already has a pending payment", code="personal_drop_in_payment_pending"
        )
    if _has_live_bank_order_claim(
        booking=booking,
        excluding_order_id=allowed_bank_order_id,
    ):
        raise BusinessLogicError(
            "Drop-in booking already has a pending bank payment",
            code="personal_drop_in_payment_pending",
        )
    if _confirmed_payment_link(booking=booking, lock=True) is not None:
        raise BusinessLogicError("Drop-in booking is already settled", code="personal_drop_in_payment_confirmed")
    if booking.state == PersonalDropInBooking.State.ATTENDED and (
        booking.debt_id is None or booking.debt.resolved_at is not None
    ):
        raise BusinessLogicError(
            "Drop-in booking is already covered",
            code="personal_drop_in_payment_covered",
        )


def _payment_debt_ids(booking: PersonalDropInBooking) -> list[int]:
    if booking.debt_id is None:
        return []
    debt = Debt.objects.for_club(booking.club_id).select_for_update(of=("self",)).get(id=booking.debt_id)
    if debt.resolved_at is not None:
        return []
    return [debt.id]


def _complete_personal_debt_ids(
    *,
    club_id: int,
    booking: PersonalDropInBooking,
    requested_debt_id: int | None,
) -> list[int]:
    """Return only this booking's open exact check-in debt, if present.

    This is a read-only command preflight.  The billing owner takes the Debt
    row lock after Payment/Subscription creation and records the reservation.
    """
    expected_debt_id = booking.debt_id
    if requested_debt_id is not None and requested_debt_id != expected_debt_id:
        raise BusinessLogicError(
            "Debt does not belong to this personal booking",
            code="personal_drop_in_debt_mismatch",
        )
    if expected_debt_id is None:
        if requested_debt_id is not None:
            raise BusinessLogicError(
                "Personal booking has no checked-in debt",
                code="personal_drop_in_debt_not_ready",
            )
        return []
    debt = Debt.objects.for_club(club_id).filter(id=expected_debt_id).first()
    if debt is None or debt.resolved_at is not None:
        raise BusinessLogicError("Personal booking debt is not open", code="personal_drop_in_debt_not_open")
    return [debt.id]


def _has_complete_personal_terms(*, club_id: int, booking_id: int) -> bool:
    """Read the durable lifecycle marker without taking an out-of-order lock."""
    return complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking_id)
    ).exists()


def _lock_personal_drop_in_payment_contract(*, club_id: int, booking_id: int) -> None:
    """Lock legacy catalog only when no complete accepted contract exists."""
    booking_ref = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related("enrollment__schedule")
        .get(id=booking_id)
    )
    terms = (
        PersonalServiceTermsSnapshot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(booking_id=booking_id)
        .first()
    )
    if is_complete_personal_terms(terms):
        if (
            terms.tariff_id_snapshot != booking_ref.tariff_id
            or terms.training_type_id_snapshot != booking_ref.enrollment.schedule.training_type_id
            or terms.payable_amount != booking_ref.price_snapshot
        ):
            raise BusinessLogicError(
                "Personal booking terms do not match its immutable booking evidence",
                code="personal_terms_booking_mismatch",
            )
        return
    schedule = booking_ref.enrollment.schedule
    validate_personal_drop_in_tariff_contract(
        club_id=club_id,
        trainer_id=schedule.trainer_id,
        location_id=schedule.location_id,
        training_type_id=schedule.training_type_id,
        tariff_id=booking_ref.tariff_id,
        lock=True,
    )


def create_personal_drop_in_payment(
    *,
    club_id: int,
    booking_id: int,
    payment_method: str,
    created_by_id: int,
    discount_ids: list[int] | None = None,
    idempotency_key: str | None = None,
    debt_id: int | None = None,
    v2_manual_admission_allowed: bool = False,
) -> PersonalDropInPaymentLink:
    if payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER}:
        raise BusinessLogicError(
            "Drop-in manual payment must be cash or transfer", code="drop_in_manual_method_required"
    )
    has_complete_terms = _has_complete_personal_terms(club_id=club_id, booking_id=booking_id)
    cleaned_key = (idempotency_key or "").strip()
    if not cleaned_key:
        cleaned_key = (
            f"personal-complete-payment-{booking_id}-{payment_method}"
            if has_complete_terms
            else _clean_idempotency_key(idempotency_key, prefix="personal-drop-in-payment")
        )
    if has_complete_terms and discount_ids:
        raise BusinessLogicError(
            "Unified personal payments do not accept discounts",
            code="personal_discounts_forbidden",
        )
    with transaction.atomic():
        existing = PersonalDropInPaymentLink.objects.for_club(club_id).filter(idempotency_key=cleaned_key).first()
        if existing is not None:
            if existing.booking_id != booking_id:
                raise BusinessLogicError(
                    "Payment idempotency key is already used", code="personal_drop_in_payment_idempotency_conflict"
                )
            if has_complete_terms and existing.payment.payment_method != payment_method:
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            return existing
        if has_complete_terms:
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            lock_complete_personal_scopes(club_id=club_id, booking_ids=[booking_id])
        _lock_personal_drop_in_payment_contract(
            club_id=club_id,
            booking_id=booking_id,
        )
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment", "enrollment__schedule", "tariff")
            .get(id=booking_id)
        )
        existing = PersonalDropInPaymentLink.objects.for_club(club_id).filter(idempotency_key=cleaned_key).first()
        if existing is not None:
            if existing.booking_id != booking.id:
                raise BusinessLogicError(
                    "Payment idempotency key is already used", code="personal_drop_in_payment_idempotency_conflict"
                )
            if has_complete_terms and existing.payment.payment_method != payment_method:
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            return existing
        complete_debt_ids = (
            _complete_personal_debt_ids(
                club_id=club_id,
                booking=booking,
                requested_debt_id=debt_id,
            )
            if has_complete_terms
            else []
        )
        _assert_booking_payment_actionable(booking=booking)
        if has_complete_terms:
            from apps.billing.service_modules.payment_creation import (
                build_personal_command_fingerprint,
                create_personal_terms_payment,
            )

            terms = complete_personal_terms_queryset(
                PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking.id)
            ).get()
            payment = create_personal_terms_payment(
                club_id=club_id,
                booking_id=booking.id,
                payment_method=payment_method,
                recorded_by_id=created_by_id,
                command_idempotency_key=cleaned_key,
                command_fingerprint=build_personal_command_fingerprint(
                    student_id=booking.enrollment.student_id,
                    terms_id=terms.id,
                    payment_method=payment_method,
                    booking_id=booking.id,
                    debt_ids=complete_debt_ids,
                ),
                debt_ids=complete_debt_ids,
            )
        else:
            if debt_id is not None and debt_id != booking.debt_id:
                raise BusinessLogicError(
                    "Debt does not belong to this personal booking",
                    code="personal_drop_in_debt_mismatch",
                )
            schedule = booking.enrollment.schedule
            from apps.billing.services import create_payment

            payment = create_payment(
                club_id=club_id,
                student_id=booking.enrollment.student_id,
                tariff_id=booking.tariff_id,
                payment_method=payment_method,
                discount_ids=discount_ids or [],
                debt_ids=_payment_debt_ids(booking),
                recorded_by_id=created_by_id,
                seller_trainer_id=schedule.trainer_id,
                package_owner_trainer_id=schedule.trainer_id,
                personal_drop_in_booking_id=booking.id,
            )
        link = PersonalDropInPaymentLink(
            club_id=club_id,
            booking=booking,
            payment=payment,
            created_by_id=created_by_id,
            idempotency_key=cleaned_key,
        )
        link.full_clean()
        link.save()
        if has_complete_terms:
            if v2_manual_admission_allowed:
                from apps.leads.services import admit_lead_for_manual_operational_admission
                from apps.students.operational_admission_contracts import ManualOperationalAdmissionEvidence

                admit_lead_for_manual_operational_admission(
                    evidence=ManualOperationalAdmissionEvidence(
                        club_id=club_id,
                        student_id=booking.enrollment.student_id,
                        payment_id=payment.id,
                        origin="personal",
                        actor_user_id=created_by_id,
                    )
                )
            else:
                from apps.leads.services import snooze_lead_for_pending_personal_payment

                snooze_lead_for_pending_personal_payment(
                    club_id=club_id,
                    student_id=booking.enrollment.student_id,
                    payment_id=payment.id,
                    actor_user_id=created_by_id,
                )
        return link


def create_personal_drop_in_bank_payment_order(
    *,
    club_id: int,
    booking_id: int,
    source: str,
    created_by_id: int,
    idempotency_key: str | None = None,
    buyer_email: str | None = None,
    buyer_phone: str | None = None,
    debt_id: int | None = None,
) -> PersonalDropInPaymentLink:
    has_complete_terms = _has_complete_personal_terms(club_id=club_id, booking_id=booking_id)
    cleaned_key = (idempotency_key or "").strip()
    if not cleaned_key:
        cleaned_key = (
            f"personal-complete-bank-order-{booking_id}"
            if has_complete_terms
            else _clean_idempotency_key(idempotency_key, prefix="personal-drop-in-bank-order")
        )
    existing = (
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .select_related("payment", "bank_payment_order")
        .filter(idempotency_key=cleaned_key)
        .first()
    )
    if existing is not None:
        if existing.booking_id != booking_id:
            raise BusinessLogicError(
                "Payment idempotency key is already used",
                code="personal_drop_in_payment_idempotency_conflict",
            )
        return existing

    with transaction.atomic():
        if has_complete_terms:
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            lock_complete_personal_scopes(club_id=club_id, booking_ids=[booking_id])
        _lock_personal_drop_in_payment_contract(
            club_id=club_id,
            booking_id=booking_id,
        )
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment", "enrollment__schedule", "tariff")
            .get(id=booking_id)
        )
        existing = PersonalDropInPaymentLink.objects.for_club(club_id).filter(idempotency_key=cleaned_key).first()
        if existing is not None:
            if existing.booking_id != booking.id:
                raise BusinessLogicError(
                    "Payment idempotency key is already used", code="personal_drop_in_payment_idempotency_conflict"
                )
            return existing
        _assert_booking_payment_actionable(booking=booking)
        schedule = booking.enrollment.schedule
        student_id = booking.enrollment.student_id
        tariff_id = booking.tariff_id
        trainer_id = schedule.trainer_id
        debt_ids = _payment_debt_ids(booking)
        if has_complete_terms:
            debt_ids = _complete_personal_debt_ids(
                club_id=club_id,
                booking=booking,
                requested_debt_id=debt_id,
            )
        elif debt_id is not None and debt_id != booking.debt_id:
            raise BusinessLogicError(
                "Debt does not belong to this personal booking",
                code="personal_drop_in_debt_mismatch",
            )
        payment_expires_at = None
        if booking.state == PersonalDropInBooking.State.SCHEDULED:
            payment_expires_at = timezone.make_aware(
                datetime.combine(schedule.one_time_date, schedule.start_time),
                club_zoneinfo(schedule.club),
            )

    order = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_related("payment", "subscription")
        .filter(
            personal_drop_in_booking_id_snapshot=booking_id,
            status__in=(
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.APPROVED,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            ),
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if order is None:
        from apps.billing.services import create_bank_payment_order

        try:
            order = create_bank_payment_order(
                club_id=club_id,
                student_id=student_id,
                tariff_id=tariff_id,
                source=source,
                created_by_id=created_by_id,
                seller_trainer_id=trainer_id,
                package_owner_trainer_id=trainer_id,
                debt_ids=debt_ids,
                buyer_email=buyer_email,
                buyer_phone=buyer_phone,
                allow_reuse=False,
                personal_drop_in_booking_id=booking_id,
                expires_at_cap=payment_expires_at,
                command_idempotency_key=(
                    cleaned_key if has_complete_terms else None
                ),
            )
        except BusinessLogicError:
            # A deterministic provider failure can terminally dispose the
            # committed BankPaymentOrder before this attendance bridge is
            # attached.  The immutable origin snapshot is enough to attach an
            # auditable terminal attempt without recreating provider I/O; the
            # exact replay/read path must not make that attempt disappear.
            terminal_order = (
                BankPaymentOrder.objects.for_club(club_id)
                .select_related("payment")
                .filter(personal_drop_in_booking_id_snapshot=booking_id)
                .order_by("-created_at", "-id")
                .first()
            )
            if terminal_order is not None:
                with transaction.atomic():
                    booking = PersonalDropInBooking.objects.for_club(club_id).select_for_update(of=("self",)).get(
                        id=booking_id
                    )
                    existing_link = (
                        PersonalDropInPaymentLink.objects.for_club(club_id)
                        .select_for_update(of=("self",))
                        .filter(
                            models.Q(idempotency_key=cleaned_key)
                            | models.Q(bank_payment_order_id=terminal_order.id)
                        )
                        .first()
                    )
                    if existing_link is None:
                        PersonalDropInPaymentLink.objects.create(
                            club_id=club_id,
                            booking=booking,
                            payment=terminal_order.payment,
                            bank_payment_order=terminal_order,
                            created_by_id=created_by_id,
                            idempotency_key=cleaned_key,
                        )
            raise
        except IntegrityError:
            order = (
                BankPaymentOrder.objects.for_club(club_id)
                .select_related("payment", "subscription")
                .filter(
                    personal_drop_in_booking_id_snapshot=booking_id,
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if order is None:
                raise

    with transaction.atomic():
        if has_complete_terms:
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            lock_complete_personal_scopes(club_id=club_id, booking_ids=[booking_id])
        _lock_personal_drop_in_payment_contract(club_id=club_id, booking_id=booking_id)
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment", "enrollment__schedule", "tariff")
            .get(id=booking_id)
        )
        existing = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "bank_payment_order")
            .filter(models.Q(idempotency_key=cleaned_key) | models.Q(bank_payment_order_id=order.id))
            .order_by("-created_at", "-id")
            .first()
        )
        if existing is not None:
            if existing.booking_id != booking.id:
                raise BusinessLogicError(
                    "Payment idempotency key is already used",
                    code="personal_drop_in_payment_idempotency_conflict",
                )
            return existing
        _assert_booking_payment_actionable(
            booking=booking,
            allowed_bank_order_id=order.id,
        )
        link = PersonalDropInPaymentLink(
            club_id=club_id,
            booking=booking,
            payment=order.payment,
            bank_payment_order=order,
            created_by_id=created_by_id,
            idempotency_key=cleaned_key,
        )
        link.full_clean()
        link.save()
        attached_link = link

    if attached_link.payment.status == Payment.Status.CONFIRMED:
        reconcile_personal_drop_in_payment_after_verification(
            payment_id=attached_link.payment_id,
            club_id=club_id,
        )
    return attached_link


def reconcile_personal_drop_in_payment_after_verification(*, payment_id: int, club_id: int) -> None:
    """Keep a confirmed prepayment usable through its scheduled occurrence end."""
    with transaction.atomic():
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "payment__subscription", "booking__enrollment__schedule")
            .filter(payment_id=payment_id)
            .first()
        )
        if link is None or link.payment.status != Payment.Status.CONFIRMED:
            return
        subscription = link.payment.subscription
        if subscription is None:
            raise BusinessLogicError("Drop-in payment subscription is required", code="drop_in_subscription_missing")
        schedule = link.booking.enrollment.schedule
        occurrence_end = timezone.make_aware(
            datetime.combine(schedule.one_time_date, schedule.end_time),
            club_zoneinfo(schedule.club),
        )
        if subscription.expires_at is None or subscription.expires_at < occurrence_end:
            subscription.expires_at = occurrence_end
            subscription.save(update_fields=["expires_at", "updated_at"])


def cancel_personal_drop_in_booking(
    *, club_id: int, booking_id: int, actor_user_id: int, reason: str = ""
) -> PersonalDropInBooking:
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope
        from apps.billing.service_modules.bank_orders import (
            close_pending_personal_drop_in_financial_family,
        )

        # The billing owner takes the full command scope before attendance
        # touches the booking.  Legacy bookings return False and retain their
        # existing refusal behavior below.
        # Check-in uses the payroll/rollout root before Trainer.  Take the
        # same leading scope for the terminal branch so a snapshot-owned bank
        # order cannot invert the shared personal command lock order.
        lock_training_group_mutation_scope(club_id=club_id)
        close_pending_personal_drop_in_financial_family(
            club_id=club_id,
            booking_id=booking_id,
            actor_user_id=actor_user_id,
            reason=reason or "personal_booking_cancelled",
        )
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment", "enrollment__schedule")
            .get(id=booking_id)
        )
        if booking.state == PersonalDropInBooking.State.CANCELLED:
            return booking
        if booking.state != PersonalDropInBooking.State.SCHEDULED:
            raise BusinessLogicError(
                "Only scheduled drop-in booking can be cancelled", code="personal_drop_in_not_cancellable"
            )
        schedule = booking.enrollment.schedule
        starts_at = timezone.make_aware(
            datetime.combine(schedule.one_time_date, schedule.start_time), club_zoneinfo(schedule.club)
        )
        if starts_at <= timezone.now():
            raise BusinessLogicError("Drop-in booking cannot be cancelled after start", code="personal_drop_in_started")
        if _live_payment_link(booking=booking, lock=True) is not None:
            raise BusinessLogicError(
                "Cancel or reject the pending payment first", code="personal_drop_in_payment_pending"
            )
        if _has_live_bank_order_claim(booking=booking):
            raise BusinessLogicError(
                "Cancel or reject the pending bank payment first",
                code="personal_drop_in_payment_pending",
            )
        booking.enrollment.status = ScheduleEnrollment.Status.CANCELLED
        booking.enrollment.full_clean()
        booking.enrollment.save(update_fields=["status", "updated_at"])
        schedule.is_active = False
        schedule.save(update_fields=["is_active", "updated_at"])
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booked_enrollment_id=booking.enrollment_id, status=PersonalAvailabilitySlot.Status.BOOKED)
            .first()
        )
        if slot is not None and slot.starts_at > timezone.now():
            slot.status = PersonalAvailabilitySlot.Status.PUBLISHED
            slot.booked_enrollment = None
            slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
        booking.state = PersonalDropInBooking.State.CANCELLED
        booking.full_clean()
        booking.save(update_fields=["state", "updated_at"])
        _save_booking_event(
            ScheduleBookingEvent(
                club_id=club_id,
                enrollment=booking.enrollment,
                schedule=schedule,
                student_id=booking.enrollment.student_id,
                actor_id=actor_user_id,
                event_type=ScheduleBookingEvent.EventType.PERSONAL_DROP_IN_CANCELLED,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                effective_date=schedule.one_time_date,
                metadata={"personal_drop_in_booking_id": booking.id, "reason": reason.strip()},
            )
        )
        return booking


def mark_personal_drop_in_no_show(
    *, club_id: int, booking_id: int, actor_user_id: int, reason: str
) -> PersonalDropInBooking:
    if not reason.strip():
        raise BusinessLogicError("No-show reason is required", code="no_show_reason_required")
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope
        from apps.billing.service_modules.bank_orders import (
            close_pending_personal_drop_in_financial_family,
        )

        lock_training_group_mutation_scope(club_id=club_id)
        close_pending_personal_drop_in_financial_family(
            club_id=club_id,
            booking_id=booking_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment", "enrollment__schedule")
            .get(id=booking_id)
        )
        if booking.state == PersonalDropInBooking.State.NO_SHOW:
            return booking
        if booking.state != PersonalDropInBooking.State.SCHEDULED:
            raise BusinessLogicError(
                "Only unattended booking can be marked no-show", code="personal_drop_in_not_no_show"
            )
        schedule = booking.enrollment.schedule
        ends_at = timezone.make_aware(
            datetime.combine(schedule.one_time_date, schedule.end_time), club_zoneinfo(schedule.club)
        )
        if ends_at > timezone.now():
            raise BusinessLogicError(
                "No-show is available only after session end", code="personal_drop_in_not_finished"
            )
        if _live_payment_link(booking=booking, lock=True) is not None:
            raise BusinessLogicError(
                "Cancel or reject the pending payment first", code="personal_drop_in_payment_pending"
            )
        booking.enrollment.status = ScheduleEnrollment.Status.CANCELLED
        booking.enrollment.full_clean()
        booking.enrollment.save(update_fields=["status", "updated_at"])
        schedule.is_active = False
        schedule.save(update_fields=["is_active", "updated_at"])
        PersonalAvailabilitySlot.objects.for_club(club_id).filter(
            booked_enrollment_id=booking.enrollment_id,
            status=PersonalAvailabilitySlot.Status.BOOKED,
        ).update(status=PersonalAvailabilitySlot.Status.CANCELLED, updated_at=timezone.now())
        booking.state = PersonalDropInBooking.State.NO_SHOW
        booking.full_clean()
        booking.save(update_fields=["state", "updated_at"])
        _save_booking_event(
            ScheduleBookingEvent(
                club_id=club_id,
                enrollment=booking.enrollment,
                schedule=schedule,
                student_id=booking.enrollment.student_id,
                actor_id=actor_user_id,
                event_type=ScheduleBookingEvent.EventType.PERSONAL_DROP_IN_NO_SHOW,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                effective_date=schedule.one_time_date,
                metadata={"personal_drop_in_booking_id": booking.id, "reason": reason.strip()},
            )
        )
        return booking
