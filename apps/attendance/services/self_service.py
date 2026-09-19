"""Capability-on personal self-service coordinator.

This module is intentionally an identity/read bridge.  It never owns a
financial lifecycle: entitlement bookings stay in ``enrollment`` and SBP
reservations/orders stay in their existing attendance/billing owners.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalSelfServiceCommand,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.services.enrollment import (
    PersonalSessionBooking,
    book_personal_availability_slot,
    create_personal_booking_payment_reservation,
)
from apps.billing.models import BankPaymentOrder
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import Club
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError


@dataclass(frozen=True)
class SelfServicePersonalCommandResult:
    command: PersonalSelfServiceCommand
    created: bool


def _clean_command_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise BusinessLogicError(
            "A stable idempotency key is required for personal self-service.",
            code="idempotency_key_required",
        )
    if len(key) > 120:
        raise BusinessLogicError(
            "Idempotency key is too long.",
            code="idempotency_key_invalid",
        )
    return key


def _clean_offer_digest(value: str | None) -> str:
    digest = (value or "").strip()
    if digest and len(digest) != 64:
        raise BusinessLogicError(
            "Personal offer digest is invalid.",
            code="personal_offer_changed",
        )
    return digest


def _assert_existing_command_matches(
    *,
    command: PersonalSelfServiceCommand,
    actor_user_id: int,
    source: str,
    student_id: int,
    slot_id: int,
    offer_digest: str,
) -> bool:
    if (
        command.actor_id != actor_user_id
        or command.source != source
        or command.student_id != student_id
        or command.availability_slot_id != slot_id
        or command.offer_digest != offer_digest
    ):
        raise BusinessLogicError(
            "Idempotency key is already used for another personal self-service command.",
            code="idempotency_conflict",
        )
    return command.result_bound_at is not None


def _command_fingerprint(
    *,
    actor_user_id: int,
    source: str,
    student_id: int,
    slot_id: int,
    action: str,
    offer_digest: str,
) -> str:
    material = "|".join(
        (
            "personal-self-service-v1",
            str(actor_user_id),
            source,
            str(student_id),
            str(slot_id),
            action,
            offer_digest,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _resolve_current_option(*, club, student_id: int, slot_id: int) -> dict:
    """Resolve one fresh, personal-only capability without trusting the client."""

    slot = (
        PersonalAvailabilitySlot.objects.for_club(club)
        .select_related("training_type")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise BusinessLogicError(
            "Personal availability slot was not found.",
            code="personal_availability_slot_not_found",
        )
    if slot.training_type.kind != "personal":
        raise BusinessLogicError(
            "Mini-group slots are not available through personal self-service.",
            code="personal_self_service_mini_group_forbidden",
        )

    from apps.attendance.selectors import get_unified_self_service_personal_options

    for option in get_unified_self_service_personal_options(
        club=club,
        student_id=student_id,
        target_date=timezone.localtime(slot.starts_at, club_zoneinfo(club)).date(),
    ):
        if option["slot_id"] == slot_id:
            return option
    raise BusinessLogicError(
        "Personal availability slot is not available.",
        code="personal_availability_slot_unavailable",
    )


def _bind_self_service_command(
    *,
    command: PersonalSelfServiceCommand,
    enrollment_id: int | None = None,
    entitlement_subscription_id: int | None = None,
    entitlement_component_id: int | None = None,
    reservation_id: int | None = None,
) -> tuple[PersonalSelfServiceCommand, bool]:
    """Perform the single permitted unbound-to-bound command transition."""

    with transaction.atomic():
        durable_command = (
            PersonalSelfServiceCommand.objects.for_club(command.club_id)
            .select_for_update(of=("self",))
            .get(id=command.id)
        )
        return _bind_self_service_command_locked(
            command=durable_command,
            enrollment_id=enrollment_id,
            entitlement_subscription_id=entitlement_subscription_id,
            entitlement_component_id=entitlement_component_id,
            reservation_id=reservation_id,
        )


def _bind_self_service_command_locked(
    *,
    command: PersonalSelfServiceCommand,
    enrollment_id: int | None = None,
    entitlement_subscription_id: int | None = None,
    entitlement_component_id: int | None = None,
    reservation_id: int | None = None,
) -> tuple[PersonalSelfServiceCommand, bool]:
    """Bind a command that is already locked by its short recovery transaction."""

    if command.result_bound_at is not None:
        return command, False
    command.enrollment_id_snapshot = enrollment_id
    command.enrollment_id = enrollment_id
    command.entitlement_subscription_id = entitlement_subscription_id
    command.entitlement_component_id = entitlement_component_id
    command.reservation_id_snapshot = reservation_id
    command.result_bound_at = timezone.now()
    command.save(
        update_fields=[
            "enrollment_id_snapshot",
            "reservation_id_snapshot",
            "enrollment",
            "entitlement_subscription",
            "entitlement_component",
            "result_bound_at",
            "updated_at",
        ]
    )
    return command, True


def _claim_self_service_command(
    *,
    club,
    command_key: str,
    command_fingerprint: str,
    actor_user_id: int,
    source: str,
    student_id: int,
    slot_id: int,
    action: str,
    offer_digest: str,
) -> tuple[PersonalSelfServiceCommand, bool]:
    """Commit only the durable key claim before any owner/provider work."""

    try:
        with transaction.atomic():
            # Match personal booking/payment owners before this insert can
            # acquire deferred FK key-share locks at commit.
            Club.objects.select_for_update(of=("self",)).only("id").get(id=club.id)
            return PersonalSelfServiceCommand.objects.for_club(club).create(
                club=club,
                command_key=command_key,
                command_fingerprint=command_fingerprint,
                actor_id=actor_user_id,
                source=source,
                student_id=student_id,
                availability_slot_id=slot_id,
                action=action,
                offer_digest=offer_digest,
            ), True
    except IntegrityError:
        with transaction.atomic():
            Club.objects.select_for_update(of=("self",)).only("id").get(id=club.id)
            command = (
                PersonalSelfServiceCommand.objects.for_club(club)
                .select_for_update(of=("self",))
                .filter(command_key=command_key)
                .first()
            )
            if command is None:
                raise
            _assert_existing_command_matches(
                command=command,
                actor_user_id=actor_user_id,
                source=source,
                student_id=student_id,
                slot_id=slot_id,
                offer_digest=offer_digest,
            )
            if command.action != action or command.command_fingerprint != command_fingerprint:
                raise BusinessLogicError(
                    "Idempotency key is already used for another personal self-service command.",
                    code="idempotency_conflict",
                )
            return command, False


def _has_unbound_entitlement_artifact(*, command: PersonalSelfServiceCommand) -> bool:
    return ScheduleBookingEvent.objects.for_club(command.club_id).filter(
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        student_id=command.student_id,
        metadata__idempotency_key=command.command_key,
        metadata__availability_slot_id=command.availability_slot_id,
    ).exists()


def _discard_new_unbound_command_if_owner_failed(*, club, command: PersonalSelfServiceCommand) -> None:
    """Remove a just-claimed shell only when no owner durable artifact exists.

    The command is intentionally not a payment/reservation authority.  If the
    locked owner revalidation rejects a stale offer or entitlement before it
    commits an event/reservation, retaining a live unbound command would make
    GET render a false, forever-incomplete action.  Existing/concurrent keys
    are never discarded by this helper; only the request that created the
    claim may call it.
    """

    with transaction.atomic():
        durable_command = (
            PersonalSelfServiceCommand.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id=command.id)
            .first()
        )
        if durable_command is None or durable_command.result_bound_at is not None:
            return
        has_booking = _has_unbound_entitlement_artifact(command=durable_command)
        has_reservation = PersonalBookingPaymentReservation.objects.for_club(club).filter(
            idempotency_key=durable_command.command_key,
            student_id=durable_command.student_id,
            availability_slot_id=durable_command.availability_slot_id,
        ).exists()
        if not has_booking and not has_reservation:
            durable_command.delete()


def _recover_unbound_booking_command_for_read(
    *,
    club,
    command: PersonalSelfServiceCommand,
) -> PersonalSelfServiceCommand:
    """Attach committed exact booking evidence without re-running an owner."""

    from apps.billing.models import Subscription, SubscriptionComponent

    with transaction.atomic():
        durable_command = (
            PersonalSelfServiceCommand.objects.for_club(club)
            .select_for_update(of=("self",))
            .get(id=command.id)
        )
        if durable_command.result_bound_at is not None:
            return durable_command
        event = (
            ScheduleBookingEvent.objects.for_club(club)
            .select_for_update(of=("self",))
            .select_related("enrollment")
            .filter(
                event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
                student_id=durable_command.student_id,
                metadata__idempotency_key=durable_command.command_key,
                metadata__availability_slot_id=durable_command.availability_slot_id,
            )
            .order_by("-id")
            .first()
        )
        if event is None:
            return durable_command
        subscription_id = event.metadata.get("subscription_id")
        component_id = event.metadata.get("subscription_component_id")
        if not isinstance(subscription_id, int) or (
            component_id is not None and not isinstance(component_id, int)
        ):
            return durable_command
        subscription = (
            Subscription.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id=subscription_id, student_id=durable_command.student_id)
            .first()
        )
        if subscription is None:
            return durable_command
        if component_id is not None and not SubscriptionComponent.objects.for_club(club).select_for_update(
            of=("self",)
        ).filter(id=component_id, subscription_id=subscription.id).exists():
            return durable_command
        return _bind_self_service_command_locked(
            command=durable_command,
            enrollment_id=event.enrollment_id,
            entitlement_subscription_id=subscription.id,
            entitlement_component_id=component_id,
        )[0]


def _recover_unbound_payment_command_for_read(
    *,
    club,
    command: PersonalSelfServiceCommand,
) -> PersonalSelfServiceCommand:
    """Attach an exact committed SBP reservation without provider I/O."""

    with transaction.atomic():
        durable_command = (
            PersonalSelfServiceCommand.objects.for_club(club)
            .select_for_update(of=("self",))
            .get(id=command.id)
        )
        if durable_command.result_bound_at is not None:
            return durable_command
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(
                idempotency_key=durable_command.command_key,
                student_id=durable_command.student_id,
                availability_slot_id=durable_command.availability_slot_id,
            )
            .first()
        )
        if reservation is None:
            return durable_command
        return _bind_self_service_command_locked(
            command=durable_command,
            reservation_id=reservation.id,
        )[0]


def _recover_unbound_command_for_read(*, club, command: PersonalSelfServiceCommand) -> PersonalSelfServiceCommand:
    """Make GET read committed owner evidence, never an incomplete command shell."""

    if command.result_bound_at is not None:
        return command
    if command.action == PersonalSelfServiceCommand.Action.BOOK:
        return _recover_unbound_booking_command_for_read(club=club, command=command)
    if command.action == PersonalSelfServiceCommand.Action.PAY:
        return _recover_unbound_payment_command_for_read(club=club, command=command)
    return command


def _recover_unbound_payment_reservation(
    *,
    club,
    command: PersonalSelfServiceCommand,
    actor_user_id: int,
    offer_digest: str,
) -> PersonalBookingPaymentReservation | None:
    """Return the exact persisted SBP attempt, completing only its owner link."""

    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(club)
        .filter(
            idempotency_key=command.command_key,
            student_id=command.student_id,
            availability_slot_id=command.availability_slot_id,
        )
        .first()
    )
    if reservation is None:
        return None
    return _create_or_resume_payment_reservation(
        club=club,
        command=command,
        actor_user_id=actor_user_id,
        offer_digest=offer_digest,
    )


def _create_or_resume_payment_reservation(
    *,
    club,
    command: PersonalSelfServiceCommand,
    actor_user_id: int,
    offer_digest: str,
) -> PersonalBookingPaymentReservation | None:
    """Use the existing reservation owner, including one known attach handoff.

    A reservation is committed before its bank-order owner dispatches.  Two
    same-key requests can therefore meet at the small interval where the
    winner's reservation exists but has not yet been attached to the order.
    The owner reports that condition as ``attachment_retry`` to generic
    callers.  A durable command replay instead takes its command/reservation
    locks, re-reads the exact evidence, then makes one owner retry.  This is
    not polling and never sends a raw payload or creates a second lifecycle.
    """

    slot = command.availability_slot

    def invoke_owner() -> PersonalBookingPaymentReservation:
        # The reservation service's existing-key branch validates its own
        # identity and either returns the terminal attempt or resumes
        # attachment of its bank order.  This is intentionally not a new
        # payment lifecycle.
        return create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=command.student_id,
            trainer_id=slot.trainer_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            location_id=slot.location_id,
            training_type_id=slot.training_type_id,
            tariff_id=None,
            availability_slot_id=slot.id,
            offer_digest=offer_digest,
            created_by_id=actor_user_id,
            source=command.source,
            idempotency_key=command.command_key,
            command_idempotency_key=command.command_key,
        )

    try:
        return invoke_owner()
    except BusinessLogicError as exc:
        if exc.code != "personal_payment_reservation_attachment_retry":
            raise

    with transaction.atomic():
        durable_command = (
            PersonalSelfServiceCommand.objects.for_club(club)
            .select_for_update(of=("self",))
            .get(id=command.id)
        )
        if durable_command.result_bound_at is not None:
            return None
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(
                idempotency_key=durable_command.command_key,
                student_id=durable_command.student_id,
                availability_slot_id=durable_command.availability_slot_id,
            )
            .first()
        )
        if reservation is None:
            raise BusinessLogicError(
                "Personal payment reservation recovery evidence is unavailable.",
                code="personal_payment_reservation_attachment_retry",
            )
        if reservation.bank_payment_order_id is not None:
            return reservation

    # The selected reservation is durable, but any provider work stays outside
    # this short lock.  One exact re-entry lets the existing owner attach the
    # winner's financial family or complete it if the winner stopped before
    # dispatch.  A second attachment error is a real owner-level fault.
    return invoke_owner()


def execute_self_service_personal_command(
    *,
    club,
    actor_user_id: int,
    source: str,
    student_id: int,
    slot_id: int,
    idempotency_key: str,
    offer_digest: str | None,
) -> SelfServicePersonalCommandResult:
    """Create or exactly replay one actor-derived entitlement/SBP command."""

    if source not in PersonalSelfServiceCommand.Source.values:
        raise BusinessLogicError("Invalid personal self-service source.", code="personal_self_service_source_invalid")
    command_key = _clean_command_key(idempotency_key)
    supplied_digest = _clean_offer_digest(offer_digest)

    command = PersonalSelfServiceCommand.objects.for_club(club).filter(command_key=command_key).first()
    option = None
    if command is not None:
        if _assert_existing_command_matches(
            command=command,
            actor_user_id=actor_user_id,
            source=source,
            student_id=student_id,
            slot_id=slot_id,
            offer_digest=supplied_digest,
        ):
            return SelfServicePersonalCommandResult(command=command, created=False)
        action = command.action
        claimed_new = False
    else:
        if not is_unified_client_journey_enabled(club=club):
            raise BusinessLogicError(
                "Unified client journey is disabled for this club.",
                code="unified_client_journey_disabled",
            )
        option = _resolve_current_option(club=club, student_id=student_id, slot_id=slot_id)
        action = (
            PersonalSelfServiceCommand.Action.BOOK
            if option["capability"] == "can_book"
            else PersonalSelfServiceCommand.Action.PAY
        )
        if action == PersonalSelfServiceCommand.Action.BOOK:
            if supplied_digest:
                raise BusinessLogicError(
                    "An entitlement booking must not submit a paid offer digest.",
                    code="personal_offer_digest_not_allowed",
                )
        else:
            if not supplied_digest:
                raise BusinessLogicError(
                    "A displayed personal offer is required.",
                    code="personal_offer_digest_required",
                )
            if supplied_digest != option["offer_digest"]:
                raise BusinessLogicError(
                    "The personal offer changed. Refresh the available slots.",
                    code="personal_offer_changed",
                )
        fingerprint = _command_fingerprint(
            actor_user_id=actor_user_id,
            source=source,
            student_id=student_id,
            slot_id=slot_id,
            action=action,
            offer_digest=supplied_digest,
        )
        command, claimed_new = _claim_self_service_command(
            club=club,
            command_key=command_key,
            command_fingerprint=fingerprint,
            actor_user_id=actor_user_id,
            source=source,
            student_id=student_id,
            slot_id=slot_id,
            action=action,
            offer_digest=supplied_digest,
        )
        if command.result_bound_at is not None:
            return SelfServicePersonalCommandResult(command=command, created=False)
    if action == PersonalSelfServiceCommand.Action.PAY:
        try:
            recovered_reservation = _recover_unbound_payment_reservation(
                club=club,
                command=command,
                actor_user_id=actor_user_id,
                offer_digest=supplied_digest,
            )
        except BusinessLogicError:
            if claimed_new:
                _discard_new_unbound_command_if_owner_failed(club=club, command=command)
            raise
        if recovered_reservation is not None:
            bound_command, bound_now = _bind_self_service_command(
                command=command,
                reservation_id=recovered_reservation.id,
            )
            return SelfServicePersonalCommandResult(
                command=bound_command,
                created=bound_now,
            )
    elif not is_unified_client_journey_enabled(club=club) and not _has_unbound_entitlement_artifact(command=command):
        if claimed_new:
            _discard_new_unbound_command_if_owner_failed(club=club, command=command)
        raise BusinessLogicError(
            "Unified client journey is disabled for this club.",
            code="unified_client_journey_disabled",
        )

    if action == PersonalSelfServiceCommand.Action.BOOK:
        if supplied_digest:
            raise BusinessLogicError(
                "An entitlement booking must not submit a paid offer digest.",
                code="personal_offer_digest_not_allowed",
            )
        try:
            booking: PersonalSessionBooking = book_personal_availability_slot(
                club_id=club.id,
                slot_id=slot_id,
                student_id=student_id,
                actor_user_id=actor_user_id,
                origin=(
                    ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
                    if source == PersonalSelfServiceCommand.Source.PARENT
                    else ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
                ),
                subscription_id=None,
                idempotency_key=command_key,
                reserve_self_service_entitlement=True,
                self_service_command_id=command.id,
            )
        except BusinessLogicError:
            if claimed_new:
                _discard_new_unbound_command_if_owner_failed(club=club, command=command)
            raise
        bound_command = PersonalSelfServiceCommand.objects.for_club(club).get(id=command.id)
        return SelfServicePersonalCommandResult(
            command=bound_command,
            created=booking.command_bound,
        )

    if not is_unified_client_journey_enabled(club=club):
        if claimed_new:
            _discard_new_unbound_command_if_owner_failed(club=club, command=command)
        raise BusinessLogicError(
            "Unified client journey is disabled for this club.",
            code="unified_client_journey_disabled",
        )
    try:
        option = _resolve_current_option(club=club, student_id=student_id, slot_id=slot_id)
    except BusinessLogicError:
        if claimed_new:
            _discard_new_unbound_command_if_owner_failed(club=club, command=command)
        raise
    if option["capability"] != "can_pay" or supplied_digest != option["offer_digest"]:
        if claimed_new:
            _discard_new_unbound_command_if_owner_failed(club=club, command=command)
        raise BusinessLogicError(
            "The personal offer changed. Refresh the available slots.",
            code="personal_offer_changed",
        )
    try:
        reservation = _create_or_resume_payment_reservation(
            club=club,
            command=command,
            actor_user_id=actor_user_id,
            offer_digest=supplied_digest,
        )
    except BusinessLogicError:
        if claimed_new:
            _discard_new_unbound_command_if_owner_failed(club=club, command=command)
        raise
    if reservation is None:
        command = PersonalSelfServiceCommand.objects.for_club(club).get(id=command.id)
        return SelfServicePersonalCommandResult(command=command, created=False)
    bound_command, bound_now = _bind_self_service_command(command=command, reservation_id=reservation.id)
    return SelfServicePersonalCommandResult(command=bound_command, created=bound_now)


def get_self_service_personal_commands(
    *,
    club,
    actor_user_id: int,
    student_id: int,
    source: str,
) -> list[PersonalSelfServiceCommand]:
    commands = list(
        PersonalSelfServiceCommand.objects.for_club(club)
        .filter(actor_id=actor_user_id, student_id=student_id, source=source)
        .select_related("availability_slot")
        .order_by("-created_at", "-id")
    )
    return [_recover_unbound_command_for_read(club=club, command=command) for command in commands]


def get_self_service_personal_command(
    *,
    club,
    actor_user_id: int,
    student_id: int,
    source: str,
    command_id: int,
) -> PersonalSelfServiceCommand | None:
    command = (
        PersonalSelfServiceCommand.objects.for_club(club)
        .filter(id=command_id, actor_id=actor_user_id, student_id=student_id, source=source)
        .select_related("availability_slot")
        .first()
    )
    if command is None:
        return None
    return _recover_unbound_command_for_read(club=club, command=command)


def _reservation_for_command(*, club, command: PersonalSelfServiceCommand):
    if command.reservation_id_snapshot is None:
        return None
    return (
        PersonalBookingPaymentReservation.objects.for_club(club)
        .select_related("bank_payment_order", "payment", "subscription", "enrollment")
        .filter(id=command.reservation_id_snapshot, student_id=command.student_id)
        .first()
    )


def _enrollment_for_command(*, club, command: PersonalSelfServiceCommand):
    if command.enrollment_id_snapshot is None:
        return None
    return (
        ScheduleEnrollment.objects.for_club(club)
        .select_related("schedule")
        .filter(id=command.enrollment_id_snapshot)
        .first()
    )


def _can_cancel_order(*, order: BankPaymentOrder, source: str) -> bool:
    if order.source != source or order.status not in {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.AUTHORIZED,
    }:
        return False
    from apps.billing.service_modules.bank_orders import provider_dispatch_blocks_cancellation

    return not provider_dispatch_blocks_cancellation(order)


def self_service_personal_command_card(*, club, command: PersonalSelfServiceCommand) -> dict:
    """Present a durable command without inferring payment confirmation."""

    reservation = _reservation_for_command(club=club, command=command)
    enrollment = _enrollment_for_command(club=club, command=command)
    slot = command.availability_slot
    allowed_actions: list[str] = []
    order = reservation.bank_payment_order if reservation is not None else None
    # A command never grants visibility into an order merely because a corrupt
    # or manual pointer happens to target its reservation.  The persisted
    # actor/source scope and the billing source must agree at presentation too.
    if order is not None and order.source != command.source:
        order = None
    booking_id = enrollment.id if enrollment is not None else None
    if booking_id is None and reservation is not None:
        booking_id = reservation.enrollment_id
    booking_enrollment = enrollment or (reservation.enrollment if reservation is not None else None)
    effective_booking_starts_at = None
    effective_booking_ends_at = None
    effective_slot_id = command.availability_slot_id
    if booking_enrollment is not None and booking_enrollment.schedule.one_time_date is not None:
        schedule = booking_enrollment.schedule
        zone = club_zoneinfo(schedule.club)
        effective_booking_starts_at = timezone.make_aware(
            datetime.combine(schedule.one_time_date, schedule.start_time),
            zone,
        )
        effective_booking_ends_at = timezone.make_aware(
            datetime.combine(schedule.one_time_date, schedule.end_time),
            zone,
        )
        effective_slot_id = (
            PersonalAvailabilitySlot.objects.for_club(club)
            .filter(booked_enrollment_id=booking_enrollment.id)
            .order_by("id")
            .values_list("id", flat=True)
            .first()
        )
    booking_cancelled = bool(
        booking_enrollment is not None
        and booking_enrollment.status == ScheduleEnrollment.Status.CANCELLED
    )
    terminal_reservation = bool(
        reservation is not None
        and reservation.status
        in {
            PersonalBookingPaymentReservation.Status.CANCELLED,
            PersonalBookingPaymentReservation.Status.EXPIRED,
        }
    )
    if booking_cancelled or terminal_reservation:
        # A retained paid subscription is not a retained one-off booking.  Do
        # not hand cancelled/expired owner evidence back as a viewable live
        # booking, and do not let an approved historical order mask that fact.
        booking_id = None

    if command.action == PersonalSelfServiceCommand.Action.BOOK:
        status = "booked" if enrollment and not booking_cancelled else "cancelled"
        order_status = ""
        if booking_id is not None:
            allowed_actions.append("view_booking")
    elif reservation is None:
        status = "incomplete"
        order_status = ""
    else:
        status = PersonalBookingPaymentReservation.Status.CANCELLED if booking_cancelled else reservation.status
        order_status = (
            ""
            if (booking_cancelled or terminal_reservation)
            else (order.status if order is not None else "")
        )
        if booking_id is not None:
            allowed_actions.append("view_booking")
        if order is not None:
            if (
                order.provider_payment_url
                and reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
                and order.status in {BankPaymentOrder.Status.CREATED, BankPaymentOrder.Status.PENDING}
            ):
                allowed_actions.append("open_bank_payment_order")
            if _can_cancel_order(order=order, source=command.source):
                allowed_actions.append("cancel_bank_payment_order")
        if (
            reservation.status
            in {
                PersonalBookingPaymentReservation.Status.CANCELLED,
                PersonalBookingPaymentReservation.Status.EXPIRED,
            }
            and (order is None or order.status in {
                BankPaymentOrder.Status.CANCELLED,
                BankPaymentOrder.Status.EXPIRED,
                BankPaymentOrder.Status.FAILED,
            })
            and is_unified_client_journey_enabled(club=club)
            and slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
            and slot.starts_at > timezone.now()
        ):
            allowed_actions.append("retry_bank_payment")

    return {
        "command_id": command.id,
        "slot_id": effective_slot_id,
        "capability": "can_book" if command.action == PersonalSelfServiceCommand.Action.BOOK else "can_pay",
        "status": status,
        "starts_at": (
            effective_booking_starts_at
            if effective_booking_starts_at is not None
            else (reservation.starts_at if reservation is not None else slot.starts_at)
        ),
        "ends_at": (
            effective_booking_ends_at
            if effective_booking_ends_at is not None
            else (reservation.ends_at if reservation is not None else slot.ends_at)
        ),
        "booking_id": booking_id,
        "reservation_id": reservation.id if reservation is not None else None,
        "bank_payment_order_id": order.id if order is not None else None,
        "provider_payment_url": (
            order.provider_payment_url if "open_bank_payment_order" in allowed_actions else ""
        ),
        "amount_snapshot": str(order.amount_snapshot) if order is not None else "",
        "order_status": order_status,
        "allowed_actions": allowed_actions,
    }


def self_service_personal_command_collections(*, club, commands: list[PersonalSelfServiceCommand]) -> dict:
    """Return all live attempts and the latest terminal attempt per slot."""

    live: list[dict] = []
    latest_terminal_by_slot: dict[int, dict] = {}
    for command in commands:
        card = self_service_personal_command_card(club=club, command=command)
        terminal = card["status"] in {"cancelled", "expired"}
        if terminal:
            latest_terminal_by_slot.setdefault(command.availability_slot_id, card)
        else:
            live.append(card)
    return {
        "live": live,
        "latest_terminal": list(latest_terminal_by_slot.values()),
    }
