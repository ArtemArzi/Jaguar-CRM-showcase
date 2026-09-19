from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

import apps.billing.service_modules.bank_orders as bank_orders
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    BankPaymentProviderEvent,
    BankPaymentReconciliationAttempt,
    Payment,
    PaymentRefundCase,
    ProviderWebhookDelivery,
)
from apps.billing.payment_providers import get_payment_provider
from apps.billing.payment_providers.base import validate_sbp_only_payment_modes
from apps.billing.service_modules._shared import _money
from apps.billing.service_modules.bank_order_review import (
    _mark_order_manual_review as _persist_manual_review,
)
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.renewals import is_renewal_manual_review_error
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def _provider_renewal_manual_review_code(exc: BusinessLogicError) -> str | None:
    """Keep the provider audit namespace while sharing the domain classifier."""

    if not is_renewal_manual_review_error(exc):
        return None
    return f"bank_payment_{exc.code}"

REFUND_MANUAL_REVIEW_CODES = {
    "bank_payment_refunded_requires_review",
    "bank_payment_refunded_partially_requires_review",
}


def _mark_order_manual_review(
    *,
    order: BankPaymentOrder,
    code: str,
    message: str = "Требуется подтверждённая сверка с банком",
) -> None:
    _persist_manual_review(order=order, code=code[:120], message=message)










REFUND_REVIEW_ORDER_STATUSES = {
    BankPaymentOrder.Status.REFUNDED,
    BankPaymentOrder.Status.REFUNDED_PARTIALLY,
}

def request_provider_reconciliation(
    *,
    club_id: int,
    order_id: int,
    provider_event_id: int | None,
    allow_manual_retry: bool = False,
    actor_user_id: int | None = None,
) -> BankPaymentReconciliationAttempt:
    """Persist/coalesce one provider reconciliation request without bank I/O."""

    now = timezone.now()
    cooldown = max(5, min(int(settings.TOCHKA_RECONCILIATION_COOLDOWN_SECONDS), 3600))
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update().get(id=order_id)
        )
        if allow_manual_retry and (
            order.provider != BankPaymentOrder.Provider.TOCHKA
            or order.status != BankPaymentOrder.Status.MANUAL_REVIEW
            or order.last_error_code in REFUND_MANUAL_REVIEW_CODES
        ):
            raise BusinessLogicError(
                "Сверка с банком недоступна для этой оплаты",
                code="tochka_manual_reconciliation_not_available",
            )
        event = None
        if provider_event_id is not None:
            event = BankPaymentProviderEvent.objects.for_club(club_id).get(id=provider_event_id)
        attempt, created = BankPaymentReconciliationAttempt.objects.for_club(club_id).select_for_update().get_or_create(
            order=order,
            defaults={
                "club_id": club_id,
                "provider_event": event,
                "status": BankPaymentReconciliationAttempt.Status.PENDING,
                "retry_at": now,
            },
        )
        if not created and event is not None and attempt.provider_event_id is None:
            attempt.provider_event = event
            attempt.save(update_fields=["provider_event", "updated_at"])
        if allow_manual_retry and not created and attempt.status in {
            BankPaymentReconciliationAttempt.Status.PENDING,
            BankPaymentReconciliationAttempt.Status.RETRY,
            BankPaymentReconciliationAttempt.Status.RUNNING,
        }:
            raise BusinessLogicError(
                "Сверка уже запрошена. Дождитесь результата",
                code="tochka_reconciliation_already_requested",
            )
        legacy_operation_discovered = (
            not created
            and attempt.status == BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
            and attempt.last_error_code == "tochka_operation_id_missing"
            and order.provider_operation_id
        )
        manual_retry_requested = allow_manual_retry
        manually_retryable = (
            manual_retry_requested
            and attempt.last_error_code not in REFUND_MANUAL_REVIEW_CODES
            and order.last_error_code not in REFUND_MANUAL_REVIEW_CODES
            and bool(order.provider_operation_id)
        )
        creation_retryable = (
            manual_retry_requested
            and attempt.last_error_code not in REFUND_MANUAL_REVIEW_CODES
            and order.last_error_code not in REFUND_MANUAL_REVIEW_CODES
            and not order.provider_operation_id
            and order.link_creation_state
            in {
                BankPaymentOrder.LinkCreationState.UNKNOWN,
                BankPaymentOrder.LinkCreationState.DISPATCHED,
            }
        )
        if manual_retry_requested and not (manually_retryable or creation_retryable):
            raise BusinessLogicError(
                "Эта оплата требует отдельной проверки поддержки",
                code="tochka_manual_reconciliation_not_retryable",
            )
        if (manually_retryable or creation_retryable) and BankPaymentOrderReviewEvent.objects.for_club(
            club_id
        ).filter(
            order=order,
            resolution=BankPaymentOrderReviewEvent.Resolution.RETRY_RECONCILIATION,
            actor__isnull=False,
            created_at__gt=now - timedelta(seconds=cooldown),
        ).exists():
            raise BusinessLogicError(
                "Сверка уже запрошена. Подождите перед повтором",
                code="tochka_reconciliation_cooldown",
            )
        if legacy_operation_discovered or manually_retryable or creation_retryable:
            previous_error_code = attempt.last_error_code
            attempt.status = (
                BankPaymentReconciliationAttempt.Status.RETRY
                if creation_retryable
                else BankPaymentReconciliationAttempt.Status.PENDING
            )
            attempt.attempt_count = 0
            attempt.retry_at = now
            attempt.lease_token = ""
            attempt.lease_expires_at = None
            attempt.last_error_code = ""
            attempt.save(
                update_fields=[
                    "status",
                    "attempt_count",
                    "retry_at",
                    "lease_token",
                    "lease_expires_at",
                    "last_error_code",
                    "updated_at",
                ]
            )
            if manually_retryable or creation_retryable:
                previous_order_status = order.status
                if creation_retryable:
                    order.status = BankPaymentOrder.Status.PENDING
                    order.creation_recovery_failure_count = 0
                    order.creation_recovery_retry_at = now
                    order.last_error_code = "provider_creation_unknown"
                    order.last_error_message = "Статус создания ссылки требует повторной сверки с банком"
                    order.save(
                        update_fields=[
                            "status",
                            "creation_recovery_failure_count",
                            "creation_recovery_retry_at",
                            "last_error_code",
                            "last_error_message",
                            "updated_at",
                        ]
                    )
                BankPaymentOrderReviewEvent.objects.create(
                    club_id=club_id,
                    order=order,
                    actor_id=actor_user_id,
                    resolution=BankPaymentOrderReviewEvent.Resolution.RETRY_RECONCILIATION,
                    previous_status=previous_order_status,
                    new_status=order.status,
                    previous_payment_status=order.payment.status,
                    new_payment_status=order.payment.status,
                    previous_subscription_status=(
                        order.subscription.status if order.subscription_id else None
                    ),
                    new_subscription_status=(
                        order.subscription.status if order.subscription_id else None
                    ),
                    reason="Повторная защищённая сверка с банком",
                    evidence_metadata={"previous_attempt_error_code": previous_error_code},
                )
        if (
            attempt.status == BankPaymentReconciliationAttempt.Status.RETRY
            and (attempt.retry_at is None or attempt.retry_at <= now - timedelta(seconds=cooldown))
        ):
            attempt.retry_at = now
            attempt.save(update_fields=["retry_at", "updated_at"])
        return attempt

def enqueue_provider_reconciliation(*, club_id: int, order_id: int) -> None:
    """Queue one coalesced attempt after its database claim is durable."""

    from django_q.tasks import async_task

    async_task(
        "apps.billing.tasks.reconcile_bank_payment_order_task",
        club_id=club_id,
        order_id=order_id,
    )

def process_due_provider_reconciliations(*, limit: int = 100) -> dict[str, int]:
    """Scheduled cross-tenant executor; each row is claimed by the reconciler."""

    if not settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED:
        return {"disabled": 1}
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    if not get_online_payment_capability().reconciliation_available:
        return {"not_ready": 1}

    now = timezone.now()
    bounded_limit = max(1, min(int(limit), 500))
    due = list(
        BankPaymentReconciliationAttempt.objects.unscoped()
        .filter(
            Q(status=BankPaymentReconciliationAttempt.Status.PENDING, retry_at__lte=now)
            | Q(status=BankPaymentReconciliationAttempt.Status.RETRY, retry_at__lte=now)
            | Q(status=BankPaymentReconciliationAttempt.Status.RUNNING, lease_expires_at__lte=now)
        )
        .order_by("retry_at", "id")
        .values_list("club_id", "order_id")[:bounded_limit]
    )
    outcomes: dict[str, int] = {}
    for club_id, order_id in due:
        try:
            outcome = reconcile_provider_payment_order(club_id=club_id, order_id=order_id)
        except Exception:
            logger.exception(
                "bank_payment_reconciliation_candidate_failed",
                extra={"club_id": club_id, "order_id": order_id},
            )
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes

def process_unknown_provider_creations(*, limit: int = 100) -> dict[str, int]:
    """Scheduled recovery for every possibly dispatched Tochka create claim."""

    if not settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED:
        return {"disabled": 1}
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    if not get_online_payment_capability().reconciliation_available:
        return {"not_ready": 1}

    from apps.billing.service_modules.bank_orders import recover_unknown_bank_payment_order

    now = timezone.now()
    bounded_limit = max(1, min(int(limit), 500))
    candidates = list(
        BankPaymentOrder.objects.unscoped()
        .filter(
            provider=BankPaymentOrder.Provider.TOCHKA,
            status__in=[
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.AUTHORIZED,
            ],
            link_creation_state__in=[
                BankPaymentOrder.LinkCreationState.DISPATCHED,
                BankPaymentOrder.LinkCreationState.UNKNOWN,
            ],
            provider_operation_id="",
        )
        .filter(Q(creation_recovery_retry_at__isnull=True) | Q(creation_recovery_retry_at__lte=now))
        .exclude(provider_payment_link_id="")
        .order_by("link_creation_dispatched_at", "id")
        .values_list("club_id", "id")[:bounded_limit]
    )
    outcomes: dict[str, int] = {}
    for club_id, order_id in candidates:
        try:
            outcome = recover_unknown_bank_payment_order(club_id=club_id, order_id=order_id)
        except Exception:
            logger.exception(
                "bank_payment_creation_recovery_candidate_failed",
                extra={"club_id": club_id, "order_id": order_id},
            )
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes

def reconcile_provider_payment_order(*, club_id: int, order_id: int) -> str:
    """Run durable claim -> bank I/O -> confirmation without locks around I/O."""

    if not settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED:
        return "disabled"

    lease_token = uuid4().hex
    now = timezone.now()
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        attempt = (
            BankPaymentReconciliationAttempt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("order")
            .filter(order_id=order_id)
            .first()
        )
        if attempt is None:
            return "missing"
        if attempt.status == BankPaymentReconciliationAttempt.Status.COMPLETED:
            return "completed"
        if attempt.status == BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW:
            return "manual_review"
        if (
            attempt.status == BankPaymentReconciliationAttempt.Status.RUNNING
            and attempt.lease_expires_at is not None
            and attempt.lease_expires_at > now
        ):
            return "leased"
        if attempt.retry_at is not None and attempt.retry_at > now:
            return "cooldown"
        order = attempt.order
        if order.status == BankPaymentOrder.Status.APPROVED or order.payment.status == Payment.Status.CONFIRMED:
            _complete_attempt(attempt=attempt)
            return "completed"
        if order.status in {
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.EXPIRED,
            BankPaymentOrder.Status.CANCELLED,
            BankPaymentOrder.Status.REFUNDED,
            BankPaymentOrder.Status.REFUNDED_PARTIALLY,
        }:
            _complete_attempt(attempt=attempt)
            return "completed"
        if attempt.attempt_count >= _max_reconciliation_attempts():
            _mark_attempt_manual_review(attempt=attempt, code="tochka_reconciliation_exhausted")
            _mark_order_manual_review(order=order, code="tochka_reconciliation_exhausted")
            return "manual_review"
        if order.provider != BankPaymentOrder.Provider.TOCHKA:
            return "not_tochka"
        operation_id = order.provider_operation_id
        if not operation_id:
            _defer_attempt_for_creation_recovery(attempt=attempt)
            return "creation_recovery"
        attempt.status = BankPaymentReconciliationAttempt.Status.RUNNING
        attempt.lease_token = lease_token
        attempt.lease_expires_at = now + timedelta(minutes=2)
        attempt.retry_at = None
        attempt.save(
            update_fields=["status", "lease_token", "lease_expires_at", "retry_at", "updated_at"]
        )

    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    if not get_online_payment_capability().reconciliation_available:
        return _record_retry(
            club_id=club_id,
            order_id=order_id,
            lease_token=lease_token,
            code="tochka_reconciliation_not_ready",
            count_failure=False,
        )

    try:
        operation = get_payment_provider(BankPaymentOrder.Provider.TOCHKA).get_payment_operation_info(
            order=order,
            operation_id=operation_id,
        )
    except BusinessLogicError as exc:
        return _record_retry(club_id=club_id, order_id=order_id, lease_token=lease_token, code=exc.code)
    except Exception:
        return _record_retry(
            club_id=club_id,
            order_id=order_id,
            lease_token=lease_token,
            code="tochka_reconciliation_unexpected_error",
        )

    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        attempt = (
            BankPaymentReconciliationAttempt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .get(order_id=order_id)
        )
        if attempt.status != BankPaymentReconciliationAttempt.Status.RUNNING or attempt.lease_token != lease_token:
            return "stale"
        if not get_online_payment_capability().reconciliation_available:
            _record_attempt_nonfailure_retry_locked(
                attempt=attempt,
                code="tochka_reconciliation_not_ready",
            )
            return "retry"
        order = (
            BankPaymentOrder.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "subscription")
            .get(id=order_id)
        )
        attempt.order = order
        provider_event = None
        if attempt.provider_event_id is not None:
            provider_event = (
                BankPaymentProviderEvent.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(id=attempt.provider_event_id)
                .first()
            )
        error_code = _operation_matches_order(order=order, operation=operation)
        if error_code:
            _mark_attempt_manual_review(attempt=attempt, code=error_code)
            _mark_order_manual_review(order=order, code=error_code)
            return "manual_review"
        normalized_status = operation.status.strip().lower()
        if normalized_status != "approved":
            provider_order_status = _provider_status_to_order_status(normalized_status)
            order.provider_status = operation.status
            if provider_order_status in REFUND_REVIEW_ORDER_STATUSES:
                refund_kind = (
                    PaymentRefundCase.Kind.FULL
                    if provider_order_status == BankPaymentOrder.Status.REFUNDED
                    else PaymentRefundCase.Kind.PARTIAL
                )
                refund_case = (
                    PaymentRefundCase.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(
                        order=order,
                        provider_event__isnull=True,
                        legacy_review_event__isnull=True,
                        status__in=[
                            PaymentRefundCase.Status.DETECTED,
                            PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
                        ],
                    )
                    .first()
                )
                if refund_case is None:
                    PaymentRefundCase.objects.create(
                        club_id=club_id,
                        order=order,
                        refund_kind=refund_kind,
                        detected_amount=(
                            order.amount_snapshot
                            if refund_kind == PaymentRefundCase.Kind.FULL
                            else None
                        ),
                        status=(
                            PaymentRefundCase.Status.DETECTED
                            if refund_kind == PaymentRefundCase.Kind.FULL
                            else PaymentRefundCase.Status.RECONCILIATION_REQUIRED
                        ),
                    )
                _mark_attempt_manual_review(
                    attempt=attempt,
                    code=f"bank_payment_{normalized_status}_requires_review",
                )
                _mark_order_manual_review(
                    order=order,
                    code=f"bank_payment_{normalized_status}_requires_review",
                )
                return "manual_review"
            if provider_order_status in {
                BankPaymentOrder.Status.FAILED,
                BankPaymentOrder.Status.EXPIRED,
                BankPaymentOrder.Status.CANCELLED,
            }:
                if provider_event is not None and provider_event.normalized_status_snapshot == "approved":
                    _mark_attempt_manual_review(
                        attempt=attempt,
                        code="bank_payment_provider_status_conflict",
                    )
                    _mark_order_manual_review(
                        order=order,
                        code="bank_payment_provider_status_conflict",
                    )
                    return "manual_review"
                order.status = provider_order_status
                order.last_error_code = ""
                order.last_error_message = ""
                order.save(
                    update_fields=[
                        "status",
                        "provider_status",
                        "last_error_code",
                        "last_error_message",
                        "updated_at",
                    ]
                )
                bank_orders._cancel_pending_bank_order_artifacts(
                    order=order,
                    reason=f"bank_payment_provider_{normalized_status}",
                )
                if provider_event is not None:
                    provider_event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
                    provider_event.processed_at = timezone.now()
                    provider_event.save(
                        update_fields=["processing_status", "processed_at", "updated_at"]
                    )
                _complete_attempt(attempt=attempt)
                return "terminal"
            if provider_order_status == BankPaymentOrder.Status.AUTHORIZED:
                order.status = BankPaymentOrder.Status.AUTHORIZED
                order.save(update_fields=["status", "provider_status", "updated_at"])
            return _record_authenticated_nonterminal_locked(
                attempt=attempt,
                order=order,
                code="tochka_operation_not_approved",
            )
        if operation.paid_at is None:
            _record_attempt_retry_locked(attempt=attempt, code="tochka_operation_not_approved")
            return "retry"
        if order.payment.status == Payment.Status.CONFIRMED or order.status == BankPaymentOrder.Status.APPROVED:
            _complete_attempt(attempt=attempt)
            return "completed"
        if order.payment.status != Payment.Status.PENDING:
            _mark_attempt_manual_review(attempt=attempt, code="bank_payment_approved_after_order_closed")
            _mark_order_manual_review(order=order, code="bank_payment_approved_after_order_closed")
            return "manual_review"

        if not _should_confirm_expired_order(order=order, paid_at=operation.paid_at):
            _mark_attempt_manual_review(attempt=attempt, code="bank_payment_late_approved")
            _mark_order_manual_review(order=order, code="bank_payment_late_approved")
            return "manual_review"
        try:
            verify_payment(
                payment_id=order.payment_id,
                club_id=club_id,
                verified_by_id=None,
                action="confirm",
                verified_at=operation.paid_at,
                allow_online=True,
            )
        except BusinessLogicError as exc:
            failure_code = _provider_renewal_manual_review_code(exc)
            if failure_code is None:
                raise
            _mark_attempt_manual_review(attempt=attempt, code=failure_code)
            _mark_order_manual_review(order=order, code=failure_code)
            return "manual_review"
        from apps.attendance.services import confirm_personal_booking_payment_reservation_for_order

        confirm_personal_booking_payment_reservation_for_order(
            club_id=club_id,
            order_id=order.id,
            actor_user_id=None,
        )
        order.status = BankPaymentOrder.Status.APPROVED
        order.paid_at = operation.paid_at
        order.confirmed_by = None
        order.last_error_code = ""
        order.last_error_message = ""
        order.save(
            update_fields=["status", "paid_at", "confirmed_by", "last_error_code", "last_error_message", "updated_at"]
        )
        if provider_event is not None:
            provider_event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
            provider_event.processed_at = timezone.now()
            provider_event.provider_paid_at_snapshot = operation.paid_at
            provider_event.save(
                update_fields=["processing_status", "processed_at", "provider_paid_at_snapshot", "updated_at"]
            )
        _complete_attempt(attempt=attempt)
        return "completed"

def _operation_matches_order(*, order: BankPaymentOrder, operation) -> str:
    try:
        validate_sbp_only_payment_modes(operation.payment_modes)
    except BusinessLogicError:
        return "tochka_operation_payment_mode_mismatch"
    if operation.operation_id != order.provider_operation_id:
        return "bank_payment_operation_mismatch"
    if operation.payment_link_id != order.provider_payment_link_id:
        return "bank_payment_link_mismatch"
    if operation.amount != order.amount_snapshot:
        return "bank_payment_amount_mismatch"
    expected_customer_code = str(order.provider_customer_code or settings.TOCHKA_CUSTOMER_CODE or "").strip()
    if not expected_customer_code or not operation.customer_code:
        return "bank_payment_customer_identity_missing"
    if operation.customer_code != expected_customer_code:
        return "bank_payment_customer_mismatch"
    expected_merchant_id = str(order.provider_merchant_id or settings.TOCHKA_MERCHANT_ID or "").strip()
    if not expected_merchant_id or not operation.merchant_id:
        return "bank_payment_merchant_identity_missing"
    if operation.merchant_id != expected_merchant_id:
        return "bank_payment_merchant_mismatch"
    return ""

def _defer_attempt_for_creation_recovery(*, attempt: BankPaymentReconciliationAttempt) -> None:
    attempt.status = BankPaymentReconciliationAttempt.Status.RETRY
    attempt.retry_at = timezone.now() + timedelta(seconds=60)
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = "tochka_operation_id_missing"
    attempt.save(
        update_fields=[
            "status",
            "retry_at",
            "lease_token",
            "lease_expires_at",
            "last_error_code",
            "updated_at",
        ]
    )

def _record_retry(
    *,
    club_id: int,
    order_id: int,
    lease_token: str,
    code: str,
    count_failure: bool = True,
) -> str:
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        attempt = BankPaymentReconciliationAttempt.objects.for_club(club_id).select_for_update().get(order_id=order_id)
        if attempt.status != BankPaymentReconciliationAttempt.Status.RUNNING or attempt.lease_token != lease_token:
            return "stale"
        attempt.order = (
            BankPaymentOrder.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .get(id=order_id)
        )
        if count_failure:
            _record_attempt_retry_locked(attempt=attempt, code=code)
        else:
            _record_attempt_nonfailure_retry_locked(attempt=attempt, code=code)
        return "retry"

def _record_attempt_retry_locked(*, attempt: BankPaymentReconciliationAttempt, code: str) -> None:
    attempt.attempt_count += 1
    if attempt.attempt_count >= _max_reconciliation_attempts():
        _mark_attempt_manual_review(attempt=attempt, code="tochka_reconciliation_exhausted")
        _mark_order_manual_review(order=attempt.order, code="tochka_reconciliation_exhausted")
        return
    delay_seconds = min(3600, 30 * (2 ** min(attempt.attempt_count, 6)))
    attempt.status = BankPaymentReconciliationAttempt.Status.RETRY
    attempt.retry_at = timezone.now() + timedelta(seconds=delay_seconds)
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = code[:120]
    attempt.save(
        update_fields=[
            "status",
            "attempt_count",
            "retry_at",
            "lease_token",
            "lease_expires_at",
            "last_error_code",
            "updated_at",
        ]
    )

def _record_attempt_nonfailure_retry_locked(
    *,
    attempt: BankPaymentReconciliationAttempt,
    code: str,
) -> None:
    cooldown = max(60, min(int(settings.TOCHKA_RECONCILIATION_COOLDOWN_SECONDS), 3600))
    attempt.status = BankPaymentReconciliationAttempt.Status.RETRY
    attempt.retry_at = timezone.now() + timedelta(seconds=cooldown)
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = code[:120]
    attempt.save(
        update_fields=[
            "status",
            "retry_at",
            "lease_token",
            "lease_expires_at",
            "last_error_code",
            "updated_at",
        ]
    )

def _record_authenticated_nonterminal_locked(
    *,
    attempt: BankPaymentReconciliationAttempt,
    order: BankPaymentOrder,
    code: str,
) -> str:
    now = timezone.now()
    if now >= order.expires_at:
        _mark_attempt_manual_review(
            attempt=attempt,
            code="tochka_operation_nonterminal_after_expiry",
        )
        _mark_order_manual_review(
            order=order,
            code="tochka_operation_nonterminal_after_expiry",
        )
        return "manual_review"
    age = now - order.created_at
    if age < timedelta(minutes=30):
        delay_seconds = 120
    elif age < timedelta(hours=6):
        delay_seconds = 900
    else:
        delay_seconds = 3600
    delay_seconds = max(
        60,
        min(delay_seconds, max(60, int((order.expires_at - now).total_seconds()))),
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.RETRY
    attempt.attempt_count = 0
    attempt.retry_at = now + timedelta(seconds=delay_seconds)
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = code[:120]
    attempt.save(
        update_fields=[
            "status",
            "attempt_count",
            "retry_at",
            "lease_token",
            "lease_expires_at",
            "last_error_code",
            "updated_at",
        ]
    )
    return "retry"

def _complete_attempt(*, attempt: BankPaymentReconciliationAttempt) -> None:
    attempt.status = BankPaymentReconciliationAttempt.Status.COMPLETED
    attempt.retry_at = None
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = ""
    attempt.save(
        update_fields=["status", "retry_at", "lease_token", "lease_expires_at", "last_error_code", "updated_at"]
    )

def _mark_attempt_manual_review(*, attempt: BankPaymentReconciliationAttempt, code: str) -> None:
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.retry_at = None
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.last_error_code = code[:120]
    attempt.save(
        update_fields=["status", "retry_at", "lease_token", "lease_expires_at", "last_error_code", "updated_at"]
    )

def _max_reconciliation_attempts() -> int:
    return max(1, min(int(getattr(settings, "TOCHKA_RECONCILIATION_MAX_ATTEMPTS", 8)), 20))

def _resolve_bank_order_for_webhook(
    *,
    provider: str,
    payment_link_id: str,
    operation_id: str,
) -> tuple[BankPaymentOrder | None, bool]:
    qs = BankPaymentOrder.objects.unscoped().select_related("payment", "subscription", "student")
    link_orders = (
        list(
            qs.filter(provider=provider, provider_payment_link_id=payment_link_id)
            .order_by("id")[:2]
        )
        if payment_link_id
        else []
    )
    operation_orders = (
        list(
            qs.filter(provider=provider, provider_operation_id=operation_id)
            .order_by("id")[:2]
        )
        if operation_id
        else []
    )
    if len(link_orders) > 1 or len(operation_orders) > 1:
        return None, True
    link_order = link_orders[0] if link_orders else None
    operation_order = operation_orders[0] if operation_orders else None
    if link_order is not None and operation_order is not None and link_order.id != operation_order.id:
        return None, True
    if (
        provider == BankPaymentOrder.Provider.TOCHKA
        and link_order is not None
        and operation_order is None
        and operation_id
        and link_order.provider_operation_id
    ):
        return None, True
    if (
        provider == BankPaymentOrder.Provider.TOCHKA
        and operation_order is not None
        and link_order is None
        and payment_link_id
    ):
        return None, True
    return link_order or operation_order, False

def _provider_identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

def _safe_request_id(value: str) -> str:
    return "".join(
        char
        for char in str(value or "")
        if char.isascii() and (char.isalnum() or char in "-_.:")
    )[:120]

def _find_provider_webhook_delivery(
    *,
    provider: str,
    payload_hash: str,
    provider_event_id_hash: str,
) -> ProviderWebhookDelivery | None:
    duplicate_q = Q(provider=provider, payload_hash=payload_hash)
    if provider_event_id_hash:
        duplicate_q |= Q(provider=provider, provider_event_id_hash=provider_event_id_hash)
    return ProviderWebhookDelivery.objects.select_related("provider_event").filter(duplicate_q).first()

def _create_or_get_provider_webhook_delivery(
    *,
    provider: str,
    payload_hash: str,
    webhook,
    request_id: str,
) -> tuple[ProviderWebhookDelivery, bool]:
    provider_event_id_hash = _provider_identifier_hash(webhook.event_id)
    duplicate = _find_provider_webhook_delivery(
        provider=provider,
        payload_hash=payload_hash,
        provider_event_id_hash=provider_event_id_hash,
    )
    if duplicate is not None:
        return duplicate, False
    try:
        with transaction.atomic():
            delivery = ProviderWebhookDelivery.objects.create(
                provider=provider,
                payload_hash=payload_hash,
                provider_event_id_hash=provider_event_id_hash,
                event_type=webhook.event_type,
                provider_status=webhook.status,
                payment_type=str(webhook.metadata.get("payment_type") or "")[:30],
                amount_snapshot=webhook.amount,
                received_at=timezone.now(),
                request_id=_safe_request_id(request_id),
            )
    except IntegrityError:
        duplicate = _find_provider_webhook_delivery(
            provider=provider,
            payload_hash=payload_hash,
            provider_event_id_hash=provider_event_id_hash,
        )
        if duplicate is None:
            raise
        return duplicate, False
    return delivery, True

def _tochka_webhook_is_actionable(webhook) -> bool:
    if webhook.event_type != "acquiringInternetPayment":
        return False
    if webhook.metadata.get("payment_type") != "sbp":
        return False
    if not all(
        (
            webhook.status,
            webhook.event_id,
            webhook.payment_link_id,
            webhook.operation_id,
            webhook.customer_code,
            webhook.merchant_id,
            webhook.amount is not None,
        )
    ):
        return False
    expected_customer = str(getattr(settings, "TOCHKA_CUSTOMER_CODE", "") or "").strip()
    expected_merchant = str(getattr(settings, "TOCHKA_MERCHANT_ID", "") or "").strip()
    if expected_customer and webhook.customer_code != expected_customer:
        return False
    if expected_merchant and webhook.merchant_id != expected_merchant:
        return False
    return True

def _finish_global_webhook_delivery(
    *,
    delivery: ProviderWebhookDelivery,
    outcome: str,
    provider_event: BankPaymentProviderEvent | None = None,
) -> ProviderWebhookDelivery:
    delivery.outcome = outcome
    delivery.processed_at = timezone.now()
    delivery.provider_event = provider_event
    delivery.save(update_fields=["outcome", "processed_at", "provider_event", "updated_at"])
    return delivery

def _provider_status_to_order_status(status: str) -> str | None:
    mapping = {
        "approved": BankPaymentOrder.Status.APPROVED,
        "authorized": BankPaymentOrder.Status.AUTHORIZED,
        "failed": BankPaymentOrder.Status.FAILED,
        "declined": BankPaymentOrder.Status.FAILED,
        "expired": BankPaymentOrder.Status.EXPIRED,
        "cancelled": BankPaymentOrder.Status.CANCELLED,
        "canceled": BankPaymentOrder.Status.CANCELLED,
        "refunded": BankPaymentOrder.Status.REFUNDED,
        "refunded_partially": BankPaymentOrder.Status.REFUNDED_PARTIALLY,
    }
    return mapping.get(status)

def _ensure_refund_case_for_provider_event(
    *,
    event: BankPaymentProviderEvent,
    order: BankPaymentOrder,
    normalized_status: str,
) -> PaymentRefundCase:
    refund_kind = (
        PaymentRefundCase.Kind.FULL
        if normalized_status == BankPaymentOrder.Status.REFUNDED
        else PaymentRefundCase.Kind.PARTIAL
    )
    refund_case, _ = PaymentRefundCase.objects.for_club(order.club_id).get_or_create(
        provider_event=event,
        defaults={
            "club_id": order.club_id,
            "order": order,
            "refund_kind": refund_kind,
            "detected_amount": (
                order.amount_snapshot
                if refund_kind == PaymentRefundCase.Kind.FULL
                else None
            ),
            "provider_refunded_at": event.received_at,
            "status": (
                PaymentRefundCase.Status.DETECTED
                if refund_kind == PaymentRefundCase.Kind.FULL
                else PaymentRefundCase.Status.RECONCILIATION_REQUIRED
            ),
        },
    )
    return refund_case

def _webhook_matches_order(*, order: BankPaymentOrder, webhook) -> tuple[bool, str, str]:
    if order.provider_payment_link_id and not webhook.payment_link_id:
        return False, "bank_payment_link_missing", "provider webhook не содержит paymentLinkId"
    if webhook.payment_link_id and webhook.payment_link_id != order.provider_payment_link_id:
        return False, "bank_payment_link_mismatch", "paymentLinkId provider webhook не совпадает с заказом"
    if order.provider_operation_id and not webhook.operation_id:
        return False, "bank_payment_operation_missing", "provider webhook не содержит operationId"
    if webhook.amount is not None and _money(webhook.amount) != _money(order.amount_snapshot):
        return False, "bank_payment_amount_mismatch", "Сумма provider webhook не совпадает с заказом"
    if order.provider_customer_code and webhook.customer_code and webhook.customer_code != order.provider_customer_code:
        return False, "bank_payment_customer_mismatch", "customerCode provider webhook не совпадает с заказом"
    if order.provider_merchant_id and webhook.merchant_id and webhook.merchant_id != order.provider_merchant_id:
        return False, "bank_payment_merchant_mismatch", "merchantId provider webhook не совпадает с заказом"
    if order.provider_operation_id and webhook.operation_id and webhook.operation_id != order.provider_operation_id:
        return False, "bank_payment_operation_mismatch", "operationId provider webhook не совпадает с заказом"
    return True, "", ""

def _should_confirm_expired_order(*, order: BankPaymentOrder, paid_at) -> bool:
    if order.expires_at >= timezone.now():
        return True
    return bool(paid_at and paid_at <= order.expires_at)

def _webhook_request_body_bytes(request_body) -> bytes:
    if isinstance(request_body, bytes):
        return request_body
    if isinstance(request_body, str):
        return request_body.encode("utf-8")
    return bytes(request_body)

def _replay_duplicate_provider_event_if_deferred(
    event: BankPaymentProviderEvent,
) -> BankPaymentProviderEvent:
    """Let an authenticated redelivery recover a previously deferred event."""
    if event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED:
        _replay_deferred_bank_payment_provider_event(
            event_id=event.id,
            club_id=event.club_id,
        )
        event.refresh_from_db()
    return event

@transaction.atomic
def process_bank_payment_webhook(
    *,
    provider: str,
    request_body: bytes,
    headers,
    request_id: str,
) -> BankPaymentProviderEvent | ProviderWebhookDelivery:
    from apps.billing.payment_providers import get_payment_provider

    if provider not in BankPaymentOrder.Provider.values:
        raise BusinessLogicError("Некорректный провайдер оплаты", code="invalid_payment_provider")
    if provider == BankPaymentOrder.Provider.MOCK and not settings.MOCK_PAYMENT_WEBHOOKS_ENABLED:
        raise BusinessLogicError(
            "Mock webhook оплаты отключён",
            code="mock_payment_webhook_disabled",
        )

    request_body = _webhook_request_body_bytes(request_body)
    max_body_bytes = int(settings.TOCHKA_WEBHOOK_MAX_BODY_BYTES)
    if not 1024 <= max_body_bytes <= 65536 or not request_body or len(request_body) > max_body_bytes:
        raise BusinessLogicError("Некорректный webhook оплаты", code="invalid_payment_webhook")
    payload_hash = hashlib.sha256(request_body).hexdigest()

    adapter = get_payment_provider(provider)
    webhook = adapter.verify_webhook(request_body=request_body, headers=headers)
    delivery, delivery_created = _create_or_get_provider_webhook_delivery(
        provider=provider,
        payload_hash=payload_hash,
        webhook=webhook,
        request_id=request_id,
    )
    if not delivery_created:
        if delivery.provider_event is not None:
            return _replay_duplicate_provider_event_if_deferred(delivery.provider_event)
        return delivery

    payload_duplicate = BankPaymentProviderEvent.objects.unscoped().filter(
        provider=provider,
        payload_hash=payload_hash,
    ).first()
    if payload_duplicate is not None:
        _finish_global_webhook_delivery(
            delivery=delivery,
            outcome=ProviderWebhookDelivery.Outcome.MATCHED,
            provider_event=payload_duplicate,
        )
        return _replay_duplicate_provider_event_if_deferred(payload_duplicate)
    if webhook.event_id:
        duplicate = BankPaymentProviderEvent.objects.unscoped().filter(
            provider=provider,
            provider_event_id=webhook.event_id,
        ).first()
        if duplicate is not None:
            _finish_global_webhook_delivery(
                delivery=delivery,
                outcome=ProviderWebhookDelivery.Outcome.MATCHED,
                provider_event=duplicate,
            )
            return _replay_duplicate_provider_event_if_deferred(duplicate)

    if provider == BankPaymentOrder.Provider.TOCHKA and not _tochka_webhook_is_actionable(webhook):
        _finish_global_webhook_delivery(
            delivery=delivery,
            outcome=ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE,
        )
        logger.warning(
            "provider_webhook_verified_non_actionable",
            extra={"delivery_id": delivery.id, "provider": provider},
        )
        return delivery

    order, identifier_conflict = _resolve_bank_order_for_webhook(
        provider=provider,
        payment_link_id=webhook.payment_link_id,
        operation_id=webhook.operation_id,
    )
    if identifier_conflict:
        _finish_global_webhook_delivery(
            delivery=delivery,
            outcome=ProviderWebhookDelivery.Outcome.VERIFIED_IDENTIFIER_CONFLICT,
        )
        logger.warning(
            "provider_webhook_identifier_conflict",
            extra={"delivery_id": delivery.id, "provider": provider},
        )
        return delivery
    if order is None:
        _finish_global_webhook_delivery(
            delivery=delivery,
            outcome=ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE,
        )
        logger.warning(
            "provider_webhook_order_not_found",
            extra={"delivery_id": delivery.id, "provider": provider},
        )
        return delivery

    received_at = timezone.now()
    normalized_status = adapter.normalize_status(webhook=webhook)
    from apps.attendance.services.training_group_memberships import lock_training_group_payment_scope

    # A selected legacy payment may be canonicalized by S6 while the provider
    # event is in flight.  Lock/defer at the club boundary before inspecting
    # payment ownership so reconciliation never races an approval, failure,
    # cancellation, expiry, or refund notification.
    rollout_state = lock_training_group_payment_scope(club_id=order.club_id)
    from apps.attendance.services.personal_locking import lock_complete_personal_scopes

    ordered_scope = lock_complete_personal_scopes(club_id=order.club_id, order_ids=[order.id])
    order_queryset = BankPaymentOrder.objects.for_club(order.club_id).select_related("payment", "subscription")
    order = (
        order_queryset.get(id=order.id)
        if ordered_scope is not None
        else order_queryset.select_for_update(of=("self",)).get(id=order.id)
    )
    try:
        with transaction.atomic():
            event = BankPaymentProviderEvent.objects.create(
                club_id=order.club_id,
                order=order,
                provider=provider,
                event_type=webhook.event_type or "acquiringInternetPayment",
                provider_event_id=webhook.event_id,
                payload_hash=payload_hash,
                provider_operation_id=webhook.operation_id,
                provider_payment_link_id=webhook.payment_link_id,
                provider_status=webhook.status,
                normalized_status_snapshot=normalized_status,
                provider_paid_at_snapshot=webhook.paid_at,
                amount_snapshot=webhook.amount,
                received_at=received_at,
                request_id=_safe_request_id(request_id),
                redacted_payload_metadata=webhook.metadata,
            )
    except IntegrityError:
        duplicate_q = Q(provider=provider, payload_hash=payload_hash)
        if webhook.event_id:
            duplicate_q |= Q(provider=provider, provider_event_id=webhook.event_id)
        duplicate = BankPaymentProviderEvent.objects.unscoped().filter(duplicate_q).first()
        if duplicate is not None:
            _finish_global_webhook_delivery(
                delivery=delivery,
                outcome=ProviderWebhookDelivery.Outcome.MATCHED,
                provider_event=duplicate,
            )
            return _replay_duplicate_provider_event_if_deferred(duplicate)
        raise

    _finish_global_webhook_delivery(
        delivery=delivery,
        outcome=ProviderWebhookDelivery.Outcome.MATCHED,
        provider_event=event,
    )

    order.provider_status = webhook.status
    if webhook.operation_id and not order.provider_operation_id:
        order.provider_operation_id = webhook.operation_id
    order.save(update_fields=["provider_status", "provider_operation_id", "updated_at"])

    if webhook.event_type and webhook.event_type != "acquiringInternetPayment":
        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.IGNORED
        event.processed_at = timezone.now()
        event.failure_code = "unsupported_webhook_type"
        event.save(update_fields=["processing_status", "processed_at", "failure_code", "updated_at"])
        return event

    matches, failure_code, failure_message = _webhook_matches_order(order=order, webhook=webhook)
    if not matches:
        _mark_order_manual_review(order=order, code=failure_code, message=failure_message)
        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
        event.processed_at = timezone.now()
        event.failure_code = failure_code
        event.failure_message = failure_message
        event.save(
            update_fields=[
                "processing_status",
                "processed_at",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )
        return event

    from apps.attendance.models import TrainingGroupRolloutState

    if (
        rollout_state is not None
        and rollout_state.mode == TrainingGroupRolloutState.Mode.RECONCILING
    ) or (
        rollout_state is None
        and TrainingGroupRolloutState.objects.for_club(order.club_id)
        .filter(mode=TrainingGroupRolloutState.Mode.RECONCILING)
        .exists()
    ):
        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.DEFERRED
        event.save(update_fields=["processing_status", "updated_at"])
        return event

    order_status = _provider_status_to_order_status(normalized_status)
    if normalized_status == "approved":
        if webhook.paid_at is None and order.payment.status != Payment.Status.CONFIRMED:
            # Official SBP callbacks do not contain paidAt. The signed event is
            # stored first; the modular reconciler obtains authenticated timing
            # evidence after this transaction commits.
            request_provider_reconciliation(
                club_id=order.club_id,
                order_id=order.id,
                provider_event_id=event.id,
            )
            transaction.on_commit(
                lambda club_id=order.club_id, order_id=order.id: enqueue_provider_reconciliation(
                    club_id=club_id,
                    order_id=order_id,
                )
            )
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.DEFERRED
            event.failure_code = "bank_payment_reconciliation_pending"
            event.save(update_fields=["processing_status", "failure_code", "updated_at"])
            return event

        paid_at = webhook.paid_at or order.paid_at or received_at
        if order.payment.status not in {Payment.Status.PENDING, Payment.Status.CONFIRMED}:
            _mark_order_manual_review(
                order=order,
                code="bank_payment_approved_after_order_closed",
                message="Provider approved payment after local order artifacts were closed",
            )
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.processed_at = timezone.now()
            event.failure_code = "bank_payment_approved_after_order_closed"
            event.save(update_fields=["processing_status", "processed_at", "failure_code", "updated_at"])
            return event

        if order.status == BankPaymentOrder.Status.APPROVED or order.payment.status == Payment.Status.CONFIRMED:
            order.status = BankPaymentOrder.Status.APPROVED
            if order.paid_at is None:
                order.paid_at = paid_at
            order.last_error_code = ""
            order.last_error_message = ""
            order.save(update_fields=["status", "paid_at", "last_error_code", "last_error_message", "updated_at"])
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_status", "processed_at", "updated_at"])
            return event

        if not _should_confirm_expired_order(order=order, paid_at=webhook.paid_at):
            _mark_order_manual_review(
                order=order,
                code="bank_payment_late_approved_without_paid_at",
                message="Provider approved expired local order without reliable paid_at",
            )
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.processed_at = timezone.now()
            event.failure_code = "bank_payment_late_approved_without_paid_at"
            event.save(update_fields=["processing_status", "processed_at", "failure_code", "updated_at"])
            return event

        try:
            verify_payment(
                payment_id=order.payment_id,
                club_id=order.club_id,
                verified_by_id=None,
                action="confirm",
                verified_at=paid_at,
                allow_online=True,
            )
            from apps.attendance.services import confirm_personal_booking_payment_reservation_for_order

            confirm_personal_booking_payment_reservation_for_order(
                club_id=order.club_id,
                order_id=order.id,
                actor_user_id=None,
            )
        except BusinessLogicError as exc:
            failure_code = _provider_renewal_manual_review_code(exc)
            if failure_code is None:
                raise
            _mark_order_manual_review(order=order, code=failure_code, message=exc.message)
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.processed_at = timezone.now()
            event.failure_code = failure_code
            event.failure_message = exc.message
            event.save(
                update_fields=[
                    "processing_status",
                    "processed_at",
                    "failure_code",
                    "failure_message",
                    "updated_at",
                ]
            )
            return event

        order.status = BankPaymentOrder.Status.APPROVED
        order.paid_at = paid_at
        order.confirmed_by = None
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
        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        event.processed_at = timezone.now()
        event.save(update_fields=["processing_status", "processed_at", "updated_at"])
        return event

    if order_status is not None:
        if order_status in REFUND_REVIEW_ORDER_STATUSES:
            _ensure_refund_case_for_provider_event(
                event=event,
                order=order,
                normalized_status=normalized_status,
            )
            failure_code = f"bank_payment_{normalized_status}_requires_review"
            failure_message = "Provider reported a refund; accounting review is required"
            _mark_order_manual_review(order=order, code=failure_code, message=failure_message)
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.processed_at = timezone.now()
            event.failure_code = failure_code
            event.failure_message = failure_message
            event.save(
                update_fields=[
                    "processing_status",
                    "processed_at",
                    "failure_code",
                    "failure_message",
                    "updated_at",
                ]
            )
            return event
        if (
            order.payment.status == Payment.Status.CONFIRMED
            and order_status
            in {
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.FAILED,
                BankPaymentOrder.Status.EXPIRED,
                BankPaymentOrder.Status.CANCELLED,
            }
        ):
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.IGNORED
            event.processed_at = timezone.now()
            event.failure_code = "bank_payment_status_after_confirmed_ignored"
            event.save(update_fields=["processing_status", "processed_at", "failure_code", "updated_at"])
            return event
        order.status = order_status
        order.save(update_fields=["status", "updated_at"])
        if order_status in {
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.EXPIRED,
            BankPaymentOrder.Status.CANCELLED,
        }:
            bank_orders._cancel_pending_bank_order_artifacts(
                order=order,
                reason=f"bank_payment_provider_{normalized_status}",
            )
        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        event.processed_at = timezone.now()
        event.save(update_fields=["processing_status", "processed_at", "updated_at"])
        return event

    event.processing_status = BankPaymentProviderEvent.ProcessingStatus.IGNORED
    event.processed_at = timezone.now()
    event.failure_code = "unsupported_provider_status"
    event.save(update_fields=["processing_status", "processed_at", "failure_code", "updated_at"])
    return event

def _replay_deferred_bank_payment_provider_event(*, event_id: int, club_id: int) -> str:
    """Apply a stored, already-verified provider event without its raw payload."""
    preview = (
        BankPaymentProviderEvent.objects.for_club(club_id)
        .filter(id=event_id)
        .values("order_id", "order__payment__target_training_group_id")
        .first()
    )
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_payment_scope

        rollout_state = lock_training_group_payment_scope(club_id=club_id)
        order = None
        if preview is not None and preview["order_id"] is not None:
            ordered_scope = lock_complete_personal_scopes(
                club_id=club_id,
                order_ids=[preview["order_id"]],
            )
            order_queryset = BankPaymentOrder.objects.for_club(club_id).select_related(
                "payment",
                "subscription",
            )
            order = (
                order_queryset.get(id=preview["order_id"])
                if ordered_scope is not None
                else order_queryset.select_for_update(of=("self",)).get(id=preview["order_id"])
            )
        event = (
            BankPaymentProviderEvent.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("order__payment", "order__subscription")
            .filter(id=event_id)
            .first()
        )
        if event is None or event.processing_status != BankPaymentProviderEvent.ProcessingStatus.DEFERRED:
            return "skipped"
        if event.order_id is None or order is None:
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.failure_code = "deferred_provider_event_order_missing"
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
            return "failed"
        if event.order_id != order.id:
            raise BusinessLogicError(
                "Deferred provider event order changed before replay could lock it.",
                code="deferred_provider_event_order_changed",
            )

        from apps.attendance.models import TrainingGroupRolloutState

        reconciling = rollout_state.mode == TrainingGroupRolloutState.Mode.RECONCILING
        if reconciling:
            return "deferred"

        normalized_status = event.normalized_status_snapshot
        paid_at = event.provider_paid_at_snapshot or order.paid_at or event.received_at
        order_status = _provider_status_to_order_status(normalized_status)

        if normalized_status == "approved":
            if event.provider_paid_at_snapshot is None and order.payment.status != Payment.Status.CONFIRMED:
                request_provider_reconciliation(
                    club_id=club_id,
                    order_id=order.id,
                    provider_event_id=event.id,
                )
                transaction.on_commit(
                    lambda club_id=order.club_id, order_id=order.id: enqueue_provider_reconciliation(
                        club_id=club_id,
                        order_id=order_id,
                    )
                )
                event.failure_code = "bank_payment_reconciliation_pending"
                event.save(update_fields=["failure_code", "updated_at"])
                return "deferred"
            if order.payment.status not in {Payment.Status.PENDING, Payment.Status.CONFIRMED}:
                _mark_order_manual_review(
                    order=order,
                    code="bank_payment_approved_after_order_closed",
                    message="Deferred provider approval arrived after local artifacts closed",
                )
                event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
                event.failure_code = "bank_payment_approved_after_order_closed"
                event.processed_at = timezone.now()
                event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
                return "failed"
            if order.status != BankPaymentOrder.Status.APPROVED and order.payment.status != Payment.Status.CONFIRMED:
                if not _should_confirm_expired_order(order=order, paid_at=event.provider_paid_at_snapshot):
                    _mark_order_manual_review(
                        order=order,
                        code="bank_payment_late_approved_without_paid_at",
                        message="Deferred provider approval is too late to confirm",
                    )
                    event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
                    event.failure_code = "bank_payment_late_approved_without_paid_at"
                    event.processed_at = timezone.now()
                    event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
                    return "failed"
                try:
                    verify_payment(
                        payment_id=order.payment_id,
                        club_id=club_id,
                        verified_by_id=None,
                        action="confirm",
                        verified_at=paid_at,
                        allow_online=True,
                    )
                    from apps.attendance.services import confirm_personal_booking_payment_reservation_for_order

                    confirm_personal_booking_payment_reservation_for_order(
                        club_id=club_id,
                        order_id=order.id,
                        actor_user_id=None,
                    )
                except BusinessLogicError as exc:
                    failure_code = _provider_renewal_manual_review_code(exc) or exc.code
                    _mark_order_manual_review(order=order, code=failure_code, message=exc.message)
                    event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
                    event.failure_code = failure_code
                    event.failure_message = exc.message
                    event.processed_at = timezone.now()
                    event.save(
                        update_fields=[
                            "processing_status",
                            "failure_code",
                            "failure_message",
                            "processed_at",
                            "updated_at",
                        ]
                    )
                    return "failed"
            order.status = BankPaymentOrder.Status.APPROVED
            order.paid_at = paid_at
            order.confirmed_by = None
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
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_status", "processed_at", "updated_at"])
            return "processed"

        if order_status in REFUND_REVIEW_ORDER_STATUSES:
            _ensure_refund_case_for_provider_event(
                event=event,
                order=order,
                normalized_status=normalized_status,
            )
            _mark_order_manual_review(
                order=order,
                code=f"bank_payment_{normalized_status}_requires_review",
                message="Deferred provider refund requires accounting review",
            )
            event.processing_status = BankPaymentProviderEvent.ProcessingStatus.FAILED
            event.failure_code = f"bank_payment_{normalized_status}_requires_review"
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
            return "failed"

        if order_status is not None:
            if order.payment.status == Payment.Status.CONFIRMED and order_status in {
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.FAILED,
                BankPaymentOrder.Status.EXPIRED,
                BankPaymentOrder.Status.CANCELLED,
            }:
                event.processing_status = BankPaymentProviderEvent.ProcessingStatus.IGNORED
                event.failure_code = "bank_payment_status_after_confirmed_ignored"
            else:
                order.status = order_status
                order.save(update_fields=["status", "updated_at"])
                if order_status in {
                    BankPaymentOrder.Status.FAILED,
                    BankPaymentOrder.Status.EXPIRED,
                    BankPaymentOrder.Status.CANCELLED,
                }:
                    bank_orders._cancel_pending_bank_order_artifacts(
                        order=order,
                        reason=f"bank_payment_provider_{normalized_status}",
                    )
                event.processing_status = BankPaymentProviderEvent.ProcessingStatus.PROCESSED
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
            return event.processing_status

        event.processing_status = BankPaymentProviderEvent.ProcessingStatus.IGNORED
        event.failure_code = "unsupported_provider_status"
        event.processed_at = timezone.now()
        event.save(update_fields=["processing_status", "failure_code", "processed_at", "updated_at"])
        return "ignored"

def replay_deferred_bank_payment_provider_events(*, club_id: int) -> dict[str, int]:
    """Replay validated deferred events once reconciliation is no longer active."""
    event_ids = list(
        BankPaymentProviderEvent.objects.for_club(club_id)
        .filter(processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED)
        .order_by("received_at", "id")
        .values_list("id", flat=True)
    )
    outcomes = {"processed": 0, "failed": 0, "ignored": 0, "deferred": 0, "skipped": 0}
    for event_id in event_ids:
        outcome = _replay_deferred_bank_payment_provider_event(event_id=event_id, club_id=club_id)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes
