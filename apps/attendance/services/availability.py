from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import PersonalAvailabilitySlot, PersonalBookingPaymentReservation, Schedule
from apps.billing.models import TrainingType
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import Club, Location
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer, TrainerLocation

ACTIVE_SLOT_STATUSES = {
    PersonalAvailabilitySlot.Status.PUBLISHED,
    PersonalAvailabilitySlot.Status.HELD,
    PersonalAvailabilitySlot.Status.BOOKED,
    PersonalAvailabilitySlot.Status.BLOCKED,
}


@dataclass(frozen=True)
class PersonalAvailabilitySkippedSlot:
    date: date
    starts_at: datetime
    ends_at: datetime
    reason_code: str


@dataclass(frozen=True)
class PersonalAvailabilityGenerationResult:
    created: list[PersonalAvailabilitySlot]
    skipped: list[PersonalAvailabilitySkippedSlot]


def _club_tz_for_id(club_id: int) -> tzinfo:
    club = Club.objects.only("id", "timezone").get(id=club_id)
    return club_zoneinfo(club)


def _aware_datetime(slot_date: date, slot_time: time, club_tz: tzinfo) -> datetime:
    value = datetime.combine(slot_date, slot_time)
    if timezone.is_naive(value):
        return timezone.make_aware(value, club_tz)
    return value


def _iter_dates(*, date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def _iter_slot_windows(
    *,
    slot_date: date,
    start_time: time,
    end_time: time,
    slot_duration_minutes: int | None,
    buffer_minutes: int,
    club_tz: tzinfo,
):
    day_start = _aware_datetime(slot_date, start_time, club_tz)
    day_end = _aware_datetime(slot_date, end_time, club_tz)
    if slot_duration_minutes is None:
        yield day_start, day_end
        return

    duration = timedelta(minutes=slot_duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)
    starts_at = day_start
    while starts_at + duration <= day_end:
        ends_at = starts_at + duration
        yield starts_at, ends_at
        starts_at = ends_at + buffer


def _validate_personal_availability_resources(
    *,
    club_id: int,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
) -> tuple[Trainer, Location, TrainingType]:
    trainer = Trainer.objects.for_club(club_id).filter(id=trainer_id, is_active=True).first()
    if trainer is None:
        raise BusinessLogicError("Trainer not found", code="trainer_not_found")

    location = Location.objects.filter(id=location_id, club_id=club_id).first()
    if location is None:
        raise BusinessLogicError("Location not found", code="location_not_found")

    if (
        not TrainerLocation.objects.for_club(club_id)
        .filter(
            trainer_id=trainer_id,
            location_id=location_id,
        )
        .exists()
    ):
        raise BusinessLogicError(
            "Trainer is not assigned to this location",
            code="trainer_location_required",
        )

    training_type = (
        TrainingType.objects.for_club(club_id)
        .filter(
            id=training_type_id,
            is_active=True,
            kind__in=[TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP],
        )
        .first()
    )
    if training_type is None:
        raise BusinessLogicError(
            "Training type must be active personal or mini-group",
            code="personal_availability_requires_personal_type",
        )
    return trainer, location, training_type


def _trainer_schedule_overlap_exists(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    club_tz: tzinfo,
) -> bool:
    local_starts_at = timezone.localtime(starts_at, club_tz)
    local_ends_at = timezone.localtime(ends_at, club_tz)
    target_date = local_starts_at.date()
    # The trainer row is locked by the caller before this range is checked.
    # Lock existing schedule rows as well, so publishing cannot race an
    # already-started mutation of a conflicting dated or recurring schedule.
    list(
        Schedule.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            is_active=True,
            start_time__lt=local_ends_at.time(),
            end_time__gt=local_starts_at.time(),
        )
        .filter(Q(one_time_date=target_date) | Q(one_time_date__isnull=True, day_of_week=target_date.weekday()))
        .values_list("id", flat=True)
    )
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    return any(
        occurrence.effective_start_time < local_ends_at.time()
        and occurrence.effective_end_time > local_starts_at.time()
        for occurrence in get_schedule_occurrences_for_date(
            club=club_id,
            target_date=target_date,
            trainer_id=trainer_id,
        )
    )


def _active_slot_overlap_exists(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    exclude_slot_id: int | None = None,
) -> bool:
    qs = (
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=ACTIVE_SLOT_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
    )
    if exclude_slot_id is not None:
        qs = qs.exclude(id=exclude_slot_id)
    return qs.exists()


def _active_reservation_overlap_exists(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
) -> bool:
    now = timezone.now()
    return (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=trainer_id,
            status__in=[
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.BOOKED,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            ],
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .filter(
            Q(status=PersonalBookingPaymentReservation.Status.BOOKED)
            | Q(status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW)
            | Q(expires_at__gt=now)
        )
        .exists()
    )


def _availability_conflict_reason(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    club_tz: tzinfo,
    exclude_slot_id: int | None = None,
) -> str:
    if _trainer_schedule_overlap_exists(
        club_id=club_id,
        trainer_id=trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
        club_tz=club_tz,
    ):
        return "schedule_overlap"
    if _active_slot_overlap_exists(
        club_id=club_id,
        trainer_id=trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
        exclude_slot_id=exclude_slot_id,
    ):
        return "slot_overlap"
    if _active_reservation_overlap_exists(
        club_id=club_id,
        trainer_id=trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
    ):
        return "reservation_overlap"
    return ""


def generate_personal_availability_slots(
    *,
    club_id: int,
    trainer_id: int,
    date_from: date,
    date_to: date,
    weekdays: list[int],
    start_time: time,
    end_time: time,
    location_id: int,
    training_type_id: int,
    slot_duration_minutes: int | None = None,
    buffer_minutes: int = 0,
) -> PersonalAvailabilityGenerationResult:
    if date_to < date_from:
        raise BusinessLogicError("date_to must be after date_from", code="invalid_date_range")
    if (date_to - date_from).days > 62:
        raise BusinessLogicError("Date range is too large", code="availability_range_too_large")
    if end_time <= start_time:
        raise BusinessLogicError("End time must be after start time", code="invalid_time_range")
    if slot_duration_minutes is not None:
        if slot_duration_minutes <= 0:
            raise BusinessLogicError("Slot duration must be positive", code="invalid_slot_duration")
        window_minutes = (
            datetime.combine(date.min, end_time) - datetime.combine(date.min, start_time)
        ).total_seconds() // 60
        if slot_duration_minutes > window_minutes:
            raise BusinessLogicError("Slot duration does not fit the window", code="invalid_slot_duration")
    if buffer_minutes < 0:
        raise BusinessLogicError("Buffer must not be negative", code="invalid_buffer")
    weekday_set = set(weekdays)
    if not weekday_set or any(day < 0 or day > 6 for day in weekday_set):
        raise BusinessLogicError("Weekdays must be in 0..6", code="invalid_weekdays")

    _validate_personal_availability_resources(
        club_id=club_id,
        trainer_id=trainer_id,
        location_id=location_id,
        training_type_id=training_type_id,
    )
    club_tz = _club_tz_for_id(club_id)

    now = timezone.now()
    created: list[PersonalAvailabilitySlot] = []
    skipped: list[PersonalAvailabilitySkippedSlot] = []

    with transaction.atomic():
        # New personal supply must be commercially usable when published.  A
        # mini-group remains on its legacy compatibility path.
        from apps.billing.models import TrainingType
        from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer

        requested_type = TrainingType.objects.for_club(club_id).filter(id=training_type_id).first()
        if (
            requested_type is not None
            and requested_type.kind == TrainingType.Kind.PERSONAL
            and is_unified_client_journey_enabled(club=club_id)
        ):
            resolve_personal_booking_offer(
                club_id=club_id,
                trainer_id=trainer_id,
                training_type_id=training_type_id,
                location_id=location_id,
                lock=True,
            )
        locked_trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=trainer_id, is_active=True)
            .first()
        )
        if locked_trainer is None:
            raise BusinessLogicError("Trainer not found", code="trainer_not_found")
        for slot_date in _iter_dates(date_from=date_from, date_to=date_to):
            if slot_date.weekday() not in weekday_set:
                continue
            for starts_at, ends_at in _iter_slot_windows(
                slot_date=slot_date,
                start_time=start_time,
                end_time=end_time,
                slot_duration_minutes=slot_duration_minutes,
                buffer_minutes=buffer_minutes,
                club_tz=club_tz,
            ):
                if starts_at <= now:
                    skipped.append(PersonalAvailabilitySkippedSlot(slot_date, starts_at, ends_at, "past"))
                    continue
                reason = _availability_conflict_reason(
                    club_id=club_id,
                    trainer_id=trainer_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    club_tz=club_tz,
                )
                if reason:
                    skipped.append(PersonalAvailabilitySkippedSlot(slot_date, starts_at, ends_at, reason))
                    continue
                slot = PersonalAvailabilitySlot(
                    club_id=club_id,
                    trainer_id=trainer_id,
                    location_id=location_id,
                    training_type_id=training_type_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status=PersonalAvailabilitySlot.Status.PUBLISHED,
                )
                slot.full_clean()
                slot.save()
                created.append(slot)

    return PersonalAvailabilityGenerationResult(created=created, skipped=skipped)


def block_personal_availability_slot(
    *,
    club_id: int,
    trainer_id: int,
    slot_id: int,
    reason: str = "",
) -> PersonalAvailabilitySlot:
    with transaction.atomic():
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=slot_id, trainer_id=trainer_id)
            .first()
        )
        if slot is None:
            raise BusinessLogicError(
                "Personal availability slot not found",
                code="personal_availability_slot_not_found",
            )
        if slot.status in {PersonalAvailabilitySlot.Status.HELD, PersonalAvailabilitySlot.Status.BOOKED}:
            raise BusinessLogicError(
                "Personal availability slot is reserved",
                code="personal_availability_slot_reserved",
            )
        if slot.status == PersonalAvailabilitySlot.Status.CANCELLED:
            raise BusinessLogicError(
                "Personal availability slot is cancelled",
                code="personal_availability_slot_cancelled",
            )
        if slot.starts_at <= timezone.now():
            raise BusinessLogicError("Past slot cannot be changed", code="personal_availability_slot_past")
        slot.status = PersonalAvailabilitySlot.Status.BLOCKED
        slot.block_reason = reason.strip()[:255]
        slot.save(update_fields=["status", "block_reason", "updated_at"])
        return slot


def unblock_personal_availability_slot(
    *,
    club_id: int,
    trainer_id: int,
    slot_id: int,
) -> PersonalAvailabilitySlot:
    slot_trainer_id = (
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .filter(id=slot_id)
        .values_list("trainer_id", flat=True)
        .first()
    )
    if slot_trainer_id is None or slot_trainer_id != trainer_id:
        raise BusinessLogicError(
            "Personal availability slot not found",
            code="personal_availability_slot_not_found",
        )
    with transaction.atomic():
        trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=trainer_id, is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError("Trainer not found", code="trainer_not_found")
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=slot_id, trainer_id=trainer_id)
            .first()
        )
        if slot is None:
            raise BusinessLogicError(
                "Personal availability slot not found",
                code="personal_availability_slot_not_found",
            )
        if slot.status != PersonalAvailabilitySlot.Status.BLOCKED:
            raise BusinessLogicError(
                "Only blocked slots can be unblocked",
                code="personal_availability_slot_not_blocked",
            )
        if slot.starts_at <= timezone.now():
            raise BusinessLogicError("Past slot cannot be changed", code="personal_availability_slot_past")
        club_tz = _club_tz_for_id(club_id)
        reason = _availability_conflict_reason(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            club_tz=club_tz,
            exclude_slot_id=slot.id,
        )
        if reason:
            raise BusinessLogicError("Personal availability slot conflicts with another event", code=reason)
        slot.status = PersonalAvailabilitySlot.Status.PUBLISHED
        slot.block_reason = ""
        slot.save(update_fields=["status", "block_reason", "updated_at"])
        return slot


def cancel_personal_availability_slot(
    *,
    club_id: int,
    trainer_id: int,
    slot_id: int,
) -> PersonalAvailabilitySlot:
    with transaction.atomic():
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=slot_id, trainer_id=trainer_id)
            .first()
        )
        if slot is None:
            raise BusinessLogicError(
                "Personal availability slot not found",
                code="personal_availability_slot_not_found",
            )
        if slot.status in {PersonalAvailabilitySlot.Status.HELD, PersonalAvailabilitySlot.Status.BOOKED}:
            raise BusinessLogicError(
                "Personal availability slot is reserved",
                code="personal_availability_slot_reserved",
            )
        if slot.status == PersonalAvailabilitySlot.Status.CANCELLED:
            return slot
        if slot.starts_at <= timezone.now():
            raise BusinessLogicError("Past slot cannot be changed", code="personal_availability_slot_past")
        slot.status = PersonalAvailabilitySlot.Status.CANCELLED
        slot.block_reason = ""
        slot.save(update_fields=["status", "block_reason", "updated_at"])
        return slot
