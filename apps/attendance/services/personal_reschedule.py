"""Dedicated exact-personal-booking reschedule lifecycle.

This is deliberately separate from generic schedule exceptions: a personal
booking has one immutable commercial/entitlement authority and may only move
to another published availability slot with the same trainer, location, and
training type.  The exact schedule/enrollment remains the booking authority;
the availability slot is its current public occupancy projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.services.personal_locking import (
    lock_complete_personal_scopes,
    lock_personal_booking_enrollment_scope,
)
from apps.billing.models import TrainingType
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError

OPEN_EXACT_BOOKING_STATUSES = {
    ScheduleEnrollment.Status.ACTIVE,
    ScheduleEnrollment.Status.TRIAL,
    ScheduleEnrollment.Status.FROZEN,
}
ACTIVE_SLOT_STATUSES = {
    PersonalAvailabilitySlot.Status.PUBLISHED,
    PersonalAvailabilitySlot.Status.HELD,
    PersonalAvailabilitySlot.Status.BOOKED,
    PersonalAvailabilitySlot.Status.BLOCKED,
}
LIVE_RESERVATION_STATUSES = {
    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
    PersonalBookingPaymentReservation.Status.BOOKED,
    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
}


@dataclass(frozen=True)
class PersonalBookingRescheduleResult:
    enrollment: ScheduleEnrollment
    schedule: Schedule
    booking: PersonalDropInBooking | None
    reservation: PersonalBookingPaymentReservation | None
    source_slot_id: int | None
    destination_slot_id: int
    created: bool


def _normalise_reason(value: str) -> str:
    reason = " ".join(value.split())
    if not reason:
        raise BusinessLogicError(
            "A reschedule reason is required.",
            code="personal_reschedule_reason_required",
        )
    if len(reason) > 500:
        raise BusinessLogicError(
            "Reschedule reason is too long.",
            code="personal_reschedule_reason_too_long",
        )
    return reason


def _normalise_idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if len(key) > 120:
        raise BusinessLogicError(
            "Idempotency key is too long.",
            code="personal_reschedule_idempotency_invalid",
        )
    return key


def _schedule_bounds(*, schedule: Schedule) -> tuple[datetime, datetime]:
    zone = club_zoneinfo(schedule.club)
    return (
        timezone.make_aware(datetime.combine(schedule.one_time_date, schedule.start_time), zone),
        timezone.make_aware(datetime.combine(schedule.one_time_date, schedule.end_time), zone),
    )


def _slot_local_bounds(*, slot: PersonalAvailabilitySlot) -> tuple[datetime, datetime]:
    zone = club_zoneinfo(slot.club)
    return timezone.localtime(slot.starts_at, zone), timezone.localtime(slot.ends_at, zone)


def _save_event(event: ScheduleBookingEvent) -> None:
    try:
        event.full_clean()
    except ValidationError as exc:
        raise BusinessLogicError(
            "Unable to record personal reschedule evidence.",
            code="personal_reschedule_event_invalid",
        ) from exc
    event.save()


def _source_booking_kind(*, club_id: int, enrollment_id: int) -> tuple[int | None, int | None]:
    """Preview immutable source ownership before acquiring its lock hierarchy."""

    booking_id = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(enrollment_id=enrollment_id)
        .values_list("id", flat=True)
        .first()
    )
    if booking_id is not None:
        return booking_id, None
    reservation_id = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(enrollment_id=enrollment_id)
        .values_list("id", flat=True)
        .first()
    )
    return None, reservation_id


def _lock_source_scope(
    *,
    club_id: int,
    enrollment_id: int,
    destination_slot_id: int,
) -> tuple[str, int | None, int | None]:
    """Take the existing D12 scope for paid sources, or entitlement roots."""

    booking_id, reservation_id = _source_booking_kind(club_id=club_id, enrollment_id=enrollment_id)
    if booking_id is not None:
        scope = lock_complete_personal_scopes(
            club_id=club_id,
            booking_ids=[booking_id],
            extra_slot_ids=[destination_slot_id],
        )
        if scope is None:
            raise BusinessLogicError(
                "Only a booking with complete personal terms can be rescheduled.",
                code="personal_reschedule_terms_required",
            )
        return "drop_in", booking_id, None
    if reservation_id is not None:
        reservation_status = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(id=reservation_id)
            .values_list("status", flat=True)
            .first()
        )
        if reservation_status in {
            PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
        }:
            raise BusinessLogicError(
                "A pending bank reservation is not a personal booking.",
                code="personal_reschedule_reservation_pending",
            )
        scope = lock_complete_personal_scopes(
            club_id=club_id,
            reservation_ids=[reservation_id],
            extra_slot_ids=[destination_slot_id],
        )
        if scope is None:
            raise BusinessLogicError(
                "Only a booking with complete personal terms can be rescheduled.",
                code="personal_reschedule_terms_required",
            )
        return "reservation", None, reservation_id

    lock_personal_booking_enrollment_scope(
        club_id=club_id,
        enrollment_id=enrollment_id,
        extra_slot_ids=[destination_slot_id],
    )
    return "entitlement", None, None


def _locked_enrollment(*, club_id: int, enrollment_id: int) -> ScheduleEnrollment:
    enrollment = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related(
            "student",
            "schedule",
            "schedule__club",
            "schedule__trainer",
            "schedule__location",
            "schedule__training_type",
        )
        .filter(id=enrollment_id)
        .first()
    )
    if enrollment is None:
        raise BusinessLogicError("Personal booking was not found.", code="personal_reschedule_not_found")
    return enrollment


def _locked_source_slot(*, club_id: int, enrollment_id: int) -> PersonalAvailabilitySlot | None:
    slots = list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(booked_enrollment_id=enrollment_id)
        .order_by("id")
    )
    if len(slots) > 1:
        raise BusinessLogicError(
            "Personal booking has ambiguous availability ownership.",
            code="personal_reschedule_source_ambiguous",
        )
    return slots[0] if slots else None


def _assert_source_is_reschedulable(
    *,
    enrollment: ScheduleEnrollment,
    booking: PersonalDropInBooking | None,
    reservation: PersonalBookingPaymentReservation | None,
) -> tuple[datetime, datetime]:
    schedule = enrollment.schedule
    if (
        schedule.one_time_date is None
        or enrollment.starts_on is None
        or enrollment.starts_on != enrollment.ends_on
        or schedule.one_time_date != enrollment.starts_on
        or not schedule.is_active
    ):
        raise BusinessLogicError(
            "Only an active exact personal booking can be rescheduled.",
            code="personal_reschedule_not_exact_booking",
        )
    if schedule.training_type.kind != TrainingType.Kind.PERSONAL:
        raise BusinessLogicError(
            "Only personal training bookings can be rescheduled through this path.",
            code="personal_reschedule_source_invalid",
        )
    if enrollment.status not in OPEN_EXACT_BOOKING_STATUSES:
        raise BusinessLogicError(
            "Terminal personal booking cannot be rescheduled.",
            code="personal_reschedule_terminal",
        )
    if booking is not None:
        if booking.state != PersonalDropInBooking.State.SCHEDULED:
            raise BusinessLogicError(
                "Terminal personal drop-in booking cannot be rescheduled.",
                code="personal_reschedule_terminal",
            )
        if enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN:
            raise BusinessLogicError(
                "Personal drop-in authority does not match its enrollment.",
                code="personal_reschedule_source_invalid",
            )
    elif reservation is not None:
        if reservation.status != PersonalBookingPaymentReservation.Status.BOOKED:
            raise BusinessLogicError(
                "A pending bank reservation is not a personal booking.",
                code="personal_reschedule_reservation_pending",
            )
        if reservation.schedule_id != schedule.id:
            raise BusinessLogicError(
                "Personal booking reservation does not match its schedule.",
                code="personal_reschedule_source_invalid",
            )
    elif enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING:
        raise BusinessLogicError(
            "Booking type cannot be rescheduled through the personal path.",
            code="personal_reschedule_source_invalid",
        )

    starts_at, ends_at = _schedule_bounds(schedule=schedule)
    if starts_at <= timezone.now():
        raise BusinessLogicError(
            "Personal booking cannot be rescheduled after it has started.",
            code="personal_reschedule_started",
        )
    if Checkin.objects.for_club(enrollment.club_id).filter(
        schedule_id=schedule.id,
        date=schedule.one_time_date,
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    ).exists() or GroupSession.objects.for_club(enrollment.club_id).filter(
        schedule_id=schedule.id,
        date=schedule.one_time_date,
    ).exists():
        raise BusinessLogicError(
            "Personal booking with attendance evidence cannot be rescheduled.",
            code="personal_reschedule_checkin_conflict",
        )
    return starts_at, ends_at


def _assert_destination_is_available(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
    destination_slot: PersonalAvailabilitySlot,
    source_slot: PersonalAvailabilitySlot | None,
    source_reservation_id: int | None,
) -> None:
    schedule = enrollment.schedule
    if (
        destination_slot.trainer_id != schedule.trainer_id
        or destination_slot.location_id != schedule.location_id
        or destination_slot.training_type_id != schedule.training_type_id
    ):
        raise BusinessLogicError(
            "Destination must keep the same trainer, location, and training type.",
            code="personal_reschedule_incompatible_destination",
        )
    if destination_slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
        raise BusinessLogicError(
            "Destination personal availability slot is not available.",
            code="personal_reschedule_destination_unavailable",
        )
    destination_starts_at, destination_ends_at = _slot_local_bounds(slot=destination_slot)
    if destination_starts_at <= timezone.now():
        raise BusinessLogicError(
            "Destination personal availability slot is stale.",
            code="personal_reschedule_destination_stale",
        )

    overlapping_slots = list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=schedule.trainer_id,
            status__in=ACTIVE_SLOT_STATUSES,
            starts_at__lt=destination_ends_at,
            ends_at__gt=destination_starts_at,
        )
        .exclude(id=destination_slot.id)
        .order_by("id")
    )
    if source_slot is not None:
        overlapping_slots = [slot for slot in overlapping_slots if slot.id != source_slot.id]
    if overlapping_slots:
        raise BusinessLogicError(
            "Destination overlaps another active personal availability slot.",
            code="personal_reschedule_destination_conflict",
        )

    live_reservations = list(
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            trainer_id=schedule.trainer_id,
            status__in=LIVE_RESERVATION_STATUSES,
            starts_at__lt=destination_ends_at,
            ends_at__gt=destination_starts_at,
        )
        .order_by("id")
    )
    if source_reservation_id is not None:
        live_reservations = [
            reservation for reservation in live_reservations if reservation.id != source_reservation_id
        ]
    if live_reservations:
        raise BusinessLogicError(
            "Destination overlaps a live personal payment reservation.",
            code="personal_reschedule_reservation_conflict",
        )

    # Availability rows do not have schedules of their own.  The selector is
    # the source of truth for recurring and exception-derived occurrences.
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    destination_date = destination_starts_at.date()
    for occurrence in get_schedule_occurrences_for_date(
        club=club_id,
        target_date=destination_date,
        trainer_id=schedule.trainer_id,
    ):
        if occurrence.schedule_id == schedule.id:
            continue
        if (
            occurrence.effective_start_time < destination_ends_at.time().replace(tzinfo=None)
            and occurrence.effective_end_time > destination_starts_at.time().replace(tzinfo=None)
        ):
            raise BusinessLogicError(
                "Destination conflicts with an existing trainer schedule.",
                code="personal_reschedule_schedule_conflict",
            )


def _idempotent_reschedule_result(
    *,
    club_id: int,
    enrollment: ScheduleEnrollment,
    destination_slot: PersonalAvailabilitySlot,
    booking: PersonalDropInBooking | None,
    reservation: PersonalBookingPaymentReservation | None,
    reason: str,
    idempotency_key: str,
) -> PersonalBookingRescheduleResult | None:
    if not idempotency_key:
        return None
    event = (
        ScheduleBookingEvent.objects.for_club(club_id)
        .filter(
            enrollment_id=enrollment.id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
            metadata__idempotency_key=idempotency_key,
        )
        .order_by("-id")
        .first()
    )
    if event is None:
        return None
    if (
        event.metadata.get("destination_availability_slot_id") != destination_slot.id
        or event.metadata.get("reason") != reason
    ):
        raise BusinessLogicError(
            "Personal reschedule idempotency key was already used for another command.",
            code="personal_reschedule_idempotency_conflict",
        )
    if (
        destination_slot.status != PersonalAvailabilitySlot.Status.BOOKED
        or destination_slot.booked_enrollment_id != enrollment.id
    ):
        raise BusinessLogicError(
            "Personal reschedule replay evidence is inconsistent.",
            code="personal_reschedule_idempotency_conflict",
        )
    return PersonalBookingRescheduleResult(
        enrollment=enrollment,
        schedule=enrollment.schedule,
        booking=booking,
        reservation=reservation,
        source_slot_id=event.metadata.get("source_availability_slot_id"),
        destination_slot_id=destination_slot.id,
        created=False,
    )


def reschedule_personal_exact_booking(
    *,
    club_id: int,
    enrollment_id: int,
    destination_slot_id: int,
    actor_user_id: int,
    reason: str,
    idempotency_key: str | None = None,
) -> PersonalBookingRescheduleResult:
    """Move one future personal booking without changing commercial evidence.

    Complete drop-in/reservation terms and entitlement bookings deliberately
    share the same exact-schedule transition.  This function never reads the
    offer resolver or catalog: price, discount, payment, subscription and
    entitlement evidence remain attached to their existing authority.
    """

    normalised_reason = _normalise_reason(reason)
    command_key = _normalise_idempotency_key(idempotency_key)

    with transaction.atomic():
        source_kind, booking_id, reservation_id = _lock_source_scope(
            club_id=club_id,
            enrollment_id=enrollment_id,
            destination_slot_id=destination_slot_id,
        )
        enrollment = _locked_enrollment(club_id=club_id, enrollment_id=enrollment_id)
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=booking_id)
            .first()
            if booking_id is not None
            else None
        )
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=reservation_id)
            .first()
            if reservation_id is not None
            else None
        )
        if source_kind == "entitlement":
            pending_reservation = (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(
                    enrollment_id=enrollment.id,
                    status__in=[
                        PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                    ],
                )
                .first()
            )
            if pending_reservation is not None:
                raise BusinessLogicError(
                    "A pending bank reservation is not a personal booking.",
                    code="personal_reschedule_reservation_pending",
                )

        source_starts_at, source_ends_at = _assert_source_is_reschedulable(
            enrollment=enrollment,
            booking=booking,
            reservation=reservation,
        )
        source_slot = _locked_source_slot(club_id=club_id, enrollment_id=enrollment.id)
        if source_slot is not None and source_slot.status != PersonalAvailabilitySlot.Status.BOOKED:
            raise BusinessLogicError(
                "Personal booking availability ownership is stale.",
                code="personal_reschedule_source_stale",
            )

        destination_slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("club")
            .filter(id=destination_slot_id)
            .first()
        )
        if destination_slot is None:
            raise BusinessLogicError(
                "Destination personal availability slot was not found.",
                code="personal_reschedule_destination_not_found",
            )
        replay = _idempotent_reschedule_result(
            club_id=club_id,
            enrollment=enrollment,
            destination_slot=destination_slot,
            booking=booking,
            reservation=reservation,
            reason=normalised_reason,
            idempotency_key=command_key,
        )
        if replay is not None:
            return replay
        _assert_destination_is_available(
            club_id=club_id,
            enrollment=enrollment,
            destination_slot=destination_slot,
            source_slot=source_slot,
            source_reservation_id=reservation.id if reservation is not None else None,
        )
        destination_starts_at, destination_ends_at = _slot_local_bounds(slot=destination_slot)

        if source_slot is not None:
            source_slot.status = PersonalAvailabilitySlot.Status.PUBLISHED
            source_slot.booked_enrollment = None
            source_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])
        destination_slot.status = PersonalAvailabilitySlot.Status.BOOKED
        destination_slot.booked_enrollment = enrollment
        destination_slot.save(update_fields=["status", "booked_enrollment", "updated_at"])

        schedule = enrollment.schedule
        schedule.day_of_week = destination_starts_at.date().weekday()
        schedule.one_time_date = destination_starts_at.date()
        schedule.start_time = destination_starts_at.time().replace(tzinfo=None)
        schedule.end_time = destination_ends_at.time().replace(tzinfo=None)
        schedule.save(update_fields=["day_of_week", "one_time_date", "start_time", "end_time", "updated_at"])
        enrollment.starts_on = destination_starts_at.date()
        enrollment.ends_on = destination_starts_at.date()
        enrollment.full_clean()
        enrollment.save(update_fields=["starts_on", "ends_on", "updated_at"])

        if reservation is not None:
            reservation.availability_slot = destination_slot
            reservation.starts_at = destination_starts_at
            reservation.ends_at = destination_ends_at
            reservation.save(update_fields=["availability_slot", "starts_at", "ends_at", "updated_at"])

        _save_event(
            ScheduleBookingEvent(
                club_id=club_id,
                enrollment=enrollment,
                schedule=schedule,
                student_id=enrollment.student_id,
                actor_id=actor_user_id,
                event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                effective_date=destination_starts_at.date(),
                metadata={
                    "booking_kind": source_kind,
                    "personal_drop_in_booking_id": booking.id if booking is not None else None,
                    "personal_booking_reservation_id": reservation.id if reservation is not None else None,
                    "source_availability_slot_id": source_slot.id if source_slot is not None else None,
                    "destination_availability_slot_id": destination_slot.id,
                    "source_starts_at": source_starts_at.isoformat(),
                    "source_ends_at": source_ends_at.isoformat(),
                    "destination_starts_at": destination_starts_at.isoformat(),
                    "destination_ends_at": destination_ends_at.isoformat(),
                    "reason": normalised_reason,
                    "idempotency_key": command_key,
                },
            )
        )
        return PersonalBookingRescheduleResult(
            enrollment=enrollment,
            schedule=schedule,
            booking=booking,
            reservation=reservation,
            source_slot_id=source_slot.id if source_slot is not None else None,
            destination_slot_id=destination_slot.id,
            created=True,
        )
