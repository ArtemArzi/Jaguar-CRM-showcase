"""Typed coordinator for the flag-on staff personal-session journey."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalPaymentMethodCorrection,
    PersonalServiceTermsSnapshot,
    PersonalStaffIntentCommand,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.personal_offers import direct_personal_offer_payload
from apps.attendance.services.drop_in import book_personal_drop_in, create_personal_drop_in_payment
from apps.attendance.services.enrollment import (
    book_personal_availability_slot,
    book_personal_session,
    create_personal_booking_payment_reservation,
)
from apps.billing.models import BankPaymentOrder, Payment, Tariff, TrainingType
from apps.billing.service_modules.payment_readiness import get_online_payment_capability
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.clubs.capabilities import (
    assert_v2_manual_admission_command_allowed,
    get_commercial_journey_capability,
    get_v1_commercial_journey_command_availability,
    get_v2_provider_command_availability,
    is_unified_client_journey_enabled,
)
from apps.clubs.models import Club, ClubSettings, Location
from apps.clubs.timezones import club_localdate_by_id, club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate

STAFF_PERSONAL_PAYMENT_METHODS = frozenset({"entitlement", "cash", "transfer", "sbp", "pay_at_visit"})


@dataclass(frozen=True)
class StaffPersonalIntentResult:
    receipt: dict
    created: bool


def _staff_command_fingerprint(*, shape: dict) -> str:
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _claim_staff_command(
    *, club_id: int, command_key: str, payment_method: str, shape: dict
) -> PersonalStaffIntentCommand:
    """Atomically reserve the key across entitlement and paid artifact families."""

    fingerprint = _staff_command_fingerprint(shape=shape)
    try:
        with transaction.atomic():
            return PersonalStaffIntentCommand.objects.create(
                club_id=club_id,
                command_key=command_key,
                command_fingerprint=fingerprint,
                payment_method=payment_method,
                command_shape=shape,
            )
    except IntegrityError:
        # The failed insert rolls its savepoint back before this query.  The
        # selected durable claim is the cross-family serialization point.
        with transaction.atomic():
            command = (
                PersonalStaffIntentCommand.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(command_key=command_key)
                .first()
            )
            if command is None:
                raise
            if command.command_fingerprint != fingerprint:
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            return command


def _bind_staff_command(
    *, command: PersonalStaffIntentCommand, receipt: dict
) -> None:
    """Record the authoritative result after its owner has committed.

    A crash in the short bind window is safe: an exact retry discovers the
    owner-local idempotency artifact and binds this claim before returning it.
    """

    with transaction.atomic():
        durable_command = (
            PersonalStaffIntentCommand.objects.for_club(command.club_id)
            .select_for_update(of=("self",))
            .get(id=command.id)
        )
        if durable_command.result_bound_at is not None:
            return
        booking_id = receipt.get("booking_id")
        payment_link_id = None
        if booking_id is not None and receipt.get("payment_id") is not None:
            payment_link_id = (
                PersonalDropInPaymentLink.objects.for_club(command.club_id)
                .filter(booking_id=booking_id, payment_id=receipt["payment_id"])
                .values_list("id", flat=True)
                .first()
            )
        durable_command.booking_id_snapshot = booking_id
        durable_command.reservation_id_snapshot = receipt.get("reservation_id")
        durable_command.enrollment_id_snapshot = receipt.get("enrollment_id")
        durable_command.payment_link_id_snapshot = payment_link_id
        durable_command.result_bound_at = timezone.now()
        durable_command.save(
            update_fields=[
                "booking_id_snapshot",
                "reservation_id_snapshot",
                "enrollment_id_snapshot",
                "payment_link_id_snapshot",
                "result_bound_at",
                "updated_at",
            ]
        )


def _schedule_bounds(*, schedule) -> tuple[datetime, datetime]:
    zone = club_zoneinfo(schedule.club)
    return (
        timezone.make_aware(datetime.combine(schedule.one_time_date, schedule.start_time), zone),
        timezone.make_aware(datetime.combine(schedule.one_time_date, schedule.end_time), zone),
    )


def _replay_staff_command(command: PersonalStaffIntentCommand) -> StaffPersonalIntentResult | None:
    """Return an exact persisted result before any mutable offer/capability read."""

    if command.booking_id_snapshot is not None:
        booking = PersonalDropInBooking.objects.for_club(command.club_id).filter(
            id=command.booking_id_snapshot
        ).first()
        if booking is not None:
            return StaffPersonalIntentResult(
                receipt=_booking_receipt_for_command(command=command, booking=booking),
                created=False,
            )
    if command.reservation_id_snapshot is not None:
        reservation = PersonalBookingPaymentReservation.objects.for_club(command.club_id).filter(
            id=command.reservation_id_snapshot
        ).first()
        if reservation is not None:
            return StaffPersonalIntentResult(
                receipt=_reservation_receipt(reservation=reservation, payment_method=command.payment_method),
                created=False,
            )
    if command.enrollment_id_snapshot is not None:
        enrollment = (
            ScheduleEnrollment.objects.for_club(command.club_id)
            .select_related(
                "schedule",
                "schedule__trainer",
                "schedule__location",
                "schedule__training_type",
                "schedule__club",
            )
            .filter(id=command.enrollment_id_snapshot)
            .first()
        )
        if enrollment is not None:
            return StaffPersonalIntentResult(
                receipt=_enrollment_entitlement_receipt(
                    enrollment=enrollment,
                    terminal_event=_latest_entitlement_terminal_event(enrollment=enrollment),
                ),
                created=False,
            )
    return None


def has_accepted_staff_personal_command(*, club_id: int, command_key: str) -> bool:
    """Return whether a claimed key has a bound result or exact owner artifact.

    A bare command claim can survive a validation failure, so it is not enough
    to cross a protocol cutover. Owner-local artifacts cover the narrow crash
    window between committing the result and binding its receipt snapshots.
    """

    key = command_key.strip()
    if not key:
        return False
    command = PersonalStaffIntentCommand.objects.for_club(club_id).filter(
        command_key=key
    ).first()
    if command is None:
        return False
    if command.result_bound_at is not None:
        return True
    return bool(
        PersonalDropInBooking.objects.for_club(club_id).filter(idempotency_key=key).exists()
        or PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(idempotency_key=key)
        .exists()
        or ScheduleBookingEvent.objects.for_club(club_id)
        .filter(
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            metadata__idempotency_key=key,
        )
        .exists()
    )


def _staff_result(
    *, command: PersonalStaffIntentCommand, result: StaffPersonalIntentResult
) -> StaffPersonalIntentResult:
    _bind_staff_command(command=command, receipt=result.receipt)
    return result


def _validate_personal_command_protocol_locked(
    *,
    club_id: int,
    expected_version: str,
    payment_method: str,
) -> bool:
    """Validate protocol after the caller holds Club, then hold ClubSettings."""

    ClubSettings.objects.select_for_update(of=("self",)).filter(club_id=club_id).first()
    capability = get_commercial_journey_capability(club=club_id)
    if expected_version == "v1":
        availability = get_v1_commercial_journey_command_availability(
            capability=capability,
        )
        if not availability.allows_new_command:
            raise BusinessLogicError(
                "Commercial journey command is unavailable for this client or tenant.",
                code=availability.code,
            )
        return False
    if expected_version != "v2":
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code="commercial_journey_unavailable",
        )
    if payment_method in {"cash", "transfer"}:
        assert_v2_manual_admission_command_allowed(capability=capability)
        return True
    payment_capability = get_online_payment_capability()
    availability = get_v2_provider_command_availability(
        capability=capability,
        provider_creation_enabled=payment_capability.enabled,
    )
    if not availability.allows_new_command:
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code=availability.code,
        )
    return False


def _slot_or_raise(*, club_id: int, slot_id: int, lock: bool = False) -> PersonalAvailabilitySlot:
    queryset = PersonalAvailabilitySlot.objects.for_club(club_id).select_related(
        "trainer", "location", "training_type"
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    slot = queryset.filter(id=slot_id).first()
    if slot is None:
        raise BusinessLogicError("Personal availability slot was not found", code="personal_slot_not_found")
    return slot


def _terms_amount(*, club_id: int, booking_id: int | None = None, reservation_id: int | None = None) -> str:
    terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
        booking_id=booking_id,
        reservation_id=reservation_id,
    ).first()
    if terms is None or terms.payable_amount is None:
        return ""
    return str(terms.payable_amount)


def _booking_origin_slot_id(*, booking: PersonalDropInBooking) -> int | None:
    """Return the immutable availability-slot identity accepted by a booking."""

    metadata_rows = (
        ScheduleBookingEvent.objects.for_club(booking.club_id)
        .filter(
            enrollment_id=booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        )
        .order_by("id")
        .values_list("metadata", flat=True)
    )
    for metadata in metadata_rows:
        if metadata.get("personal_drop_in_booking_id") == booking.id:
            slot_id = metadata.get("availability_slot_id")
            return int(slot_id) if slot_id is not None else None
    return None


def _booking_effective_slot_id(*, booking: PersonalDropInBooking) -> int | None:
    """Return the current occupancy slot, retaining origin evidence separately.

    A personal reschedule intentionally preserves the original booking event
    and its accepted slot metadata. Receipts must instead show the slot that
    currently owns the booking enrollment, or the exact schedule for direct
    bookings.
    """

    return (
        PersonalAvailabilitySlot.objects.for_club(booking.club_id)
        .filter(booked_enrollment_id=booking.enrollment_id)
        .order_by("id")
        .values_list("id", flat=True)
        .first()
    )


def _booking_receipt(
    *,
    booking: PersonalDropInBooking,
    payment_method: str,
    link_id: int | None = None,
    bank_order_id: int | None = None,
    use_latest_link: bool = True,
    actor_role: str | None = None,
) -> dict:
    booking = (
        PersonalDropInBooking.objects.for_club(booking.club_id)
        .select_related(
            "enrollment__schedule__trainer",
            "enrollment__schedule__location",
            "enrollment__schedule__training_type",
            "debt",
        )
        .get(id=booking.id)
    )
    schedule = booking.enrollment.schedule
    slot_id = _booking_effective_slot_id(booking=booking)
    slot = PersonalAvailabilitySlot.objects.for_club(booking.club_id).filter(id=slot_id).first()
    links = (
        PersonalDropInPaymentLink.objects.for_club(booking.club_id)
        .select_related("payment__subscription", "bank_payment_order")
        .filter(booking_id=booking.id)
        .order_by("-created_at", "-id")
    )
    link = (
        links.filter(id=link_id).first()
        if link_id is not None
        else (links.first() if use_latest_link else None)
    )
    payment = link.payment if link is not None else None
    order = link.bank_payment_order if link is not None else None
    if order is None and bank_order_id is not None:
        order = (
            BankPaymentOrder.objects.for_club(booking.club_id)
            .select_related("payment__subscription")
            .filter(id=bank_order_id, personal_drop_in_booking_id_snapshot=booking.id)
            .first()
        )
        payment = order.payment if order is not None else None
    can_manage_bank_order = not (
        order is not None
        and actor_role == "trainer"
        and order.source != BankPaymentOrder.Source.TRAINER
    )
    receipt_payment_method = "sbp" if order is not None else payment_method
    if booking.state in {PersonalDropInBooking.State.CANCELLED, PersonalDropInBooking.State.NO_SHOW}:
        allowed_actions = ["view_booking"]
        status = booking.state
    elif payment is not None and payment.status == Payment.Status.PENDING:
        if order is not None:
            from apps.billing.service_modules.bank_orders import provider_dispatch_blocks_cancellation

            if not can_manage_bank_order:
                allowed_actions = ["owner_review"]
            elif order.status == BankPaymentOrder.Status.MANUAL_REVIEW or provider_dispatch_blocks_cancellation(order):
                allowed_actions = ["refresh_or_reconcile", "owner_review"]
            else:
                allowed_actions = [
                    "open_bank_payment_order",
                    "cancel_if_safe",
                    "replace_payment_method",
                ]
        elif actor_role in {"owner", "admin"}:
            allowed_actions = ["replace_payment_method"]
        else:
            allowed_actions = ["owner_review"]
        status = order.status if order is not None else payment.status
    elif booking.state == PersonalDropInBooking.State.ATTENDED and booking.debt_id and booking.debt.resolved_at is None:
        allowed_actions = ["settle_exact_debt"]
        status = "debt_open"
    elif payment is not None and payment.status == Payment.Status.CONFIRMED:
        allowed_actions = ["view_booking"]
        status = "confirmed"
    elif payment is not None and payment.status == Payment.Status.REJECTED:
        allowed_actions = (
            ["retry_bank_payment"]
            if receipt_payment_method == "sbp" and booking.state == PersonalDropInBooking.State.SCHEDULED
            else ["view_booking"]
        )
        status = "rejected"
    elif payment_method == "pay_at_visit":
        allowed_actions = ["pay_at_visit"]
        status = "pay_at_visit"
    else:
        allowed_actions = ["view_booking"]
        status = booking.state
    return {
        "kind": "personal_staff_intent",
        "booking_id": booking.id,
        "reservation_id": None,
        "payment_id": payment.id if payment else None,
        "subscription_id": payment.subscription_id if payment else None,
        "bank_payment_order_id": order.id if order else None,
        "debt_id": booking.debt_id,
        "slot_id": slot.id if slot is not None else None,
        "schedule_id": schedule.id,
        "enrollment_id": booking.enrollment_id,
        "starts_at": slot.starts_at if slot is not None else _schedule_bounds(schedule=schedule)[0],
        "ends_at": slot.ends_at if slot is not None else _schedule_bounds(schedule=schedule)[1],
        "trainer_id": schedule.trainer_id,
        "trainer_name": f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip(),
        "location_id": schedule.location_id,
        "location_name": schedule.location.name,
        "training_type_id": schedule.training_type_id,
        "training_type_name": schedule.training_type.name,
        "tariff_id": booking.tariff_id,
        "tariff_name": booking.tariff_name_snapshot,
        "amount": _terms_amount(club_id=booking.club_id, booking_id=booking.id) or str(booking.price_snapshot),
        "payment_method": receipt_payment_method,
        "status": status,
        "provider_payment_url": order.provider_payment_url if order and can_manage_bank_order else "",
        "allowed_actions": allowed_actions,
        "resource_route": f"/api/personal-drop-in-bookings/{booking.id}/",
        "attempted_at": (
            link.created_at
            if link is not None
            else (order.created_at if order is not None else booking.created_at)
        ),
    }


def _booking_receipt_for_command(
    *, command: PersonalStaffIntentCommand, booking: PersonalDropInBooking
) -> dict:
    """Read exactly the payment attempt owned by one staff command.

    A booking may legitimately gain later settlement attempts.  Those later
    links belong to their own idempotency commands and must never rewrite the
    receipt returned for an earlier staff command (including pay-at-visit,
    which deliberately owns no link at all).
    """

    if command.result_bound_at is not None:
        return _booking_receipt(
            booking=booking,
            payment_method=command.payment_method,
            link_id=command.payment_link_id_snapshot,
            use_latest_link=False,
        )
    if command.payment_method == "pay_at_visit":
        return _booking_receipt(
            booking=booking,
            payment_method=command.payment_method,
            use_latest_link=False,
        )
    matching_link_id = (
        PersonalDropInPaymentLink.objects.for_club(command.club_id)
        .filter(booking_id=booking.id, idempotency_key=command.command_key)
        .values_list("id", flat=True)
        .first()
    )
    return _booking_receipt(
        booking=booking,
        payment_method=command.payment_method,
        link_id=matching_link_id,
        use_latest_link=False,
    )


def _reservation_receipt(
    *, reservation: PersonalBookingPaymentReservation, payment_method: str, actor_role: str | None = None
) -> dict:
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(reservation.club_id)
        .select_related("trainer", "location", "training_type", "tariff", "payment__subscription", "bank_payment_order")
        .get(id=reservation.id)
    )
    order = reservation.bank_payment_order
    can_manage_bank_order = not (
        order is not None
        and actor_role == "trainer"
        and order.source != BankPaymentOrder.Source.TRAINER
    )
    retryable_reservation_states = {
        PersonalBookingPaymentReservation.Status.CANCELLED,
        PersonalBookingPaymentReservation.Status.EXPIRED,
    }
    retryable_order_states = {
        BankPaymentOrder.Status.FAILED,
        BankPaymentOrder.Status.CANCELLED,
        BankPaymentOrder.Status.EXPIRED,
    }
    terminal_attempt = (
        reservation.status in retryable_reservation_states
        or (order is not None and order.status in retryable_order_states)
    )
    slot_is_retryable = bool(
        reservation.availability_slot_id
        and PersonalAvailabilitySlot.objects.for_club(reservation.club_id)
        .filter(
            id=reservation.availability_slot_id,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        .exists()
    )
    direct_context_is_retryable = False
    if reservation.availability_slot_id is None and terminal_attempt:
        # A direct-time retry has no availability-slot row to consult.  Its
        # exact context remains admissible only while the original time is in
        # the future and no live reservation or schedule now owns it.
        club_tz = club_zoneinfo(
            Club.objects.only("id", "timezone").get(id=reservation.club_id)
        )
        local_starts_at = timezone.localtime(reservation.starts_at, club_tz)
        local_ends_at = timezone.localtime(reservation.ends_at, club_tz)
        direct_context_is_retryable = reservation.ends_at > timezone.now() and not (
            PersonalBookingPaymentReservation.objects.for_club(reservation.club_id)
            .filter(
                trainer_id=reservation.trainer_id,
                starts_at__lt=reservation.ends_at,
                ends_at__gt=reservation.starts_at,
                status__in=(
                    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                ),
            )
            .exclude(id=reservation.id)
            .exists()
        )
        if direct_context_is_retryable:
            from apps.attendance.models import Schedule

            direct_context_is_retryable = not Schedule.objects.for_club(reservation.club_id).filter(
                trainer_id=reservation.trainer_id,
                one_time_date=local_starts_at.date(),
                start_time__lt=local_ends_at.timetz().replace(tzinfo=None),
                end_time__gt=local_starts_at.timetz().replace(tzinfo=None),
            ).exists()
    if terminal_attempt and (
        slot_is_retryable or direct_context_is_retryable
    ):
        allowed_actions = ["retry_bank_payment"]
    elif terminal_attempt:
        allowed_actions = ["view_booking"]
    elif order is not None:
        from apps.billing.service_modules.bank_orders import provider_dispatch_blocks_cancellation

        if not can_manage_bank_order:
            allowed_actions = ["owner_review"]
        elif order.status == BankPaymentOrder.Status.MANUAL_REVIEW or provider_dispatch_blocks_cancellation(order):
            allowed_actions = ["refresh_or_reconcile", "owner_review"]
        else:
            allowed_actions = [
                "open_bank_payment_order",
                "cancel_if_safe",
                "replace_payment_method",
            ]
    else:
        allowed_actions = ["owner_review"]
    return {
        "kind": "personal_staff_intent",
        "booking_id": None,
        "reservation_id": reservation.id,
        "payment_id": reservation.payment_id,
        "subscription_id": reservation.subscription_id,
        "bank_payment_order_id": reservation.bank_payment_order_id,
        "debt_id": None,
        "slot_id": reservation.availability_slot_id,
        "schedule_id": reservation.schedule_id,
        "enrollment_id": reservation.enrollment_id,
        "starts_at": reservation.starts_at,
        "ends_at": reservation.ends_at,
        "trainer_id": reservation.trainer_id,
        "trainer_name": f"{reservation.trainer.first_name} {reservation.trainer.last_name}".strip(),
        "location_id": reservation.location_id,
        "location_name": reservation.location.name,
        "training_type_id": reservation.training_type_id,
        "training_type_name": reservation.training_type.name,
        "tariff_id": reservation.tariff_id,
        "tariff_name": reservation.tariff.name,
        "amount": _terms_amount(club_id=reservation.club_id, reservation_id=reservation.id),
        "payment_method": payment_method,
        "status": order.status if order is not None else reservation.status,
        "provider_payment_url": order.provider_payment_url if order and can_manage_bank_order else "",
        "allowed_actions": allowed_actions,
        "resource_route": f"/api/personal-availability/staff-payment-reservations/{reservation.id}/",
        "attempted_at": reservation.created_at,
    }


def _entitlement_receipt(*, result, slot: PersonalAvailabilitySlot) -> dict:
    schedule = result.schedule
    return {
        "kind": "personal_staff_intent",
        "booking_id": None,
        "reservation_id": None,
        "payment_id": None,
        "subscription_id": None,
        "bank_payment_order_id": None,
        "debt_id": None,
        "slot_id": slot.id,
        "schedule_id": schedule.id,
        "enrollment_id": result.enrollment.id,
        "starts_at": slot.starts_at,
        "ends_at": slot.ends_at,
        "trainer_id": slot.trainer_id,
        "trainer_name": f"{slot.trainer.first_name} {slot.trainer.last_name}".strip(),
        "location_id": slot.location_id,
        "location_name": slot.location.name,
        "training_type_id": slot.training_type_id,
        "training_type_name": slot.training_type.name,
        "tariff_id": None,
        "tariff_name": "",
        "amount": "",
        "payment_method": "entitlement",
        "status": "booked",
        "provider_payment_url": "",
        "allowed_actions": ["view_booking"],
        "resource_route": f"/api/schedules/{schedule.id}/",
        "attempted_at": result.enrollment.created_at,
    }


def _direct_entitlement_receipt(*, result) -> dict:
    schedule = result.schedule
    return {
        "kind": "personal_staff_intent",
        "booking_id": None,
        "reservation_id": None,
        "payment_id": None,
        "subscription_id": None,
        "bank_payment_order_id": None,
        "debt_id": None,
        "slot_id": None,
        "schedule_id": schedule.id,
        "enrollment_id": result.enrollment.id,
        "starts_at": _schedule_bounds(schedule=schedule)[0],
        "ends_at": _schedule_bounds(schedule=schedule)[1],
        "trainer_id": schedule.trainer_id,
        "trainer_name": f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip(),
        "location_id": schedule.location_id,
        "location_name": schedule.location.name,
        "training_type_id": schedule.training_type_id,
        "training_type_name": schedule.training_type.name,
        "tariff_id": None,
        "tariff_name": "",
        "amount": "",
        "payment_method": "entitlement",
        "status": "booked",
        "provider_payment_url": "",
        "allowed_actions": ["view_booking"],
        "resource_route": f"/api/schedules/{schedule.id}/",
        "attempted_at": result.enrollment.created_at,
    }


def _enrollment_entitlement_receipt(*, enrollment, terminal_event=None) -> dict:
    schedule = enrollment.schedule
    return {
        "kind": "personal_staff_intent",
        "booking_id": None,
        "reservation_id": None,
        "payment_id": None,
        "subscription_id": None,
        "bank_payment_order_id": None,
        "debt_id": None,
        "slot_id": (
            PersonalAvailabilitySlot.objects.for_club(enrollment.club_id)
            .filter(booked_enrollment_id=enrollment.id)
            .values_list("id", flat=True)
            .first()
        ),
        "schedule_id": schedule.id,
        "enrollment_id": enrollment.id,
        "starts_at": _schedule_bounds(schedule=schedule)[0],
        "ends_at": _schedule_bounds(schedule=schedule)[1],
        "trainer_id": schedule.trainer_id,
        "trainer_name": f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip(),
        "location_id": schedule.location_id,
        "location_name": schedule.location.name,
        "training_type_id": schedule.training_type_id,
        "training_type_name": schedule.training_type.name,
        "tariff_id": None,
        "tariff_name": "",
        "amount": "",
        "payment_method": "entitlement",
        "status": "cancelled" if terminal_event is not None else "booked",
        "provider_payment_url": "",
        "allowed_actions": ["view_booking"],
        "resource_route": f"/api/schedules/{schedule.id}/",
        "attempted_at": terminal_event.created_at if terminal_event is not None else enrollment.created_at,
    }


def _latest_entitlement_terminal_event(*, enrollment):
    return (
        ScheduleBookingEvent.objects.for_club(enrollment.club_id)
        .filter(
            enrollment_id=enrollment.id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def get_staff_direct_personal_offer(
    *,
    club_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    discount_id: int | None = None,
) -> dict:
    """Build the exact server-owned paid direct-time offer preview."""
    club = Club.objects.only("id", "timezone").filter(id=club_id).first()
    if club is None:
        raise BusinessLogicError("Club was not found", code="club_not_found")
    club_tz = club_zoneinfo(club)
    starts_at = (
        timezone.make_aware(starts_at, club_tz)
        if timezone.is_naive(starts_at)
        else timezone.localtime(starts_at, club_tz)
    )
    ends_at = (
        timezone.make_aware(ends_at, club_tz)
        if timezone.is_naive(ends_at)
        else timezone.localtime(ends_at, club_tz)
    )
    if ends_at <= starts_at or starts_at.date() != ends_at.date():
        raise BusinessLogicError("Invalid personal booking time", code="invalid_personal_booking_time")
    trainer = Trainer.objects.for_club(club_id).filter(id=trainer_id, is_active=True).first()
    if trainer is None:
        raise BusinessLogicError("Trainer does not belong to this club", code="trainer_club_mismatch")
    if not Location.objects.filter(id=location_id, club_id=club_id).exists():
        raise BusinessLogicError("Location does not belong to this club", code="location_club_mismatch")
    training_type = TrainingType.objects.for_club(club_id).filter(id=training_type_id, is_active=True).first()
    if training_type is None:
        raise BusinessLogicError("Training type does not belong to this club", code="training_type_club_mismatch")
    if training_type.kind != TrainingType.Kind.PERSONAL:
        raise BusinessLogicError(
            "Direct staff intent requires a personal training type",
            code="personal_direct_mini_group_forbidden",
        )
    if not TrainerLocation.objects.for_club(club_id).filter(
        trainer_id=trainer_id, location_id=location_id
    ).exists():
        raise BusinessLogicError("Trainer is not assigned to this location", code="trainer_location_required")
    if not TrainerRate.objects.for_club(club_id).filter(
        trainer_id=trainer_id, location_id=location_id, training_type_id=training_type_id
    ).exists():
        raise BusinessLogicError("Trainer rate is required for this personal booking", code="trainer_rate_required")
    offer = resolve_personal_booking_offer(
        club_id=club_id,
        trainer_id=trainer_id,
        training_type_id=training_type_id,
        location_id=location_id,
        discount_id=discount_id,
    )
    return direct_personal_offer_payload(
        offer=offer,
        trainer_id=trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location_id,
        training_type_id=training_type_id,
    )


def get_personal_commercial_context(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int | None = None,
    actor_role: str | None = None,
) -> list[dict]:
    """Return only personal-intent receipts relevant to one staff card.

    It intentionally is not a club finance ledger: it contains no unrelated
    payments and limits terminal reservation history to the latest attempt per
    concrete slot while retaining all live personal attempts.
    """

    bookings = PersonalDropInBooking.objects.for_club(club_id).filter(enrollment__student_id=student_id)
    reservations = PersonalBookingPaymentReservation.objects.for_club(club_id).filter(student_id=student_id)
    if trainer_id is not None:
        bookings = bookings.filter(enrollment__schedule__trainer_id=trainer_id)
        reservations = reservations.filter(trainer_id=trainer_id)

    receipts: list[dict] = []
    live_order_statuses = {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.APPROVED,
        BankPaymentOrder.Status.AUTHORIZED,
        BankPaymentOrder.Status.MANUAL_REVIEW,
    }
    for booking in bookings.select_related("enrollment__schedule").order_by("-created_at", "-id"):
        links = list(
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment")
            .filter(booking_id=booking.id)
            .order_by("-created_at", "-id")
        )
        linked_order_ids = {link.bank_payment_order_id for link in links if link.bank_payment_order_id is not None}
        unattached_orders = list(
            BankPaymentOrder.objects.for_club(club_id)
            .select_related("payment")
            .filter(personal_drop_in_booking_id_snapshot=booking.id)
            .exclude(id__in=linked_order_ids)
            .order_by("-created_at", "-id")
        )
        attempts = [
            (link.created_at, link.id, "link", link, link.payment.status == Payment.Status.PENDING)
            for link in links
        ] + [
            (order.created_at, order.id, "order", order, order.status in live_order_statuses)
            for order in unattached_orders
        ]
        attempts.sort(key=lambda attempt: (attempt[0], attempt[1]), reverse=True)
        live_attempts = [attempt for attempt in attempts if attempt[4]]
        latest_terminal_attempt = next((attempt for attempt in attempts if not attempt[4]), None)
        if not attempts:
            receipts.append(
                _booking_receipt(
                    booking=booking,
                    payment_method="pay_at_visit",
                    actor_role=actor_role,
                )
            )
            continue
        for _created_at, _id, kind, attempt, _is_live in [
            *live_attempts,
            *([latest_terminal_attempt] if latest_terminal_attempt is not None else []),
        ]:
            if kind == "link":
                receipts.append(
                    _booking_receipt(
                        booking=booking,
                        payment_method=attempt.payment.payment_method,
                        link_id=attempt.id,
                        actor_role=actor_role,
                    )
                )
            else:
                receipts.append(
                    _booking_receipt(
                        booking=booking,
                        payment_method="sbp",
                        bank_order_id=attempt.id,
                        use_latest_link=False,
                        actor_role=actor_role,
                    )
                )

        # `pay_at_visit` deliberately creates no Payment row.  A correction to
        # it still is an active, durable replacement attempt, while the
        # rejected source payment stays in compact history above.  Project it
        # from its immutable correction lineage rather than inventing a fake
        # financial link.
        pay_at_visit_correction = (
            PersonalPaymentMethodCorrection.objects.for_club(club_id)
            .filter(
                replacement_booking_id=booking.id,
                replacement_payment_method="pay_at_visit",
                replacement_payment_link__isnull=True,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if pay_at_visit_correction is not None:
            receipts.append(
                _booking_receipt(
                    booking=booking,
                    payment_method="pay_at_visit",
                    use_latest_link=False,
                    actor_role=actor_role,
                )
            )

    live_statuses = {
        PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
        PersonalBookingPaymentReservation.Status.BOOKED,
        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
    }
    latest_terminal_contexts: set[tuple] = set()
    for reservation in reservations.select_related("availability_slot").order_by("-created_at", "-id"):
        if reservation.status not in live_statuses:
            terminal_context = (
                ("slot", reservation.availability_slot_id)
                if reservation.availability_slot_id is not None
                else (
                    "direct",
                    reservation.trainer_id,
                    reservation.starts_at,
                    reservation.ends_at,
                    reservation.location_id,
                    reservation.training_type_id,
                )
            )
            if terminal_context in latest_terminal_contexts:
                continue
            latest_terminal_contexts.add(terminal_context)
        receipts.append(
            _reservation_receipt(
                reservation=reservation,
                payment_method="sbp",
                actor_role=actor_role,
            )
        )
    covered_enrollment_ids = {
        receipt["enrollment_id"]
        for receipt in receipts
        if receipt["enrollment_id"] is not None
    }
    entitlement_enrollments = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_related(
            "schedule__trainer",
            "schedule__location",
            "schedule__training_type",
        )
        .filter(
            student_id=student_id,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            schedule__training_type__kind=TrainingType.Kind.PERSONAL,
            status__in=[ScheduleEnrollment.Status.ACTIVE, ScheduleEnrollment.Status.TRIAL],
        )
        .order_by("-created_at", "-id")
    )
    if trainer_id is not None:
        entitlement_enrollments = entitlement_enrollments.filter(schedule__trainer_id=trainer_id)
    for enrollment in entitlement_enrollments:
        if enrollment.id not in covered_enrollment_ids:
            receipts.append(_enrollment_entitlement_receipt(enrollment=enrollment))
    terminal_entitlement_events = (
        ScheduleBookingEvent.objects.for_club(club_id)
        .select_related(
            "enrollment__schedule__trainer",
            "enrollment__schedule__location",
            "enrollment__schedule__training_type",
            "enrollment__schedule__club",
        )
        .filter(
            student_id=student_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
            enrollment__created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            enrollment__schedule__training_type__kind=TrainingType.Kind.PERSONAL,
        )
        .order_by("-created_at", "-id")
    )
    seen_terminal_entitlement_enrollments: set[int] = set()
    for event in terminal_entitlement_events:
        if (
            event.enrollment_id in covered_enrollment_ids
            or event.enrollment_id in seen_terminal_entitlement_enrollments
        ):
            continue
        seen_terminal_entitlement_enrollments.add(event.enrollment_id)
        receipts.append(_enrollment_entitlement_receipt(enrollment=event.enrollment, terminal_event=event))
    receipts.extend(
        _group_and_renewal_commercial_context(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
        )
    )
    return receipts


def _group_and_renewal_commercial_context(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int | None,
) -> list[dict]:
    """Project only contextual group/renewal families onto one staff card."""

    online_retry_available = get_online_payment_capability().enabled
    payments = (
        Payment.objects.for_club(club_id)
        .select_related(
            "tariff",
            "subscription__renewed_from",
            "target_training_group",
            "seller_trainer",
            "package_owner_trainer",
        )
        .filter(student_id=student_id)
        .filter(
            Q(target_training_group__isnull=False)
            | Q(subscription__renewed_from__isnull=False)
        )
        .order_by("-created_at", "-id")
    )
    if trainer_id is not None:
        payments = payments.filter(
            Q(target_trainer_id_snapshot=trainer_id)
            | Q(seller_trainer_id=trainer_id)
            | Q(package_owner_trainer_id=trainer_id)
            # The caller's student/lead scope was proven by the enclosing
            # context endpoint.  A source-only renewal has no group/seller
            # snapshot, so retain it without turning this into a finance list.
            | Q(subscription__renewed_from__isnull=False)
        )
    payment_list = list(payments)
    order_by_payment_id: dict[int, BankPaymentOrder] = {}
    for order in (
        BankPaymentOrder.objects.for_club(club_id)
        .filter(payment_id__in=[payment.id for payment in payment_list])
        .order_by("payment_id", "-created_at", "-id")
    ):
        order_by_payment_id.setdefault(order.payment_id, order)
    live_order_statuses = {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.AUTHORIZED,
        BankPaymentOrder.Status.MANUAL_REVIEW,
    }
    terminal_by_context: set[tuple] = set()
    receipts: list[dict] = []
    for payment in payment_list:
        order = order_by_payment_id.get(payment.id)
        order_is_actor_allowed = order is not None and (
            trainer_id is None or order.source == BankPaymentOrder.Source.TRAINER
        )
        kind = "renewal" if payment.subscription and payment.subscription.renewed_from_id else "group_sale"
        context = (
            kind,
            payment.subscription.renewed_from_id if kind == "renewal" else payment.target_training_group_id,
            payment.target_schedule_id if kind == "group_sale" else None,
            payment.target_start_date if kind == "group_sale" else None,
        )
        status = order.status if order is not None else payment.status
        live = payment.status == Payment.Status.PENDING and (
            order is None or order.status in live_order_statuses
        )
        if not live:
            if context in terminal_by_context:
                continue
            terminal_by_context.add(context)

        source = payment.subscription.renewed_from if kind == "renewal" else None
        terminal_actionable = False
        if not live and status in {
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.CANCELLED,
            BankPaymentOrder.Status.EXPIRED,
            Payment.Status.REJECTED,
        }:
            if kind == "renewal":
                terminal_actionable = bool(
                    source is not None
                    and source.deleted_at is None
                    and source.status
                    not in {"cancelled", "frozen", "pending"}
                    and (order is None or order_is_actor_allowed)
                )
            else:
                terminal_actionable = bool(
                    payment.target_start_date is not None
                    and payment.target_start_date >= club_localdate_by_id(club_id)
                    and (order is None or order_is_actor_allowed)
                )
        if live and order_is_actor_allowed and order.status in {
            BankPaymentOrder.Status.CREATED,
            BankPaymentOrder.Status.PENDING,
            BankPaymentOrder.Status.AUTHORIZED,
        }:
            allowed_actions = ["open_bank_payment_order"]
        elif (
            terminal_actionable
            and order_is_actor_allowed
            and order.status
            in {
                BankPaymentOrder.Status.FAILED,
                BankPaymentOrder.Status.CANCELLED,
                BankPaymentOrder.Status.EXPIRED,
            }
            and online_retry_available
        ):
            allowed_actions = ["retry_bank_payment"]
        elif terminal_actionable and order is None and payment.payment_method in {
            Payment.Method.CASH,
            Payment.Method.TRANSFER,
        }:
            allowed_actions = ["create_renewal" if kind == "renewal" else "create_group_sale"]
        elif payment.status == Payment.Status.CONFIRMED and kind == "renewal":
            allowed_actions = ["view_subscription"] if trainer_id is None else []
        elif live and order is None and trainer_id is None:
            allowed_actions = ["view_payment"]
        else:
            allowed_actions = []
        renewal_target_tariff_id = None
        renewal_target_tariff_name = ""
        renewal_target_price = None
        if kind == "renewal" and terminal_actionable and source is not None:
            try:
                from apps.billing.service_modules.renewals import get_renewal_offer

                renewal_offer = get_renewal_offer(
                    club_id=club_id,
                    source_tariff=source.tariff,
                )
            except (BusinessLogicError, Tariff.DoesNotExist):
                renewal_offer = None
            if renewal_offer is not None and renewal_offer.is_available:
                renewal_target_tariff_id = renewal_offer.target_tariff_id
                renewal_target_tariff_name = renewal_offer.target_tariff_name
                renewal_target_price = renewal_offer.target_price
        resource_route = ""
        if allowed_actions == ["view_subscription"]:
            resource_route = f"/api/billing/subscriptions/{payment.subscription_id}/"
        elif allowed_actions == ["view_payment"]:
            resource_route = f"/api/billing/payments/{payment.id}/"
        elif (
            allowed_actions
            and allowed_actions[0] == "open_bank_payment_order"
            and order_is_actor_allowed
        ):
            resource_route = f"/api/billing/bank-payment-orders/{order.id}/"
        receipts.append(
            {
                "kind": kind,
                "booking_id": None,
                "reservation_id": None,
                "payment_id": payment.id,
                "subscription_id": payment.subscription_id,
                "renewed_from_subscription_id": source.id if source is not None else None,
                "bank_payment_order_id": order.id if order_is_actor_allowed else None,
                "debt_id": None,
                "slot_id": None,
                "schedule_id": payment.target_schedule_id,
                "training_group_id": payment.target_training_group_id,
                "group_membership_id": (
                    payment.conversion_group_membership_id or payment.target_group_membership_id
                ),
                "group_name": payment.target_group_name_snapshot,
                "target_start_date": payment.target_start_date,
                "enrollment_id": payment.conversion_enrollment_id,
                "starts_at": None,
                "ends_at": None,
                "trainer_id": payment.target_trainer_id_snapshot or 0,
                "trainer_name": payment.target_trainer_name_snapshot,
                "location_id": payment.target_location_id_snapshot or 0,
                "location_name": payment.target_location_name_snapshot,
                "training_type_id": payment.target_training_type_id_snapshot or payment.tariff.training_type_id,
                "training_type_name": payment.tariff.training_type.name,
                "tariff_id": payment.tariff_id,
                "tariff_name": (
                    payment.renewal_source_tariff_name_snapshot
                    if kind == "renewal" and payment.renewal_source_tariff_name_snapshot
                    else payment.tariff.name
                ),
                "renewal_target_tariff_id": renewal_target_tariff_id,
                "renewal_target_tariff_name": renewal_target_tariff_name,
                "renewal_target_price": renewal_target_price,
                "amount": str(payment.amount),
                "payment_method": "sbp" if order is not None else payment.payment_method,
                "status": status,
                "provider_payment_url": (
                    order.provider_payment_url
                    if order_is_actor_allowed and allowed_actions == ["open_bank_payment_order"]
                    else ""
                ),
                "allowed_actions": allowed_actions,
                "resource_route": resource_route,
                "attempted_at": order.created_at if order_is_actor_allowed else payment.created_at,
            }
        )
    return receipts


def submit_staff_personal_intent(
    *,
    club_id: int,
    slot_id: int,
    student_id: int,
    payment_method: str,
    subscription_id: int | None,
    offer_digest: str | None,
    idempotency_key: str,
    actor_user_id: int,
    bank_source: str,
    discount_id: int | None = None,
    v2_manual_admission_command: bool = False,
    command_protocol_version: str | None = None,
) -> StaffPersonalIntentResult:
    """Create one server-derived personal intent from a published slot only."""

    effective_protocol_version = command_protocol_version or (
        "v2" if v2_manual_admission_command else None
    )

    if payment_method not in STAFF_PERSONAL_PAYMENT_METHODS:
        raise BusinessLogicError("Unsupported personal payment method", code="personal_payment_method_invalid")
    command_key = idempotency_key.strip()
    if not command_key or len(command_key) > 120:
        raise BusinessLogicError("A valid idempotency key is required", code="idempotency_key_required")
    command = _claim_staff_command(
        club_id=club_id,
        command_key=command_key,
        payment_method=payment_method,
        shape={
            "kind": "slot",
            "slot_id": slot_id,
            "student_id": student_id,
            "payment_method": payment_method,
            "subscription_id": subscription_id,
            "offer_digest": offer_digest or "",
            "discount_id": discount_id,
        },
    )
    replay = _replay_staff_command(command)
    if replay is not None:
        return replay
    command_slot = _slot_or_raise(club_id=club_id, slot_id=slot_id)
    if command_slot.training_type.kind != TrainingType.Kind.PERSONAL:
        raise BusinessLogicError(
            "Staff personal intent requires a personal training type",
            code="personal_staff_intent_mini_group_forbidden",
        )
    if payment_method == "entitlement":
        if discount_id is not None:
            raise BusinessLogicError(
                "Entitlement booking does not accept a discount",
                code="personal_discount_forbidden",
            )
        if offer_digest:
            raise BusinessLogicError(
                "Entitlement booking does not accept an offer digest",
                code="personal_offer_digest_forbidden",
            )
    elif not (offer_digest or "").strip():
        raise BusinessLogicError("A displayed personal offer is required", code="personal_offer_digest_required")

    # Replay must survive the slot transition to held/booked.  We only return
    # authoritative artifacts with the matching command shape; a reused key
    # never starts a second method family.
    if payment_method == "entitlement":
        # Entitlement booking intentionally has no Payment/DropInBooking
        # family.  Its immutable command evidence is the booking event, so it
        # must be consulted before the published-slot gate on a retry.
        existing_entitlement_event = (
            ScheduleBookingEvent.objects.for_club(club_id)
            .select_related("enrollment", "schedule")
            .filter(
                event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
                metadata__idempotency_key=command_key,
            )
            .order_by("id")
            .first()
        )
        if existing_entitlement_event is not None:
            slot = _slot_or_raise(club_id=club_id, slot_id=slot_id)
            if (
                existing_entitlement_event.student_id != student_id
                or existing_entitlement_event.metadata.get("availability_slot_id") != slot.id
                or existing_entitlement_event.metadata.get("subscription_id") != subscription_id
            ):
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            return _staff_result(command=command, result=StaffPersonalIntentResult(
                receipt=_enrollment_entitlement_receipt(
                    enrollment=existing_entitlement_event.enrollment,
                    terminal_event=_latest_entitlement_terminal_event(
                        enrollment=existing_entitlement_event.enrollment
                    ),
                ),
                created=False,
            ))
    existing_booking = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(idempotency_key=command_key)
        .first()
    )
    if existing_booking is not None:
        if payment_method == "sbp" or existing_booking.enrollment.student_id != student_id:
            raise BusinessLogicError(
                "Idempotency key was already used for another command",
                code="idempotency_conflict",
            )
        if _booking_origin_slot_id(booking=existing_booking) != slot_id:
            raise BusinessLogicError(
                "Idempotency key was already used for another command",
                code="idempotency_conflict",
            )
        existing_link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment")
            .filter(booking_id=existing_booking.id, idempotency_key=command_key)
            .first()
        )
        if existing_link is None and payment_method != "pay_at_visit":
            raise BusinessLogicError(
                "Idempotency key was already used for another command",
                code="idempotency_conflict",
            )
        if existing_link is not None and existing_link.payment.payment_method != payment_method:
            raise BusinessLogicError(
                "Idempotency key was already used for another command",
                code="idempotency_conflict",
            )
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_booking_receipt_for_command(command=command, booking=existing_booking),
            created=False,
        ))
    existing_reservation = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(idempotency_key=command_key)
        .first()
    )
    if existing_reservation is not None:
        if (
            payment_method != "sbp"
            or existing_reservation.student_id != student_id
            or existing_reservation.availability_slot_id != slot_id
        ):
            raise BusinessLogicError(
                "Idempotency key was already used for another command",
                code="idempotency_conflict",
            )
        # A provider crash can leave a durable order claim before its
        # reservation attachment.  Same-key replay is the recovery command;
        # let the reservation owner attach the existing order without a new
        # provider dispatch.
        if existing_reservation.bank_payment_order_id is None:
            slot = _slot_or_raise(club_id=club_id, slot_id=slot_id)
            existing_reservation = create_personal_booking_payment_reservation(
                club_id=club_id,
                student_id=student_id,
                trainer_id=slot.trainer_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                location_id=slot.location_id,
                training_type_id=slot.training_type_id,
                tariff_id=None,
                availability_slot_id=slot.id,
                offer_digest=offer_digest,
                created_by_id=actor_user_id,
                source=bank_source,
                idempotency_key=command_key,
                command_idempotency_key=command_key,
            )
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_reservation_receipt(reservation=existing_reservation, payment_method=payment_method),
            created=False,
        ))

    if not is_unified_client_journey_enabled(club=club_id):
        raise BusinessLogicError(
            "Unified client journey is disabled for this club",
            code="unified_client_journey_disabled",
        )

    # Online reservation creation commits its durable claim before the provider
    # link dispatch.  Do not wrap it in the manual booking transaction: a
    # provider-side effect must never be rolled back locally and recreated as a
    # second order after a worker crash.
    if payment_method == "sbp":
        slot = _slot_or_raise(club_id=club_id, slot_id=slot_id)
        existing_reservation_id = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(idempotency_key=command_key)
            .values_list("id", flat=True)
            .first()
        )
        def invoke_reservation_owner():
            return create_personal_booking_payment_reservation(
                club_id=club_id,
                student_id=student_id,
                trainer_id=slot.trainer_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                location_id=slot.location_id,
                training_type_id=slot.training_type_id,
                tariff_id=None,
                availability_slot_id=slot.id,
                offer_digest=offer_digest,
                discount_id=discount_id,
                created_by_id=actor_user_id,
                source=bank_source,
                idempotency_key=command_key,
                command_idempotency_key=command_key,
                _locked_pre_create_validator=(
                    lambda: _validate_personal_command_protocol_locked(
                        club_id=club_id,
                        expected_version=effective_protocol_version,
                        payment_method=payment_method,
                    )
                    if effective_protocol_version is not None
                    else None
                ),
            )

        attachment_replayed = False
        try:
            reservation = invoke_reservation_owner()
        except BusinessLogicError as exc:
            if exc.code != "personal_payment_reservation_attachment_retry":
                raise
            replay = _replay_staff_command(command)
            if replay is not None:
                return replay
            attachment_replayed = True
            reservation = invoke_reservation_owner()
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_reservation_receipt(reservation=reservation, payment_method=payment_method),
            created=existing_reservation_id is None and not attachment_replayed,
        ))

    # Cash/transfer and pay-at-visit are one local command.  The underlying
    # service transactions are savepoints, so a failed manual financial family
    # rolls back the booking/slot/terms too instead of leaving an orphan.
    # Do not lock the slot here: acceptance services acquire catalog,
    # trainer, student, then the exact attendance root in D12 order.
    with transaction.atomic():
        # SBP reservations use Club as the command-arbitration prefix before
        # catalog locks. Manual/entitlement owners must enter through the same
        # prefix so their deferred tenant FKs cannot invert at commit.
        Club.objects.select_for_update(of=("self",)).get(id=club_id)
        v2_manual_admission_allowed = False
        if effective_protocol_version is not None:
            locked_replay = _replay_staff_command(command)
            if locked_replay is not None:
                return locked_replay
            v2_manual_admission_allowed = _validate_personal_command_protocol_locked(
                club_id=club_id,
                expected_version=effective_protocol_version,
                payment_method=payment_method,
            )
        slot = _slot_or_raise(club_id=club_id, slot_id=slot_id)
        if slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
            raise BusinessLogicError("Personal availability slot is not published", code="personal_slot_not_available")

        if payment_method == "entitlement":
            result = book_personal_availability_slot(
                club_id=club_id,
                slot_id=slot_id,
                student_id=student_id,
                actor_user_id=actor_user_id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                subscription_id=subscription_id,
                idempotency_key=command_key,
            )
            return _staff_result(command=command, result=StaffPersonalIntentResult(
                receipt=_entitlement_receipt(result=result, slot=slot),
                created=result.created,
            ))

        booking_result = book_personal_drop_in(
            club_id=club_id,
            student_id=student_id,
            trainer_id=slot.trainer_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            location_id=slot.location_id,
            training_type_id=slot.training_type_id,
            tariff_id=None,
            actor_user_id=actor_user_id,
            availability_slot_id=slot.id,
            offer_digest=offer_digest,
            discount_id=discount_id,
            idempotency_key=command_key,
        )
        booking = booking_result.booking
        if payment_method in {"cash", "transfer"}:
            create_personal_drop_in_payment(
                club_id=club_id,
                booking_id=booking.id,
                payment_method=payment_method,
                created_by_id=actor_user_id,
                discount_ids=[],
                idempotency_key=command_key,
                v2_manual_admission_allowed=v2_manual_admission_allowed,
            )
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_booking_receipt(booking=booking, payment_method=payment_method),
            created=booking_result.created,
        ))


def submit_staff_direct_personal_intent(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    starts_at: datetime,
    ends_at: datetime,
    location_id: int,
    training_type_id: int,
    payment_method: str,
    subscription_id: int | None,
    offer_digest: str | None,
    idempotency_key: str,
    actor_user_id: int,
    bank_source: str,
    discount_id: int | None = None,
    v2_manual_admission_command: bool = False,
    command_protocol_version: str | None = None,
) -> StaffPersonalIntentResult:
    """Create a staff personal intent for an exact trainer/time, not a slot.

    This is deliberately a separate command from slot booking.  The preview
    digest binds the trainer/time/location/type and designated offer version;
    a client cannot turn an arbitrary direct calendar cell into a paid booking.
    """
    effective_protocol_version = command_protocol_version or (
        "v2" if v2_manual_admission_command else None
    )
    if payment_method not in STAFF_PERSONAL_PAYMENT_METHODS:
        raise BusinessLogicError("Unsupported personal payment method", code="personal_payment_method_invalid")
    command_key = idempotency_key.strip()
    if not command_key or len(command_key) > 120:
        raise BusinessLogicError("A valid idempotency key is required", code="idempotency_key_required")
    # Replays are reads of their immutable artifacts.  They intentionally do
    # not consult today's catalog/default flag before returning a receipt.
    club = Club.objects.only("id", "timezone").filter(id=club_id).first()
    if club is None:
        raise BusinessLogicError("Club was not found", code="club_not_found")
    club_tz = club_zoneinfo(club)
    local_starts_at = (
        timezone.make_aware(starts_at, club_tz)
        if timezone.is_naive(starts_at)
        else timezone.localtime(starts_at, club_tz)
    )
    local_ends_at = (
        timezone.make_aware(ends_at, club_tz)
        if timezone.is_naive(ends_at)
        else timezone.localtime(ends_at, club_tz)
    )
    command = _claim_staff_command(
        club_id=club_id,
        command_key=command_key,
        payment_method=payment_method,
        shape={
            "kind": "direct",
            "student_id": student_id,
            "trainer_id": trainer_id,
            "starts_at": local_starts_at.isoformat(),
            "ends_at": local_ends_at.isoformat(),
            "location_id": location_id,
            "training_type_id": training_type_id,
            "payment_method": payment_method,
            "subscription_id": subscription_id,
            "offer_digest": offer_digest or "",
            "discount_id": discount_id,
        },
    )
    replay = _replay_staff_command(command)
    if replay is not None:
        return replay
    existing_booking = PersonalDropInBooking.objects.for_club(club_id).filter(idempotency_key=command_key).first()
    if existing_booking is not None:
        schedule = existing_booking.enrollment.schedule
        if (
            existing_booking.enrollment.student_id != student_id
            or schedule.trainer_id != trainer_id
            or schedule.location_id != location_id
            or schedule.training_type_id != training_type_id
            or schedule.one_time_date != local_starts_at.date()
            or schedule.start_time != local_starts_at.timetz().replace(tzinfo=None)
            or schedule.end_time != local_ends_at.timetz().replace(tzinfo=None)
        ):
            raise BusinessLogicError("Idempotency key is already used for another command", code="idempotency_conflict")
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment")
            .filter(booking_id=existing_booking.id, idempotency_key=command_key)
            .first()
        )
        if (
            payment_method == "sbp"
            or (link is None and payment_method != "pay_at_visit")
            or (link is not None and link.payment.payment_method != payment_method)
        ):
            raise BusinessLogicError("Idempotency key is already used for another command", code="idempotency_conflict")
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_booking_receipt_for_command(command=command, booking=existing_booking),
            created=False,
        ))
    existing_reservation = PersonalBookingPaymentReservation.objects.for_club(club_id).filter(
        idempotency_key=command_key
    ).first()
    if existing_reservation is not None:
        if (
            payment_method != "sbp"
            or existing_reservation.student_id != student_id
            or existing_reservation.trainer_id != trainer_id
            or existing_reservation.location_id != location_id
            or existing_reservation.training_type_id != training_type_id
            or existing_reservation.starts_at != local_starts_at
            or existing_reservation.ends_at != local_ends_at
            or existing_reservation.availability_slot_id is not None
        ):
            raise BusinessLogicError("Idempotency key is already used for another command", code="idempotency_conflict")
        if existing_reservation.bank_payment_order_id is None:
            existing_reservation = create_personal_booking_payment_reservation(
                club_id=club_id,
                student_id=student_id,
                trainer_id=trainer_id,
                starts_at=local_starts_at,
                ends_at=local_ends_at,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=None,
                availability_slot_id=None,
                offer_digest=offer_digest,
                created_by_id=actor_user_id,
                source=bank_source,
                idempotency_key=command_key,
                command_idempotency_key=command_key,
            )
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_reservation_receipt(reservation=existing_reservation, payment_method=payment_method),
            created=False,
        ))

    if not is_unified_client_journey_enabled(club=club_id):
        raise BusinessLogicError(
            "Unified client journey is disabled for this club",
            code="unified_client_journey_disabled",
        )

    if payment_method == "entitlement":
        if discount_id is not None:
            raise BusinessLogicError(
                "Entitlement booking does not accept a discount",
                code="personal_discount_forbidden",
            )
        if offer_digest:
            raise BusinessLogicError(
                "Entitlement booking does not accept an offer digest",
                code="personal_offer_digest_forbidden",
            )
        training_type = TrainingType.objects.for_club(club_id).filter(
            id=training_type_id,
            is_active=True,
        ).first()
        if training_type is None:
            raise BusinessLogicError(
                "Training type does not belong to this club",
                code="training_type_club_mismatch",
            )
        if training_type.kind != TrainingType.Kind.PERSONAL:
            raise BusinessLogicError(
                "Staff personal intents do not support mini-groups",
                code="personal_direct_mini_group_forbidden",
            )
    else:
        # This validation is the authoritative digest source for a new paid
        # mode. Entitlement never needs a currently-designated paid tariff.
        preview = get_staff_direct_personal_offer(
            club_id=club_id,
            trainer_id=trainer_id,
            starts_at=local_starts_at,
            ends_at=local_ends_at,
            location_id=location_id,
            training_type_id=training_type_id,
            discount_id=discount_id,
        )
        if offer_digest == preview["offer_digest"]:
            preview = None
    if payment_method != "entitlement" and preview is not None:
        error = BusinessLogicError(
            "The personal offer changed; refresh before booking",
            code="personal_offer_changed",
        )
        error.safe_payload = {"current_offer": preview}
        raise error

    # This service commits the durable reservation before dispatching provider
    # I/O.  Do not wrap this branch in the local cash/transfer transaction.
    if payment_method == "sbp":
        def invoke_reservation_owner():
            return create_personal_booking_payment_reservation(
                club_id=club_id,
                student_id=student_id,
                trainer_id=trainer_id,
                starts_at=local_starts_at,
                ends_at=local_ends_at,
                location_id=location_id,
                training_type_id=training_type_id,
                tariff_id=None,
                availability_slot_id=None,
                offer_digest=offer_digest,
                discount_id=discount_id,
                created_by_id=actor_user_id,
                source=bank_source,
                idempotency_key=command_key,
                command_idempotency_key=command_key,
                _locked_pre_create_validator=(
                    lambda: _validate_personal_command_protocol_locked(
                        club_id=club_id,
                        expected_version=effective_protocol_version,
                        payment_method=payment_method,
                    )
                    if effective_protocol_version is not None
                    else None
                ),
            )

        attachment_replayed = False
        try:
            reservation = invoke_reservation_owner()
        except BusinessLogicError as exc:
            if exc.code != "personal_payment_reservation_attachment_retry":
                raise
            replay = _replay_staff_command(command)
            if replay is not None:
                return replay
            attachment_replayed = True
            reservation = invoke_reservation_owner()
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_reservation_receipt(reservation=reservation, payment_method=payment_method),
            created=not attachment_replayed,
        ))

    with transaction.atomic():
        # Match the slot-based staff owner and SBP reservation lock prefix.
        Club.objects.select_for_update(of=("self",)).get(id=club_id)
        v2_manual_admission_allowed = False
        if effective_protocol_version is not None:
            locked_replay = _replay_staff_command(command)
            if locked_replay is not None:
                return locked_replay
            v2_manual_admission_allowed = _validate_personal_command_protocol_locked(
                club_id=club_id,
                expected_version=effective_protocol_version,
                payment_method=payment_method,
            )
        if payment_method == "entitlement":
            result = book_personal_session(
                club_id=club_id,
                student_id=student_id,
                trainer_id=trainer_id,
                starts_at=local_starts_at,
                ends_at=local_ends_at,
                location_id=location_id,
                training_type_id=training_type_id,
                subscription_id=subscription_id,
                actor_user_id=actor_user_id,
                idempotency_key=command_key,
                enforce_entitlement_capacity=True,
            )
            return _staff_result(
                command=command,
                result=StaffPersonalIntentResult(
                    receipt=_direct_entitlement_receipt(result=result), created=result.created
                ),
            )
        booking_result = book_personal_drop_in(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
            starts_at=local_starts_at,
            ends_at=local_ends_at,
            location_id=location_id,
            training_type_id=training_type_id,
            tariff_id=None,
            actor_user_id=actor_user_id,
            availability_slot_id=None,
            offer_digest=offer_digest,
            discount_id=discount_id,
            idempotency_key=command_key,
        )
        if payment_method in {"cash", "transfer"}:
            create_personal_drop_in_payment(
                club_id=club_id,
                booking_id=booking_result.booking.id,
                payment_method=payment_method,
                created_by_id=actor_user_id,
                discount_ids=[],
                idempotency_key=command_key,
                v2_manual_admission_allowed=v2_manual_admission_allowed,
            )
        return _staff_result(command=command, result=StaffPersonalIntentResult(
            receipt=_booking_receipt(booking=booking_result.booking, payment_method=payment_method),
            created=booking_result.created,
        ))
