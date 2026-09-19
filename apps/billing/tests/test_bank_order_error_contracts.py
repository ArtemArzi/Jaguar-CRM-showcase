from __future__ import annotations

import ast
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    ScheduleEnrollment,
    TrainingGroupRolloutState,
)
from apps.attendance.tests.factories import (
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupRolloutStateFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import BankPaymentOrder, Debt, Payment, Subscription, Tariff, TrainingType
from apps.billing.service_modules import bank_orders
from apps.billing.service_modules.renewals import build_subscription_command_fingerprint
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import Trainer
from apps.trainers.tests.factories import TrainerFactory

ROOT = Path(__file__).resolve().parents[3]

BANK_ORDER_DIRECT_ERROR_LEDGER = (
    (
        "_assert_locked_exact_group_renewal_target",
        "renewal_group_membership_required",
        "Точное групповое продление требует действующее участие в выбранной группе",
        "test_exact_group_renewal_target_requires_live_membership",
    ),
    (
        "_assert_no_live_non_sbp_bank_payment_order",
        "bank_payment_order_legacy_payment_mode",
        "Активная ссылка использует недоступный способ оплаты. Отмените её и создайте новую",
        "test_noncanonical_live_order_blocks_new_family_when_reuse_is_disabled",
    ),
    (
        "_receipt_mode",
        "invalid_receipt_mode",
        "Некорректный режим чеков",
        "test_receipt_mode_rejects_unknown_value",
    ),
    (
        "create_bank_payment_order",
        "bank_payment_order_expiry_elapsed",
        "Срок этой оплаты уже истёк",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "create_bank_payment_order",
        "bank_payment_order_ttl_too_short",
        "До окончания записи недостаточно времени для безопасной ссылки СБП",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "create_bank_payment_order",
        "invalid_payment_source",
        "Некорректный источник оплаты",
        "test_create_bank_order_direct_preconditions_are_stable",
    ),
    (
        "create_bank_payment_order",
        "target_schedule_required",
        "Для групповой оплаты укажите группу и дату старта",
        "test_create_bank_order_direct_preconditions_are_stable",
    ),
    (
        "create_bank_payment_order",
        "mock_payment_order_creation_disabled",
        "Mock-провайдер не может создавать платёжные ссылки в этом окружении",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "create_bank_payment_order",
        "online_payment_order_creation_disabled",
        "Создание онлайн-оплаты временно отключено",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "create_bank_payment_order",
        "payment_subscription_required",
        "Для онлайн-оплаты нужен абонемент",
        "test_create_bank_order_requires_subscription_and_rolls_back",
    ),
    (
        "create_bank_payment_order",
        "receipt_buyer_email_required",
        "Для фискального чека нужен email покупателя",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "create_bank_payment_order",
        "tochka_payment_creation_not_ready",
        "Онлайн-оплата Точки отключена перед отправкой",
        "test_tochka_creation_rechecks_kill_switch_immediately_before_dispatch",
    ),
    (
        "create_bank_payment_order",
        "tochka_payment_creation_not_ready",
        "Онлайн-оплата Точки пока не готова к созданию ссылок",
        "test_create_bank_order_payment_preflight_errors_are_stable",
    ),
    (
        "_find_reusable_bank_payment_order",
        "bank_payment_order_private_intent_exists",
        "У ученика уже есть самостоятельная ссылка на эту оплату",
        "test_trainer_cannot_receive_a_private_self_service_link_while_it_blocks_duplicates",
    ),
    (
        "_find_reusable_bank_payment_order",
        "bank_payment_order_pending_exists",
        "У ученика уже есть активная ссылка на оплату с другим составом",
        "test_pending_order_with_different_debt_selection_blocks_second_order",
    ),
    (
        "_find_reusable_bank_payment_order",
        "bank_payment_order_pending_family_changed",
        "Активная ссылка на оплату изменилась и требует ручной проверки",
        "test_reuse_rejects_drifted_locked_financial_family",
    ),
    (
        "_find_reusable_bank_payment_order",
        "bank_payment_order_target_changed",
        "Целевая группа изменилась, выберите группу заново",
        "test_reuse_rejects_changed_locked_group_target",
    ),
    (
        "_find_reusable_bank_payment_order",
        "bank_payment_order_target_changed",
        "Целевая группа изменилась, выберите группу заново",
        "test_reuse_rejects_changed_locked_group_target",
    ),
    (
        "close_pending_personal_drop_in_financial_family",
        "personal_drop_in_bank_order_mismatch",
        "Personal payment order does not match its payment link",
        "test_close_personal_financial_family_integrity_error_contracts",
    ),
    (
        "close_pending_personal_drop_in_financial_family",
        "personal_drop_in_lock_scope_changed",
        "Personal booking ownership changed while closing pending payment",
        "test_close_personal_financial_family_integrity_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "bank_payment_order_live_command_conflict",
        "Для этой оплаты уже существует активная ссылка с другим ключом команды",
        "test_live_order_rejects_distinct_command_key_without_mutation",
    ),
    (
        "create_bank_payment_order",
        "command_replay_order_missing",
        "Команда оплаты требует ручной проверки",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "command_replay_order_missing",
        "Команда оплаты требует ручной проверки",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "idempotency_conflict",
        "Idempotency key was already used for another command",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "idempotency_conflict",
        "Idempotency key was already used for another command",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "invalid_command_identity",
        "Некорректная идентичность команды",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "personal_drop_in_booking_not_found",
        "Personal booking was not found",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "personal_drop_in_debt_changed",
        "Personal booking debt changed before payment creation",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "personal_drop_in_payment_not_actionable",
        "Personal booking is no longer payable",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "personal_terms_tariff_mismatch",
        "Personal payment terms do not match the requested tariff",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_debt_not_allowed",
        "Продление не может принять произвольный долг",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_group_target_required",
        "Групповое продление требует точную группу, слот и дату старта",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_source_not_allowed",
        "Продление нельзя привязать к персональной записи",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_source_not_found",
        "Абонемент для продления не найден",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_source_required",
        "Укажите источник продления",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_source_required",
        "Укажите источник продления",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_tariff_mismatch",
        "Тариф продления изменился",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_tariff_mismatch",
        "Тариф продления не совпадает с источником",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "_legacy_renewal_source_preview",
        "renewal_source_ambiguous",
        "Источник продления требует ручного выбора",
        "test_legacy_source_preview_rejects_ambiguous_accepted_order_family",
    ),
    (
        "_legacy_renewal_source_preview",
        "renewal_source_ambiguous",
        "Источник продления требует ручного выбора",
        "test_legacy_source_preview_rejects_ambiguous_historical_sources",
    ),
    (
        "create_bank_payment_order",
        "invalid_payment_source",
        "Некорректный источник оплаты",
        "test_create_bank_order_direct_preconditions_are_stable",
    ),
    (
        "create_bank_payment_order",
        "renewal_group_target_required",
        "Групповое продление требует точную группу, слот и дату старта",
        "test_unified_bank_order_precondition_error_contracts",
    ),
    (
        "create_bank_payment_order",
        "renewal_offer_stale",
        "Тариф продления больше недоступен",
        "test_create_bank_order_rejects_stale_current_offer_without_mutation",
    ),
    (
        "create_bank_payment_order",
        "renewal_source_finalized_successor",
        "У этого абонемента уже есть ожидающее или подтверждённое продление",
        "test_create_bank_order_rejects_finalized_successor_without_mutation",
    ),
    (
        "_assert_self_service_bank_order_allowed",
        "self_service_tariff_not_allowed",
        "Тариф недоступен для самостоятельного продления",
        "test_self_service_guards_preserve_exact_error_contract",
    ),
    (
        "_assert_self_service_bank_order_allowed",
        "self_service_discount_not_supported",
        "Самостоятельная оплата со скидкой пока недоступна",
        "test_self_service_guards_preserve_exact_error_contract",
    ),
    (
        "_assert_self_service_bank_order_allowed",
        "self_service_debt_payment_not_supported",
        "Самостоятельная оплата долгов пока недоступна",
        "test_self_service_guards_preserve_exact_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_not_found",
        "Ссылка на оплату не найдена",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_forbidden",
        "Ссылка на оплату недоступна",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_cancel_source_forbidden",
        "Ссылку на оплату нельзя отменить в этом разделе",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_not_cancellable",
        "Можно отменить только активную неоплаченную ссылку",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_provider_dispatch_not_cancellable",
        "Ссылка Точки уже могла быть выдана банком и ждёт оплаты или истечения",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_payment_not_pending",
        "Оплата по этой ссылке уже обработана",
        "test_cancel_bank_order_direct_error_contract",
    ),
    (
        "cancel_bank_payment_order",
        "bank_payment_order_subscription_not_pending",
        "Абонемент по этой ссылке уже обработан",
        "test_cancel_bank_order_direct_error_contract",
    ),
)

BANK_ORDER_IMPLICIT_ERROR_LEDGER = {
    "create_bank_payment_order": (
        {
            "trigger": "invalid provider adapter selection before transaction",
            "exception": "apps.common.exceptions.BusinessLogicError",
            "code": "invalid_payment_provider",
            "message": "Некорректный провайдер оплаты",
            "evidence": "test_create_bank_order_rejects_invalid_provider_without_mutation",
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "none",
            "audit_before_failure": "none",
            "rollback": "not applicable; provider selection precedes the transaction",
        },
        {
            "trigger": "tenant tariff lookup",
            "exception": "apps.billing.models.Tariff.DoesNotExist",
            "code": "none",
            "message": "Tariff matching query does not exist.",
            "evidence": "test_create_bank_order_propagates_tenant_lookup_without_mutation",
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "none",
            "audit_before_failure": "none",
            "rollback": "atomic transaction exits without Payment, Subscription or order rows",
        },
        {
            "trigger": "downstream create_payment business precondition",
            "exception": "apps.common.exceptions.BusinessLogicError",
            "code": "downstream_payment_rejected",
            "message": "downstream payment rejected",
            "evidence": "test_create_bank_order_preserves_downstream_payment_error_and_rollback",
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "transient Payment and Subscription rows",
            "audit_before_failure": "none",
            "rollback": "the outer order transaction rolls back downstream payment mutations",
        },
        {
            "trigger": "provider adapter BusinessLogicError after financial family commit",
            "exception": "apps.common.exceptions.BusinessLogicError",
            "code": "provider_rejected",
            "message": "provider rejected",
            "evidence": (
                "TestBankPaymentOrders."
                "test_provider_create_failure_rejects_pending_artifacts_and_releases_reserved_debt"
            ),
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "order, payment, subscription and debt reservation exist",
            "audit_before_failure": "reserved debt event exists",
            "rollback": "cleanup commits failed order, rejected payment, deleted subscription and released debt",
        },
        {
            "trigger": "unexpected provider adapter exception after financial family commit",
            "exception": "builtins.RuntimeError",
            "code": "provider_unexpected_error",
            "message": "network exploded",
            "evidence": "TestBankPaymentOrders.test_provider_unexpected_failure_rejects_pending_artifacts",
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "order, payment and subscription exist",
            "audit_before_failure": "none for the no-debt fixture",
            "rollback": "cleanup commits failed order, rejected payment and deleted subscription",
        },
        {
            "trigger": "linked disposition fails during deterministic provider cleanup",
            "exception": "builtins.RuntimeError",
            "code": "none",
            "message": "reservation cleanup failed",
            "evidence": "test_provider_failure_cleanup_rolls_back_terminal_and_financial_mutations",
            "interface_call": "create_bank_payment_order",
            "mutations_before_failure": "transient failed order, rejected payment and deleted subscription",
            "audit_before_failure": "none for the no-debt fixture",
            "rollback": "order, payment and subscription remain one coherent pending family for cancel or expiry",
        },
    ),
    "cancel_bank_payment_order": (
        {
            "trigger": "artifact cleanup fails inside the cancel transaction",
            "exception": "builtins.RuntimeError",
            "code": "none",
            "message": "cleanup failed after cancel",
            "evidence": "test_cancel_cleanup_failure_rolls_back_order_and_financial_family",
            "interface_call": "cancel_bank_payment_order",
            "mutations_before_failure": "transient cancelled order status inside the atomic block",
            "audit_before_failure": "none from the injected cleanup failure",
            "rollback": "order, payment and subscription remain in their original pending state",
        },
    ),
    "expire_bank_payment_orders": (
        {
            "trigger": "artifact cleanup fails inside per-order expiry transaction",
            "exception": "builtins.RuntimeError",
            "code": "none",
            "message": "cleanup failed during expiry",
            "evidence": "test_expire_cleanup_failure_rolls_back_current_order",
            "interface_call": "expire_bank_payment_orders",
            "mutations_before_failure": "transient expired status inside the atomic block",
            "audit_before_failure": "none from the injected cleanup failure",
            "rollback": "the current order rolls back to pending and the exception stops the batch",
        },
        {
            "trigger": "terminal status recheck and successful expiry count",
            "exception": "none",
            "code": "none",
            "message": "returns one expired order",
            "evidence": (
                "TestBankPaymentOrders."
                "test_expire_bank_payment_orders_rejects_pending_artifacts_and_releases_reserved_debt"
            ),
            "interface_call": "expire_bank_payment_orders",
            "mutations_before_failure": "not a failure; order/payment/subscription/debt reach terminal state",
            "audit_before_failure": "reserved and rejected debt events are retained",
            "rollback": "not applicable; successful return contract is one",
        },
        {
            "trigger": "selected order disappears before row lock",
            "exception": "unreachable",
            "code": "none",
            "message": "BankPaymentOrder financial history is not hard-deleted",
            "unreachable_proof": (
                "The model has no hard-delete lifecycle and project policy forbids deleting "
                "financial history; the id list and lock run in the same process without a "
                "supported concurrent deletion path."
            ),
            "mutations_before_failure": "none",
            "audit_before_failure": "none",
            "rollback": "not applicable because the state is unreachable by supported writes",
        },
    ),
}


def _direct_raises() -> list[tuple[str, str, str]]:
    path = ROOT / "apps/billing/service_modules/bank_orders.py"
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
            message = node.exc.args[0].value
            result.append((function.name, code, message))
    return sorted(result)


def _evidence_node(name: str) -> ast.FunctionDef | None:
    node_name = name.rsplit(".", 1)[-1]
    for relative in (
        "apps/billing/tests/test_bank_order_error_contracts.py",
        "apps/billing/tests/test_bank_payment_orders.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == node_name:
                return node
    return None


def test_bank_order_error_ledger_exhausts_direct_and_implicit_interfaces():
    expected_direct = sorted(
        (function, code, message) for function, code, message, _evidence in BANK_ORDER_DIRECT_ERROR_LEDGER
    )
    assert _direct_raises() == expected_direct
    assert set(BANK_ORDER_IMPLICIT_ERROR_LEDGER) == {
        "create_bank_payment_order",
        "cancel_bank_payment_order",
        "expire_bank_payment_orders",
    }
    for function, code, message, evidence in BANK_ORDER_DIRECT_ERROR_LEDGER:
        node = _evidence_node(evidence)
        assert node is not None, evidence
        calls = {
            (candidate.func.id if isinstance(candidate.func, ast.Name) else candidate.func.attr)
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name | ast.Attribute)
        }
        expected_calls = {function}
        if function in {
            "_assert_no_live_non_sbp_bank_payment_order",
            "_find_reusable_bank_payment_order",
        }:
            expected_calls.add("create_bank_payment_order")
        assert calls & expected_calls, f"{evidence} does not call {sorted(expected_calls)}"
        literals = {
            candidate.value
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
        }
        assert code in literals, f"{evidence} lacks {code}"
        assert message in literals, f"{evidence} lacks {message}"
    required_implicit_fields = {
        "trigger",
        "exception",
        "code",
        "message",
        "mutations_before_failure",
        "audit_before_failure",
        "rollback",
    }
    for interface, branches in BANK_ORDER_IMPLICIT_ERROR_LEDGER.items():
        for branch in branches:
            assert required_implicit_fields <= set(branch)
            assert all(isinstance(branch[field], str) and branch[field] for field in required_implicit_fields)
            evidence_kinds = {"evidence", "unreachable_proof"} & set(branch)
            assert len(evidence_kinds) == 1
            if "unreachable_proof" in branch:
                assert branch["exception"] == "unreachable"
                assert len(branch["unreachable_proof"]) > 40
                continue

            node = _evidence_node(branch["evidence"])
            assert node is not None, branch["evidence"]
            calls = {
                (candidate.func.id if isinstance(candidate.func, ast.Name) else candidate.func.attr)
                for candidate in ast.walk(node)
                if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name | ast.Attribute)
            }
            assert branch["interface_call"] == interface
            assert interface in calls, f"{branch['evidence']} does not call {interface}"
            if branch["exception"] == "none":
                continue
            literals = {
                candidate.value
                for candidate in ast.walk(node)
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
            }
            if branch["code"] != "none":
                assert branch["code"] in literals, f"{branch['evidence']} lacks {branch['code']}"
            assert branch["message"] in literals, f"{branch['evidence']} lacks {branch['message']}"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "code", "message"),
    [
        ("identity", "invalid_command_identity", "Некорректная идентичность команды"),
        (
            "replay_conflict",
            "idempotency_conflict",
            "Idempotency key was already used for another command",
        ),
        (
            "replay_missing_order",
            "command_replay_order_missing",
            "Команда оплаты требует ручной проверки",
        ),
        (
            "renewal_personal",
            "renewal_source_not_allowed",
            "Продление нельзя привязать к персональной записи",
        ),
        (
            "renewal_debt",
            "renewal_debt_not_allowed",
            "Продление не может принять произвольный долг",
        ),
        (
            "renewal_group_target",
            "renewal_group_target_required",
            "Групповое продление требует точную группу, слот и дату старта",
        ),
        (
            "renewal_missing",
            "renewal_source_not_found",
            "Абонемент для продления не найден",
        ),
        (
            "renewal_preview_tariff",
            "renewal_tariff_mismatch",
            "Тариф продления не совпадает с источником",
        ),
        ("renewal_required", "renewal_source_required", "Укажите источник продления"),
        (
            "personal_terms_tariff",
            "personal_terms_tariff_mismatch",
            "Personal payment terms do not match the requested tariff",
        ),
        (
            "personal_booking_missing",
            "personal_drop_in_booking_not_found",
            "Personal booking was not found",
        ),
        (
            "personal_booking_closed",
            "personal_drop_in_payment_not_actionable",
            "Personal booking is no longer payable",
        ),
        (
            "personal_debt_changed",
            "personal_drop_in_debt_changed",
            "Personal booking debt changed before payment creation",
        ),
        (
            "renewal_locked_tariff",
            "renewal_tariff_mismatch",
            "Тариф продления изменился",
        ),
    ],
)
def test_unified_bank_order_precondition_error_contracts(
    settings,
    club,
    owner_user,
    scenario,
    code,
    message,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)
    kwargs = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
    }
    contexts = []

    if scenario == "identity":
        kwargs["command_idempotency_key"] = "x" * 121
    elif scenario in {"replay_conflict", "replay_missing_order"}:
        key = f"bank-order-{scenario}"
        fingerprint = build_subscription_command_fingerprint(
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.ONLINE,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            payment_method=Payment.Method.ONLINE,
            command_idempotency_key=key,
            command_fingerprint=("0" * 64 if scenario == "replay_conflict" else fingerprint),
        )
        kwargs["command_idempotency_key"] = key
    elif scenario in {
        "renewal_personal",
        "renewal_debt",
        "renewal_group_target",
        "renewal_preview_tariff",
        "renewal_locked_tariff",
    }:
        source_training_type = TrainingTypeFactory(
            club=club,
            kind=(
                TrainingType.Kind.GROUP
                if scenario == "renewal_group_target"
                else TrainingType.Kind.PERSONAL
            ),
        )
        source_tariff = TariffFactory(
            club=club,
            training_type=source_training_type,
        )
        source = SubscriptionFactory(club=club, student=student, tariff=source_tariff)
        kwargs["renewed_from_subscription_id"] = source.id
        kwargs["tariff_id"] = source_tariff.id
        if scenario == "renewal_personal":
            kwargs["personal_drop_in_booking_id"] = 987654
        elif scenario == "renewal_debt":
            kwargs["debt_ids"] = [987654]
        elif scenario == "renewal_preview_tariff":
            kwargs["tariff_id"] = tariff.id
        elif scenario == "renewal_group_target":
            settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
            rollout = TrainingGroupRolloutStateFactory(club=club)
            update_training_group_rollout_state_for_test(
                TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
                mode=TrainingGroupRolloutState.Mode.ACTIVE,
            )
            kwargs["target_training_group_id"] = 987654
        else:
            changed_tariff = TariffFactory(club=club)
            contexts.append(
                patch(
                    "apps.billing.service_modules.renewals.lock_and_validate_exact_renewal_source",
                    return_value=SimpleNamespace(tariff_id=changed_tariff.id),
                )
            )
    elif scenario == "renewal_missing":
        kwargs["renewed_from_subscription_id"] = 987654
    elif scenario == "renewal_required":
        kwargs["tariff_id"] = None
    else:
        terms = SimpleNamespace(
            terms_version=PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V1,
            tariff_id_snapshot=(tariff.id + 987654 if scenario == "personal_terms_tariff" else tariff.id),
            tariff_name_snapshot=tariff.name,
        )
        terms_qs = MagicMock()
        terms_qs.select_for_update.return_value.filter.return_value.first.return_value = terms
        booking = SimpleNamespace(
            state=(
                PersonalDropInBooking.State.CANCELLED
                if scenario == "personal_booking_closed"
                else PersonalDropInBooking.State.SCHEDULED
            ),
            debt_id=(987654 if scenario == "personal_debt_changed" else None),
        )
        scope = SimpleNamespace(bookings_by_id=({} if scenario == "personal_booking_missing" else {987654: booking}))
        contexts.extend(
            [
                patch.object(
                    PersonalServiceTermsSnapshot.objects,
                    "for_club",
                    return_value=terms_qs,
                ),
                patch(
                    "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
                    return_value=scope,
                ),
            ]
        )
        if scenario == "personal_debt_changed":
            debt_qs = MagicMock()
            debt_qs.select_for_update.return_value.filter.return_value.first.return_value = SimpleNamespace(id=987654)
            contexts.append(patch.object(Debt.objects, "for_club", return_value=debt_qs))
            kwargs["debt_ids"] = [123456]
        kwargs["personal_drop_in_booking_id"] = 987654

    payment_count_before = Payment.objects.for_club(club).count()
    order_count_before = BankPaymentOrder.objects.for_club(club).count()
    subscription_count_before = Subscription.objects.for_club(club).count()
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        with pytest.raises(BusinessLogicError) as exc_info:
            bank_orders.create_bank_payment_order(**kwargs)

    assert exc_info.value.code == code
    assert exc_info.value.message == message
    assert Payment.objects.for_club(club).count() == payment_count_before
    assert BankPaymentOrder.objects.for_club(club).count() == order_count_before
    assert Subscription.objects.for_club(club).count() == subscription_count_before


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "code", "message"),
    [
        (
            "lock_scope",
            "personal_drop_in_lock_scope_changed",
            "Personal booking ownership changed while closing pending payment",
        ),
        (
            "order_link",
            "personal_drop_in_bank_order_mismatch",
            "Personal payment order does not match its payment link",
        ),
    ],
)
def test_close_personal_financial_family_integrity_error_contracts(
    club,
    owner_user,
    scenario,
    code,
    message,
):
    booking_id = 101
    enrollment_id = 102
    student_id = 103
    trainer_id = 104
    payment_id = 105
    order_id = 106
    preview = {
        "enrollment_id": enrollment_id,
        "enrollment__student_id": student_id,
        "enrollment__schedule__trainer_id": trainer_id,
    }
    booking = SimpleNamespace(
        id=booking_id,
        enrollment_id=(enrollment_id + 1 if scenario == "lock_scope" else enrollment_id),
        enrollment=SimpleNamespace(
            student_id=student_id,
            schedule=SimpleNamespace(trainer_id=trainer_id),
        ),
        state=PersonalDropInBooking.State.SCHEDULED,
    )
    link = SimpleNamespace(
        payment_id=payment_id,
        bank_payment_order_id=order_id,
    )
    payment = SimpleNamespace(
        id=payment_id,
        status=Payment.Status.PENDING,
        subscription_id=None,
    )
    order = SimpleNamespace(
        id=order_id,
        payment_id=payment_id + 1,
        personal_drop_in_booking_id_snapshot=booking_id,
        status=BankPaymentOrder.Status.PENDING,
        created_by_id=owner_user.id,
    )

    booking_qs = MagicMock()
    booking_qs.select_related.return_value.filter.return_value.values.return_value.first.return_value = preview
    booking_qs.select_for_update.return_value.select_related.return_value.get.return_value = booking
    terms_qs = MagicMock()
    terms_qs.filter.return_value.exists.return_value = True
    link_qs = MagicMock()
    link_qs.filter.return_value.values_list.return_value = []
    link_qs.select_for_update.return_value.filter.return_value.order_by.return_value = [link]
    order_qs = MagicMock()
    order_qs.filter.return_value.values_list.return_value = []
    order_qs.select_for_update.return_value.filter.return_value.order_by.return_value = [order]
    payment_qs = MagicMock()
    payment_qs.select_for_update.return_value.filter.return_value.order_by.return_value = [payment]
    subscription_qs = MagicMock()
    subscription_qs.select_for_update.return_value.filter.return_value.order_by.return_value = []
    trainer_qs = MagicMock()
    student_qs = MagicMock()
    slot_qs = MagicMock()
    enrollment_qs = MagicMock()

    with (
        patch.object(PersonalDropInBooking.objects, "for_club", return_value=booking_qs),
        patch.object(PersonalServiceTermsSnapshot.objects, "for_club", return_value=terms_qs),
        patch.object(PersonalDropInPaymentLink.objects, "for_club", return_value=link_qs),
        patch.object(BankPaymentOrder.objects, "for_club", return_value=order_qs),
        patch.object(Payment.objects, "for_club", return_value=payment_qs),
        patch.object(Subscription.objects, "for_club", return_value=subscription_qs),
        patch.object(Trainer.objects, "for_club", return_value=trainer_qs),
        patch.object(bank_orders.Student.objects, "for_club", return_value=student_qs),
        patch.object(PersonalAvailabilitySlot.objects, "for_club", return_value=slot_qs),
        patch.object(ScheduleEnrollment.objects, "for_club", return_value=enrollment_qs),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders.close_pending_personal_drop_in_financial_family(
            club_id=club.id,
            booking_id=booking_id,
            actor_user_id=owner_user.id,
            reason="cancelled",
        )

    assert exc_info.value.code == code
    assert exc_info.value.message == message


@pytest.mark.django_db
def test_receipt_mode_rejects_unknown_value(settings):
    settings.TOCHKA_RECEIPT_MODE = "unknown"
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders._receipt_mode()
    assert exc_info.value.code == "invalid_receipt_mode"
    assert exc_info.value.message == "Некорректный режим чеков"


def test_exact_group_renewal_target_requires_live_membership():
    with (
        patch.object(bank_orders, "_validate_group_conversion_target", return_value=MagicMock()),
        patch.object(
            bank_orders,
            "_resolve_canonical_group_payment_target",
            return_value=(None, None, None),
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders._assert_locked_exact_group_renewal_target(
            club_id=1,
            student=SimpleNamespace(id=2),
            tariff=MagicMock(),
            target_training_group_id=3,
            target_schedule_id=4,
            target_start_date=timezone.localdate(),
            rollout_state=MagicMock(),
        )

    assert exc_info.value.code == "renewal_group_membership_required"
    assert exc_info.value.message == (
        "Точное групповое продление требует действующее участие в выбранной группе"
    )


@pytest.mark.django_db
def test_create_bank_order_direct_preconditions_are_stable(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)

    with pytest.raises(BusinessLogicError) as invalid_source:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source="invalid",
            created_by_id=owner_user.id,
        )
    assert invalid_source.value.code == "invalid_payment_source"
    assert invalid_source.value.message == "Некорректный источник оплаты"

    group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    group_tariff = TariffFactory(club=club, training_type=group_type)
    with pytest.raises(BusinessLogicError) as missing_target:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=group_tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            enforce_trainer_group_contract=True,
        )
    assert missing_target.value.code == "target_schedule_required"
    assert missing_target.value.message == "Для групповой оплаты укажите группу и дату старта"
    assert BankPaymentOrder.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_legacy_source_preview_rejects_ambiguous_accepted_order_family(club, owner_user):
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    sources = [
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
        )
        for _ in range(2)
    ]
    for source_subscription in sources:
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            subscription=source_subscription,
        )
        BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=source_subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.OWNER,
            status=BankPaymentOrder.Status.CREATED,
            amount_snapshot=payment.amount,
            purpose_snapshot="Renewal ambiguity",
            expires_at=timezone.now() + timedelta(days=1),
            renewed_from_subscription=source_subscription,
            created_by=owner_user,
        )

    payment_count_before = Payment.objects.for_club(club).count()
    order_count_before = BankPaymentOrder.objects.for_club(club).count()
    subscription_count_before = Subscription.objects.for_club(club).count()
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders._legacy_renewal_source_preview(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
        )

    assert exc_info.value.code == "renewal_source_ambiguous"
    assert exc_info.value.message == "Источник продления требует ручного выбора"
    assert Payment.objects.for_club(club).count() == payment_count_before
    assert BankPaymentOrder.objects.for_club(club).count() == order_count_before
    assert Subscription.objects.for_club(club).count() == subscription_count_before


@pytest.mark.django_db
def test_legacy_source_preview_rejects_ambiguous_historical_sources(club):
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    for _ in range(2):
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
        )

    payment_count_before = Payment.objects.for_club(club).count()
    order_count_before = BankPaymentOrder.objects.for_club(club).count()
    subscription_count_before = Subscription.objects.for_club(club).count()
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders._legacy_renewal_source_preview(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
        )

    assert exc_info.value.code == "renewal_source_ambiguous"
    assert exc_info.value.message == "Источник продления требует ручного выбора"
    assert Payment.objects.for_club(club).count() == payment_count_before
    assert BankPaymentOrder.objects.for_club(club).count() == order_count_before
    assert Subscription.objects.for_club(club).count() == subscription_count_before


@pytest.mark.django_db
def test_create_bank_order_rejects_stale_current_offer_without_mutation(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    source_tariff = TariffFactory(club=club, training_type=training_type)
    stale_tariff = TariffFactory(club=club, training_type=training_type, is_active=False)
    source = SubscriptionFactory(club=club, student=student, tariff=source_tariff)
    stale_offer = SimpleNamespace(target_tariff=stale_tariff, target_tariff_id=stale_tariff.id)

    payment_count_before = Payment.objects.for_club(club).count()
    order_count_before = BankPaymentOrder.objects.for_club(club).count()
    subscription_count_before = Subscription.objects.for_club(club).count()
    with (
        patch("apps.billing.service_modules.renewals.get_renewal_offer", return_value=stale_offer),
        patch("apps.billing.service_modules.renewals.validate_expected_renewal_offer"),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=source_tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            renewed_from_subscription_id=source.id,
        )

    assert exc_info.value.code == "renewal_offer_stale"
    assert exc_info.value.message == "Тариф продления больше недоступен"
    assert Payment.objects.for_club(club).count() == payment_count_before
    assert BankPaymentOrder.objects.for_club(club).count() == order_count_before
    assert Subscription.objects.for_club(club).count() == subscription_count_before


@pytest.mark.django_db
def test_create_bank_order_rejects_finalized_successor_without_mutation(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    source = SubscriptionFactory(club=club, student=student, tariff=tariff)
    bank_orders.create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        renewed_from_subscription_id=source.id,
        command_idempotency_key="renewal-family-first",
    )

    payment_count_before = Payment.objects.for_club(club).count()
    order_count_before = BankPaymentOrder.objects.for_club(club).count()
    subscription_count_before = Subscription.objects.for_club(club).count()
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            renewed_from_subscription_id=source.id,
            command_idempotency_key="renewal-family-second",
        )

    assert exc_info.value.code == "renewal_source_finalized_successor"
    assert exc_info.value.message == "У этого абонемента уже есть ожидающее или подтверждённое продление"
    assert Payment.objects.for_club(club).count() == payment_count_before
    assert BankPaymentOrder.objects.for_club(club).count() == order_count_before
    assert Subscription.objects.for_club(club).count() == subscription_count_before


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "code", "message"),
    [
        (
            "creation_disabled",
            "online_payment_order_creation_disabled",
            "Создание онлайн-оплаты временно отключено",
        ),
        (
            "mock_disabled",
            "mock_payment_order_creation_disabled",
            "Mock-провайдер не может создавать платёжные ссылки в этом окружении",
        ),
        (
            "tochka_not_ready",
            "tochka_payment_creation_not_ready",
            "Онлайн-оплата Точки пока не готова к созданию ссылок",
        ),
        (
            "expiry_elapsed",
            "bank_payment_order_expiry_elapsed",
            "Срок этой оплаты уже истёк",
        ),
        (
            "ttl_too_short",
            "bank_payment_order_ttl_too_short",
            "До окончания записи недостаточно времени для безопасной ссылки СБП",
        ),
        (
            "receipt_email",
            "receipt_buyer_email_required",
            "Для фискального чека нужен email покупателя",
        ),
    ],
)
def test_create_bank_order_payment_preflight_errors_are_stable(
    settings,
    club,
    owner_user,
    scenario,
    code,
    message,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)
    expires_at_cap = None
    online_ready = True

    if scenario == "creation_disabled":
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
    elif scenario == "mock_disabled":
        settings.DEBUG = False
    else:
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        if scenario == "tochka_not_ready":
            online_ready = False
        elif scenario == "expiry_elapsed":
            expires_at_cap = timezone.now() - timedelta(seconds=1)
        elif scenario == "ttl_too_short":
            expires_at_cap = timezone.now() + timedelta(seconds=119)
        elif scenario == "receipt_email":
            settings.TOCHKA_RECEIPT_MODE = BankPaymentOrder.ReceiptMode.TOCHKA_RECEIPT

    with (
        patch(
            "apps.billing.payment_providers.base.online_payments_enabled",
            return_value=online_ready,
        ),
        patch("apps.billing.payment_providers.get_payment_provider", return_value=MagicMock()),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            expires_at_cap=expires_at_cap,
        )

    assert exc_info.value.code == code
    assert exc_info.value.message == message
    assert BankPaymentOrder.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_create_bank_order_requires_subscription_and_rolls_back(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)

    with (
        patch.object(
            bank_orders,
            "create_payment",
            return_value=SimpleNamespace(subscription=None),
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            personal_drop_in_booking_id=987654,
        )
    assert exc_info.value.code == "payment_subscription_required"
    assert exc_info.value.message == "Для онлайн-оплаты нужен абонемент"
    assert BankPaymentOrder.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_create_bank_order_rejects_invalid_provider_without_mutation(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = "invalid-provider"
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
    assert exc_info.value.code == "invalid_payment_provider"
    assert exc_info.value.message == "Некорректный провайдер оплаты"
    assert BankPaymentOrder.objects.for_club(club).count() == 0
    assert Payment.objects.for_club(club).count() == 0
    assert Subscription.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_create_bank_order_preserves_downstream_payment_error_and_rollback(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)

    def create_transient_financial_family_then_raise(**_kwargs):
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            status=Payment.Status.PENDING,
        )
        raise BusinessLogicError(
            "downstream payment rejected",
            code="downstream_payment_rejected",
        )

    with (
        patch.object(
            bank_orders,
            "create_payment",
            side_effect=create_transient_financial_family_then_raise,
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            personal_drop_in_booking_id=987654,
        )
    assert exc_info.value.code == "downstream_payment_rejected"
    assert exc_info.value.message == "downstream payment rejected"
    assert BankPaymentOrder.objects.for_club(club).count() == 0
    assert Payment.objects.for_club(club).count() == 0
    assert Subscription.objects.for_club(club).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("discount_ids", "debt_ids", "allow_new", "code", "message"),
    [
        (
            None,
            None,
            False,
            "self_service_tariff_not_allowed",
            "Тариф недоступен для самостоятельного продления",
        ),
        (
            [1],
            None,
            False,
            "self_service_discount_not_supported",
            "Самостоятельная оплата со скидкой пока недоступна",
        ),
        (
            None,
            [1],
            False,
            "self_service_debt_payment_not_supported",
            "Самостоятельная оплата долгов пока недоступна",
        ),
    ],
)
def test_self_service_guards_preserve_exact_error_contract(
    club,
    discount_ids,
    debt_ids,
    allow_new,
    code,
    message,
):
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club)
    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders._assert_self_service_bank_order_allowed(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            discount_ids=discount_ids,
            debt_ids=debt_ids,
            allow_new_self_service_subscription=allow_new,
        )
    assert exc_info.value.code == code
    assert exc_info.value.message == message


def _create_order(*, settings, club, owner_user):
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
    return order, student, tariff


@pytest.mark.django_db
def test_live_order_rejects_distinct_command_key_without_mutation(
    settings,
    club,
    owner_user,
):
    _order, student, tariff = _create_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    command_key = "bank-order-distinct-live-family"
    fingerprint = build_subscription_command_fingerprint(
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method=Payment.Method.ONLINE,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            command_idempotency_key=command_key,
            command_fingerprint=fingerprint,
            reject_reusable_order_for_distinct_key=True,
        )

    assert exc_info.value.code == "bank_payment_order_live_command_conflict"
    assert exc_info.value.message == (
        "Для этой оплаты уже существует активная ссылка с другим ключом команды"
    )
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1
    assert Subscription.objects.for_club(club).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "code", "message"),
    [
        ("missing", "bank_payment_order_not_found", "Ссылка на оплату не найдена"),
        ("student", "bank_payment_order_forbidden", "Ссылка на оплату недоступна"),
        (
            "source",
            "bank_payment_order_cancel_source_forbidden",
            "Ссылку на оплату нельзя отменить в этом разделе",
        ),
        (
            "terminal",
            "bank_payment_order_not_cancellable",
            "Можно отменить только активную неоплаченную ссылку",
        ),
        (
            "provider_dispatch",
            "bank_payment_order_provider_dispatch_not_cancellable",
            "Ссылка Точки уже могла быть выдана банком и ждёт оплаты или истечения",
        ),
        (
            "payment",
            "bank_payment_order_payment_not_pending",
            "Оплата по этой ссылке уже обработана",
        ),
        (
            "subscription",
            "bank_payment_order_subscription_not_pending",
            "Абонемент по этой ссылке уже обработан",
        ),
    ],
)
def test_cancel_bank_order_direct_error_contract(
    settings,
    club,
    owner_user,
    scenario,
    code,
    message,
):
    order, student, _tariff = _create_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    kwargs = {
        "club_id": club.id,
        "order_id": order.id,
        "actor_user_id": owner_user.id,
    }
    if scenario == "missing":
        kwargs["order_id"] = order.id + 987654
    elif scenario == "student":
        kwargs["allowed_student_id"] = StudentFactory(club=club).id
    elif scenario == "source":
        kwargs["allowed_sources"] = {BankPaymentOrder.Source.STUDENT}
    elif scenario == "terminal":
        order.status = BankPaymentOrder.Status.APPROVED
        order.save(update_fields=["status", "updated_at"])
    elif scenario == "provider_dispatch":
        order.provider = BankPaymentOrder.Provider.TOCHKA
        order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
        order.save(update_fields=["provider", "link_creation_state", "updated_at"])
    elif scenario == "payment":
        order.payment.status = Payment.Status.CONFIRMED
        order.payment.save(update_fields=["status", "updated_at"])
    elif scenario == "subscription":
        order.subscription.status = Subscription.Status.ACTIVE
        order.subscription.save(update_fields=["status", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders.cancel_bank_payment_order(**kwargs)
    assert exc_info.value.code == code
    assert exc_info.value.message == message
    order.refresh_from_db()
    assert order.status != BankPaymentOrder.Status.CANCELLED
    assert order.student_id == student.id


@pytest.mark.django_db
def test_reuse_rejects_drifted_locked_financial_family(settings, club, owner_user):
    order, student, tariff = _create_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    fake_payment = SimpleNamespace(
        student_id=student.id + 1,
        tariff_id=tariff.id,
        status=Payment.Status.PENDING,
    )
    payment_qs = MagicMock()
    payment_qs.select_for_update.return_value.select_related.return_value.get.return_value = fake_payment

    with (
        patch.object(Payment.objects, "for_club", return_value=payment_qs),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        bank_orders._find_reusable_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=order.source,
            student=student,
            discount_ids=None,
            debt_ids=None,
            seller_trainer_id=None,
            package_owner_trainer_id=None,
            target_schedule_id=None,
            target_training_group_id=None,
            target_start_date=None,
            personal_booking_reservation_id=None,
            personal_drop_in_booking_id=None,
            now=timezone.now(),
            tariff=tariff,
            rollout_state=None,
            preflight_training_group_id=None,
            enforce_trainer_group_contract=False,
        )
    assert exc_info.value.code == "bank_payment_order_pending_family_changed"
    assert exc_info.value.message == "Активная ссылка на оплату изменилась и требует ручной проверки"


@pytest.mark.django_db
def test_reuse_rejects_changed_locked_group_target(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=trainer,
    )
    other_group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=trainer,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        training_type=training_type,
        location=group.location,
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    today = timezone.localdate()
    days_until_slot = (schedule.day_of_week - today.weekday()) % 7 or 7
    target_start_date = today + timedelta(days=days_until_slot)
    order = bank_orders.create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        bank_orders._find_reusable_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=order.source,
            student=student,
            discount_ids=None,
            debt_ids=None,
            seller_trainer_id=None,
            package_owner_trainer_id=None,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
            personal_booking_reservation_id=None,
            personal_drop_in_booking_id=None,
            now=timezone.now(),
            tariff=tariff,
            rollout_state=rollout,
            preflight_training_group_id=other_group.id,
            enforce_trainer_group_contract=False,
        )
    assert exc_info.value.code == "bank_payment_order_target_changed"
    assert exc_info.value.message == "Целевая группа изменилась, выберите группу заново"


@pytest.mark.django_db
def test_create_bank_order_propagates_tenant_lookup_without_mutation(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    with pytest.raises(Tariff.DoesNotExist) as exc_info:
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=987654,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
    assert str(exc_info.value) == "Tariff matching query does not exist."
    assert BankPaymentOrder.objects.for_club(club).count() == 0


@pytest.mark.django_db(transaction=True)
def test_provider_failure_cleanup_rolls_back_terminal_and_financial_mutations(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)

    with (
        patch(
            "apps.billing.payment_providers.mock.MockPaymentProvider.create_payment_link",
            side_effect=BusinessLogicError("provider rejected", code="provider_rejected"),
        ),
        patch(
            "apps.attendance.services.close_personal_booking_payment_reservation_for_order",
            side_effect=RuntimeError("reservation cleanup failed"),
        ),
        pytest.raises(RuntimeError, match="reservation cleanup failed"),
    ):
        bank_orders.create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

    order = BankPaymentOrder.objects.for_club(club).get(student=student)
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.CREATED
    assert order.payment.status == Payment.Status.PENDING
    assert order.subscription.status == Subscription.Status.PENDING
    assert order.subscription.deleted_at is None


@pytest.mark.django_db(transaction=True)
def test_cancel_cleanup_failure_rolls_back_order_and_financial_family(
    settings,
    club,
    owner_user,
):
    order, _student, _tariff = _create_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    with (
        patch.object(
            bank_orders,
            "_cancel_pending_bank_order_artifacts",
            side_effect=RuntimeError("cleanup failed after cancel"),
        ),
        pytest.raises(RuntimeError, match="cleanup failed after cancel"),
    ):
        bank_orders.cancel_bank_payment_order(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
        )
    order.refresh_from_db()
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    assert order.status in bank_orders.LIVE_BANK_PAYMENT_ORDER_STATUSES
    assert order.payment.status == Payment.Status.PENDING
    assert order.subscription.status == Subscription.Status.PENDING
    assert order.subscription.deleted_at is None


@pytest.mark.django_db(transaction=True)
def test_expire_cleanup_failure_rolls_back_current_order(settings, club, owner_user):
    order, _student, _tariff = _create_order(
        settings=settings,
        club=club,
        owner_user=owner_user,
    )
    now = timezone.now()
    order.expires_at = now - timedelta(seconds=1)
    order.save(update_fields=["expires_at", "updated_at"])

    with (
        patch.object(
            bank_orders,
            "_cancel_pending_bank_order_artifacts",
            side_effect=RuntimeError("cleanup failed during expiry"),
        ),
        pytest.raises(RuntimeError, match="cleanup failed during expiry"),
    ):
        bank_orders.expire_bank_payment_orders(now=now)
    order.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.PENDING
