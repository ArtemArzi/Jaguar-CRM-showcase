from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

import apps.billing.service_modules.bank_orders as bank_orders
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
)
from apps.billing.service_modules.payment_review import verify_payment
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def assert_provider_backed_confirmation(
    *,
    order: BankPaymentOrder,
    resolution: str,
) -> None:
    if (
        order.provider == BankPaymentOrder.Provider.TOCHKA
        and resolution == BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID
    ):
        raise BusinessLogicError(
            "Оплату Точки можно подтвердить только после сверки с банком",
            code="tochka_manual_confirm_denied",
        )






def _mark_order_manual_review(
    *,
    order: BankPaymentOrder,
    code: str,
    message: str,
) -> None:
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = code
    order.last_error_message = message
    order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])

    from apps.attendance.services import close_personal_booking_payment_reservation_for_order

    close_personal_booking_payment_reservation_for_order(
        club_id=order.club_id,
        order_id=order.id,
        status="manual_review",
        reason=message,
        code=code,
    )


SAFE_REVIEW_EVIDENCE_KEYS = {
    "provider_event_id",
    "operation_id",
    "payment_link_id",
    "note",
    "refund_amount",
    "entitlement_action",
    "legacy_enrollment_action",
    "legacy_enrollment_id",
}


def _sanitize_review_evidence(evidence: dict | None) -> dict:
    if not evidence:
        return {}

    sanitized: dict[str, str] = {}
    for key, value in evidence.items():
        if key not in SAFE_REVIEW_EVIDENCE_KEYS or value in (None, ""):
            continue
        if key == "refund_amount":
            try:
                amount = Decimal(str(value))
            except Exception as exc:
                raise BusinessLogicError(
                    "Некорректная сумма возврата",
                    code="bank_payment_review_invalid_refund_amount",
                ) from exc
            if amount <= 0:
                raise BusinessLogicError(
                    "Сумма возврата должна быть больше нуля",
                    code="bank_payment_review_invalid_refund_amount",
                )
            sanitized[key] = str(amount.quantize(Decimal("0.01")))
            continue
        if key == "legacy_enrollment_id":
            try:
                enrollment_id = int(value)
            except (TypeError, ValueError) as exc:
                raise BusinessLogicError(
                    "Некорректное зачисление для возврата",
                    code="bank_payment_review_invalid_legacy_enrollment",
                ) from exc
            if enrollment_id <= 0:
                raise BusinessLogicError(
                    "Некорректное зачисление для возврата",
                    code="bank_payment_review_invalid_legacy_enrollment",
                )
            sanitized[key] = str(enrollment_id)
            continue
        sanitized[key] = str(value).strip()[:200]
    return sanitized


def _clean_review_reason(reason: str) -> str:
    return reason.strip()[:500]


def _matches_existing_review_replay(
    *,
    event: BankPaymentOrderReviewEvent | None,
    resolution: str,
    reason: str,
    evidence_metadata: dict[str, str],
) -> bool:
    if event is None or event.resolution != resolution or event.reason != reason:
        return False
    return all(
        event.evidence_metadata.get(key) == value
        for key, value in evidence_metadata.items()
    )


def _refund_case_for_manual_review(
    *,
    order: BankPaymentOrder,
    resolution: str,
    evidence_metadata: dict[str, str],
    resolved_at,
) -> PaymentRefundCase:
    refund_case = (
        PaymentRefundCase.objects.for_club(order.club_id)
        .select_for_update(of=("self",))
        .filter(
            order_id=order.id,
            status__in=[
                PaymentRefundCase.Status.DETECTED,
                PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
            ],
        )
        .order_by("provider_refunded_at", "id")
        .first()
    )
    if refund_case is not None:
        return refund_case

    is_full = resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED
    detected_amount = order.amount_snapshot if is_full else None
    if not is_full and evidence_metadata.get("refund_amount"):
        detected_amount = Decimal(evidence_metadata["refund_amount"])
    return PaymentRefundCase.objects.create(
        club_id=order.club_id,
        order=order,
        refund_kind=(PaymentRefundCase.Kind.FULL if is_full else PaymentRefundCase.Kind.PARTIAL),
        detected_amount=detected_amount,
        provider_refunded_at=resolved_at,
        status=(
            PaymentRefundCase.Status.DETECTED
            if detected_amount is not None
            else PaymentRefundCase.Status.RECONCILIATION_REQUIRED
        ),
    )


def resolve_bank_payment_order_manual_review(
    *,
    club_id: int,
    order_id: int,
    actor_user_id: int,
    resolution: str,
    reason: str = "",
    evidence: dict | None = None,
    resolved_at=None,
) -> BankPaymentOrder:
    if resolution not in BankPaymentOrderReviewEvent.Resolution.values:
        raise BusinessLogicError(
            "Некорректное решение по спорной ссылке на оплату",
            code="bank_payment_review_invalid_resolution",
        )

    clean_reason = _clean_review_reason(reason)
    if not clean_reason:
        raise BusinessLogicError(
            "Укажите причину ручного решения по ссылке на оплату",
            code="bank_payment_review_reason_required",
        )
    evidence_metadata = _sanitize_review_evidence(evidence)
    effective_resolved_at = resolved_at or timezone.now()
    if timezone.is_naive(effective_resolved_at):
        effective_resolved_at = timezone.make_aware(effective_resolved_at, timezone.get_current_timezone())

    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order_queryset = BankPaymentOrder.objects.for_club(club_id).select_related("payment", "subscription")
        order = (
            order_queryset.get(id=order_id)
            if ordered_scope is not None
            else order_queryset.select_for_update(of=("self",)).get(id=order_id)
        )
        if order.status != BankPaymentOrder.Status.MANUAL_REVIEW:
            existing_event = (
                BankPaymentOrderReviewEvent.objects.for_club(club_id)
                .filter(order_id=order.id)
                .order_by("-created_at", "-id")
                .first()
            )
            if _matches_existing_review_replay(
                event=existing_event,
                resolution=resolution,
                reason=clean_reason,
                evidence_metadata=evidence_metadata,
            ):
                return order
            raise BusinessLogicError(
                "Решить вручную можно только ссылку в статусе manual review",
                code="bank_payment_order_not_manual_review",
            )
        assert_provider_backed_confirmation(order=order, resolution=resolution)

        previous_status = order.status
        previous_payment_status = order.payment.status
        previous_subscription_status = order.subscription.status
        resolved_refund = None
        resolved_refund_case = None

        if resolution == BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID:
            if order.payment.status == Payment.Status.PENDING:
                verify_payment(
                    payment_id=order.payment_id,
                    club_id=club_id,
                    verified_by_id=actor_user_id,
                    action="confirm",
                    verified_at=effective_resolved_at,
                    allow_online=True,
                )
            elif order.payment.status != Payment.Status.CONFIRMED:
                raise BusinessLogicError(
                    "Нельзя подтвердить оплату в текущем статусе платежа",
                    code="bank_payment_review_payment_not_confirmable",
                )
            order.status = BankPaymentOrder.Status.APPROVED
            order.paid_at = order.paid_at or effective_resolved_at
            order.confirmed_by_id = actor_user_id
            order.last_error_code = ""
            order.last_error_message = ""
            order.save(
                update_fields=[
                    "status",
                    "paid_at",
                    "confirmed_by",
                    "last_error_code",
                    "last_error_message",
                    "updated_at",
                ]
            )

            from apps.attendance.services import confirm_personal_booking_payment_reservation_for_order

            confirm_personal_booking_payment_reservation_for_order(
                club_id=club_id,
                order_id=order.id,
                actor_user_id=actor_user_id,
            )
        elif resolution == BankPaymentOrderReviewEvent.Resolution.REJECT:
            if order.payment.status != Payment.Status.PENDING:
                raise BusinessLogicError(
                    "Отклонить можно только необработанную онлайн-оплату",
                    code="bank_payment_review_payment_not_rejectable",
                )
            order.status = BankPaymentOrder.Status.FAILED
            order.last_error_code = "manual_review_rejected"
            order.last_error_message = clean_reason
            order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
            bank_orders._cancel_pending_bank_order_artifacts(
                order=order,
                reason=clean_reason,
                actor_user_id=actor_user_id,
            )
        elif resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED:
            if order.payment.status != Payment.Status.CONFIRMED:
                raise BusinessLogicError(
                    "Отметить возврат можно только после подтвержденной оплаты",
                    code="bank_payment_review_payment_not_confirmed",
                )
            resolved_refund_case = _refund_case_for_manual_review(
                order=order,
                resolution=resolution,
                evidence_metadata=evidence_metadata,
                resolved_at=effective_resolved_at,
            )
            prior_refunded = (
                PaymentRefund.objects.for_club(club_id)
                .filter(payment_id=order.payment_id)
                .aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            from apps.billing.refund_services import approve_payment_refund_case

            resolved_refund = approve_payment_refund_case(
                club_id=club_id,
                case_id=resolved_refund_case.id,
                actor_user_id=actor_user_id,
                idempotency_key=f"payment-refund-case-{resolved_refund_case.id}",
                amount=order.payment.amount - prior_refunded,
                refund_kind=PaymentRefund.Kind.FULL,
                entitlement_action=evidence_metadata.get("entitlement_action"),
                legacy_enrollment_action=evidence_metadata.get("legacy_enrollment_action"),
                legacy_enrollment_id=(
                    int(evidence_metadata["legacy_enrollment_id"])
                    if evidence_metadata.get("legacy_enrollment_id")
                    else None
                ),
                reason=clean_reason,
            )
        elif resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY:
            if order.payment.status != Payment.Status.CONFIRMED:
                raise BusinessLogicError(
                    "Отметить частичный возврат можно только после подтвержденной оплаты",
                    code="bank_payment_review_payment_not_confirmed",
                )
            if "refund_amount" not in evidence_metadata:
                raise BusinessLogicError(
                    "Укажите сумму частичного возврата",
                    code="bank_payment_review_refund_amount_required",
                )
            refund_amount = Decimal(evidence_metadata["refund_amount"])
            if refund_amount >= order.amount_snapshot:
                raise BusinessLogicError(
                    "Сумма частичного возврата должна быть меньше суммы заказа",
                    code="bank_payment_review_partial_refund_amount_invalid",
                )
            resolved_refund_case = _refund_case_for_manual_review(
                order=order,
                resolution=resolution,
                evidence_metadata=evidence_metadata,
                resolved_at=effective_resolved_at,
            )
            from apps.billing.refund_services import approve_payment_refund_case

            resolved_refund = approve_payment_refund_case(
                club_id=club_id,
                case_id=resolved_refund_case.id,
                actor_user_id=actor_user_id,
                idempotency_key=f"payment-refund-case-{resolved_refund_case.id}",
                amount=refund_amount,
                refund_kind=PaymentRefund.Kind.PARTIAL,
                reason=clean_reason,
            )

        if resolved_refund is not None:
            evidence_metadata.update(
                {
                    "accounting_effect": "payment_refund_posted",
                    "refund_id": str(resolved_refund.id),
                    "refund_case_id": str(resolved_refund.refund_case_id),
                    "entitlement_disposition": resolved_refund.entitlement_disposition,
                    "enrollment_disposition": resolved_refund.enrollment_disposition,
                    "personal_booking_disposition": resolved_refund.personal_booking_disposition,
                    "refund_status": resolved_refund.status,
                }
            )

        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        review_event = BankPaymentOrderReviewEvent.objects.create(
            club_id=club_id,
            order=order,
            actor_id=actor_user_id,
            resolution=resolution,
            previous_status=previous_status,
            new_status=order.status,
            previous_payment_status=previous_payment_status,
            new_payment_status=order.payment.status,
            previous_subscription_status=previous_subscription_status,
            new_subscription_status=order.subscription.status,
            reason=clean_reason,
            evidence_metadata=evidence_metadata,
        )
        if resolved_refund_case is not None and resolved_refund_case.legacy_review_event_id is None:
            resolved_refund_case.legacy_review_event = review_event
            resolved_refund_case.save(update_fields=["legacy_review_event", "updated_at"])
        order.refresh_from_db()
        return order
