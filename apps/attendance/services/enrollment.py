from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalSelfServiceCommand,
    PersonalServiceTermsSnapshot,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    TrainingGroupMembership,
    complete_personal_terms_queryset,
)
from apps.attendance.personal_offers import direct_personal_offer_payload, personal_offer_payload
from apps.attendance.services.personal_terms import (
    create_complete_personal_terms_snapshot,
    create_legacy_partial_personal_terms_snapshot,
)
from apps.billing.models import BankPaymentOrder, Payment, Subscription, SubscriptionComponent, Tariff, TrainingType
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import Club, Location
from apps.clubs.timezones import club_local_day_start_by_id, club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate

OPEN_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.ACTIVE,
    ScheduleEnrollment.Status.TRIAL,
    ScheduleEnrollment.Status.FROZEN,
)

TERMINAL_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.TRANSFERRED,
    ScheduleEnrollment.Status.CANCELLED,
)


def _raise_personal_drop_in_action_required() -> None:
    raise BusinessLogicError(
        "Use the dedicated personal drop-in booking action",
        code="personal_drop_in_use_booking_action",
    )


def _assert_schedule_accepts_generic_enrollment(*, club_id: int, schedule_id: int) -> None:
    if PersonalDropInBooking.objects.for_club(club_id).filter(
        enrollment__schedule_id=schedule_id,
    ).exists():
        _raise_personal_drop_in_action_required()


def _assert_enrollment_uses_generic_lifecycle(*, club_id: int, enrollment_id: int) -> None:
    if PersonalDropInBooking.objects.for_club(club_id).filter(
        enrollment_id=enrollment_id,
    ).exists():
        _raise_personal_drop_in_action_required()
    if ScheduleEnrollment.objects.for_club(club_id).filter(
        id=enrollment_id,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    ).exists():
        raise BusinessLogicError(
            "Use the dedicated personal booking cancellation action.",
            code="personal_booking_use_booking_action",
        )
    if ScheduleEnrollment.objects.for_club(club_id).filter(
        id=enrollment_id,
        training_group_membership_id__isnull=False,
    ).exists():
        raise BusinessLogicError(
            "Use the canonical training group membership lifecycle.",
            code="group_membership_action_required",
        )
    if Payment.objects.for_club(club_id).filter(
        conversion_enrollment_id=enrollment_id,
        status=Payment.Status.PENDING,
        conversion_enrollment__created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    ).exists():
        raise BusinessLogicError(
            "Операционную запись ожидающей оплаты нельзя менять этим действием",
            code="pending_payment_enrollment_action_forbidden",
        )


def _local_datetime(target_date: date, slot_time) -> datetime:
    value = datetime.combine(target_date, slot_time)
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _as_local_aware(value: datetime) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _as_club_aware(value: datetime, *, club) -> datetime:
    club_tz = club_zoneinfo(club)
    if timezone.is_naive(value):
        return timezone.make_aware(value, club_tz)
    return timezone.localtime(value, club_tz)


def _ensure_personal_booking_starts_in_future(starts_at: datetime, *, code: str) -> None:
    if _as_local_aware(starts_at) <= timezone.now():
        raise BusinessLogicError(
            "Personal booking requires a future slot",
            code=code,
        )


def _ensure_self_service_group_booking_starts_in_future(
    *,
    occurrence,
    club,
    origin: str,
) -> None:
    if origin not in SELF_SERVICE_BOOKING_ORIGINS:
        return
    starts_at = timezone.make_aware(
        datetime.combine(occurrence.effective_date, occurrence.effective_start_time),
        club_zoneinfo(club),
    )
    if starts_at <= timezone.now():
        raise BusinessLogicError(
            "Self-service booking requires a future session",
            code="self_booking_past_session",
        )


def _active_through_date_q(*, field_name: str, target_date: date, club_id: int) -> models.Q:
    return models.Q(**{f"{field_name}__isnull": True}) | models.Q(
        **{f"{field_name}__gt": club_local_day_start_by_id(club_id, target_date)}
    )


GUEST_BOOKING_STUDENT_STATUSES = {
    Student.Status.ACTIVE,
    Student.Status.LEAD,
    Student.Status.TRIAL,
    Student.Status.AT_RISK,
    Student.Status.CHURNED,
}

DATED_EXPECTED_CREATED_FROM = {
    ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
    ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
    ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
}

PERSONAL_PAYMENT_RESERVATION_HOLD = timedelta(minutes=60)

ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES = (
    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
    PersonalBookingPaymentReservation.Status.BOOKED,
    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
)

ACTIVE_PERSONAL_AVAILABILITY_SLOT_STATUSES = (
    PersonalAvailabilitySlot.Status.PUBLISHED,
    PersonalAvailabilitySlot.Status.HELD,
    PersonalAvailabilitySlot.Status.BOOKED,
    PersonalAvailabilitySlot.Status.BLOCKED,
)

SELF_SERVICE_BOOKING_ORIGINS = {
    ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
    ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
}

GUEST_BOOKING_CREATED_FROM = {
    ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
    ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
}


@dataclass(frozen=True)
class GuestGroupVisitBooking:
    enrollment: ScheduleEnrollment
    created: bool
    already_member: bool
    origin: str

    @property
    def is_guest_visit(self) -> bool:
        return (
            self.enrollment.created_from in GUEST_BOOKING_CREATED_FROM
            and self.enrollment.starts_on is not None
            and self.enrollment.starts_on == self.enrollment.ends_on
        )

    @property
    def financial_preview(self) -> dict[str, str]:
        return {
            "code": "resolved_at_checkin",
            "message": "Финансы будут рассчитаны при check-in",
        }


@dataclass(frozen=True)
class PersonalSessionBooking:
    schedule: Schedule
    enrollment: ScheduleEnrollment
    created: bool
    entitlement_subscription_id: int | None = None
    entitlement_component_id: int | None = None
    availability_slot_id: int | None = None
    # ``True`` only when this booking owner made the command's one allowed
    # result transition in the same transaction as the booking evidence.
    command_bound: bool = False


@dataclass(frozen=True)
class BookingCancellation:
    enrollment: ScheduleEnrollment
    event_created: bool


def _raise_validation_error(exc: ValidationError) -> None:
    messages = []
    if hasattr(exc, "message_dict"):
        for field, field_messages in exc.message_dict.items():
            messages.append(f"{field}: {', '.join(field_messages)}")
    else:
        messages.extend(exc.messages)
    message = "; ".join(messages) or "Invalid schedule enrollment"
    raise BusinessLogicError(message, code="invalid_schedule_enrollment") from exc


def _validate_open_status(status: str) -> None:
    if status not in OPEN_ENROLLMENT_STATUSES:
        raise BusinessLogicError(
            "Enrollment status must be active, trial, or frozen",
            code="invalid_enrollment_status",
        )


def _save_enrollment(enrollment: ScheduleEnrollment) -> ScheduleEnrollment:
    try:
        enrollment.full_clean()
    except ValidationError as exc:
        _raise_validation_error(exc)
    enrollment.save()
    return enrollment


def _save_booking_event(event: ScheduleBookingEvent) -> ScheduleBookingEvent:
    try:
        event.full_clean()
    except ValidationError as exc:
        _raise_validation_error(exc)
    event.save()
    return event


def _get_enrollment_for_update(*, club_id: int, enrollment_id: int) -> ScheduleEnrollment:
    enrollment = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update()
        .select_related("student", "schedule")
        .filter(id=enrollment_id)
        .first()
    )
    if enrollment is None:
        raise BusinessLogicError(
            "Enrollment not found",
            code="enrollment_not_found",
        )
    return enrollment


def _get_existing_guest_booking(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    target_date: date,
) -> ScheduleEnrollment | None:
    return (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_related("student", "schedule")
        .filter(
            student_id=student_id,
            schedule_id=schedule_id,
            starts_on=target_date,
            ends_on=target_date,
            status__in=OPEN_ENROLLMENT_STATUSES,
            created_from__in=DATED_EXPECTED_CREATED_FROM,
        )
        .order_by("id")
        .first()
    )


def _booking_time(value: datetime):
    return value.time().replace(tzinfo=None, microsecond=0)


def _personal_session_group_name(student: Student) -> str:
    display_name = f"{student.last_name} {student.first_name}".strip() or f"#{student.id}"
    return f"Персоналка: {display_name}"[:100]


def _get_existing_personal_booking(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    starts_at: datetime,
    ends_at: datetime,
) -> ScheduleEnrollment | None:
    target_date = starts_at.date()
    return (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_related("student", "schedule", "schedule__trainer", "schedule__location", "schedule__training_type")
        .filter(
            student_id=student_id,
            starts_on=target_date,
            ends_on=target_date,
            status__in=OPEN_ENROLLMENT_STATUSES,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            schedule__trainer_id=trainer_id,
            schedule__location_id=location_id,
            schedule__training_type_id=training_type_id,
            schedule__one_time_date=target_date,
            schedule__start_time=_booking_time(starts_at),
            schedule__end_time=_booking_time(ends_at),
        )
        .order_by("id")
        .first()
    )


def _get_personal_booking_subscription(
    *,
    club_id: int,
    student_id: int,
    location_id: int,
    training_type_id: int,
    target_date: date,
    subscription_id: int | None = None,
    lock: bool = True,
    starts_at: datetime | None = None,
) -> Subscription | None:
    qs = (
        Subscription.objects.for_club(club_id)
        # The legacy eligibility branch outer-joins SubscriptionComponent.
        .select_related("tariff")
        .filter(
            student_id=student_id,
            status=Subscription.Status.ACTIVE,
            deleted_at__isnull=True,
        )
        .filter(
            _active_through_date_q(
                field_name="expires_at",
                target_date=target_date,
                club_id=club_id,
            )
        )
        .order_by("expires_at", "id")
    )
    # Reads for options must use the same resolver as mutations, but must not
    # acquire a row lock outside an atomic block on PostgreSQL.  Mutations keep
    # the historical locking default.  Lock every candidate parent before a
    # component query: personal booking and generic check-in both use the
    # typed ``Subscription -> SubscriptionComponent`` tail after attendance
    # roots, so component-first selection would create an inverse cycle.
    if lock:
        qs = qs.select_for_update(of=("self",))
        list(qs.order_by("id").values_list("id", flat=True))
    legacy_qs = (
        qs.filter(
            tariff__training_type_id=training_type_id,
            components__isnull=True,
        )
        .filter(models.Q(trainings_left__isnull=True) | models.Q(trainings_left__gt=0))
        .filter(models.Q(scope=Tariff.Scope.CLUB) | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
    )
    components = (
        SubscriptionComponent.objects.for_club(club_id)
        .select_related("subscription")
        .filter(
            subscription__student_id=student_id,
            subscription__status=Subscription.Status.ACTIVE,
            subscription__deleted_at__isnull=True,
            training_type_id=training_type_id,
            is_active=True,
        )
        .filter(
            _active_through_date_q(
                field_name="subscription__expires_at",
                target_date=target_date,
                club_id=club_id,
            )
        )
        .filter(models.Q(credits_left__isnull=True) | models.Q(credits_left__gt=0))
        .filter(models.Q(scope=Tariff.Scope.CLUB) | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
        .order_by("subscription__expires_at", "subscription_id", "id")
    )
    if lock:
        components = components.select_for_update(of=("self",))
    if subscription_id is not None:
        legacy = legacy_qs.filter(id=subscription_id).first()
        if legacy is not None:
            return legacy
        components = components.filter(subscription_id=subscription_id)

    from apps.attendance.services.checkin import _component_has_weekly_capacity

    for scoped_components in (
        components.filter(scope=Tariff.Scope.LOCATION, location_id=location_id),
        components.filter(scope=Tariff.Scope.CLUB),
    ):
        for component in scoped_components:
            if _component_has_weekly_capacity(component=component, checkin_date=target_date):
                subscription = qs.get(id=component.subscription_id)
                if starts_at is not None:
                    try:
                        assert_personal_entitlement_booking_capacity(
                            subscription=subscription,
                            component=component,
                            starts_at=starts_at,
                        )
                    except BusinessLogicError:
                        # A component can cover this training type but already
                        # be promised to a future visit.  Keep scanning under
                        # the same Subscription -> Component lock tail rather
                        # than falling through to a paid option.
                        continue
                return subscription
    if subscription_id is not None:
        return None
    for legacy_subscription in (
        legacy_qs.filter(scope=Tariff.Scope.LOCATION, location_id=location_id),
        legacy_qs.filter(scope=Tariff.Scope.CLUB),
    ):
        for subscription in legacy_subscription:
            if starts_at is not None:
                try:
                    assert_personal_entitlement_booking_capacity(
                        subscription=subscription,
                        component=None,
                        starts_at=starts_at,
                    )
                except BusinessLogicError:
                    continue
            return subscription
    return None


def get_personal_booking_entitlement(
    *,
    club_id: int,
    student_id: int,
    location_id: int,
    training_type_id: int,
    target_date: date,
    lock: bool = True,
    subscription_id: int | None = None,
    starts_at: datetime | None = None,
) -> tuple[Subscription, SubscriptionComponent | None] | None:
    """Resolve the same subscription/component priority used by booking.

    Component evidence is returned for the capability-on coordinator so its
    future-booking reservation guards never use the older tariff-only read.
    ``None`` remains the exact no-coverage answer; legacy subscriptions carry
    a ``None`` component because they have no component authority to reserve.
    """

    subscription = _get_personal_booking_subscription(
        club_id=club_id,
        student_id=student_id,
        location_id=location_id,
        training_type_id=training_type_id,
        target_date=target_date,
        lock=lock,
        subscription_id=subscription_id,
        starts_at=starts_at,
    )
    if subscription is None:
        return None
    components = (
        SubscriptionComponent.objects.for_club(club_id)
        .select_related("subscription")
        .filter(
            subscription_id=subscription.id,
            training_type_id=training_type_id,
            is_active=True,
        )
        .filter(models.Q(credits_left__isnull=True) | models.Q(credits_left__gt=0))
        .filter(
            models.Q(scope=Tariff.Scope.CLUB)
            | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id)
        )
        .order_by("subscription__expires_at", "subscription_id", "id")
    )
    if lock:
        components = components.select_for_update(of=("self",))
    from apps.attendance.services.checkin import _component_has_weekly_capacity

    for component_scope in (Tariff.Scope.LOCATION, Tariff.Scope.CLUB):
        for component in components.filter(
            scope=component_scope,
            **({"location_id": location_id} if component_scope == Tariff.Scope.LOCATION else {}),
        ):
            if _component_has_weekly_capacity(component=component, checkin_date=target_date):
                if starts_at is not None:
                    try:
                        assert_personal_entitlement_booking_capacity(
                            subscription=subscription,
                            component=component,
                            starts_at=starts_at,
                        )
                    except BusinessLogicError:
                        continue
                return subscription, component
    if not components.exists():
        return subscription, None
    return None


def _active_personal_entitlement_reservations(*, subscription: Subscription):
    """Return live personal-booking evidence that still reserves entitlement.

    The booking owner writes this event atomically with the enrollment.  It is
    therefore the one ledger shared by self-service, staff booking and
    attendance; a coordinator command is deliberately not capacity evidence.
    """

    consumed_checkin = Checkin.objects.for_club(subscription.club_id).filter(
        student_id=models.OuterRef("enrollment__student_id"),
        schedule_id=models.OuterRef("enrollment__schedule_id"),
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    )
    return (
        ScheduleBookingEvent.objects.for_club(subscription.club_id)
        .filter(
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            metadata__subscription_id=subscription.id,
            enrollment__status__in=OPEN_ENROLLMENT_STATUSES,
        )
        .annotate(has_exact_checkin=models.Exists(consumed_checkin))
        .filter(has_exact_checkin=False)
    )


def _legacy_personal_booking_component_id(*, event: ScheduleBookingEvent, subscription_id: int) -> int | None:
    """Return only durable, unambiguous component evidence for a legacy event.

    Booking events are append-only.  A pre-component event therefore cannot be
    rewritten to look as if it chose a component.  A bound self-service command
    is immutable evidence; otherwise a single component snapshot that covers
    the event is the sole safe runtime attribution.  Two candidate components
    are deliberately left unresolved so the reservation remains conservative.
    """

    command_component_id = (
        PersonalSelfServiceCommand.objects.for_club(event.club_id)
        .filter(
            enrollment_id=event.enrollment_id,
            entitlement_subscription_id=subscription_id,
            entitlement_component_id__isnull=False,
        )
        .values_list("entitlement_component_id", flat=True)
        .first()
    )
    if command_component_id is not None:
        return command_component_id

    schedule = event.schedule
    candidate_ids = list(
        SubscriptionComponent.objects.for_club(event.club_id)
        .filter(subscription_id=subscription_id, training_type_id=schedule.training_type_id)
        .filter(
            models.Q(scope=Tariff.Scope.CLUB)
            | models.Q(scope=Tariff.Scope.LOCATION, location_id=schedule.location_id)
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    return candidate_ids[0] if len(candidate_ids) == 1 else None


def personal_booking_event_component_id(*, event: ScheduleBookingEvent, subscription_id: int) -> int | None:
    """Read exact component evidence without changing the booking event."""

    raw_component_id = (event.metadata or {}).get("subscription_component_id")
    if (
        isinstance(raw_component_id, int)
        and raw_component_id > 0
        and SubscriptionComponent.objects.for_club(event.club_id)
        .filter(id=raw_component_id, subscription_id=subscription_id)
        .exists()
    ):
        return raw_component_id
    return _legacy_personal_booking_component_id(
        event=event,
        subscription_id=subscription_id,
    )


def _reserved_personal_entitlement_enrollment_ids(
    *,
    reservations,
    subscription: Subscription,
    component: SubscriptionComponent,
) -> set[int]:
    """Count exact component evidence, conservatively retaining legacy rows.

    Missing component metadata must never make an active personal booking
    disappear from the component capacity ledger.  If immutable evidence cannot
    identify exactly one component, the row reserves every candidate component
    instead of guessing from a mutable tariff/catalogue.
    """

    reservation_events = list(
        reservations.select_related("schedule").order_by("enrollment_id", "id")
    )
    reserved_enrollment_ids: set[int] = set()
    for event in reservation_events:
        attributed_component_id = personal_booking_event_component_id(
            event=event,
            subscription_id=subscription.id,
        )
        if attributed_component_id is None or attributed_component_id == component.id:
            reserved_enrollment_ids.add(event.enrollment_id)
    return reserved_enrollment_ids


def _assert_personal_entitlement_capacity(
    *,
    subscription: Subscription,
    component: SubscriptionComponent | None,
    starts_at: datetime,
    proposed_consumption: int,
    own_student_id: int | None = None,
    own_schedule_id: int | None = None,
    own_checkin_id: int | None = None,
    finite_code: str,
    finite_message: str,
    weekly_code: str,
    weekly_message: str,
    legacy_code: str,
    legacy_message: str,
) -> None:
    """Assert one shared reservation/consumption capacity invariant.

    ``proposed_consumption`` is one for either a prospective future reservation
    or a check-in.  For an exact booked visit, that visit's own reservation is
    excluded: it changes state from reserved to consumed once, never twice.
    All other active personal bookings remain reserved even after their start
    time until an explicit cancellation/no-show disposition.
    """

    from apps.attendance.services.checkin import _week_bounds
    from apps.billing.models import TariffComponent

    club_zone = club_zoneinfo(subscription.club)
    local_starts_at = timezone.localtime(starts_at, club_zone)
    active_booking_reservations = _active_personal_entitlement_reservations(subscription=subscription)
    if own_student_id is not None and own_schedule_id is not None:
        active_booking_reservations = active_booking_reservations.exclude(
            enrollment__student_id=own_student_id,
            enrollment__schedule_id=own_schedule_id,
        )
    if component is None:
        if subscription.trainings_left is not None:
            reserved = active_booking_reservations.values("enrollment_id").distinct().count()
            if reserved + proposed_consumption > subscription.trainings_left:
                raise BusinessLogicError(legacy_message, code=legacy_code)
        return

    component_booking_enrollment_ids = _reserved_personal_entitlement_enrollment_ids(
        reservations=active_booking_reservations,
        subscription=subscription,
        component=component,
    )
    if component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
        reserved = len(component_booking_enrollment_ids)
        # Remaining capacity already includes consumed/imported visits, exact
        # renewal carry and accepted correction deltas. Purchased total is an
        # immutable money basis, not today's reservation ceiling.
        if reserved + proposed_consumption > (component.credits_left or 0):
            raise BusinessLogicError(finite_message, code=finite_code)
    if component.entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT:
        week_start, week_end = _week_bounds(local_starts_at.date())
        used_checkins_this_week = Checkin.objects.for_club(subscription.club_id).filter(
            subscription_component_id=component.id,
            date__gte=week_start,
            date__lte=week_end,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        if own_checkin_id is not None:
            used_checkins_this_week = used_checkins_this_week.exclude(id=own_checkin_id)
        used_this_week = used_checkins_this_week.count()
        reserved_this_week = len(
            _reserved_personal_entitlement_enrollment_ids(
                reservations=active_booking_reservations.filter(
                    effective_date__gte=week_start,
                    effective_date__lte=week_end,
                ),
                subscription=subscription,
                component=component,
            )
        )
        if used_this_week + reserved_this_week + proposed_consumption > (component.weekly_limit or 0):
            raise BusinessLogicError(weekly_message, code=weekly_code)


def assert_personal_entitlement_booking_capacity(
    *,
    subscription: Subscription,
    component: SubscriptionComponent | None,
    starts_at: datetime,
) -> None:
    """Reserve one future personal booking under its selected entitlement."""

    _assert_personal_entitlement_capacity(
        subscription=subscription,
        component=component,
        starts_at=starts_at,
        proposed_consumption=1,
        finite_code="subscription_component_future_capacity_exhausted",
        finite_message="The subscription component has no remaining future booking capacity.",
        weekly_code="subscription_component_future_capacity_exhausted",
        weekly_message="The subscription component has no remaining weekly booking capacity.",
        legacy_code="subscription_future_capacity_exhausted",
        legacy_message="The subscription has no remaining future booking capacity.",
    )


def assert_personal_entitlement_checkin_capacity(
    *,
    subscription: Subscription,
    component: SubscriptionComponent | None,
    checkin_at: datetime,
    student_id: int,
    schedule_id: int,
    checkin_id: int,
) -> None:
    """Consume one visit without stealing capacity reserved by another slot."""

    _assert_personal_entitlement_capacity(
        subscription=subscription,
        component=component,
        starts_at=checkin_at,
        proposed_consumption=1,
        own_student_id=student_id,
        own_schedule_id=schedule_id,
        own_checkin_id=checkin_id,
        finite_code="subscription_component_credits_exhausted",
        finite_message="В компоненте абонемента закончились посещения",
        weekly_code="subscription_component_weekly_limit_exceeded",
        weekly_message="В компоненте абонемента закончился недельный лимит",
        legacy_code="subscription_limit_exceeded",
        legacy_message="В абонементе закончились посещения",
    )


# Kept as a private compatibility name while selector/caller edits land in a
# dirty shared worktree.  New owner paths use the explicit shared authority.
_assert_self_service_future_entitlement_capacity = assert_personal_entitlement_booking_capacity


def _bind_self_service_booking_command(
    *,
    club_id: int,
    command_id: int,
    command_key: str | None,
    student_id: int,
    slot_id: int,
    enrollment: ScheduleEnrollment,
    subscription_id: int,
    component_id: int | None,
) -> bool:
    """Bind exact booking evidence while the booking owner's locks are held."""

    from apps.attendance.models import PersonalSelfServiceCommand

    command = (
        PersonalSelfServiceCommand.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=command_id)
        .first()
    )
    if (
        command is None
        or command.action != PersonalSelfServiceCommand.Action.BOOK
        or command.command_key != (command_key or "")
        or command.student_id != student_id
        or command.availability_slot_id != slot_id
    ):
        raise BusinessLogicError(
            "Personal self-service booking command evidence is unavailable.",
            code="personal_self_service_evidence_missing",
        )

    expected = (
        enrollment.id,
        subscription_id,
        component_id,
    )
    if command.result_bound_at is not None:
        if (
            command.enrollment_id_snapshot,
            command.entitlement_subscription_id,
            command.entitlement_component_id,
        ) != expected:
            raise BusinessLogicError(
                "Personal self-service booking command result conflicts with its owner evidence.",
                code="personal_self_service_evidence_conflict",
            )
        return False

    command.enrollment_id_snapshot = enrollment.id
    command.enrollment_id = enrollment.id
    command.entitlement_subscription_id = subscription_id
    command.entitlement_component_id = component_id
    command.result_bound_at = timezone.now()
    command.save(
        update_fields=[
            "enrollment_id_snapshot",
            "enrollment",
            "entitlement_subscription",
            "entitlement_component",
            "result_bound_at",
            "updated_at",
        ]
    )
    return True


def _personal_booking_payment_hold_expires_at(*, starts_at: datetime, now: datetime) -> datetime:
    hold_expires_at = now + PERSONAL_PAYMENT_RESERVATION_HOLD
    if starts_at <= now:
        return now
    return min(hold_expires_at, starts_at)


def _matching_personal_subscription_exists(
    *,
    club_id: int,
    student_id: int,
    location_id: int,
    training_type_id: int,
    target_date: date,
    starts_at: datetime,
) -> bool:
    if _has_pending_personal_booking_subscription(
        club_id=club_id,
        student_id=student_id,
        location_id=location_id,
        training_type_id=training_type_id,
        lock=True,
    ):
        return True
    entitlement = get_personal_booking_entitlement(
        club_id=club_id,
        student_id=student_id,
        location_id=location_id,
        training_type_id=training_type_id,
        target_date=target_date,
        starts_at=starts_at,
        lock=True,
    )
    return entitlement is not None


def has_pending_personal_booking_subscription(
    *,
    club_id: int,
    student_id: int,
    location_id: int,
    training_type_id: int,
) -> bool:
    """Read whether an existing pending personal purchase owns this next step."""

    return _has_pending_personal_booking_subscription(
        club_id=club_id,
        student_id=student_id,
        location_id=location_id,
        training_type_id=training_type_id,
        lock=False,
    )


def _has_pending_personal_booking_subscription(
    *,
    club_id: int,
    student_id: int,
    location_id: int,
    training_type_id: int,
    lock: bool,
) -> bool:
    pending_subscriptions = (
        Subscription.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            status=Subscription.Status.PENDING,
            tariff__training_type_id=training_type_id,
            deleted_at__isnull=True,
        )
        .filter(models.Q(scope=Tariff.Scope.CLUB) | models.Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
    )
    if lock:
        pending_subscriptions = pending_subscriptions.select_for_update(of=("self",))
    return pending_subscriptions.order_by("id").only("id").first() is not None


def _active_personal_payment_reservation_overlap_exists(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
    exclude_id: int | None = None,
) -> bool:
    qs = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=ACTIVE_PERSONAL_PAYMENT_RESERVATION_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .filter(
            models.Q(status=PersonalBookingPaymentReservation.Status.BOOKED)
            | models.Q(status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW)
            | models.Q(expires_at__gt=now)
        )
    )
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.exists()


def _personal_booking_overlap_exists(
    *,
    club_id: int,
    trainer_id: int,
    target_date: date,
    starts_at: datetime,
    ends_at: datetime,
    lock: bool = True,
) -> bool:
    qs = Schedule.objects.for_club(club_id)
    if lock:
        qs = qs.select_for_update()
    candidates = qs.filter(
        trainer_id=trainer_id,
        is_active=True,
        start_time__lt=_booking_time(ends_at),
        end_time__gt=_booking_time(starts_at),
    ).filter(
        models.Q(one_time_date=target_date) | models.Q(one_time_date__isnull=True, day_of_week=target_date.weekday())
    )
    if lock:
        list(candidates.values_list("id", flat=True))

    from apps.attendance.selectors import get_schedule_occurrences_for_date

    return any(
        occurrence.effective_start_time < _booking_time(ends_at)
        and occurrence.effective_end_time > _booking_time(starts_at)
        for occurrence in get_schedule_occurrences_for_date(
            club=club_id,
            target_date=target_date,
            trainer_id=trainer_id,
        )
    )


def _get_personal_payment_availability_slot(
    *,
    club_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    starts_at: datetime,
    ends_at: datetime,
    availability_slot_id: int | None,
) -> PersonalAvailabilitySlot | None:
    qs = PersonalAvailabilitySlot.objects.for_club(club_id).select_for_update(of=("self",))
    if availability_slot_id is not None:
        slot = qs.filter(id=availability_slot_id).first()
        if slot is None:
            raise BusinessLogicError(
                "Personal availability slot not found",
                code="personal_availability_slot_not_found",
            )
    else:
        slot = (
            qs.filter(
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            .order_by("id")
            .first()
        )
        if slot is None:
            return None

    if (
        slot.trainer_id != trainer_id
        or slot.location_id != location_id
        or slot.training_type_id != training_type_id
        or slot.starts_at != starts_at
        or slot.ends_at != ends_at
    ):
        raise BusinessLogicError(
            "Personal availability slot does not match requested reservation",
            code="personal_availability_slot_mismatch",
        )
    if slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
        raise BusinessLogicError(
            "Personal availability slot is not available",
            code="personal_availability_slot_unavailable",
        )
    return slot


def _lock_overlapping_personal_availability_slots(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
) -> list[PersonalAvailabilitySlot]:
    """Lock every live slot in the booking window after locking its trainer."""
    return list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=ACTIVE_PERSONAL_AVAILABILITY_SLOT_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .order_by("id")
    )


def _get_exact_personal_availability_slot_for_direct_booking(
    *,
    club_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    starts_at: datetime,
    ends_at: datetime,
) -> PersonalAvailabilitySlot | None:
    slot = (
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
            status__in=ACTIVE_PERSONAL_AVAILABILITY_SLOT_STATUSES,
        )
        .order_by("id")
        .first()
    )
    if slot is None:
        return None
    if slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
        raise BusinessLogicError(
            "Personal availability slot is not available",
            code="personal_availability_slot_unavailable",
        )
    return slot


def _release_personal_payment_availability_slot(*, club_id: int, slot_id: int | None) -> None:
    if slot_id is None:
        return
    slot = PersonalAvailabilitySlot.objects.for_club(club_id).select_for_update(of=("self",)).filter(id=slot_id).first()
    if slot is not None and slot.status == PersonalAvailabilitySlot.Status.HELD and slot.booked_enrollment_id is None:
        slot.status = PersonalAvailabilitySlot.Status.PUBLISHED
        slot.save(update_fields=["status", "updated_at"])


def _record_guest_booking_event(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
    target_date: date,
    origin: str,
    actor_user_id: int | None,
    idempotency_key: str | None,
) -> None:
    metadata = {}
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    _save_booking_event(
        ScheduleBookingEvent(
            club_id=club_id,
            enrollment=enrollment,
            schedule=enrollment.schedule,
            student=enrollment.student,
            actor_id=actor_user_id,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED,
            origin=origin,
            effective_date=target_date,
            metadata=metadata,
        )
    )


def _record_personal_booking_event(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
    target_date: date,
    actor_user_id: int | None,
    idempotency_key: str | None,
    subscription_id: int | None,
    subscription_component_id: int | None = None,
    origin: str,
    availability_slot_id: int | None = None,
) -> None:
    metadata = {}
    if availability_slot_id is not None:
        metadata["availability_slot_id"] = availability_slot_id
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    if subscription_id is not None:
        metadata["subscription_id"] = subscription_id
    if subscription_component_id is not None:
        metadata["subscription_component_id"] = subscription_component_id
    _save_booking_event(
        ScheduleBookingEvent(
            club_id=club_id,
            enrollment=enrollment,
            schedule=enrollment.schedule,
            student=enrollment.student,
            actor_id=actor_user_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=origin,
            effective_date=target_date,
            metadata=metadata,
        )
    )


def _record_booking_cancellation_event(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
    event_type: str,
    origin: str,
    actor_user_id: int | None,
    reason: str,
) -> None:
    metadata = {}
    if reason:
        metadata["reason"] = reason
    _save_booking_event(
        ScheduleBookingEvent(
            club_id=club_id,
            enrollment=enrollment,
            schedule=enrollment.schedule,
            student=enrollment.student,
            actor_id=actor_user_id,
            event_type=event_type,
            origin=origin,
            effective_date=enrollment.starts_on,
            metadata=metadata,
        )
    )


def _ensure_enrollment_is_open(enrollment: ScheduleEnrollment) -> None:
    if enrollment.status not in OPEN_ENROLLMENT_STATUSES:
        raise BusinessLogicError(
            "Enrollment is already closed",
            code="enrollment_closed",
        )


def book_guest_group_visit(
    *,
    club_id: int,
    schedule_id: int,
    target_date: date,
    student_id: int | None = None,
    lead_id: int | None = None,
    origin: str = ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
    actor_user_id: int | None = None,
    idempotency_key: str | None = None,
    created_from: str = ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
    allow_lead_conversion: bool = True,
    require_financial_eligibility: bool = False,
) -> GuestGroupVisitBooking:
    if (student_id is None) == (lead_id is None):
        raise BusinessLogicError(
            "Provide exactly one of student_id or lead_id",
            code="exactly_one_guest_identity_required",
        )
    if origin not in ScheduleBookingEvent.Origin.values:
        raise BusinessLogicError(
            "Invalid guest booking origin",
            code="invalid_guest_booking_origin",
        )
    if created_from not in GUEST_BOOKING_CREATED_FROM:
        raise BusinessLogicError(
            "Invalid guest booking source",
            code="invalid_guest_booking_source",
        )

    booking_student_id = student_id if student_id is not None else lead_id
    try:
        return _book_guest_group_visit_locked(
            club_id=club_id,
            schedule_id=schedule_id,
            target_date=target_date,
            student_id=booking_student_id,
            origin=origin,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            created_from=created_from,
            allow_lead_conversion=allow_lead_conversion,
            require_financial_eligibility=require_financial_eligibility,
        )
    except IntegrityError:
        existing = _get_existing_guest_booking(
            club_id=club_id,
            student_id=booking_student_id,
            schedule_id=schedule_id,
            target_date=target_date,
        )
        if existing is not None:
            return GuestGroupVisitBooking(
                enrollment=existing,
                created=False,
                already_member=False,
                origin=origin,
            )
        raise


def book_personal_session(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    subscription_id: int | None = None,
    subscription_component_id: int | None = None,
    actor_user_id: int | None = None,
    idempotency_key: str | None = None,
    origin: str = ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
    availability_slot_id: int | None = None,
    payment_reservation_id: int | None = None,
    accepted_complete_terms: bool = False,
    enforce_entitlement_capacity: bool = False,
) -> PersonalSessionBooking:
    if origin not in ScheduleBookingEvent.Origin.values:
        raise BusinessLogicError(
            "Invalid personal booking origin",
            code="invalid_personal_booking_origin",
        )
    club = Club.objects.only("id", "timezone").filter(id=club_id).first()
    if club is not None:
        starts_at = _as_club_aware(starts_at, club=club)
        ends_at = _as_club_aware(ends_at, club=club)
    _ensure_personal_booking_starts_in_future(starts_at, code="personal_booking_past_slot")

    existing = _get_existing_personal_booking(
        club_id=club_id,
        student_id=student_id,
        trainer_id=trainer_id,
        location_id=location_id,
        training_type_id=training_type_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    if existing is not None:
        return PersonalSessionBooking(schedule=existing.schedule, enrollment=existing, created=False)

    try:
        return _book_personal_session_locked(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location_id,
            training_type_id=training_type_id,
            subscription_id=subscription_id,
            subscription_component_id=subscription_component_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            origin=origin,
            availability_slot_id=availability_slot_id,
            payment_reservation_id=payment_reservation_id,
            accepted_complete_terms=accepted_complete_terms,
            enforce_entitlement_capacity=enforce_entitlement_capacity,
        )
    except IntegrityError:
        target_date = starts_at.date()
        existing = _get_existing_personal_booking(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        if existing is not None:
            return PersonalSessionBooking(schedule=existing.schedule, enrollment=existing, created=False)
        if _personal_booking_overlap_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            target_date=target_date,
            starts_at=starts_at,
            ends_at=ends_at,
            lock=False,
        ):
            raise BusinessLogicError(
                "Trainer already has a schedule in this time slot",
                code="personal_booking_slot_conflict",
            )
        raise


def book_personal_availability_slot(
    *,
    club_id: int,
    slot_id: int,
    student_id: int,
    actor_user_id: int | None,
    origin: str,
    subscription_id: int | None = None,
    idempotency_key: str | None = None,
    reserve_self_service_entitlement: bool = False,
    self_service_command_id: int | None = None,
) -> PersonalSessionBooking:
    if origin not in ScheduleBookingEvent.Origin.values:
        raise BusinessLogicError(
            "Invalid personal booking origin",
            code="invalid_personal_booking_origin",
        )
    if self_service_command_id is not None and not reserve_self_service_entitlement:
        raise BusinessLogicError(
            "Personal self-service command binding requires an entitlement booking.",
            code="personal_self_service_evidence_missing",
        )

    slot_owner = (
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .filter(id=slot_id)
        .values_list("trainer_id", "training_type_id")
        .first()
    )
    if slot_owner is None:
        raise BusinessLogicError(
            "Personal availability slot not found",
            code="personal_availability_slot_not_found",
        )
    slot_trainer_id, slot_training_type_id = slot_owner

    with transaction.atomic():
        # Personal payment reservations arbitrate a slot under the same prefix.
        # Keep this order ahead of Trainer so deferred club FKs and catalog
        # revalidation cannot invert against a concurrent SBP reservation.
        Club.objects.select_for_update(of=("self",)).only("id").get(id=club_id)
        training_type = (
            TrainingType.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=slot_training_type_id)
            .only("id")
            .first()
        )
        if training_type is None:
            raise BusinessLogicError(
                "Personal availability training type not found",
                code="personal_availability_training_type_not_found",
            )
        trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=slot_trainer_id, is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError(
                "Trainer does not belong to this club",
                code="trainer_club_mismatch",
            )
        student = (
            Student.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=student_id, deleted_at__isnull=True)
            .first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related(
                "trainer",
                "location",
                "training_type",
                "booked_enrollment",
                "booked_enrollment__schedule",
                "booked_enrollment__schedule__trainer",
                "booked_enrollment__schedule__location",
                "booked_enrollment__schedule__training_type",
            )
            .filter(
                id=slot_id,
                trainer_id=trainer.id,
                training_type_id=training_type.id,
            )
            .first()
        )
        if slot is None:
            raise BusinessLogicError(
                "Personal availability slot not found",
                code="personal_availability_slot_not_found",
            )
        _ensure_personal_booking_starts_in_future(
            slot.starts_at,
            code="personal_availability_slot_past",
        )

        if slot.status == PersonalAvailabilitySlot.Status.BOOKED:
            enrollment = slot.booked_enrollment
            if (
                enrollment is not None
                and enrollment.student_id == student_id
                and enrollment.status in OPEN_ENROLLMENT_STATUSES
                and enrollment.created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
            ):
                if reserve_self_service_entitlement or subscription_id is not None:
                    evidence = (
                        ScheduleBookingEvent.objects.for_club(club_id)
                        .filter(
                            enrollment_id=enrollment.id,
                            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
                        )
                        .order_by("-id")
                        .values_list("metadata", flat=True)
                        .first()
                    ) or {}
                    subscription_evidence_id = evidence.get("subscription_id")
                    component_evidence_id = evidence.get("subscription_component_id")
                    if not isinstance(subscription_evidence_id, int):
                        raise BusinessLogicError(
                            "Personal self-service booking evidence is incomplete.",
                            code="personal_self_service_evidence_missing",
                        )
                    subscription_evidence = (
                        Subscription.objects.for_club(club_id)
                        .select_for_update(of=("self",))
                        .filter(id=subscription_evidence_id, student_id=student_id)
                        .first()
                    )
                    if subscription_evidence is None:
                        raise BusinessLogicError(
                            "Personal self-service booking evidence is unavailable.",
                            code="personal_self_service_evidence_missing",
                        )
                    if component_evidence_id is not None:
                        if not isinstance(component_evidence_id, int) or not SubscriptionComponent.objects.for_club(
                            club_id
                        ).select_for_update(of=("self",)).filter(
                            id=component_evidence_id,
                            subscription_id=subscription_evidence_id,
                        ).exists():
                            raise BusinessLogicError(
                                "Personal self-service booking evidence is unavailable.",
                                code="personal_self_service_evidence_missing",
                            )
                command_bound = False
                if self_service_command_id is not None:
                    command_bound = _bind_self_service_booking_command(
                        club_id=club_id,
                        command_id=self_service_command_id,
                        command_key=idempotency_key,
                        student_id=student.id,
                        slot_id=slot.id,
                        enrollment=enrollment,
                        subscription_id=subscription_evidence_id,
                        component_id=component_evidence_id,
                    )
                return PersonalSessionBooking(
                    schedule=enrollment.schedule,
                    enrollment=enrollment,
                    created=False,
                    entitlement_subscription_id=(
                        subscription_evidence_id
                        if reserve_self_service_entitlement or subscription_id is not None
                        else None
                    ),
                    entitlement_component_id=(
                        component_evidence_id
                        if reserve_self_service_entitlement or subscription_id is not None
                        else None
                    ),
                    command_bound=command_bound,
                )
            raise BusinessLogicError(
                "Personal availability slot is already booked",
                code="personal_availability_slot_taken",
            )
        if slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
            raise BusinessLogicError(
                "Personal availability slot is not available",
                code="personal_availability_slot_unavailable",
            )
        if slot.training_type.kind not in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}:
            raise BusinessLogicError(
                "Personal availability requires a personal or mini-group training type",
                code="personal_availability_requires_personal_type",
            )

        target_date = timezone.localtime(slot.starts_at, club_zoneinfo(slot.club)).date()
        entitlement_component_id = None
        if reserve_self_service_entitlement:
            if subscription_id is not None:
                raise BusinessLogicError(
                    "Personal self-service resolves entitlement on the server.",
                    code="personal_self_service_subscription_not_allowed",
                )
        if reserve_self_service_entitlement or subscription_id is not None:
            entitlement = get_personal_booking_entitlement(
                club_id=club_id,
                student_id=student_id,
                location_id=slot.location_id,
                training_type_id=slot.training_type_id,
                target_date=target_date,
                subscription_id=subscription_id,
                starts_at=slot.starts_at,
            )
            if entitlement is None:
                # The selector intentionally hides exhausted entitlement from
                # a fresh read.  A concurrently stale BOOK command, however,
                # must retain the owner-level capacity error rather than
                # degrading it to a generic missing subscription.
                exhausted_entitlement = get_personal_booking_entitlement(
                    club_id=club_id,
                    student_id=student_id,
                    location_id=slot.location_id,
                    training_type_id=slot.training_type_id,
                    target_date=target_date,
                    subscription_id=subscription_id,
                    starts_at=None,
                )
                if exhausted_entitlement is not None:
                    assert_personal_entitlement_booking_capacity(
                        subscription=exhausted_entitlement[0],
                        component=exhausted_entitlement[1],
                        starts_at=slot.starts_at,
                    )
                subscription = None
            else:
                subscription, component = entitlement
                assert_personal_entitlement_booking_capacity(
                    subscription=subscription,
                    component=component,
                    starts_at=slot.starts_at,
                )
                entitlement_component_id = component.id if component is not None else None
        else:
            subscription = _get_personal_booking_subscription(
                club_id=club_id,
                student_id=student_id,
                location_id=slot.location_id,
                training_type_id=slot.training_type_id,
                target_date=target_date,
                subscription_id=subscription_id,
            )
        if subscription is None:
            raise BusinessLogicError(
                "Subscription is not available for this personal booking",
                code="subscription_not_available",
            )

        booking = book_personal_session(
            club_id=club_id,
            student_id=student_id,
            trainer_id=slot.trainer_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            location_id=slot.location_id,
            training_type_id=slot.training_type_id,
            subscription_id=subscription.id,
            subscription_component_id=entitlement_component_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            origin=origin,
            availability_slot_id=slot.id,
            enforce_entitlement_capacity=True,
        )
        slot.status = PersonalAvailabilitySlot.Status.BOOKED
        slot.booked_enrollment = booking.enrollment
        slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
        command_bound = False
        if self_service_command_id is not None:
            command_bound = _bind_self_service_booking_command(
                club_id=club_id,
                command_id=self_service_command_id,
                command_key=idempotency_key,
                student_id=student.id,
                slot_id=slot.id,
                enrollment=booking.enrollment,
                subscription_id=subscription.id,
                component_id=entitlement_component_id,
            )
        return PersonalSessionBooking(
            schedule=booking.schedule,
            enrollment=booking.enrollment,
            created=booking.created,
            entitlement_subscription_id=subscription.id,
            entitlement_component_id=entitlement_component_id,
            command_bound=command_bound,
        )


def get_personal_booking_payment_reservations(
    *,
    club_id: int,
    student_id: int,
    status: str | None = None,
    allowed_sources: set[str] | None = None,
    allowed_trainer_id: int | None = None,
) -> list[PersonalBookingPaymentReservation]:
    qs = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_related(
            "student",
            "trainer",
            "location",
            "training_type",
            "tariff",
            "availability_slot",
            "payment",
            "bank_payment_order",
            "subscription",
            "schedule",
            "enrollment",
        )
        .filter(student_id=student_id)
        .order_by("-created_at", "-id")
    )
    if allowed_sources is not None:
        qs = qs.filter(bank_payment_order__source__in=allowed_sources)
    if allowed_trainer_id is not None:
        qs = qs.filter(trainer_id=allowed_trainer_id)
    if status:
        if status == "open_actionable":
            qs = qs.filter(
                status__in=[
                    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ]
            )
        else:
            qs = qs.filter(status=status)
    return list(qs)


def _assert_personal_payment_reservation_replay_matches(
    *,
    reservation: PersonalBookingPaymentReservation,
    student_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    tariff_id: int | None,
    starts_at: datetime,
    ends_at: datetime,
    availability_slot_id: int | None,
    ignore_tariff: bool = False,
) -> None:
    if (
        reservation.student_id != student_id
        or reservation.trainer_id != trainer_id
        or reservation.location_id != location_id
        or reservation.training_type_id != training_type_id
        or (not ignore_tariff and reservation.tariff_id != tariff_id)
        or reservation.starts_at != starts_at
        or reservation.ends_at != ends_at
        or reservation.availability_slot_id != availability_slot_id
    ):
        raise BusinessLogicError(
            "Idempotency key is already used for another personal payment reservation",
            code="personal_payment_reservation_idempotency_conflict",
        )


def _live_bank_order_for_personal_reservation(
    *,
    club_id: int,
    reservation_id: int,
) -> BankPaymentOrder | None:
    return (
        BankPaymentOrder.objects.for_club(club_id)
        .select_related("payment", "subscription")
        .filter(
            personal_booking_reservation_id_snapshot=reservation_id,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _attach_personal_reservation_bank_order(
    *,
    club_id: int,
    reservation_id: int,
    order: BankPaymentOrder,
) -> PersonalBookingPaymentReservation:
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        # Provider dispatch has already created its financial family.  Re-lock
        # its immutable reservation origin before attaching the result so this
        # handler never starts at BankOrder/Payment and reaches attendance
        # rows later.
        ordered_scope = lock_complete_personal_scopes(
            club_id=club_id,
            reservation_ids=[reservation_id],
        )
        reservation = (
            ordered_scope.reservations_by_id.get(reservation_id)
            if ordered_scope is not None
            else PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .get(id=reservation_id)
        )
        if reservation is None:
            raise BusinessLogicError(
                "Personal payment reservation was not found",
                code="personal_payment_reservation_not_found",
            )
        order = (
            ordered_scope.orders_by_id.get(order.id, order)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "subscription")
            .get(id=order.id)
        )
        if reservation.bank_payment_order_id is not None:
            if reservation.bank_payment_order_id != order.id:
                raise BusinessLogicError(
                    "Personal payment reservation already has another bank order",
                    code="personal_payment_reservation_order_conflict",
                )
            return reservation
        if (
            order.club_id != club_id
            or order.personal_booking_reservation_id_snapshot != reservation.id
            or order.student_id != reservation.student_id
            or order.payment.student_id != reservation.student_id
            or order.payment.tariff_id != reservation.tariff_id
            or order.subscription.student_id != reservation.student_id
            or order.subscription.tariff_id != reservation.tariff_id
        ):
            raise BusinessLogicError(
                "Bank order does not match personal payment reservation",
                code="personal_payment_reservation_order_mismatch",
            )
        reservation.payment = order.payment
        reservation.subscription = order.subscription
        reservation.bank_payment_order = order
        try:
            reservation.full_clean()
        except ValidationError as exc:
            _raise_validation_error(exc)
        reservation.save(
            update_fields=[
                "payment",
                "subscription",
                "bank_payment_order",
                "updated_at",
            ]
        )
        if order.expires_at > reservation.expires_at:
            order.expires_at = reservation.expires_at
            order.save(update_fields=["expires_at", "updated_at"])
        return reservation


def _reconcile_attached_personal_reservation(
    *,
    club_id: int,
    reservation: PersonalBookingPaymentReservation,
) -> PersonalBookingPaymentReservation:
    order = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_related("payment")
        .get(id=reservation.bank_payment_order_id)
    )
    if order.status == BankPaymentOrder.Status.APPROVED or order.payment.status == Payment.Status.CONFIRMED:
        confirm_personal_booking_payment_reservation_for_order(
            club_id=club_id,
            order_id=order.id,
            actor_user_id=None,
        )
        return (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order", "payment", "subscription")
            .get(id=reservation.id)
        )
    if order.status in {
        BankPaymentOrder.Status.MANUAL_REVIEW,
        BankPaymentOrder.Status.FAILED,
        BankPaymentOrder.Status.EXPIRED,
        BankPaymentOrder.Status.CANCELLED,
    }:
        if order.status == BankPaymentOrder.Status.MANUAL_REVIEW:
            reservation_status = PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
        elif order.status == BankPaymentOrder.Status.EXPIRED:
            reservation_status = PersonalBookingPaymentReservation.Status.EXPIRED
        else:
            reservation_status = PersonalBookingPaymentReservation.Status.CANCELLED
        close_personal_booking_payment_reservation_for_order(
            club_id=club_id,
            order_id=order.id,
            status=reservation_status,
            reason=order.last_error_message,
            code=order.last_error_code,
        )
        return (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order", "payment", "subscription")
            .get(id=reservation.id)
        )
    return reservation


def _ensure_personal_reservation_bank_order(
    *,
    club_id: int,
    reservation_id: int,
    source: str,
    created_by_id: int,
    buyer_email: str | None,
    buyer_phone: str | None,
    command_idempotency_key: str | None = None,
    _locked_pre_create_validator=None,
) -> PersonalBookingPaymentReservation:
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_related("bank_payment_order")
        .get(id=reservation_id)
    )
    if reservation.bank_payment_order_id is not None:
        if reservation.bank_payment_order.source != source:
            raise BusinessLogicError(
                "Personal payment reservation belongs to another payment source",
                code="personal_payment_reservation_source_conflict",
            )
        return _reconcile_attached_personal_reservation(
            club_id=club_id,
            reservation=reservation,
        )

    order = _live_bank_order_for_personal_reservation(
        club_id=club_id,
        reservation_id=reservation.id,
    )
    if order is not None and order.source != source:
        raise BusinessLogicError(
            "Personal payment reservation belongs to another payment source",
            code="personal_payment_reservation_source_conflict",
        )
    if order is None:
        try:
            from apps.billing.services import create_bank_payment_order

            order = create_bank_payment_order(
                club_id=club_id,
                student_id=reservation.student_id,
                tariff_id=reservation.tariff_id,
                source=source,
                created_by_id=created_by_id,
                seller_trainer_id=reservation.trainer_id,
                package_owner_trainer_id=reservation.trainer_id,
                buyer_email=buyer_email,
                buyer_phone=buyer_phone,
                allow_new_self_service_subscription=True,
                allow_reuse=False,
                personal_booking_reservation_id=reservation.id,
                expires_at_cap=reservation.expires_at,
                command_idempotency_key=command_idempotency_key,
                _locked_pre_reuse_validator=_locked_pre_create_validator,
            )
        except Exception as exc:
            order = _live_bank_order_for_personal_reservation(
                club_id=club_id,
                reservation_id=reservation.id,
            )
            if order is None:
                with transaction.atomic():
                    from apps.attendance.services.personal_locking import lock_personal_booking_reservation_scope

                    lock_personal_booking_reservation_scope(
                        club_id=club_id,
                        reservation_id=reservation.id,
                    )
                    locked = (
                        PersonalBookingPaymentReservation.objects.for_club(club_id)
                        .select_for_update(of=("self",))
                        .get(id=reservation.id)
                    )
                    if locked.bank_payment_order_id is not None:
                        return locked
                    locked.status = PersonalBookingPaymentReservation.Status.CANCELLED
                    locked.last_error_code = getattr(
                        exc,
                        "code",
                        "personal_payment_order_create_failed",
                    )
                    locked.last_error_message = str(exc)
                    locked.save(
                        update_fields=[
                            "status",
                            "last_error_code",
                            "last_error_message",
                            "updated_at",
                        ]
                    )
                    _release_personal_payment_availability_slot(
                        club_id=club_id,
                        slot_id=locked.availability_slot_id,
                    )
                raise
            if order.source != source:
                raise BusinessLogicError(
                    "Personal payment reservation belongs to another payment source",
                    code="personal_payment_reservation_source_conflict",
                ) from exc

    attached = _attach_personal_reservation_bank_order(
        club_id=club_id,
        reservation_id=reservation.id,
        order=order,
    )
    return _reconcile_attached_personal_reservation(
        club_id=club_id,
        reservation=attached,
    )


def create_personal_booking_payment_reservation(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    tariff_id: int | None,
    availability_slot_id: int | None = None,
    created_by_id: int,
    source: str,
    offer_digest: str | None = None,
    discount_id: int | None = None,
    idempotency_key: str | None = None,
    buyer_email: str | None = None,
    buyer_phone: str | None = None,
    command_idempotency_key: str | None = None,
    _locked_pre_create_validator=None,
) -> PersonalBookingPaymentReservation:
    club = Club.objects.only("id", "timezone").filter(id=club_id).first()
    if club is not None:
        starts_at = _as_club_aware(starts_at, club=club)
        ends_at = _as_club_aware(ends_at, club=club)
    else:
        if timezone.is_naive(starts_at):
            starts_at = timezone.make_aware(starts_at, timezone.get_current_timezone())
        if timezone.is_naive(ends_at):
            ends_at = timezone.make_aware(ends_at, timezone.get_current_timezone())

    if ends_at <= starts_at:
        raise BusinessLogicError(
            "Personal booking end time must be after start time",
            code="invalid_personal_booking_time",
        )
    if starts_at.date() != ends_at.date():
        raise BusinessLogicError(
            "Personal booking must start and end on the same date",
            code="personal_booking_crosses_date",
        )

    now = timezone.now()
    if starts_at <= now:
        raise BusinessLogicError(
            "Personal payment reservation requires a future slot",
            code="personal_payment_reservation_past_slot",
        )

    cleaned_key = (idempotency_key or "").strip()
    existing_complete_terms = bool(
        cleaned_key
        and complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
                reservation__idempotency_key=cleaned_key,
            )
        ).exists()
    )
    # A complete snapshot is a lifecycle authority, not a presentation flag.
    unified_personal_policy = is_unified_client_journey_enabled(club=club_id) or existing_complete_terms
    if unified_personal_policy and not cleaned_key:
        raise BusinessLogicError(
            "A stable idempotency key is required for a unified personal payment reservation",
            code="idempotency_key_required",
        )
    if unified_personal_policy and cleaned_key:
        existing_replay = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order")
            .filter(idempotency_key=cleaned_key)
            .first()
        )
        if existing_replay is not None:
            _assert_personal_payment_reservation_replay_matches(
                reservation=existing_replay,
                student_id=student_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=None,
                starts_at=starts_at,
                ends_at=ends_at,
                availability_slot_id=availability_slot_id,
                ignore_tariff=True,
            )
            if existing_replay.status in {
                PersonalBookingPaymentReservation.Status.CANCELLED,
                PersonalBookingPaymentReservation.Status.EXPIRED,
            }:
                # A same-key replay is a read of the original terminal attempt,
                # never permission to silently mint a replacement link.
                return existing_replay
            return _ensure_personal_reservation_bank_order(
                club_id=club_id,
                reservation_id=existing_replay.id,
                source=source,
                created_by_id=created_by_id,
                buyer_email=buyer_email,
                buyer_phone=buyer_phone,
                command_idempotency_key=command_idempotency_key,
            )
    if unified_personal_policy:
        if not (offer_digest or "").strip():
            raise BusinessLogicError(
                "A displayed personal offer is required",
                code="personal_offer_digest_required",
            )
        # This preview only makes legacy replay lookup deterministic.  The
        # locked re-resolution below remains the acceptance authority.
        tariff_id = resolve_personal_booking_offer(
            club_id=club_id,
            trainer_id=trainer_id,
            training_type_id=training_type_id,
            location_id=location_id,
            discount_id=discount_id,
        ).tariff.id
    if tariff_id is None:
        raise BusinessLogicError(
            "A tariff is required for the compatibility reservation path",
            code="personal_payment_tariff_required",
        )

    existing = None
    if cleaned_key:
        existing = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order")
            .filter(idempotency_key=cleaned_key)
            .first()
        )
        if existing is not None:
            _assert_personal_payment_reservation_replay_matches(
                reservation=existing,
                student_id=student_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=tariff_id,
                starts_at=starts_at,
                ends_at=ends_at,
                availability_slot_id=availability_slot_id,
            )
    if existing is None:
        def find_reusable_reservation():
            return (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_related("bank_payment_order")
                .filter(
                    student_id=student_id,
                    trainer_id=trainer_id,
                    location_id=location_id,
                    training_type_id=training_type_id,
                    tariff_id=tariff_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status__in=[
                        PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                    ],
                    expires_at__gt=now,
                )
                .order_by("-created_at", "-id")
                .first()
            )

        if _locked_pre_create_validator is None:
            existing = find_reusable_reservation()
        else:
            # A same-slot reservation under a distinct key is a new command,
            # not K1 replay.  Arbitrate it under Club -> ClubSettings before
            # it may reuse or attach to the existing payment family.
            with transaction.atomic():
                Club.objects.select_for_update(of=("self",)).get(id=club_id)
                _locked_pre_create_validator()
                existing = find_reusable_reservation()
    if existing is not None:
        if existing.status in {
            PersonalBookingPaymentReservation.Status.CANCELLED,
            PersonalBookingPaymentReservation.Status.EXPIRED,
        }:
            return existing
        return _ensure_personal_reservation_bank_order(
            club_id=club_id,
            reservation_id=existing.id,
            source=source,
            created_by_id=created_by_id,
            buyer_email=buyer_email,
            buyer_phone=buyer_phone,
            command_idempotency_key=command_idempotency_key,
            _locked_pre_create_validator=_locked_pre_create_validator,
        )

    with transaction.atomic():
        if cleaned_key or _locked_pre_create_validator is not None:
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
        # K1 may have committed after the optimistic lookup and while this
        # retry waited for Club.  Resolve that accepted reservation before a
        # mutable protocol check.
        if cleaned_key:
            locked_replay = (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related(
                    "availability_slot",
                    "bank_payment_order",
                    "payment",
                    "subscription",
                )
                .filter(idempotency_key=cleaned_key)
                .first()
            )
            if locked_replay is not None:
                _assert_personal_payment_reservation_replay_matches(
                    reservation=locked_replay,
                    student_id=student_id,
                    trainer_id=trainer_id,
                    location_id=location_id,
                    training_type_id=training_type_id,
                    tariff_id=tariff_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    availability_slot_id=availability_slot_id,
                )
                if locked_replay.status in {
                    PersonalBookingPaymentReservation.Status.CANCELLED,
                    PersonalBookingPaymentReservation.Status.EXPIRED,
                } or locked_replay.bank_payment_order_id is not None:
                    return locked_replay
                raise BusinessLogicError(
                    "Personal payment reservation attachment is in progress; retry the same request",
                    code="personal_payment_reservation_attachment_retry",
                )
        if _locked_pre_create_validator is not None:
            _locked_pre_create_validator()
        offer = None
        if unified_personal_policy:
            # Catalog precedes trainer/student/slot locks under D12.
            offer = resolve_personal_booking_offer(
                club_id=club_id,
                trainer_id=trainer_id,
                training_type_id=training_type_id,
                location_id=location_id,
                discount_id=discount_id,
                lock=True,
            )
            tariff_id = offer.tariff.id
        # Keep the same contention boundary as direct and drop-in bookings.
        # Reservation replays also take this lock so they cannot invert the
        # order while another operation holds a reservation row.
        trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=trainer_id, is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError(
                "Trainer does not belong to this club",
                code="trainer_club_mismatch",
            )
        student = (
            Student.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=student_id, deleted_at__isnull=True)
            .first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )
        staff_can_start_online_booking = source in {
            BankPaymentOrder.Source.TRAINER,
            BankPaymentOrder.Source.ADMIN,
            BankPaymentOrder.Source.OWNER,
        }
        staff_eligible_statuses = {
            Student.Status.LEAD,
            Student.Status.TRIAL,
            Student.Status.ACTIVE,
            Student.Status.AT_RISK,
            Student.Status.CHURNED,
        }
        if student.status != Student.Status.ACTIVE and not (
            staff_can_start_online_booking and student.status in staff_eligible_statuses
        ):
            raise BusinessLogicError(
                "Student is not eligible for personal booking",
                code="student_ineligible",
            )
        if cleaned_key:
            existing_by_key = (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("availability_slot", "bank_payment_order", "payment", "subscription")
                .filter(idempotency_key=cleaned_key)
                .first()
            )
            if existing_by_key is not None:
                if (
                    existing_by_key.student_id != student_id
                    or existing_by_key.trainer_id != trainer_id
                    or existing_by_key.location_id != location_id
                    or existing_by_key.training_type_id != training_type_id
                    or existing_by_key.tariff_id != tariff_id
                    or existing_by_key.starts_at != starts_at
                    or existing_by_key.ends_at != ends_at
                    or existing_by_key.availability_slot_id != availability_slot_id
                ):
                    raise BusinessLogicError(
                        "Idempotency key is already used for another personal payment reservation",
                        code="personal_payment_reservation_idempotency_conflict",
                    )
                if existing_by_key.bank_payment_order_id is not None:
                    return existing_by_key
                raise BusinessLogicError(
                    "Personal payment reservation attachment is in progress; retry the same request",
                    code="personal_payment_reservation_attachment_retry",
                )

        existing_same_slot = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("availability_slot", "bank_payment_order", "payment", "subscription")
            .filter(
                student_id=student_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=tariff_id,
                starts_at=starts_at,
                ends_at=ends_at,
                status__in=[
                    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ],
                expires_at__gt=now,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if existing_same_slot is not None:
            if existing_same_slot.bank_payment_order_id is not None:
                return existing_same_slot
            raise BusinessLogicError(
                "Personal payment reservation attachment is in progress; retry the same request",
                code="personal_payment_reservation_attachment_retry",
            )
        existing_same_tariff = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                student_id=student_id,
                tariff_id=tariff_id,
                status__in=[
                    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ],
            )
            .filter(
                models.Q(status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW) | models.Q(expires_at__gt=now)
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if existing_same_tariff is not None:
            raise BusinessLogicError(
                "Student already has a pending personal payment reservation",
                code="personal_payment_reservation_pending_exists",
            )

        location = Location.objects.filter(id=location_id, club_id=club_id).first()
        if location is None:
            raise BusinessLogicError(
                "Location does not belong to this club",
                code="location_club_mismatch",
            )
        if (
            not TrainerLocation.objects.for_club(club_id)
            .filter(
                trainer=trainer,
                location=location,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Trainer is not assigned to this location",
                code="trainer_location_required",
            )

        training_type = (
            TrainingType.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=training_type_id, is_active=True)
            .first()
        )
        if training_type is None:
            raise BusinessLogicError(
                "Training type does not belong to this club",
                code="training_type_club_mismatch",
            )
        if unified_personal_policy and training_type.kind != TrainingType.Kind.PERSONAL:
            raise BusinessLogicError(
                "Unified personal payment reservation requires a personal training type",
                code="personal_payment_mini_group_forbidden",
            )
        if not unified_personal_policy and training_type.kind not in {
            TrainingType.Kind.PERSONAL,
            TrainingType.Kind.MINI_GROUP,
        }:
            raise BusinessLogicError(
                "Personal booking requires a personal or mini-group training type",
                code="personal_booking_requires_personal_type",
            )
        tariff = (
            Tariff.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("training_type", "location")
            .filter(id=tariff_id, is_active=True)
            .first()
        )
        if tariff is None:
            raise BusinessLogicError(
                "Tariff does not belong to this club",
                code="tariff_club_mismatch",
            )
        if tariff.training_type_id != training_type_id:
            raise BusinessLogicError(
                "Tariff is not available for this personal training type",
                code="personal_payment_tariff_training_type_mismatch",
            )
        if tariff.scope == Tariff.Scope.LOCATION and tariff.location_id != location_id:
            raise BusinessLogicError(
                "Tariff is not available for this location",
                code="personal_payment_tariff_location_mismatch",
            )
        if (
            not TrainerRate.objects.for_club(club_id)
            .filter(
                trainer=trainer,
                location=location,
                training_type=training_type,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Trainer rate is required for this personal booking",
                code="trainer_rate_required",
            )

        if _matching_personal_subscription_exists(
            club_id=club_id,
            student_id=student_id,
            location_id=location_id,
            training_type_id=training_type_id,
            target_date=starts_at.date(),
            starts_at=starts_at,
        ):
            raise BusinessLogicError(
                "Student already has a usable personal subscription",
                code="personal_subscription_already_available",
            )
        if _personal_booking_overlap_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            target_date=starts_at.date(),
            starts_at=starts_at,
            ends_at=ends_at,
        ):
            raise BusinessLogicError(
                "Trainer already has a schedule in this time slot",
                code="personal_booking_slot_conflict",
            )
        _lock_overlapping_personal_availability_slots(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        if _active_personal_payment_reservation_overlap_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=starts_at,
            ends_at=ends_at,
            now=now,
        ):
            raise BusinessLogicError(
                "Trainer already has a pending personal payment reservation in this time slot",
                code="personal_payment_reservation_slot_conflict",
            )

        availability_slot = _get_personal_payment_availability_slot(
            club_id=club_id,
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
            availability_slot_id=availability_slot_id,
        )
        if unified_personal_policy:
            shown_offer = (
                personal_offer_payload(slot=availability_slot, offer=offer)
                if availability_slot is not None
                else direct_personal_offer_payload(
                    offer=offer,
                    trainer_id=trainer.id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    location_id=location.id,
                    training_type_id=training_type.id,
                )
            )
            if offer_digest != shown_offer["offer_digest"]:
                error = BusinessLogicError(
                    "The personal offer changed; refresh the slot before payment",
                    code="personal_offer_changed",
                )
                error.safe_payload = {"current_offer": shown_offer}
                raise error
        reservation = PersonalBookingPaymentReservation(
            club_id=club_id,
            student=student,
            trainer=trainer,
            location=location,
            training_type=training_type,
            tariff=tariff,
            availability_slot=availability_slot,
            starts_at=starts_at,
            ends_at=ends_at,
            expires_at=_personal_booking_payment_hold_expires_at(starts_at=starts_at, now=now),
            idempotency_key=cleaned_key,
            created_by_id=created_by_id,
        )
        try:
            reservation.full_clean()
        except ValidationError as exc:
            _raise_validation_error(exc)
        reservation.save()
        if unified_personal_policy:
            create_complete_personal_terms_snapshot(offer=offer, reservation=reservation)
        else:
            create_legacy_partial_personal_terms_snapshot(
                tariff=tariff,
                reservation=reservation,
            )
        if availability_slot is not None:
            availability_slot.status = PersonalAvailabilitySlot.Status.HELD
            availability_slot.save(update_fields=["status", "updated_at"])

    return _ensure_personal_reservation_bank_order(
        club_id=club_id,
        reservation_id=reservation.id,
        source=source,
        created_by_id=created_by_id,
        buyer_email=buyer_email,
        buyer_phone=buyer_phone,
        command_idempotency_key=command_idempotency_key,
    )


def _payment_reservation_booking_origin(reservation: PersonalBookingPaymentReservation) -> str:
    source = reservation.bank_payment_order.source if reservation.bank_payment_order_id else ""
    if source == BankPaymentOrder.Source.STUDENT:
        return ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    if source == BankPaymentOrder.Source.PARENT:
        return ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
    return ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION


def close_personal_booking_payment_reservation_for_order(
    *,
    club_id: int,
    order_id: int,
    status: str,
    reason: str = "",
    code: str = "",
) -> PersonalBookingPaymentReservation | None:
    if status not in {
        PersonalBookingPaymentReservation.Status.CANCELLED,
        PersonalBookingPaymentReservation.Status.EXPIRED,
        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
    }:
        raise BusinessLogicError(
            "Invalid personal payment reservation close status",
            code="invalid_personal_payment_reservation_status",
        )
    with transaction.atomic():
        from apps.attendance.services.personal_locking import (
            lock_complete_personal_scopes,
            lock_personal_booking_reservation_scope,
        )

        reservation_id = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(bank_payment_order_id=order_id)
            .values_list("id", flat=True)
            .first()
        )
        if reservation_id is not None:
            lock_personal_booking_reservation_scope(
                club_id=club_id,
                reservation_id=reservation_id,
            )
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        reservation = (
            next(iter(ordered_scope.reservations_by_id.values()), None)
            if ordered_scope is not None
            else PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("availability_slot")
            .filter(bank_payment_order_id=order_id)
            .first()
        )
        if reservation is None:
            return None
        if reservation.status == PersonalBookingPaymentReservation.Status.BOOKED:
            return reservation
        if status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW:
            if reservation.status != PersonalBookingPaymentReservation.Status.PENDING_PAYMENT:
                return reservation
        elif reservation.status not in {
            PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
        }:
            return reservation
        reservation.status = status
        if code:
            reservation.last_error_code = code
        if reason:
            reservation.last_error_message = reason
        reservation.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
        if status in {
            PersonalBookingPaymentReservation.Status.CANCELLED,
            PersonalBookingPaymentReservation.Status.EXPIRED,
        }:
            _release_personal_payment_availability_slot(
                club_id=club_id,
                slot_id=reservation.availability_slot_id,
            )
        return reservation


def confirm_personal_booking_payment_reservation_for_order(
    *,
    club_id: int,
    order_id: int,
    actor_user_id: int | None,
) -> PersonalBookingPaymentReservation | None:
    try:
        with transaction.atomic():
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
            if ordered_scope is not None:
                reservation = next(iter(ordered_scope.reservations_by_id.values()), None)
            else:
                reservation = (
                    PersonalBookingPaymentReservation.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .select_related("availability_slot", "bank_payment_order", "payment", "subscription")
                    .filter(bank_payment_order_id=order_id)
                    .first()
                )
            if reservation is None:
                return None
            if reservation.status == PersonalBookingPaymentReservation.Status.BOOKED:
                return reservation
            confirmable_statuses = {
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            }
            if reservation.status not in confirmable_statuses:
                raise BusinessLogicError(
                    "Personal payment reservation is not pending",
                    code="personal_payment_reservation_not_pending",
                )
            if reservation.subscription_id is None:
                raise BusinessLogicError(
                    "Personal payment reservation has no subscription",
                    code="personal_payment_reservation_subscription_required",
                )
            target_date = timezone.localtime(
                reservation.starts_at,
                club_zoneinfo(reservation.club),
            ).date()
            entitlement = get_personal_booking_entitlement(
                club_id=club_id,
                student_id=reservation.student_id,
                location_id=reservation.location_id,
                training_type_id=reservation.training_type_id,
                target_date=target_date,
                lock=True,
                subscription_id=reservation.subscription_id,
                starts_at=reservation.starts_at,
            )
            if entitlement is None:
                exhausted_entitlement = get_personal_booking_entitlement(
                    club_id=club_id,
                    student_id=reservation.student_id,
                    location_id=reservation.location_id,
                    training_type_id=reservation.training_type_id,
                    target_date=target_date,
                    lock=True,
                    subscription_id=reservation.subscription_id,
                    starts_at=None,
                )
                if exhausted_entitlement is not None:
                    _assert_self_service_future_entitlement_capacity(
                        subscription=exhausted_entitlement[0],
                        component=exhausted_entitlement[1],
                        starts_at=reservation.starts_at,
                    )
                raise BusinessLogicError(
                    "Personal payment reservation subscription is not available.",
                    code="personal_payment_reservation_subscription_required",
                )
            selected_subscription, selected_component = entitlement
            _assert_self_service_future_entitlement_capacity(
                subscription=selected_subscription,
                component=selected_component,
                starts_at=reservation.starts_at,
            )
            availability_slot = None
            if reservation.availability_slot_id is not None:
                availability_slot = (
                    PersonalAvailabilitySlot.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(id=reservation.availability_slot_id)
                    .first()
                )
                if availability_slot is None:
                    raise BusinessLogicError(
                        "Personal availability slot not found",
                        code="personal_availability_slot_not_found",
                    )
                if availability_slot.status not in {
                    PersonalAvailabilitySlot.Status.HELD,
                    PersonalAvailabilitySlot.Status.PUBLISHED,
                }:
                    raise BusinessLogicError(
                        "Personal availability slot is not available",
                        code="personal_availability_slot_unavailable",
                    )
            booking = book_personal_session(
                club_id=club_id,
                student_id=reservation.student_id,
                trainer_id=reservation.trainer_id,
                starts_at=reservation.starts_at,
                ends_at=reservation.ends_at,
                location_id=reservation.location_id,
                training_type_id=reservation.training_type_id,
                subscription_id=reservation.subscription_id,
                subscription_component_id=(selected_component.id if selected_component is not None else None),
                actor_user_id=actor_user_id,
                idempotency_key=reservation.idempotency_key or f"personal-payment-reservation-{reservation.id}",
                origin=_payment_reservation_booking_origin(reservation),
                availability_slot_id=reservation.availability_slot_id,
                payment_reservation_id=reservation.id,
                accepted_complete_terms=complete_personal_terms_queryset(
                    PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
                        reservation_id=reservation.id,
                    )
                ).exists(),
            )
            if availability_slot is not None:
                availability_slot.status = PersonalAvailabilitySlot.Status.BOOKED
                availability_slot.booked_enrollment = booking.enrollment
                availability_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
            reservation.schedule = booking.schedule
            reservation.enrollment = booking.enrollment
            reservation.status = PersonalBookingPaymentReservation.Status.BOOKED
            reservation.last_error_code = ""
            reservation.last_error_message = ""
            reservation.save(
                update_fields=[
                    "schedule",
                    "enrollment",
                    "status",
                    "last_error_code",
                    "last_error_message",
                    "updated_at",
                ]
            )
            return reservation
    except BusinessLogicError as exc:
        with transaction.atomic():
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
            reservation = (
                next(iter(ordered_scope.reservations_by_id.values()), None)
                if ordered_scope is not None
                else PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(bank_payment_order_id=order_id)
                .first()
            )
            if reservation is not None and reservation.status in {
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            }:
                reservation.status = PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
                reservation.last_error_code = exc.code
                reservation.last_error_message = exc.message
                reservation.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
        raise


def _book_personal_session_locked(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    subscription_id: int | None,
    subscription_component_id: int | None = None,
    actor_user_id: int | None,
    idempotency_key: str | None,
    origin: str,
    availability_slot_id: int | None,
    payment_reservation_id: int | None = None,
    accepted_complete_terms: bool = False,
    enforce_entitlement_capacity: bool = False,
) -> PersonalSessionBooking:
    if ends_at <= starts_at:
        raise BusinessLogicError(
            "Personal booking end time must be after start time",
            code="invalid_personal_booking_time",
        )
    if starts_at.date() != ends_at.date():
        raise BusinessLogicError(
            "Personal booking must start and end on the same date",
            code="personal_booking_crosses_date",
        )

    with transaction.atomic():
        # All mutations that can occupy a trainer's time use this order:
        # trainer -> student -> schedule/availability/reservation rows.
        trainer = Trainer.objects.for_club(club_id).select_for_update().filter(id=trainer_id, is_active=True).first()
        if trainer is None:
            raise BusinessLogicError(
                "Trainer does not belong to this club",
                code="trainer_club_mismatch",
            )
        student = (
            Student.objects.for_club(club_id).select_for_update().filter(id=student_id, deleted_at__isnull=True).first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )
        if student.status != Student.Status.ACTIVE:
            raise BusinessLogicError(
                "Student is not eligible for personal booking",
                code="student_ineligible",
            )
        if not Location.objects.filter(id=location_id, club_id=club_id).exists():
            raise BusinessLogicError(
                "Location does not belong to this club",
                code="location_club_mismatch",
            )
        if (
            not TrainerLocation.objects.for_club(club_id)
            .filter(
                trainer=trainer,
                location_id=location_id,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Trainer is not assigned to this location",
                code="trainer_location_required",
            )

        training_type = None
        if not accepted_complete_terms:
            training_type = (
                TrainingType.objects.for_club(club_id)
                .select_for_update()
                .filter(id=training_type_id, is_active=True)
                .first()
            )
            if training_type is None:
                raise BusinessLogicError(
                    "Training type does not belong to this club",
                    code="training_type_club_mismatch",
                )
            if training_type.kind not in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}:
                raise BusinessLogicError(
                    "Personal booking requires a personal or mini-group training type",
                    code="personal_booking_requires_personal_type",
                )
            if (
                not TrainerRate.objects.for_club(club_id)
                .filter(
                    trainer=trainer,
                    location_id=location_id,
                    training_type=training_type,
                )
                .exists()
            ):
                raise BusinessLogicError(
                    "Trainer rate is required for this personal booking",
                    code="trainer_rate_required",
                )

        if subscription_id is None:
            raise BusinessLogicError(
                "Subscription is required for personal booking",
                code="subscription_required_for_personal_booking",
            )

        target_date = starts_at.date()
        subscription = (
            Subscription.objects.for_club(club_id)
            .filter(
                id=subscription_id,
                student_id=student.id,
                status=Subscription.Status.ACTIVE,
                deleted_at__isnull=True,
            )
            .first()
            if accepted_complete_terms
            else None
        )
        entitlement = get_personal_booking_entitlement(
            club_id=club_id,
            student_id=student.id,
            location_id=location_id,
            training_type_id=training_type_id,
            target_date=target_date,
            subscription_id=subscription_id,
            starts_at=starts_at if enforce_entitlement_capacity else None,
        )
        selected_component = None
        if entitlement is not None:
            subscription, selected_component = entitlement
            if enforce_entitlement_capacity:
                assert_personal_entitlement_booking_capacity(
                    subscription=subscription,
                    component=selected_component,
                    starts_at=starts_at,
                )
            subscription_component_id = selected_component.id if selected_component is not None else None
        elif enforce_entitlement_capacity:
            exhausted_entitlement = get_personal_booking_entitlement(
                club_id=club_id,
                student_id=student.id,
                location_id=location_id,
                training_type_id=training_type_id,
                target_date=target_date,
                subscription_id=subscription_id,
                starts_at=None,
            )
            if exhausted_entitlement is not None:
                assert_personal_entitlement_booking_capacity(
                    subscription=exhausted_entitlement[0],
                    component=exhausted_entitlement[1],
                    starts_at=starts_at,
                )
            subscription = None
        if subscription is None:
            raise BusinessLogicError(
                "Subscription is not available for this personal booking",
                code="subscription_not_available",
            )

        existing = _get_existing_personal_booking(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        if existing is not None:
            return PersonalSessionBooking(schedule=existing.schedule, enrollment=existing, created=False)
        if _personal_booking_overlap_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            target_date=target_date,
            starts_at=starts_at,
            ends_at=ends_at,
        ):
            raise BusinessLogicError(
                "Trainer already has a schedule in this time slot",
                code="personal_booking_slot_conflict",
            )
        _lock_overlapping_personal_availability_slots(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        if _active_personal_payment_reservation_overlap_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=starts_at,
            ends_at=ends_at,
            now=timezone.now(),
            exclude_id=payment_reservation_id,
        ):
            raise BusinessLogicError(
                "Trainer already has a pending personal payment reservation in this time slot",
                code="personal_payment_reservation_slot_conflict",
            )

        direct_availability_slot = None
        if availability_slot_id is None:
            direct_availability_slot = _get_exact_personal_availability_slot_for_direct_booking(
                club_id=club_id,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        resolved_availability_slot_id = availability_slot_id or (
            direct_availability_slot.id if direct_availability_slot is not None else None
        )

        schedule = Schedule.objects.create(
            club_id=club_id,
            day_of_week=target_date.weekday(),
            start_time=_booking_time(starts_at),
            end_time=_booking_time(ends_at),
            group_name=_personal_session_group_name(student),
            trainer=trainer,
            location_id=location_id,
            training_type_id=training_type_id,
            one_time_date=target_date,
        )
        enrollment = ScheduleEnrollment(
            club_id=club_id,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        )
        enrollment = _save_enrollment(enrollment)
        _record_personal_booking_event(
            club_id=club_id,
            enrollment=enrollment,
            target_date=target_date,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            subscription_id=subscription_id,
            subscription_component_id=subscription_component_id,
            origin=origin,
            availability_slot_id=resolved_availability_slot_id,
        )
        if direct_availability_slot is not None:
            direct_availability_slot.status = PersonalAvailabilitySlot.Status.BOOKED
            direct_availability_slot.booked_enrollment = enrollment
            direct_availability_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
        return PersonalSessionBooking(
            schedule=schedule,
            enrollment=enrollment,
            created=True,
            availability_slot_id=resolved_availability_slot_id,
        )


def _reopen_personal_availability_slot_for_cancelled_booking(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
) -> None:
    slots = list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            booked_enrollment=enrollment,
            status=PersonalAvailabilitySlot.Status.BOOKED,
        )
    )
    for slot in slots:
        slot.status = PersonalAvailabilitySlot.Status.PUBLISHED
        slot.booked_enrollment = None
        slot.save(update_fields=["status", "booked_enrollment", "updated_at"])


def _cancel_dated_booking(
    *,
    club_id: int,
    enrollment_id: int,
    booking_created_from: set[str],
    cancellation_event_type: str,
    actor_user_id: int | None,
    origin: str,
    reason: str = "",
    deactivate_schedule: bool = False,
) -> BookingCancellation:
    if origin not in ScheduleBookingEvent.Origin.values:
        raise BusinessLogicError(
            "Invalid booking cancellation origin",
            code="invalid_booking_cancellation_origin",
        )

    cleaned_reason = reason.strip()
    confirmed_reservation_id = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(
            enrollment_id=enrollment_id,
            status=PersonalBookingPaymentReservation.Status.BOOKED,
        )
        .values_list("id", flat=True)
        .first()
    )
    with transaction.atomic():
        if cancellation_event_type == ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED:
            from apps.attendance.services.personal_locking import lock_personal_booking_enrollment_scope
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            # Entitlement bookings have no payment reservation, but they share
            # exact check-in's Trainer -> Student -> slot/Schedule -> enrollment
            # prefix.  Acquire it before the generic cancellation code ever
            # touches the enrollment row.
            lock_training_group_mutation_scope(club_id=club_id)
            lock_personal_booking_enrollment_scope(
                club_id=club_id,
                enrollment_id=enrollment_id,
            )
        if confirmed_reservation_id is not None:
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            # A confirmed reservation owns a booked slot.  Reopen it only
            # after its complete origin was locked in slot -> reservation ->
            # enrollment order, never by starting at the enrollment row.  The
            # payroll/rollout root leads every financial check-in writer; take
            # it here too before Trainer so later cancellation event writes do
            # not invert Club -> Trainer with a concurrent exact check-in.
            lock_complete_personal_scopes(
                club_id=club_id,
                reservation_ids=[confirmed_reservation_id],
            )
        confirmed_reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                id=confirmed_reservation_id,
                enrollment_id=enrollment_id,
                status=PersonalBookingPaymentReservation.Status.BOOKED,
            )
            .first()
            if confirmed_reservation_id is not None
            else None
        )
        enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("student", "schedule", "schedule__trainer")
            .filter(id=enrollment_id)
            .first()
        )
        if enrollment is None:
            raise BusinessLogicError(
                "Booking not found",
                code="booking_not_found",
            )
        if enrollment.created_from not in booking_created_from:
            raise BusinessLogicError(
                "Booking type does not match this cancellation endpoint",
                code="booking_type_mismatch",
            )
        if enrollment.starts_on is None or enrollment.ends_on is None or enrollment.starts_on != enrollment.ends_on:
            raise BusinessLogicError(
                "Only one-day bookings can be cancelled here",
                code="booking_not_cancellable",
            )

        target_date = enrollment.starts_on
        if enrollment.status == ScheduleEnrollment.Status.CANCELLED:
            return BookingCancellation(enrollment=enrollment, event_created=False)

        _ensure_enrollment_is_open(enrollment)
        club_today = timezone.localtime(timezone.now(), club_zoneinfo(enrollment.schedule.club)).date()
        if target_date < club_today:
            raise BusinessLogicError(
                "Booking cannot be cancelled after the session date",
                code="booking_past_date",
            )

        if (
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=enrollment.student_id,
                schedule_id=enrollment.schedule_id,
                date=target_date,
                deleted_at__isnull=True,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Booking cannot be cancelled after check-in",
                code="booking_already_checked_in",
            )
        if (
            GroupSession.objects.for_club(club_id)
            .filter(
                schedule_id=enrollment.schedule_id,
                date=target_date,
                closed_at__isnull=False,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Booking cannot be cancelled after the session is closed",
                code="booking_session_closed",
            )

        enrollment.status = ScheduleEnrollment.Status.CANCELLED
        enrollment = _save_enrollment(enrollment)
        if deactivate_schedule and enrollment.schedule.is_active:
            enrollment.schedule.is_active = False
            enrollment.schedule.save(update_fields=["is_active"])
        if cancellation_event_type == ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED:
            _reopen_personal_availability_slot_for_cancelled_booking(
                club_id=club_id,
                enrollment=enrollment,
            )
            if confirmed_reservation is not None:
                # The purchase remains settled and its subscription untouched;
                # this is only the terminal disposition of the booked visit.
                # Never create a refund or replacement credit here.
                confirmed_reservation.status = PersonalBookingPaymentReservation.Status.CANCELLED
                confirmed_reservation.last_error_code = "personal_booking_cancelled_after_confirmation"
                confirmed_reservation.last_error_message = cleaned_reason
                confirmed_reservation.save(
                    update_fields=["status", "last_error_code", "last_error_message", "updated_at"]
                )

        _record_booking_cancellation_event(
            club_id=club_id,
            enrollment=enrollment,
            event_type=cancellation_event_type,
            origin=origin,
            actor_user_id=actor_user_id,
            reason=cleaned_reason,
        )
        return BookingCancellation(enrollment=enrollment, event_created=True)


def cancel_guest_booking(
    *,
    club_id: int,
    enrollment_id: int,
    actor_user_id: int | None,
    origin: str,
    reason: str = "",
) -> BookingCancellation:
    return _cancel_dated_booking(
        club_id=club_id,
        enrollment_id=enrollment_id,
        booking_created_from=GUEST_BOOKING_CREATED_FROM,
        cancellation_event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_CANCELLED,
        actor_user_id=actor_user_id,
        origin=origin,
        reason=reason,
    )


def cancel_personal_booking(
    *,
    club_id: int,
    enrollment_id: int,
    actor_user_id: int | None,
    origin: str,
    reason: str = "",
) -> BookingCancellation:
    return _cancel_dated_booking(
        club_id=club_id,
        enrollment_id=enrollment_id,
        booking_created_from={ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING},
        cancellation_event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        actor_user_id=actor_user_id,
        origin=origin,
        reason=reason,
        deactivate_schedule=True,
    )


def _book_guest_group_visit_locked(
    *,
    club_id: int,
    schedule_id: int,
    target_date: date,
    student_id: int,
    origin: str,
    actor_user_id: int | None,
    idempotency_key: str | None,
    created_from: str,
    allow_lead_conversion: bool,
    require_financial_eligibility: bool,
) -> GuestGroupVisitBooking:
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    with transaction.atomic():
        student = (
            Student.objects.for_club(club_id).select_for_update().filter(id=student_id, deleted_at__isnull=True).first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )
        if student.status not in GUEST_BOOKING_STUDENT_STATUSES:
            raise BusinessLogicError(
                "Student is not eligible for guest booking",
                code="student_ineligible",
            )
        if student.status == Student.Status.LEAD and not allow_lead_conversion:
            raise BusinessLogicError(
                "Student is not eligible for guest booking",
                code="student_ineligible",
            )

        schedule = (
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("training_type", "location", "trainer")
            .filter(id=schedule_id)
            .first()
        )
        if schedule is None:
            raise BusinessLogicError(
                "Schedule does not belong to this club",
                code="schedule_club_mismatch",
            )
        _assert_schedule_accepts_generic_enrollment(
            club_id=club_id,
            schedule_id=schedule.id,
        )
        if not schedule.is_active:
            raise BusinessLogicError(
                "Schedule is inactive",
                code="schedule_inactive",
            )
        if schedule.training_type is None or schedule.training_type.kind != TrainingType.Kind.GROUP:
            raise BusinessLogicError(
                "Guest visits are available only for group schedules",
                code="guest_visit_requires_group_schedule",
            )
        if schedule.one_time_date is not None:
            raise BusinessLogicError(
                "Guest visits are available only for recurring group schedules",
                code="guest_visit_requires_recurring_schedule",
            )
        occurrence = next(
            (
                occurrence
                for occurrence in get_schedule_occurrences_for_date(
                    club=schedule.club,
                    target_date=target_date,
                )
                if occurrence.schedule_id == schedule.id
            ),
            None,
        )
        if occurrence is None:
            raise BusinessLogicError(
                "No schedule occurrence exists for this date",
                code="schedule_occurrence_not_found",
            )
        _ensure_self_service_group_booking_starts_in_future(
            occurrence=occurrence,
            club=schedule.club,
            origin=origin,
        )

        if (
            GroupSession.objects.for_club(club_id)
            .filter(
                schedule=schedule,
                date=target_date,
                closed_at__isnull=False,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Group session is already closed",
                code="group_session_closed",
            )

        if student.status == Student.Status.LEAD:
            from apps.leads.models import LeadLifecycleEvent

            old_lead_status = student.lead_status
            trial_date = datetime.combine(occurrence.effective_date, occurrence.effective_start_time)
            if timezone.is_naive(trial_date):
                trial_date = timezone.make_aware(trial_date, club_zoneinfo(schedule.club))
            student.status = Student.Status.TRIAL
            student.lead_status = Student.LeadStatus.TRIAL_BOOKED
            student.trial_date = trial_date
            student.save(update_fields=["status", "lead_status", "trial_date", "updated_at"])
            LeadLifecycleEvent.objects.create(
                club_id=club_id,
                student=student,
                actor_id=actor_user_id,
                event_type=LeadLifecycleEvent.EventType.TRIAL_BOOKED,
                old_lead_status=old_lead_status or "",
                new_lead_status=Student.LeadStatus.TRIAL_BOOKED,
                old_trainer_id=student.assigned_trainer_id,
                new_trainer_id=student.assigned_trainer_id,
                metadata={
                    "origin": origin,
                    "schedule_id": schedule.id,
                    "effective_date": target_date.isoformat(),
                    "created_from": created_from,
                },
            )

        permanent_enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update()
            .select_related("student", "schedule")
            .filter(
                student=student,
                schedule=schedule,
                ends_on__isnull=True,
                status__in=OPEN_ENROLLMENT_STATUSES,
            )
            .filter(models.Q(starts_on__isnull=True) | models.Q(starts_on__lte=target_date))
            .order_by("id")
            .first()
        )
        if permanent_enrollment is not None:
            return GuestGroupVisitBooking(
                enrollment=permanent_enrollment,
                created=False,
                already_member=True,
                origin=origin,
            )

        existing_guest = _get_existing_guest_booking(
            club_id=club_id,
            student_id=student.id,
            schedule_id=schedule.id,
            target_date=target_date,
        )
        if existing_guest is not None:
            return GuestGroupVisitBooking(
                enrollment=existing_guest,
                created=False,
                already_member=False,
                origin=origin,
            )

        if require_financial_eligibility:
            from apps.attendance.selectors import get_guest_booking_financial_eligibility

            financial = get_guest_booking_financial_eligibility(
                club=schedule.club,
                student_id=student.id,
                training_type_id=schedule.training_type_id,
                location_id=schedule.location_id,
                target_date=target_date,
            )
            if financial["financial_status"] == "blocked":
                raise BusinessLogicError(
                    "Drop-in price is required for no-subscription guest booking",
                    code=financial["reason_code"],
                )

        enrollment = ScheduleEnrollment(
            club_id=club_id,
            student=student,
            schedule=schedule,
            status=(
                ScheduleEnrollment.Status.TRIAL
                if student.status == Student.Status.TRIAL
                else ScheduleEnrollment.Status.ACTIVE
            ),
            starts_on=target_date,
            ends_on=target_date,
            created_from=created_from,
        )
        _save_enrollment(enrollment)
        _record_guest_booking_event(
            club_id=club_id,
            enrollment=enrollment,
            target_date=target_date,
            origin=origin,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )
        return GuestGroupVisitBooking(
            enrollment=enrollment,
            created=True,
            already_member=False,
            origin=origin,
        )


def enroll_student_in_schedule(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    status: str = ScheduleEnrollment.Status.ACTIVE,
    starts_on: date | None = None,
    ends_on: date | None = None,
    trial_at: datetime | None = None,
    created_from: str = ScheduleEnrollment.CreatedFrom.MANUAL,
    actor_user_id: int | None = None,
) -> ScheduleEnrollment:
    _validate_open_status(status)
    is_dated_booking = starts_on is not None and ends_on is not None and created_from in DATED_EXPECTED_CREATED_FROM
    schedule_preview = (
        Schedule.objects.for_club(club_id)
        .select_related("training_type")
        .filter(id=schedule_id)
        .only("id", "training_group_id", "one_time_date", "training_type__kind")
        .first()
    )
    if schedule_preview is None:
        raise BusinessLogicError(
            "Schedule does not belong to this club",
            code="schedule_club_mismatch",
        )
    if (
        schedule_preview.training_group_id is not None
        and schedule_preview.one_time_date is None
        and not is_dated_booking
        and created_from in {
            ScheduleEnrollment.CreatedFrom.MANUAL,
            ScheduleEnrollment.CreatedFrom.IMPORT,
        }
    ):
        if starts_on is None:
            raise BusinessLogicError(
                "A mapped group enrollment requires an explicit start date.",
                code="training_group_membership_starts_on_required",
            )
        from apps.attendance.services.training_group_memberships import create_training_group_membership

        membership = create_training_group_membership(
            club_id=club_id,
            student_id=student_id,
            training_group_id=schedule_preview.training_group_id,
            starts_on=starts_on,
            source=(
                TrainingGroupMembership.Source.IMPORT
                if created_from == ScheduleEnrollment.CreatedFrom.IMPORT
                else TrainingGroupMembership.Source.MANUAL
            ),
            status=(
                TrainingGroupMembership.Status.FROZEN
                if status == ScheduleEnrollment.Status.FROZEN
                else TrainingGroupMembership.Status.ACTIVE
            ),
            actor_user_id=actor_user_id,
            rationale="legacy_schedule_enrollment_routed_to_group",
            idempotency_key=(
                f"schedule-enrollment:{club_id}:{student_id}:{schedule_preview.training_group_id}:"
                f"{starts_on.isoformat()}:{created_from}"
            ),
        )
        return (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_related("student", "schedule")
            .get(training_group_membership_id=membership.id, schedule_id=schedule_id)
        )
    with transaction.atomic():
        if (
            schedule_preview.one_time_date is None
            and schedule_preview.training_type.kind == "group"
            and not is_dated_booking
            and created_from
            in {
                ScheduleEnrollment.CreatedFrom.MANUAL,
                ScheduleEnrollment.CreatedFrom.IMPORT,
                ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            }
        ):
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            # Reconciliation must drain the legacy permanent-enrollment seam
            # before it takes selected schedule locks.  Otherwise an unmapped
            # source could be inserted after preview and escape the approved
            # backfill batch.
            lock_training_group_mutation_scope(club_id=club_id)
        student = (
            Student.objects.for_club(club_id).select_for_update().filter(id=student_id, deleted_at__isnull=True).first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )

        schedule = Schedule.objects.for_club(club_id).select_for_update().filter(id=schedule_id).first()
        if schedule is None:
            raise BusinessLogicError(
                "Schedule does not belong to this club",
                code="schedule_club_mismatch",
            )

        _assert_schedule_accepts_generic_enrollment(
            club_id=club_id,
            schedule_id=schedule.id,
        )

        if is_dated_booking:
            enrollment = (
                ScheduleEnrollment.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    student=student,
                    schedule=schedule,
                    starts_on=starts_on,
                    ends_on=ends_on,
                    created_from=created_from,
                    status__in=OPEN_ENROLLMENT_STATUSES,
                )
                .order_by("id")
                .first()
            )
        else:
            enrollment = (
                ScheduleEnrollment.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    student=student,
                    schedule=schedule,
                    ends_on__isnull=True,
                    status__in=OPEN_ENROLLMENT_STATUSES,
                )
                .order_by("id")
                .first()
            )
        if enrollment is None:
            enrollment = ScheduleEnrollment(
                club_id=club_id,
                student=student,
                schedule=schedule,
            )

        enrollment.status = status
        enrollment.starts_on = starts_on
        enrollment.ends_on = ends_on
        enrollment.trial_at = trial_at
        enrollment.created_from = created_from
        _save_enrollment(enrollment)

    return enrollment


def assert_paid_conversion_enrollment_available(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
) -> None:
    """Reject a new payment-owned membership that would overwrite open roster state."""
    schedule = Schedule.objects.for_club(club_id).select_for_update().filter(id=schedule_id).first()
    if schedule is None:
        raise BusinessLogicError(
            "Schedule does not belong to this club",
            code="schedule_club_mismatch",
        )

    permanent_sources = (
        ScheduleEnrollment.CreatedFrom.MANUAL,
        ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        ScheduleEnrollment.CreatedFrom.IMPORT,
    )
    conflicting_enrollment = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update()
        .filter(
            student_id=student_id,
            ends_on__isnull=True,
            status__in=OPEN_ENROLLMENT_STATUSES,
        )
        .filter(
            models.Q(schedule_id=schedule.id)
            | models.Q(
                schedule__training_type_id=schedule.training_type_id,
                status__in=(
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.FROZEN,
                ),
                created_from__in=permanent_sources,
            )
        )
        .order_by("id")
        .first()
    )
    if conflicting_enrollment is not None:
        raise BusinessLogicError(
            "У ученика уже есть открытая постоянная запись этого типа",
            code="manual_operational_admission_enrollment_conflict",
        )


def create_paid_conversion_enrollment(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    starts_on: date,
) -> ScheduleEnrollment:
    """Create payment-owned group admission without adopting an existing enrollment."""
    schedule_preview = (
        Schedule.objects.for_club(club_id)
        .select_related("training_type")
        .filter(id=schedule_id)
        .only("id", "one_time_date", "training_type__kind")
        .first()
    )
    if schedule_preview is None:
        raise BusinessLogicError(
            "Schedule does not belong to this club",
            code="schedule_club_mismatch",
        )
    with transaction.atomic():
        if (
            schedule_preview.one_time_date is None
            and schedule_preview.training_type.kind == "group"
        ):
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            lock_training_group_mutation_scope(club_id=club_id)
        student = (
            Student.objects.for_club(club_id)
            .select_for_update()
            .filter(id=student_id, deleted_at__isnull=True)
            .first()
        )
        if student is None:
            raise BusinessLogicError(
                "Student does not belong to this club",
                code="student_club_mismatch",
            )
        assert_paid_conversion_enrollment_available(
            club_id=club_id,
            student_id=student.id,
            schedule_id=schedule_id,
        )
        enrollment = ScheduleEnrollment(
            club_id=club_id,
            student=student,
            schedule_id=schedule_id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=starts_on,
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        enrollment.full_clean()
        enrollment.save()
        return enrollment


def cancel_schedule_enrollment(
    *,
    club_id: int,
    enrollment_id: int,
    ends_on: date,
    actor_user_id: int | None = None,
) -> ScheduleEnrollment:
    projection = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=enrollment_id)
        .only("id", "training_group_membership_id")
        .first()
    )
    if projection is not None and projection.training_group_membership_id is not None:
        from apps.attendance.services.training_group_memberships import cancel_training_group_membership

        cancel_training_group_membership(
            membership_id=projection.training_group_membership_id,
            club_id=club_id,
            ends_on=ends_on,
            actor_user_id=actor_user_id,
            rationale="legacy_schedule_enrollment_cancel_routed_to_group",
            idempotency_key=f"schedule-enrollment-cancel:{projection.id}:{ends_on.isoformat()}",
        )
        return ScheduleEnrollment.objects.for_club(club_id).get(id=projection.id)
    with transaction.atomic():
        enrollment = _get_enrollment_for_update(
            club_id=club_id,
            enrollment_id=enrollment_id,
        )
        _assert_enrollment_uses_generic_lifecycle(
            club_id=club_id,
            enrollment_id=enrollment.id,
        )
        _ensure_enrollment_is_open(enrollment)
        enrollment.status = ScheduleEnrollment.Status.CANCELLED
        enrollment.ends_on = ends_on
        return _save_enrollment(enrollment)


def transfer_schedule_enrollment(
    *,
    club_id: int,
    enrollment_id: int,
    target_schedule_id: int,
    ends_on: date,
    actor_user_id: int | None = None,
) -> tuple[ScheduleEnrollment, ScheduleEnrollment]:
    projection = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=enrollment_id)
        .only("id", "training_group_membership_id")
        .first()
    )
    if projection is not None and projection.training_group_membership_id is not None:
        target_schedule = Schedule.objects.for_club(club_id).filter(id=target_schedule_id).only(
            "id", "training_group_id"
        ).first()
        if target_schedule is None:
            raise BusinessLogicError(
                "Schedule does not belong to this club",
                code="schedule_club_mismatch",
            )
        if target_schedule.training_group_id is None:
            raise BusinessLogicError(
                "Target schedule requires a canonical training group.",
                code="group_membership_action_required",
            )
        from apps.attendance.services.training_group_memberships import transfer_training_group_membership

        _, target_membership = transfer_training_group_membership(
            membership_id=projection.training_group_membership_id,
            club_id=club_id,
            target_training_group_id=target_schedule.training_group_id,
            ends_on=ends_on,
            actor_user_id=actor_user_id,
            rationale="legacy_schedule_enrollment_transfer_routed_to_group",
            idempotency_key=f"schedule-enrollment-transfer:{projection.id}:{target_schedule_id}:{ends_on.isoformat()}",
        )
        return (
            ScheduleEnrollment.objects.for_club(club_id).get(id=projection.id),
            ScheduleEnrollment.objects.for_club(club_id).get(
                training_group_membership_id=target_membership.id,
                schedule_id=target_schedule_id,
            ),
        )
    with transaction.atomic():
        enrollment = _get_enrollment_for_update(
            club_id=club_id,
            enrollment_id=enrollment_id,
        )
        _assert_enrollment_uses_generic_lifecycle(
            club_id=club_id,
            enrollment_id=enrollment.id,
        )
        _ensure_enrollment_is_open(enrollment)
        if enrollment.schedule_id == target_schedule_id:
            raise BusinessLogicError(
                "Target schedule must differ from current schedule",
                code="same_schedule_transfer",
            )

        target_schedule = Schedule.objects.for_club(club_id).select_for_update().filter(id=target_schedule_id).first()
        if target_schedule is None:
            raise BusinessLogicError(
                "Schedule does not belong to this club",
                code="schedule_club_mismatch",
            )

        enrollment.status = ScheduleEnrollment.Status.TRANSFERRED
        enrollment.ends_on = ends_on
        closed_enrollment = _save_enrollment(enrollment)
        new_enrollment = enroll_student_in_schedule(
            club_id=club_id,
            student_id=enrollment.student_id,
            schedule_id=target_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=ends_on + timedelta(days=1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        return closed_enrollment, new_enrollment


def freeze_schedule_enrollment(
    *,
    club_id: int,
    enrollment_id: int,
    actor_user_id: int | None = None,
) -> ScheduleEnrollment:
    projection = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=enrollment_id)
        .only("id", "training_group_membership_id")
        .first()
    )
    if projection is not None and projection.training_group_membership_id is not None:
        from apps.attendance.services.training_group_memberships import freeze_training_group_membership

        freeze_training_group_membership(
            membership_id=projection.training_group_membership_id,
            club_id=club_id,
            actor_user_id=actor_user_id,
            rationale="legacy_schedule_enrollment_freeze_routed_to_group",
            idempotency_key=f"schedule-enrollment-freeze:{projection.id}",
        )
        return ScheduleEnrollment.objects.for_club(club_id).get(id=projection.id)
    with transaction.atomic():
        enrollment = _get_enrollment_for_update(
            club_id=club_id,
            enrollment_id=enrollment_id,
        )
        _assert_enrollment_uses_generic_lifecycle(
            club_id=club_id,
            enrollment_id=enrollment.id,
        )
        _ensure_enrollment_is_open(enrollment)
        enrollment.status = ScheduleEnrollment.Status.FROZEN
        return _save_enrollment(enrollment)


def unfreeze_schedule_enrollment(
    *,
    club_id: int,
    enrollment_id: int,
    actor_user_id: int | None = None,
) -> ScheduleEnrollment:
    projection = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=enrollment_id)
        .only("id", "training_group_membership_id")
        .first()
    )
    if projection is not None and projection.training_group_membership_id is not None:
        from apps.attendance.services.training_group_memberships import unfreeze_training_group_membership

        unfreeze_training_group_membership(
            membership_id=projection.training_group_membership_id,
            club_id=club_id,
            actor_user_id=actor_user_id,
            rationale="legacy_schedule_enrollment_unfreeze_routed_to_group",
            idempotency_key=f"schedule-enrollment-unfreeze:{projection.id}",
        )
        return ScheduleEnrollment.objects.for_club(club_id).get(id=projection.id)
    with transaction.atomic():
        enrollment = _get_enrollment_for_update(
            club_id=club_id,
            enrollment_id=enrollment_id,
        )
        _assert_enrollment_uses_generic_lifecycle(
            club_id=club_id,
            enrollment_id=enrollment.id,
        )
        _ensure_enrollment_is_open(enrollment)
        enrollment.status = ScheduleEnrollment.Status.ACTIVE
        return _save_enrollment(enrollment)
