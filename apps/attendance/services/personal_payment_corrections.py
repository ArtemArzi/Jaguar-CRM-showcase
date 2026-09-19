"""Safe, append-only correction of a personal payment method."""

from __future__ import annotations

import hashlib
import json

from django.db import IntegrityError, transaction

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInPaymentLink,
    PersonalPaymentMethodCorrection,
    PersonalServiceTermsSnapshot,
    complete_personal_terms_queryset,
)
from apps.attendance.services.drop_in import (
    book_personal_drop_in_from_frozen_reservation,
    create_personal_drop_in_payment,
    preview_personal_time_conflict_scope,
)
from apps.billing.models import BankPaymentOrder, Payment, Tariff, TrainingType
from apps.billing.service_modules.bank_orders import (
    cancel_bank_payment_order,
    provider_dispatch_blocks_cancellation,
)
from apps.billing.service_modules.payment_review import verify_payment
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer

REPLACEMENT_METHODS = frozenset({Payment.Method.CASH, Payment.Method.TRANSFER, "pay_at_visit"})
OWNER_PAYMENT_CORRECTION_ROLES = frozenset({"owner", "admin"})


def _fingerprint(*, shape: dict) -> str:
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _child_idempotency_key(*, operation: str, parent_key: str) -> str:
    """Derive a bounded stable key without weakening the public 120-char contract."""

    digest = hashlib.sha256(parent_key.encode()).hexdigest()
    return f"personal-correction-{operation}:{digest}"


def _complete_terms_for_reservation(
    *, club_id: int, reservation_id: int
) -> PersonalServiceTermsSnapshot:
    terms = (
        complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(reservation_id=reservation_id)
        )
        .first()
    )
    if terms is None or terms.terms_version != PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2:
        raise BusinessLogicError(
            "Personal payment correction requires complete frozen terms",
            code="personal_payment_replacement_terms_missing",
        )
    return terms


def _complete_terms_for_booking(*, club_id: int, booking_id: int) -> PersonalServiceTermsSnapshot:
    terms = (
        complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booking_id=booking_id)
        )
        .first()
    )
    if terms is None or terms.terms_version != PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2:
        raise BusinessLogicError(
            "Personal payment correction requires complete frozen terms",
            code="personal_payment_replacement_terms_missing",
        )
    return terms


def _replayed_correction_receipt(*, correction: PersonalPaymentMethodCorrection) -> dict:
    if correction.replacement_booking_id is None:
        raise BusinessLogicError(
            "Personal payment correction was not completed",
            code="personal_payment_replacement_incomplete",
        )
    # Keep receipt projection in its existing owner.  The correction service
    # owns mutation only and never invents a second commercial read model.
    from apps.attendance.services.staff_intents import _booking_receipt

    return _booking_receipt(
        booking=correction.replacement_booking,
        payment_method=correction.replacement_payment_method,
        link_id=correction.replacement_payment_link_id,
        use_latest_link=False,
    )


def _claim_or_replay(
    *,
    club_id: int,
    actor_user_id: int,
    idempotency_key: str,
    shape: dict,
    source_terms: PersonalServiceTermsSnapshot,
    original_reservation: PersonalBookingPaymentReservation | None = None,
    original_payment: Payment | None = None,
    original_order: BankPaymentOrder | None = None,
) -> tuple[PersonalPaymentMethodCorrection, dict | None]:
    fingerprint = _fingerprint(shape=shape)
    try:
        # The uniqueness race must roll back its savepoint before the exact
        # replay lookup.  The enclosing lifecycle transaction stays usable.
        with transaction.atomic():
            correction = PersonalPaymentMethodCorrection.objects.create(
                club_id=club_id,
                original_reservation=original_reservation,
                original_payment=original_payment,
                original_bank_payment_order=original_order,
                source_terms=source_terms,
                replacement_payment_method=shape["replacement_payment_method"],
                reason=shape["reason"],
                actor_id=actor_user_id,
                idempotency_key=idempotency_key,
                command_fingerprint=fingerprint,
                command_shape=shape,
            )
        return correction, None
    except IntegrityError:
        correction = (
            PersonalPaymentMethodCorrection.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("replacement_booking", "replacement_payment_link")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if correction is None:
            raise
        if correction.command_fingerprint != fingerprint:
            raise BusinessLogicError(
                "Idempotency key was already used for another correction",
                code="idempotency_conflict",
            )
        return correction, _replayed_correction_receipt(correction=correction)


def replace_personal_payment_method(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int,
    actor_role: str,
    replacement_payment_method: str,
    reason: str,
    idempotency_key: str,
    reservation_id: int | None = None,
    payment_id: int | None = None,
) -> dict:
    """Append a safe replacement attempt without ever editing paid history."""

    if replacement_payment_method not in REPLACEMENT_METHODS:
        raise BusinessLogicError(
            "Unsupported personal payment replacement method",
            code="personal_payment_replacement_method_invalid",
        )
    cleaned_key = idempotency_key.strip()
    cleaned_reason = " ".join(reason.split())
    if not cleaned_key or len(cleaned_key) > 120:
        raise BusinessLogicError("A valid idempotency key is required", code="idempotency_key_required")
    if not cleaned_reason:
        raise BusinessLogicError(
            "A reason is required for a payment-method correction",
            code="personal_payment_replacement_reason_required",
        )
    if bool(reservation_id) == bool(payment_id):
        raise BusinessLogicError(
            "Choose exactly one original personal payment attempt",
            code="personal_payment_replacement_origin_required",
        )

    with transaction.atomic():
        # Payment review and bank-order owners enter complete-personal scopes
        # through Club. Corrections must do the same before catalog/trainer
        # locks or a nested reject can invert the order.
        Club.objects.select_for_update(of=("self",)).only("id").get(id=club_id)
        if reservation_id:
            # Establish the D12 lock prefix before touching one member of the
            # bank/reservation family.  Cancellation and exact-slot reacquire
            # then operate under the same serialised lifecycle scope.
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            reservation_preview = (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .filter(id=reservation_id, student_id=student_id)
                .values(
                    "trainer_id",
                    "training_type_id",
                    "tariff_id",
                    "starts_at",
                    "ends_at",
                )
                .first()
            )
            if reservation_preview is None:
                raise BusinessLogicError(
                    "Personal payment reservation was not found",
                    code="personal_payment_reservation_not_found",
                )
            conflict_scope = preview_personal_time_conflict_scope(
                club_id=club_id,
                trainer_id=reservation_preview["trainer_id"],
                starts_at=reservation_preview["starts_at"],
                ends_at=reservation_preview["ends_at"],
            )
            # Match personal offer/catalog acceptance before the D12 mutable
            # and financial suffix. PostgreSQL FK checks on the replacement
            # Schedule/booking then cannot wait back on catalog rows while a
            # catalog update is waiting for this trainer.
            TrainingType.objects.for_club(club_id).select_for_update(of=("self",)).get(
                id=reservation_preview["training_type_id"],
            )
            Trainer.objects.for_club(club_id).select_for_update(of=("self",)).get(
                id=reservation_preview["trainer_id"],
            )
            Tariff.objects.for_club(club_id).select_for_update(of=("self",)).get(
                id=reservation_preview["tariff_id"],
            )
            lock_complete_personal_scopes(
                club_id=club_id,
                reservation_ids=[reservation_id],
                extra_slot_ids=conflict_scope.slot_ids,
                extra_schedule_ids=conflict_scope.schedule_ids,
                extra_reservation_ids=conflict_scope.reservation_ids,
            )
            reservation = (
                PersonalBookingPaymentReservation.objects.for_club(club_id)
                .select_related("payment", "bank_payment_order")
                .select_for_update(of=("self",))
                .filter(id=reservation_id, student_id=student_id)
                .first()
            )
            if reservation is None:
                raise BusinessLogicError(
                    "Personal payment reservation was not found",
                    code="personal_payment_reservation_not_found",
                )
            order = reservation.bank_payment_order
            payment = reservation.payment
            terms = _complete_terms_for_reservation(club_id=club_id, reservation_id=reservation.id)
            shape = {
                "origin": "reservation",
                "reservation_id": reservation.id,
                "bank_payment_order_id": order.id if order is not None else None,
                "source_terms_id": terms.id,
                "terms_version": terms.terms_version,
                "tariff_id": terms.tariff_id_snapshot,
                "discount_id": terms.discount_id_snapshot,
                "base_amount": str(terms.base_amount),
                "discount_amount": str(terms.discount_amount),
                "payable_amount": str(terms.payable_amount),
                "replacement_payment_method": replacement_payment_method,
                "reason": cleaned_reason,
            }
            correction, replay = _claim_or_replay(
                club_id=club_id,
                actor_user_id=actor_user_id,
                idempotency_key=cleaned_key,
                shape=shape,
                source_terms=terms,
                original_reservation=reservation,
                original_payment=payment,
                original_order=order,
            )
            if replay is not None:
                return replay
            if (
                order is None
                or payment is None
                or reservation.status != PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
                or payment.status != Payment.Status.PENDING
            ):
                raise BusinessLogicError(
                    "Only an active SBP reservation can be replaced",
                    code="personal_payment_replacement_not_pending",
                )
            if order.status == BankPaymentOrder.Status.MANUAL_REVIEW or provider_dispatch_blocks_cancellation(order):
                raise BusinessLogicError(
                    "The bank link may still succeed and requires reconciliation",
                    code="personal_payment_replacement_reconciliation_required",
                )
            allowed_sources = (
                {BankPaymentOrder.Source.TRAINER}
                if actor_role == "trainer"
                else None
            )
            cancel_bank_payment_order(
                club_id=club_id,
                order_id=order.id,
                actor_user_id=actor_user_id,
                allowed_sources=allowed_sources,
                reason="personal_payment_method_replaced",
            )
            replacement = book_personal_drop_in_from_frozen_reservation(
                club_id=club_id,
                reservation_id=reservation.id,
                source_terms_id=terms.id,
                actor_user_id=actor_user_id,
                idempotency_key=_child_idempotency_key(operation="booking", parent_key=cleaned_key),
                conflict_scope=conflict_scope,
            )
            replacement_link = None
            if replacement_payment_method in {Payment.Method.CASH, Payment.Method.TRANSFER}:
                replacement_link = create_personal_drop_in_payment(
                    club_id=club_id,
                    booking_id=replacement.booking.id,
                    payment_method=replacement_payment_method,
                    created_by_id=actor_user_id,
                    discount_ids=[],
                    idempotency_key=_child_idempotency_key(operation="payment", parent_key=cleaned_key),
                )
            correction.replacement_booking = replacement.booking
            correction.replacement_payment_link = replacement_link
            correction.save(update_fields=["replacement_booking", "replacement_payment_link", "updated_at"])
            return _replayed_correction_receipt(correction=correction)

        if actor_role not in OWNER_PAYMENT_CORRECTION_ROLES:
            raise BusinessLogicError(
                "A pending manual payment requires owner review",
                code="personal_payment_replacement_owner_review_required",
            )
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        lock_complete_personal_scopes(
            club_id=club_id,
            payment_ids=[payment_id],
        )
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "booking")
            .filter(payment_id=payment_id, booking__enrollment__student_id=student_id)
            .first()
        )
        if link is None:
            raise BusinessLogicError("Personal payment was not found", code="personal_payment_not_found")
        payment = link.payment
        if payment.payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER}:
            raise BusinessLogicError(
                "Use the reservation correction for an SBP payment",
                code="personal_payment_replacement_origin_invalid",
            )
        if payment.status == Payment.Status.CONFIRMED:
            raise BusinessLogicError(
                "Confirmed money requires a refund or credit decision",
                code="personal_payment_replacement_refund_required",
            )
        if payment.status != Payment.Status.PENDING:
            raise BusinessLogicError(
                "Only a pending manual payment can be replaced",
                code="personal_payment_replacement_not_pending",
            )
        terms = _complete_terms_for_booking(club_id=club_id, booking_id=link.booking_id)
        shape = {
            "origin": "manual_payment",
            "payment_id": payment.id,
            "booking_id": link.booking_id,
            "source_terms_id": terms.id,
            "terms_version": terms.terms_version,
            "tariff_id": terms.tariff_id_snapshot,
            "discount_id": terms.discount_id_snapshot,
            "base_amount": str(terms.base_amount),
            "discount_amount": str(terms.discount_amount),
            "payable_amount": str(terms.payable_amount),
            "replacement_payment_method": replacement_payment_method,
            "reason": cleaned_reason,
        }
        correction, replay = _claim_or_replay(
            club_id=club_id,
            actor_user_id=actor_user_id,
            idempotency_key=cleaned_key,
            shape=shape,
            source_terms=terms,
            original_payment=payment,
        )
        if replay is not None:
            return replay
        verify_payment(
            payment_id=payment.id,
            club_id=club_id,
            verified_by_id=actor_user_id,
            action="reject",
            rejection_reason=cleaned_reason,
        )
        replacement_link = None
        if replacement_payment_method in {Payment.Method.CASH, Payment.Method.TRANSFER}:
            replacement_link = create_personal_drop_in_payment(
                club_id=club_id,
                booking_id=link.booking_id,
                payment_method=replacement_payment_method,
                created_by_id=actor_user_id,
                discount_ids=[],
                idempotency_key=_child_idempotency_key(operation="payment", parent_key=cleaned_key),
            )
        correction.replacement_booking = link.booking
        correction.replacement_payment_link = replacement_link
        correction.save(update_fields=["replacement_booking", "replacement_payment_link", "updated_at"])
        return _replayed_correction_receipt(correction=correction)
