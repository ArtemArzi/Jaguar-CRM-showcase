from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    BankPaymentProviderEvent,
    Payment,
    PaymentRefundCase,
    ProviderWebhookDelivery,
    TrainingType,
)
from apps.billing.service_modules import bank_order_review, bank_orders, provider_events
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory

ROOT = Path(__file__).resolve().parents[3]

PROVIDER_REVIEW_DIRECT_LEDGER = (
    (
        "provider_events",
        "process_bank_payment_webhook",
        "invalid_payment_webhook",
        "Некорректный webhook оплаты",
        "test_provider_webhook_direct_errors_have_no_mutation",
    ),
    (
        "provider_events",
        "process_bank_payment_webhook",
        "invalid_payment_provider",
        "Некорректный провайдер оплаты",
        "test_provider_webhook_direct_errors_have_no_mutation",
    ),
    (
        "provider_events",
        "process_bank_payment_webhook",
        "mock_payment_webhook_disabled",
        "Mock webhook оплаты отключён",
        "test_provider_webhook_direct_errors_have_no_mutation",
    ),
    (
        "provider_events",
        "request_provider_reconciliation",
        "tochka_manual_reconciliation_not_available",
        "Сверка с банком недоступна для этой оплаты",
        "test_owner_manual_retry_rejects_non_tochka_at_service_boundary",
    ),
    (
        "provider_events",
        "request_provider_reconciliation",
        "tochka_manual_reconciliation_not_retryable",
        "Эта оплата требует отдельной проверки поддержки",
        "test_owner_manual_retry_rejects_unsupported_creation_state",
    ),
    (
        "provider_events",
        "request_provider_reconciliation",
        "tochka_reconciliation_already_requested",
        "Сверка уже запрошена. Дождитесь результата",
        "test_owner_retry_rejects_immediate_repeat_while_attempt_is_active",
    ),
    (
        "provider_events",
        "request_provider_reconciliation",
        "tochka_reconciliation_cooldown",
        "Сверка уже запрошена. Подождите перед повтором",
        "test_owner_retry_reopens_nonrefund_mismatch_once_per_cooldown",
    ),
    (
        "provider_events",
        "_replay_deferred_bank_payment_provider_event",
        "deferred_provider_event_order_changed",
        "Deferred provider event order changed before replay could lock it.",
        "test_deferred_replay_rejects_changed_locked_order",
    ),
    (
        "bank_order_review",
        "assert_provider_backed_confirmation",
        "tochka_manual_confirm_denied",
        "Оплату Точки можно подтвердить только после сверки с банком",
        "test_tochka_confirmation_guard_error_contract",
    ),
    (
        "bank_order_review",
        "_sanitize_review_evidence",
        "bank_payment_review_invalid_refund_amount",
        "Сумма возврата должна быть больше нуля",
        "test_review_evidence_validation_contract",
    ),
    (
        "bank_order_review",
        "_sanitize_review_evidence",
        "bank_payment_review_invalid_legacy_enrollment",
        "Некорректное зачисление для возврата",
        "test_review_evidence_validation_contract",
    ),
    (
        "bank_order_review",
        "_sanitize_review_evidence",
        "bank_payment_review_invalid_refund_amount",
        "Некорректная сумма возврата",
        "test_review_evidence_validation_contract",
    ),
    (
        "bank_order_review",
        "_sanitize_review_evidence",
        "bank_payment_review_invalid_legacy_enrollment",
        "Некорректное зачисление для возврата",
        "test_review_evidence_validation_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_invalid_resolution",
        "Некорректное решение по спорной ссылке на оплату",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_reason_required",
        "Укажите причину ручного решения по ссылке на оплату",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_order_not_manual_review",
        "Решить вручную можно только ссылку в статусе manual review",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_payment_not_confirmable",
        "Нельзя подтвердить оплату в текущем статусе платежа",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_payment_not_rejectable",
        "Отклонить можно только необработанную онлайн-оплату",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_payment_not_confirmed",
        "Отметить возврат можно только после подтвержденной оплаты",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_payment_not_confirmed",
        "Отметить частичный возврат можно только после подтвержденной оплаты",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_refund_amount_required",
        "Укажите сумму частичного возврата",
        "test_manual_review_direct_error_contract",
    ),
    (
        "bank_order_review",
        "resolve_bank_payment_order_manual_review",
        "bank_payment_review_partial_refund_amount_invalid",
        "Сумма частичного возврата должна быть меньше суммы заказа",
        "test_manual_review_direct_error_contract",
    ),
)

PROVIDER_REVIEW_IMPLICIT_LEDGER = {
    "process_bank_payment_webhook": (
        {
            "trigger": "provider signature rejection before duplicate replay",
            "exception": "apps.common.exceptions.BusinessLogicError",
            "code": "invalid_provider_signature",
            "message": "invalid provider signature",
            "evidence": "test_invalid_signature_cannot_replay_existing_duplicate",
            "interface_call": "process_bank_payment_webhook",
            "mutations_before_failure": "one pre-existing deferred provider event",
            "audit_before_failure": "the original redacted provider-event row",
            "rollback": "rejection leaves the duplicate untouched and never invokes replay",
        },
        {
            "trigger": "provider-event unique constraint replay",
            "exception": "none",
            "code": "none",
            "message": "duplicate delivery returns the original verified event",
            "evidence": (
                "TestBankPaymentOrders."
                "test_authenticated_duplicate_redelivery_replays_deferred_event_after_exit"
            ),
            "interface_call": "process_bank_payment_webhook",
            "mutations_before_failure": "one original provider event",
            "audit_before_failure": "original redacted provider-event row",
            "rollback": "duplicate insert rolls back and returns the existing event",
        },
    ),
    "replay_deferred_bank_payment_provider_events": (
        {
            "trigger": "confirmation delegate BusinessLogicError",
            "exception": "apps.common.exceptions.BusinessLogicError handled as failed review",
            "code": "deferred_confirmation_rejected",
            "message": "deferred confirmation rejected",
            "evidence": "test_deferred_confirmation_failure_is_accounted_for",
            "interface_call": "replay_deferred_bank_payment_provider_events",
            "mutations_before_failure": "deferred provider event and pending financial family",
            "audit_before_failure": "verified deferred provider-event row",
            "rollback": "event/order transition is committed as failed manual review",
        },
    ),
    "resolve_bank_payment_order_manual_review": (
        {
            "trigger": "missing or wrong-club bank order lookup",
            "exception": "apps.billing.models.BankPaymentOrder.DoesNotExist",
            "code": "none",
            "message": "BankPaymentOrder matching query does not exist.",
            "evidence": "test_manual_review_tenant_lookup_escapes_without_mutation",
            "interface_call": "resolve_bank_payment_order_manual_review",
            "mutations_before_failure": "none",
            "audit_before_failure": "none",
            "rollback": "tenant-scoped lookup fails before review or financial mutation",
        },
        {
            "trigger": "refund approval service failure",
            "exception": "apps.common.exceptions.BusinessLogicError propagated",
            "code": "refund_approval_failed",
            "message": "refund approval failed",
            "evidence": "test_manual_review_refund_handoff_failure_rolls_back",
            "interface_call": "resolve_bank_payment_order_manual_review",
            "mutations_before_failure": "locked review order and refund case",
            "audit_before_failure": "provider/refund case evidence exists",
            "rollback": "manual-review transaction rolls back order/refund/review-event mutations",
        },
    ),
}


def _direct_raises(module_name: str) -> list[tuple[str, str, str]]:
    path = ROOT / f"apps/billing/service_modules/{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                not isinstance(node, ast.Raise)
                or not isinstance(node.exc, ast.Call)
                or not isinstance(node.exc.func, ast.Name)
                or node.exc.func.id != "BusinessLogicError"
            ):
                continue
            code = next(
                keyword.value.value
                for keyword in node.exc.keywords
                if keyword.arg == "code" and isinstance(keyword.value, ast.Constant)
            )
            result.append((function.name, code, node.exc.args[0].value))
    return sorted(result)


def _evidence_node(name: str) -> ast.FunctionDef | None:
    node_name = name.rsplit(".", 1)[-1]
    for relative in (
        "apps/billing/tests/test_provider_error_contracts.py",
        "apps/billing/tests/test_bank_payment_orders.py",
        "apps/billing/tests/test_provider_reconciliation.py",
        "apps/billing/tests/test_refunds.py",
        "apps/billing/tests/test_tochka_webhooks.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == node_name:
                return node
    return None


def test_provider_review_error_ledger_is_ast_complete_and_structured():
    for module_name in ("provider_events", "bank_order_review"):
        expected = sorted(
            (function, code, message)
            for module, function, code, message, _evidence in PROVIDER_REVIEW_DIRECT_LEDGER
            if module == module_name
        )
        assert _direct_raises(module_name) == expected
    assert set(PROVIDER_REVIEW_IMPLICIT_LEDGER) == {
        "process_bank_payment_webhook",
        "replay_deferred_bank_payment_provider_events",
        "resolve_bank_payment_order_manual_review",
    }
    for module, function, code, message, evidence in PROVIDER_REVIEW_DIRECT_LEDGER:
        node = _evidence_node(evidence)
        assert node is not None, evidence
        calls = {
            (
                candidate.func.id
                if isinstance(candidate.func, ast.Name)
                else candidate.func.attr
            )
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name | ast.Attribute)
        }
        expected_calls = {function}
        if function in {
            "_sanitize_review_evidence",
            "_replay_deferred_bank_payment_provider_event",
        }:
            expected_calls.add(
                "resolve_bank_payment_order_manual_review"
                if module == "bank_order_review"
                else "_replay_deferred_bank_payment_provider_event"
            )
        assert calls & expected_calls, f"{evidence} does not call {sorted(expected_calls)}"
        literals = {
            candidate.value
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
        }
        assert code in literals, f"{evidence} lacks {code}"
        assert message in literals, f"{evidence} lacks {message}"

    required = {
        "trigger",
        "exception",
        "code",
        "message",
        "evidence",
        "interface_call",
        "mutations_before_failure",
        "audit_before_failure",
        "rollback",
    }
    assert all(
        required <= set(branch)
        and all(isinstance(branch[field], str) and branch[field] for field in required)
        for branches in PROVIDER_REVIEW_IMPLICIT_LEDGER.values()
        for branch in branches
    )
    for interface, branches in PROVIDER_REVIEW_IMPLICIT_LEDGER.items():
        for branch in branches:
            node = _evidence_node(branch["evidence"])
            assert node is not None, branch["evidence"]
            calls = {
                (
                    candidate.func.id
                    if isinstance(candidate.func, ast.Name)
                    else candidate.func.attr
                )
                for candidate in ast.walk(node)
                if isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Name | ast.Attribute)
            }
            assert branch["interface_call"] == interface
            assert interface in calls, (
                f"{branch['evidence']} does not call {interface}"
            )
            literals = {
                candidate.value
                for candidate in ast.walk(node)
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
            }
            if branch["code"] != "none":
                assert branch["code"] in literals, (
                    f"{branch['evidence']} lacks {branch['code']}"
                )
            if branch["code"] != "none" or branch["exception"] != "none":
                assert branch["message"] in literals, (
                    f"{branch['evidence']} lacks {branch['message']}"
                )


@pytest.mark.django_db
def test_provider_webhook_direct_errors_have_no_mutation(settings, club):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    with pytest.raises(BusinessLogicError) as invalid_provider:
        provider_events.process_bank_payment_webhook(
            provider="invalid",
            request_body=b"{}",
            headers={},
            request_id="invalid-provider",
        )
    assert invalid_provider.value.code == "invalid_payment_provider"
    assert invalid_provider.value.message == "Некорректный провайдер оплаты"

    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = False
    with pytest.raises(BusinessLogicError) as mock_disabled:
        provider_events.process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=b'{}',
            headers={},
            request_id="mock-disabled",
        )
    assert mock_disabled.value.code == "mock_payment_webhook_disabled"
    assert mock_disabled.value.message == "Mock webhook оплаты отключён"

    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True
    with pytest.raises(BusinessLogicError) as invalid_webhook:
        provider_events.process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=b"",
            headers={},
            request_id="invalid-webhook",
        )
    assert invalid_webhook.value.code == "invalid_payment_webhook"
    assert invalid_webhook.value.message == "Некорректный webhook оплаты"

    body = json.dumps(
        {
            "webhookType": "acquiringInternetPayment",
            "event_id": "missing-order",
            "status": "APPROVED",
            "paymentLinkId": "missing-link",
            "operationId": "missing-operation",
            "amount": "1.00",
        }
    ).encode()
    delivery = provider_events.process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=body,
        headers={},
        request_id="missing-order",
    )
    assert isinstance(delivery, ProviderWebhookDelivery)
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE
    assert BankPaymentProviderEvent.objects.for_club(club).count() == 0


def test_tochka_confirmation_guard_error_contract():
    order = SimpleNamespace(provider=BankPaymentOrder.Provider.TOCHKA)

    with pytest.raises(BusinessLogicError) as exc_info:
        bank_order_review.assert_provider_backed_confirmation(
            order=order,
            resolution=BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
        )

    assert exc_info.value.code == "tochka_manual_confirm_denied"
    assert (
        exc_info.value.message
        == "Оплату Точки можно подтвердить только после сверки с банком"
    )


@pytest.mark.django_db
def test_provider_adapter_error_propagates_without_mutation(settings, club):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    adapter = MagicMock()
    adapter.verify_webhook.side_effect = BusinessLogicError(
        "provider webhook rejected",
        code="provider_webhook_rejected",
    )
    with (
        patch(
            "apps.billing.payment_providers.get_payment_provider",
            return_value=adapter,
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider_events.process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=b"untrusted",
            headers={"X-Untrusted": "redacted"},
            request_id="provider-rejected",
        )
    assert exc_info.value.code == "provider_webhook_rejected"
    assert exc_info.value.message == "provider webhook rejected"
    assert BankPaymentProviderEvent.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_invalid_signature_cannot_replay_existing_duplicate(settings, club):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    request_body = b"untrusted duplicate payload"
    event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=None,
        provider=BankPaymentOrder.Provider.MOCK,
        event_type="acquiringInternetPayment",
        payload_hash=hashlib.sha256(request_body).hexdigest(),
        provider_status="APPROVED",
        normalized_status_snapshot="approved",
        received_at=timezone.now(),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        redacted_payload_metadata={},
    )
    adapter = MagicMock()
    adapter.verify_webhook.side_effect = BusinessLogicError(
        "invalid provider signature",
        code="invalid_provider_signature",
    )
    with (
        patch(
            "apps.billing.payment_providers.get_payment_provider",
            return_value=adapter,
        ),
        patch.object(
            provider_events,
            "_replay_duplicate_provider_event_if_deferred",
        ) as replay_duplicate,
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider_events.process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=request_body,
            headers={"X-Untrusted-Signature": "redacted"},
            request_id="invalid-signature-duplicate",
        )
    assert exc_info.value.code == "invalid_provider_signature"
    assert exc_info.value.message == "invalid provider signature"
    adapter.verify_webhook.assert_called_once_with(
        request_body=request_body,
        headers={"X-Untrusted-Signature": "redacted"},
    )
    replay_duplicate.assert_not_called()
    event.refresh_from_db()
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
    assert event.failure_code == ""
    assert BankPaymentProviderEvent.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_deferred_replay_rejects_changed_locked_order(club):
    preview_qs = MagicMock()
    preview_qs.filter.return_value.values.return_value.first.return_value = {
        "order_id": 1,
        "order__payment__target_training_group_id": None,
    }
    event = SimpleNamespace(
        processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        order_id=2,
    )
    event_qs = MagicMock()
    event_qs.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = event
    event_manager = MagicMock(side_effect=[preview_qs, event_qs])
    order = SimpleNamespace(id=1)
    order_qs = MagicMock()
    order_qs.select_for_update.return_value.select_related.return_value.get.return_value = order

    with (
        patch.object(BankPaymentProviderEvent.objects, "for_club", event_manager),
        patch.object(BankPaymentOrder.objects, "for_club", return_value=order_qs),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider_events._replay_deferred_bank_payment_provider_event(
            event_id=1,
            club_id=club.id,
        )
    assert exc_info.value.code == "deferred_provider_event_order_changed"
    assert (
        exc_info.value.message
        == "Deferred provider event order changed before replay could lock it."
    )


@pytest.mark.django_db
def test_deferred_confirmation_failure_is_accounted_for(
    settings,
    club,
    owner_user,
):
    order = _create_review_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=BankPaymentOrder.Provider.MOCK,
        event_type="acquiringInternetPayment",
        provider_event_id="deferred-confirmation-rejected",
        payload_hash="a" * 64,
        provider_status="APPROVED",
        normalized_status_snapshot="approved",
        provider_paid_at_snapshot=timezone.now(),
        amount_snapshot=order.amount_snapshot,
        received_at=timezone.now(),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        redacted_payload_metadata={},
    )
    with patch.object(
        provider_events,
        "verify_payment",
        side_effect=BusinessLogicError(
            "deferred confirmation rejected",
            code="deferred_confirmation_rejected",
        ),
    ):
        outcomes = provider_events.replay_deferred_bank_payment_provider_events(
            club_id=club.id,
        )

    assert outcomes["failed"] == 1
    event.refresh_from_db()
    order.refresh_from_db()
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
    assert event.failure_code == "deferred_confirmation_rejected"
    assert event.failure_message == "deferred confirmation rejected"
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert order.last_error_code == "deferred_confirmation_rejected"
    assert order.last_error_message == "deferred confirmation rejected"

    replay_outcomes = provider_events.replay_deferred_bank_payment_provider_events(
        club_id=club.id,
    )
    event.refresh_from_db()
    order.refresh_from_db()
    assert replay_outcomes == {
        "processed": 0,
        "failed": 0,
        "ignored": 0,
        "deferred": 0,
        "skipped": 0,
    }
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
    assert event.failure_code == "deferred_confirmation_rejected"
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW


@pytest.mark.parametrize(
    ("evidence", "code", "message"),
    [
        (
            {"refund_amount": "0"},
            "bank_payment_review_invalid_refund_amount",
            "Сумма возврата должна быть больше нуля",
        ),
        (
            {"refund_amount": "not-money"},
            "bank_payment_review_invalid_refund_amount",
            "Некорректная сумма возврата",
        ),
        (
            {"legacy_enrollment_id": "not-an-id"},
            "bank_payment_review_invalid_legacy_enrollment",
            "Некорректное зачисление для возврата",
        ),
        (
            {"legacy_enrollment_id": "0"},
            "bank_payment_review_invalid_legacy_enrollment",
            "Некорректное зачисление для возврата",
        ),
    ],
)
def test_review_evidence_validation_contract(evidence, code, message):
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_order_review._sanitize_review_evidence(evidence)
    assert exc_info.value.code == code
    assert exc_info.value.message == message


def _create_review_order(*, settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    order = bank_orders.create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    return order


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "resolution", "code", "message"),
    [
        (
            "invalid_resolution",
            "invalid",
            "bank_payment_review_invalid_resolution",
            "Некорректное решение по спорной ссылке на оплату",
        ),
        (
            "blank_reason",
            BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
            "bank_payment_review_reason_required",
            "Укажите причину ручного решения по ссылке на оплату",
        ),
        (
            "not_review",
            BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
            "bank_payment_order_not_manual_review",
            "Решить вручную можно только ссылку в статусе manual review",
        ),
        (
            "confirm_rejected",
            BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
            "bank_payment_review_payment_not_confirmable",
            "Нельзя подтвердить оплату в текущем статусе платежа",
        ),
        (
            "reject_confirmed",
            BankPaymentOrderReviewEvent.Resolution.REJECT,
            "bank_payment_review_payment_not_rejectable",
            "Отклонить можно только необработанную онлайн-оплату",
        ),
        (
            "refund_pending",
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
            "bank_payment_review_payment_not_confirmed",
            "Отметить возврат можно только после подтвержденной оплаты",
        ),
        (
            "partial_pending",
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
            "bank_payment_review_payment_not_confirmed",
            "Отметить частичный возврат можно только после подтвержденной оплаты",
        ),
        (
            "partial_missing",
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
            "bank_payment_review_refund_amount_required",
            "Укажите сумму частичного возврата",
        ),
        (
            "partial_full",
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
            "bank_payment_review_partial_refund_amount_invalid",
            "Сумма частичного возврата должна быть меньше суммы заказа",
        ),
    ],
)
def test_manual_review_direct_error_contract(
    settings,
    club,
    owner_user,
    scenario,
    resolution,
    code,
    message,
):
    order = _create_review_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    reason = "reviewed"
    evidence = None
    if scenario == "blank_reason":
        reason = " "
    if scenario not in {"invalid_resolution", "blank_reason", "not_review"}:
        order.status = BankPaymentOrder.Status.MANUAL_REVIEW
        order.save(update_fields=["status", "updated_at"])
    if scenario in {"confirm_rejected"}:
        order.payment.status = Payment.Status.REJECTED
        order.payment.save(update_fields=["status", "updated_at"])
    if scenario in {"reject_confirmed", "partial_missing", "partial_full"}:
        order.payment.status = Payment.Status.CONFIRMED
        order.payment.save(update_fields=["status", "updated_at"])
    if scenario == "partial_full":
        evidence = {"refund_amount": str(order.amount_snapshot)}

    with pytest.raises(BusinessLogicError) as exc_info:
        bank_order_review.resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=resolution,
            reason=reason,
            evidence=evidence,
        )
    assert exc_info.value.code == code
    assert exc_info.value.message == message
    order.refresh_from_db()
    assert order.status != BankPaymentOrder.Status.APPROVED
    assert BankPaymentOrderReviewEvent.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_manual_review_tenant_lookup_escapes_without_mutation(
    settings,
    club,
    owner_user,
):
    order = _create_review_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    other_club = ClubFactory()
    for club_id, order_id in (
        (other_club.id, order.id),
        (club.id, order.id + 999_999),
    ):
        with pytest.raises(BankPaymentOrder.DoesNotExist) as exc_info:
            bank_order_review.resolve_bank_payment_order_manual_review(
                club_id=club_id,
                order_id=order_id,
                actor_user_id=owner_user.id,
                resolution=BankPaymentOrderReviewEvent.Resolution.REJECT,
                reason="tenant lookup check",
                evidence={},
            )
        assert str(exc_info.value) == "BankPaymentOrder matching query does not exist."

    order.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.PENDING
    assert order.payment.status == Payment.Status.PENDING
    assert BankPaymentOrderReviewEvent.objects.unscoped().count() == 0
    assert PaymentRefundCase.objects.unscoped().count() == 0


@pytest.mark.django_db
def test_manual_review_refund_handoff_failure_rolls_back(
    settings,
    club,
    owner_user,
):
    order = _create_review_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.payment.status = Payment.Status.CONFIRMED
    order.payment.save(update_fields=["status", "updated_at"])
    order.save(update_fields=["status", "updated_at"])

    with (
        patch(
            "apps.billing.refund_services.approve_payment_refund_case",
            side_effect=BusinessLogicError(
                "refund approval failed",
                code="refund_approval_failed",
            ),
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_order_review.resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
            reason="provider statement checked",
            evidence={},
        )
    assert exc_info.value.code == "refund_approval_failed"
    assert exc_info.value.message == "refund approval failed"
    order.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert PaymentRefundCase.objects.for_club(club).count() == 0
    assert BankPaymentOrderReviewEvent.objects.for_club(club).count() == 0
