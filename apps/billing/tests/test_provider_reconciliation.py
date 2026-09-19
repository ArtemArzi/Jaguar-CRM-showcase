from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone
from django_q.models import Schedule

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    BankPaymentReconciliationAttempt,
    Payment,
    Subscription,
    TrainingType,
)
from apps.billing.payment_providers.base import ProviderLinkResult, ProviderOperationInfo
from apps.billing.service_modules.provider_events import (
    process_due_provider_reconciliations,
    process_unknown_provider_creations,
    reconcile_provider_payment_order,
    request_provider_reconciliation,
)
from apps.billing.services import create_bank_payment_order, expire_bank_payment_orders
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory


def _order(*, settings, club, owner_user):
    settings.DEBUG = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    return create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )


def _as_tochka(order, *, state=BankPaymentOrder.LinkCreationState.READY, operation_id="operation-1"):
    order.provider = BankPaymentOrder.Provider.TOCHKA
    order.provider_operation_id = operation_id
    order.provider_customer_code = "customer-1"
    order.provider_merchant_id = "merchant-1"
    order.provider_payment_modes = ["sbp"]
    order.link_creation_state = state
    order.link_creation_dispatched_at = timezone.now()
    order.save(
        update_fields=[
            "provider",
            "provider_operation_id",
            "provider_customer_code",
            "provider_merchant_id",
            "provider_payment_modes",
            "link_creation_state",
            "link_creation_dispatched_at",
            "updated_at",
        ]
    )
    return order


def _tochka_creation_subject(*, settings, club):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    settings.TOCHKA_CUSTOMER_CODE = "customer-1"
    settings.TOCHKA_MERCHANT_ID = "merchant-1"
    settings.TOCHKA_RECEIPT_MODE = BankPaymentOrder.ReceiptMode.NONE
    student = StudentFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    return student, tariff


@pytest.mark.django_db
def test_scheduled_due_reconciliation_completes_from_authenticated_operation(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    order = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)
    provider = Mock()
    provider.get_payment_operation_info.return_value = ProviderOperationInfo(
        status="APPROVED",
        operation_id=order.provider_operation_id,
        payment_link_id=order.provider_payment_link_id,
        payment_url=order.provider_payment_url,
        amount=order.amount_snapshot,
        customer_code=order.provider_customer_code,
        merchant_id=order.provider_merchant_id,
        paid_at=timezone.now(),
        payment_modes=["sbp"],
    )

    with (
        patch("apps.billing.service_modules.provider_events.get_payment_provider", return_value=provider),
        patch(
            "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
            return_value=SimpleNamespace(reconciliation_available=True),
        ),
    ):
        outcomes = process_due_provider_reconciliations(limit=10)

    order.refresh_from_db()
    order.payment.refresh_from_db()
    attempt = BankPaymentReconciliationAttempt.objects.for_club(club).get(order=order)
    assert outcomes == {"completed": 1}
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert order.payment.status == Payment.Status.CONFIRMED
    assert attempt.status == BankPaymentReconciliationAttempt.Status.COMPLETED
    provider.get_payment_operation_info.assert_called_once()


@pytest.mark.django_db
def test_reconciliation_exhaustion_moves_attempt_and_order_to_manual_review(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    settings.TOCHKA_RECONCILIATION_MAX_ATTEMPTS = 2
    order = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    attempt = request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)
    attempt.attempt_count = 2
    attempt.save(update_fields=["attempt_count", "updated_at"])

    with patch("apps.billing.service_modules.provider_events.get_payment_provider") as get_provider:
        outcome = reconcile_provider_payment_order(club_id=club.id, order_id=order.id)

    order.refresh_from_db()
    attempt.refresh_from_db()
    assert outcome == "manual_review"
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert order.last_error_code == "tochka_reconciliation_exhausted"
    assert attempt.status == BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    get_provider.assert_not_called()


@pytest.mark.django_db
def test_scheduled_unknown_creation_recovery_scans_only_dispatched_claims(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    candidate = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.UNKNOWN,
        operation_id="",
    )
    _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))

    with (
        patch(
            "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
            return_value=SimpleNamespace(reconciliation_available=True),
        ),
        patch(
            "apps.billing.service_modules.bank_orders.recover_unknown_bank_payment_order",
            return_value="unknown",
        ) as recover,
    ):
        outcomes = process_unknown_provider_creations(limit=10)

    assert outcomes == {"unknown": 1}
    recover.assert_called_once_with(club_id=club.id, order_id=candidate.id)


@pytest.mark.django_db
def test_generic_expiry_does_not_cancel_possibly_dispatched_tochka_claims(
    settings,
    club,
    owner_user,
):
    ready = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    protected = [
        _as_tochka(
            _order(settings=settings, club=club, owner_user=owner_user),
            state=state,
            operation_id="" if state != BankPaymentOrder.LinkCreationState.READY else "operation",
        )
        for state in (
            BankPaymentOrder.LinkCreationState.CLAIMED,
            BankPaymentOrder.LinkCreationState.DISPATCHED,
            BankPaymentOrder.LinkCreationState.UNKNOWN,
        )
    ]
    now = timezone.now()
    BankPaymentOrder.objects.for_club(club).filter(
        id__in=[ready.id, *(order.id for order in protected)]
    ).update(expires_at=now - timedelta(minutes=1))

    expired = expire_bank_payment_orders(now=now)

    ready.refresh_from_db()
    assert expired == 1
    assert ready.status == BankPaymentOrder.Status.EXPIRED
    for order in protected:
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.PENDING
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING


def test_periodic_provider_task_runs_both_durable_scanners(settings):
    from apps.billing.tasks import process_due_bank_payment_provider_work

    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    with (
        patch(
            "apps.billing.service_modules.provider_events.process_due_provider_reconciliations",
            return_value={"completed": 2},
        ) as reconciliations,
        patch(
            "apps.billing.service_modules.provider_events.process_unknown_provider_creations",
            return_value={"unknown": 1},
        ) as creations,
    ):
        result = process_due_bank_payment_provider_work()

    assert result == {"reconciliation": {"completed": 2}, "creation_recovery": {"unknown": 1}}
    reconciliations.assert_called_once_with()
    creations.assert_called_once_with()


@pytest.mark.django_db
def test_provider_recovery_schedule_is_registered_every_minute():
    call_command("register_scheduled_tasks")

    schedule = Schedule.objects.get(name="process_due_bank_payment_provider_work")
    assert schedule.func == "apps.billing.tasks.process_due_bank_payment_provider_work"
    assert schedule.schedule_type == Schedule.MINUTES
    assert schedule.minutes == 1


def test_retailer_readiness_refresh_task_is_disabled_by_default(settings):
    from apps.billing.tasks import refresh_tochka_payment_readiness_task

    settings.TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED = False
    with patch(
        "apps.billing.service_modules.payment_readiness.refresh_tochka_retailer_readback"
    ) as refresh:
        result = refresh_tochka_payment_readiness_task()

    assert result == {"disabled": 1}
    refresh.assert_not_called()


def test_retailer_readiness_refresh_task_requires_tochka_and_an_operational_flag(settings):
    from apps.billing.tasks import refresh_tochka_payment_readiness_task

    settings.TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED = True
    with patch(
        "apps.billing.service_modules.payment_readiness.refresh_tochka_retailer_readback"
    ) as refresh:
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        assert refresh_tochka_payment_readiness_task() == {"provider_not_selected": 1}

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
        settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = False
        assert refresh_tochka_payment_readiness_task() == {"operations_disabled": 1}

    refresh.assert_not_called()


def test_retailer_readiness_refresh_task_calls_provider_once_when_enabled(settings):
    from apps.billing.tasks import refresh_tochka_payment_readiness_task

    settings.TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    snapshot = SimpleNamespace(
        retailer_status="REG",
        is_active=True,
        checked_at=timezone.now(),
    )
    with patch(
        "apps.billing.service_modules.payment_readiness.refresh_tochka_retailer_readback",
        return_value=snapshot,
    ) as refresh:
        result = refresh_tochka_payment_readiness_task()

    assert result == {"stored": 1}
    refresh.assert_called_once_with()


def test_retailer_readiness_refresh_task_redacts_unexpected_exception(settings):
    from apps.billing.tasks import refresh_tochka_payment_readiness_task

    settings.TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    sensitive_error = "synthetic-token merchant-secret customer-secret"
    with (
        patch(
            "apps.billing.service_modules.payment_readiness.refresh_tochka_retailer_readback",
            side_effect=RuntimeError(sensitive_error),
        ) as refresh,
        patch("apps.billing.tasks.logger.error") as error_log,
    ):
        result = refresh_tochka_payment_readiness_task()

    assert result == {"error": 1}
    refresh.assert_called_once_with()
    error_log.assert_called_once_with("tochka_retailer_readiness_refresh_failed")
    logged_arguments = repr(error_log.call_args)
    assert "synthetic-token" not in logged_arguments
    assert "merchant-secret" not in logged_arguments
    assert "customer-secret" not in logged_arguments


@pytest.mark.django_db
def test_retailer_readiness_refresh_schedule_is_registered_every_fifteen_minutes():
    call_command("register_scheduled_tasks")

    schedule = Schedule.objects.get(name="refresh_tochka_payment_readiness")
    assert schedule.func == "apps.billing.tasks.refresh_tochka_payment_readiness_task"
    assert schedule.schedule_type == Schedule.MINUTES
    assert schedule.minutes == 15


@pytest.mark.django_db
def test_successful_tochka_creation_schedules_missing_webhook_reconciliation(
    settings,
    club,
    owner_user,
):
    student, tariff = _tochka_creation_subject(settings=settings, club=club)
    provider = Mock()
    provider.create_payment_link.return_value = ProviderLinkResult(
        payment_url="https://bank.example/pay/known-operation",
        payment_link_id="placeholder",
        provider_status="CREATED",
        operation_id="operation-known",
        customer_code="customer-1",
        merchant_id="merchant-1",
        payment_modes=["sbp"],
    )

    def create_link(*, order):
        return ProviderLinkResult(
            payment_url="https://bank.example/pay/known-operation",
            payment_link_id=order.provider_payment_link_id,
            provider_status="CREATED",
            operation_id="operation-known",
            customer_code="customer-1",
            merchant_id="merchant-1",
            payment_modes=["sbp"],
        )

    provider.create_payment_link.side_effect = create_link
    with (
        patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        patch("apps.billing.payment_providers.base.online_payments_enabled", return_value=True),
    ):
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

    attempt = BankPaymentReconciliationAttempt.objects.for_club(club).get(order=order)
    assert attempt.status == BankPaymentReconciliationAttempt.Status.PENDING
    provider.get_payment_operation_info.return_value = ProviderOperationInfo(
        status="APPROVED",
        operation_id=order.provider_operation_id,
        payment_link_id=order.provider_payment_link_id,
        amount=order.amount_snapshot,
        customer_code=order.provider_customer_code,
        merchant_id=order.provider_merchant_id,
        paid_at=timezone.now(),
        payment_modes=["sbp"],
    )
    with (
        patch("apps.billing.service_modules.provider_events.get_payment_provider", return_value=provider),
        patch(
            "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
            return_value=SimpleNamespace(reconciliation_available=True),
        ),
    ):
        outcomes = process_due_provider_reconciliations(limit=10)

    order.refresh_from_db()
    order.payment.refresh_from_db()
    assert outcomes == {"completed": 1}
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert order.payment.status == Payment.Status.CONFIRMED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("provider_status", "order_status"),
    [
        ("FAILED", BankPaymentOrder.Status.FAILED),
        ("EXPIRED", BankPaymentOrder.Status.EXPIRED),
        ("CANCELLED", BankPaymentOrder.Status.CANCELLED),
    ],
)
def test_authenticated_terminal_operation_closes_pending_financial_family(
    settings,
    club,
    owner_user,
    provider_status,
    order_status,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    order = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)
    provider = Mock()
    provider.get_payment_operation_info.return_value = ProviderOperationInfo(
        status=provider_status,
        operation_id=order.provider_operation_id,
        payment_link_id=order.provider_payment_link_id,
        amount=order.amount_snapshot,
        customer_code=order.provider_customer_code,
        merchant_id=order.provider_merchant_id,
        payment_modes=["sbp"],
    )

    with (
        patch("apps.billing.service_modules.provider_events.get_payment_provider", return_value=provider),
        patch(
            "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
            return_value=SimpleNamespace(reconciliation_available=True),
        ),
    ):
        outcome = reconcile_provider_payment_order(club_id=club.id, order_id=order.id)

    order.refresh_from_db()
    order.payment.refresh_from_db()
    attempt = BankPaymentReconciliationAttempt.objects.for_club(club).get(order=order)
    assert outcome == "terminal"
    assert order.status == order_status
    assert order.payment.status == Payment.Status.REJECTED
    assert attempt.status == BankPaymentReconciliationAttempt.Status.COMPLETED


@pytest.mark.django_db
def test_missing_operation_routes_to_creation_recovery_without_manual_review(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="",
    )
    request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)

    outcome = reconcile_provider_payment_order(club_id=club.id, order_id=order.id)

    order.refresh_from_db()
    attempt = BankPaymentReconciliationAttempt.objects.for_club(club).get(order=order)
    assert outcome == "creation_recovery"
    assert order.status == BankPaymentOrder.Status.PENDING
    assert attempt.status == BankPaymentReconciliationAttempt.Status.RETRY
    assert attempt.last_error_code == "tochka_operation_id_missing"


@pytest.mark.django_db
def test_discovered_operation_revives_legacy_missing_operation_manual_attempt(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="",
    )
    attempt = request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = "tochka_operation_id_missing"
    attempt.retry_at = None
    attempt.save(update_fields=["status", "last_error_code", "retry_at", "updated_at"])
    order.provider_operation_id = "operation-recovered"
    order.save(update_fields=["provider_operation_id", "updated_at"])

    revived = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )

    assert revived.status == BankPaymentReconciliationAttempt.Status.PENDING
    assert revived.last_error_code == ""
    assert revived.retry_at is not None


@pytest.mark.django_db
def test_owner_retry_reopens_backfilled_legacy_manual_attempt_with_operation(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="operation-legacy-manual",
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.save(update_fields=["status", "updated_at"])
    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = "tochka_legacy_manual_review"
    attempt.retry_at = None
    attempt.save(update_fields=["status", "last_error_code", "retry_at", "updated_at"])

    revived = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
        allow_manual_retry=True,
        actor_user_id=owner_user.id,
    )

    assert revived.status == BankPaymentReconciliationAttempt.Status.PENDING
    assert revived.attempt_count == 0
    assert revived.last_error_code == ""
    assert revived.retry_at is not None


@pytest.mark.django_db
def test_owner_retry_reopens_nonrefund_mismatch_once_per_cooldown(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="operation-mismatch-manual",
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "bank_payment_amount_mismatch"
    order.save(update_fields=["status", "last_error_code", "updated_at"])
    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = "bank_payment_amount_mismatch"
    attempt.retry_at = None
    attempt.save(update_fields=["status", "last_error_code", "retry_at", "updated_at"])

    revived = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
        allow_manual_retry=True,
        actor_user_id=owner_user.id,
    )

    assert revived.status == BankPaymentReconciliationAttempt.Status.PENDING
    assert revived.attempt_count == 0
    assert revived.last_error_code == ""

    revived.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    revived.last_error_code = "bank_payment_amount_mismatch"
    revived.retry_at = None
    revived.save(update_fields=["status", "last_error_code", "retry_at", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        request_provider_reconciliation(
            club_id=club.id,
            order_id=order.id,
            provider_event_id=None,
            allow_manual_retry=True,
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "tochka_reconciliation_cooldown"
    assert exc_info.value.message == "Сверка уже запрошена. Подождите перед повтором"


@pytest.mark.django_db
def test_owner_retry_rejects_immediate_repeat_while_attempt_is_active(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="operation-active-manual",
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "bank_payment_amount_mismatch"
    order.save(update_fields=["status", "last_error_code", "updated_at"])
    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = order.last_error_code
    attempt.retry_at = None
    attempt.save(update_fields=["status", "last_error_code", "retry_at", "updated_at"])

    revived = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
        allow_manual_retry=True,
        actor_user_id=owner_user.id,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        request_provider_reconciliation(
            club_id=club.id,
            order_id=order.id,
            provider_event_id=None,
            allow_manual_retry=True,
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "tochka_reconciliation_already_requested"
    assert exc_info.value.message == "Сверка уже запрошена. Дождитесь результата"
    revived.refresh_from_db()
    assert revived.status == BankPaymentReconciliationAttempt.Status.PENDING
    assert (
        BankPaymentOrderReviewEvent.objects.for_club(club)
        .filter(order=order, resolution=BankPaymentOrderReviewEvent.Resolution.RETRY_RECONCILIATION)
        .count()
        == 1
    )


@pytest.mark.django_db
def test_owner_retry_creates_and_audits_missing_attempt(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.DISPATCHED,
        operation_id="operation-missing-attempt",
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "bank_payment_amount_mismatch"
    order.save(update_fields=["status", "last_error_code", "updated_at"])

    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
        allow_manual_retry=True,
        actor_user_id=owner_user.id,
    )

    assert attempt.status == BankPaymentReconciliationAttempt.Status.PENDING
    review_event = BankPaymentOrderReviewEvent.objects.for_club(club).get(
        order=order,
        resolution=BankPaymentOrderReviewEvent.Resolution.RETRY_RECONCILIATION,
    )
    assert review_event.actor_id == owner_user.id
    assert review_event.previous_status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert review_event.new_status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert review_event.evidence_metadata == {"previous_attempt_error_code": ""}


@pytest.mark.django_db
def test_owner_manual_retry_rejects_non_tochka_at_service_boundary(
    settings,
    club,
    owner_user,
):
    order = _order(settings=settings, club=club, owner_user=owner_user)
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "bank_payment_amount_mismatch"
    order.save(update_fields=["status", "last_error_code", "updated_at"])
    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = order.last_error_code
    attempt.save(update_fields=["status", "last_error_code", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        request_provider_reconciliation(
            club_id=club.id,
            order_id=order.id,
            provider_event_id=None,
            allow_manual_retry=True,
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "tochka_manual_reconciliation_not_available"
    assert exc_info.value.message == "Сверка с банком недоступна для этой оплаты"
    attempt.refresh_from_db()
    assert attempt.status == BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW


@pytest.mark.django_db
def test_owner_manual_retry_rejects_unsupported_creation_state(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.READY,
        operation_id="",
    )
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "tochka_operation_id_missing"
    order.save(update_fields=["status", "last_error_code", "updated_at"])
    attempt = request_provider_reconciliation(
        club_id=club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
    attempt.last_error_code = "tochka_operation_id_missing"
    attempt.save(update_fields=["status", "last_error_code", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        request_provider_reconciliation(
            club_id=club.id,
            order_id=order.id,
            provider_event_id=None,
            allow_manual_retry=True,
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "tochka_manual_reconciliation_not_retryable"
    assert exc_info.value.message == "Эта оплата требует отдельной проверки поддержки"


@pytest.mark.django_db
def test_owner_manual_retry_rejects_refund_review_at_service_boundary(
    settings,
    club,
    owner_user,
):
    order = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    order.status = BankPaymentOrder.Status.MANUAL_REVIEW
    order.last_error_code = "bank_payment_refunded_requires_review"
    order.save(update_fields=["status", "last_error_code", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        request_provider_reconciliation(
            club_id=club.id,
            order_id=order.id,
            provider_event_id=None,
            allow_manual_retry=True,
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "tochka_manual_reconciliation_not_available"
    assert not BankPaymentReconciliationAttempt.objects.for_club(club).filter(order=order).exists()


@pytest.mark.django_db
def test_missing_reconciliation_identity_moves_order_to_manual_review(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    settings.TOCHKA_CUSTOMER_CODE = ""
    settings.TOCHKA_MERCHANT_ID = ""
    order = _as_tochka(_order(settings=settings, club=club, owner_user=owner_user))
    order.provider_customer_code = ""
    order.provider_merchant_id = ""
    order.save(update_fields=["provider_customer_code", "provider_merchant_id", "updated_at"])
    request_provider_reconciliation(club_id=club.id, order_id=order.id, provider_event_id=None)
    provider = Mock()
    provider.get_payment_operation_info.return_value = ProviderOperationInfo(
        status="APPROVED",
        operation_id=order.provider_operation_id,
        payment_link_id=order.provider_payment_link_id,
        amount=order.amount_snapshot,
        paid_at=timezone.now(),
        payment_modes=["sbp"],
    )

    with (
        patch("apps.billing.service_modules.provider_events.get_payment_provider", return_value=provider),
        patch(
            "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
            return_value=SimpleNamespace(reconciliation_available=True),
        ),
    ):
        outcome = reconcile_provider_payment_order(club_id=club.id, order_id=order.id)

    order.refresh_from_db()
    assert outcome == "manual_review"
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert order.last_error_code == "bank_payment_customer_identity_missing"


@pytest.mark.django_db
def test_reconciliation_kill_switch_blocks_all_scheduled_provider_work(
    settings,
    club,
    owner_user,
):
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = False
    _as_tochka(
        _order(settings=settings, club=club, owner_user=owner_user),
        state=BankPaymentOrder.LinkCreationState.UNKNOWN,
        operation_id="",
    )
    from apps.billing.tasks import process_due_bank_payment_provider_work

    with (
        patch("apps.billing.service_modules.provider_events.get_payment_provider") as get_provider,
        patch("apps.billing.service_modules.bank_orders.recover_unknown_bank_payment_order") as recover,
    ):
        result = process_due_bank_payment_provider_work()

    assert result == {
        "reconciliation": {"disabled": 1},
        "creation_recovery": {"disabled": 1},
    }
    get_provider.assert_not_called()
    recover.assert_not_called()


@pytest.mark.django_db
def test_crash_after_financial_commit_leaves_recoverable_dispatched_claim(
    settings,
    club,
    owner_user,
):
    student, tariff = _tochka_creation_subject(settings=settings, club=club)
    provider = Mock()
    provider.create_payment_link.side_effect = KeyboardInterrupt()

    with (
        patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        patch("apps.billing.payment_providers.base.online_payments_enabled", return_value=True),
        pytest.raises(KeyboardInterrupt),
    ):
        create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

    order = BankPaymentOrder.objects.for_club(club).get(student=student)
    assert order.link_creation_state == BankPaymentOrder.LinkCreationState.DISPATCHED
    assert order.link_creation_dispatched_at is not None
    assert order.status == BankPaymentOrder.Status.CREATED


@pytest.mark.django_db
def test_receipt_validation_fails_before_financial_family_or_provider_io(
    settings,
    club,
    owner_user,
):
    student, tariff = _tochka_creation_subject(settings=settings, club=club)
    settings.TOCHKA_RECEIPT_MODE = BankPaymentOrder.ReceiptMode.TOCHKA_RECEIPT
    provider = Mock()

    from apps.common.exceptions import BusinessLogicError

    with (
        patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        patch("apps.billing.payment_providers.base.online_payments_enabled", return_value=True),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

    assert exc_info.value.code == "receipt_buyer_email_required"
    assert not BankPaymentOrder.objects.for_club(club).filter(student=student).exists()
    provider.create_payment_link.assert_not_called()
