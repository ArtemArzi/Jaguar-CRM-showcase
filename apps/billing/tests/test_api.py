import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import PersonalDropInBooking, ScheduleEnrollment, TrainingGroupRolloutState
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
    TrainingGroupRolloutStateFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    DebtWriteOffEvent,
    Expense,
    Payment,
    PaymentReturnState,
    Subscription,
    SubscriptionFreeze,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.payment_providers.base import ProviderRetailerInfo, online_payments_enabled
from apps.billing.schemas import BankPaymentOrderOut
from apps.billing.service_modules.payment_readiness import record_authenticated_retailer_readback
from apps.billing.tests.factories import (
    DebtFactory,
    DiscountFactory,
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    SubscriptionFreezeFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.grades.tests.factories import GradeSystemFactory
from apps.leads.models import LeadLifecycleEvent
from apps.leads.tests.factories import LeadFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


class _CapturedLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


NON_CURRENT_PACKAGE_CASES = [
    ("expired", Subscription.Status.EXPIRED, 30, 8, False),
    ("pending", Subscription.Status.PENDING, 30, 8, False),
    ("frozen", Subscription.Status.FROZEN, 30, 8, False),
    ("past_expiry", Subscription.Status.ACTIVE, -1, 8, False),
    ("depleted", Subscription.Status.ACTIVE, 30, 0, False),
    ("soft_deleted", Subscription.Status.ACTIVE, 30, 8, True),
]


def test_approved_unfulfilled_bank_order_remains_refreshable_for_staff():
    order = SimpleNamespace(
        status=BankPaymentOrder.Status.APPROVED,
        subscription=SimpleNamespace(status=Subscription.Status.PENDING),
        payment_action_mode="staff",
        can_refresh_source_allowed=True,
    )

    assert BankPaymentOrderOut.resolve_fulfillment_state(order) == "fulfillment_pending"
    assert BankPaymentOrderOut.resolve_can_request_refresh(order) is True


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _create_active_package_allocation(
    *,
    club,
    student,
    owner_trainer,
    status=Subscription.Status.ACTIVE,
    expires_at=None,
    trainings_left=None,
    deleted=False,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    subscription = SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=status,
        expires_at=expires_at,
        trainings_left=tariff.trainings_limit if trainings_left is None else trainings_left,
        paid_amount=Decimal("5000"),
    )
    if deleted:
        subscription.soft_delete()
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
        is_active=True,
    )
    return subscription


def _configure_ready_live_provider(settings, *, checked_at=None):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
    settings.TOCHKA_JWT_TOKEN = "test-payment-provider-token"
    settings.TOCHKA_CUSTOMER_CODE = "configured-customer"
    settings.TOCHKA_MERCHANT_ID = "configured-merchant"
    settings.TOCHKA_API_BASE_URL = "https://enter.tochka.com/uapi"
    settings.TOCHKA_WEBHOOK_KEY_MODE = "pem"
    settings.TOCHKA_WEBHOOK_PUBLIC_KEY = "configured-webhook-public-key"
    settings.TOCHKA_PAYMENT_MODES = ["sbp"]
    settings.TOCHKA_FISCALIZATION_READY = True
    settings.TOCHKA_FISCALIZATION_DECISION_ID = "test-fiscal-decision"
    settings.TOCHKA_RECEIPT_MODE = BankPaymentOrder.ReceiptMode.NONE
    settings.JAGUAR_PAYMENT_RETURN_ORIGIN = settings.JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN
    return record_authenticated_retailer_readback(
        retailer_info=ProviderRetailerInfo(
            status="REG",
            is_active=True,
            merchant_id="configured-merchant",
            payment_modes=["sbp"],
            checked_at=checked_at or timezone.now(),
        )
    )


def _payment_capability_fields(
    *,
    enabled: bool = False,
    staff: bool = True,
    reason: str = "mock_provider",
    creation_enabled: bool = True,
    reconciliation_enabled: bool = True,
    reconciliation_available: bool = False,
) -> dict:
    return {
        "online_payments_enabled": enabled,
        "payment_mode": "sbp",
        "payment_modes": ["sbp"],
        "payment_creation_enabled": creation_enabled,
        "payment_reconciliation_enabled": reconciliation_enabled,
        "payment_unavailable_reason": reason if staff else ("unavailable" if not enabled else ""),
        "can_create_payment_order": enabled,
        "can_request_payment_reconciliation": staff and reconciliation_available,
    }


@pytest.mark.django_db
class TestPaymentCapabilitiesAPI:
    @pytest.mark.parametrize(
        ("user_fixture", "role"),
        [
            ("owner_user", "owner"),
            ("admin_user", "admin"),
            ("trainer_user", "trainer"),
            ("student_user", "student"),
            ("parent_user", "parent"),
        ],
    )
    def test_returns_disabled_payment_and_canonical_selection_capabilities_to_every_supported_role(
        self,
        request,
        settings,
        club,
        user_fixture,
        role,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.DEBUG = False
        user = request.getfixturevalue(user_fixture)

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(user, club, role=role),
        )

        assert response.status_code == 200
        assert response.json() == {
            **_payment_capability_fields(staff=role in {"owner", "admin", "trainer"}),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

    def test_enables_mock_only_when_explicit_development_order_and_webhook_switches_are_on(
        self,
        settings,
        club,
        owner_user,
        student_user,
    ):
        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True

        enabled = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert enabled.status_code == 200
        assert enabled.json() == {
            **_payment_capability_fields(
                enabled=True,
                reason="",
                reconciliation_enabled=False,
            ),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

        student_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(student_user, club, role="student"),
        )
        assert student_response.status_code == 200
        assert student_response.json() == {
            **_payment_capability_fields(
                enabled=True,
                staff=False,
                reason="",
                reconciliation_enabled=False,
            ),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

    @pytest.mark.parametrize(
        ("setting_name", "setting_value", "expected_reason"),
        [
            ("DEBUG", False, "mock_provider"),
            ("ONLINE_PAYMENT_ORDER_CREATION_ENABLED", False, "creation_disabled"),
            ("MOCK_PAYMENT_ORDER_CREATION_ENABLED", False, "mock_payment_order_creation_disabled"),
            ("MOCK_PAYMENT_WEBHOOKS_ENABLED", False, "mock_payment_webhook_disabled"),
        ],
    )
    def test_mock_capability_fails_closed_when_any_safe_switch_is_off(
        self,
        settings,
        club,
        owner_user,
        setting_name,
        setting_value,
        expected_reason,
    ):
        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True
        setattr(settings, setting_name, setting_value)

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["online_payments_enabled"] is False
        assert response.json()["payment_unavailable_reason"] == expected_reason
        assert response.json()["can_create_payment_order"] is False

    def test_fails_closed_for_unknown_or_incomplete_provider_configuration(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = "unknown"

        unknown_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.TOCHKA_JWT_TOKEN = ""
        settings.TOCHKA_CUSTOMER_CODE = "configured-customer"
        settings.TOCHKA_WEBHOOK_PUBLIC_KEY = "configured-webhook-public-key"
        incomplete_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert unknown_response.status_code == 200
        assert unknown_response.json() == {
            **_payment_capability_fields(reason="unknown_provider"),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }
        assert incomplete_response.status_code == 200
        assert incomplete_response.json() == {
            **_payment_capability_fields(reason="credentials_missing"),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

    @pytest.mark.parametrize("webhook_public_key", [None, "", "   "])
    def test_fails_closed_when_tochka_webhook_verification_key_is_missing_or_blank(
        self,
        settings,
        club,
        owner_user,
        webhook_public_key,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.TOCHKA_JWT_TOKEN = "test-payment-provider-token"
        settings.TOCHKA_CUSTOMER_CODE = "configured-customer"
        settings.TOCHKA_MERCHANT_ID = "configured-merchant"
        settings.TOCHKA_API_BASE_URL = "https://enter.tochka.com/uapi"
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = settings.JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN
        settings.TOCHKA_WEBHOOK_PUBLIC_KEY = webhook_public_key

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json() == {
            **_payment_capability_fields(reason="webhook_verification_not_ready"),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

    def test_enables_only_ready_live_provider_without_disclosing_configuration(
        self,
        settings,
        club,
        owner_user,
    ):
        snapshot = _configure_ready_live_provider(settings)

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert snapshot.customer_code_hash != "configured-customer"
        assert snapshot.merchant_id_hash != "configured-merchant"
        assert len(snapshot.customer_code_hash) == len(snapshot.merchant_id_hash) == 64
        assert "configured-customer" not in response.content.decode()
        assert "configured-merchant" not in response.content.decode()
        assert response.json() == {
            **_payment_capability_fields(
                enabled=True,
                reason="",
                reconciliation_available=True,
            ),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
            "training_group_payment_selection_mode": "legacy",
            "canonical_group_selection_enabled": False,
        }

    def test_ready_live_provider_stays_disabled_until_creation_switch_is_enabled(
        self,
        settings,
        club,
        owner_user,
    ):
        _configure_ready_live_provider(settings)
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["online_payments_enabled"] is False
        assert response.json()["payment_unavailable_reason"] == "creation_disabled"
        assert response.json()["can_request_payment_reconciliation"] is True

    def test_ready_live_provider_accepts_extra_retailer_capabilities_but_keeps_sbp_only_creation(
        self,
        settings,
        club,
        owner_user,
    ):
        snapshot = _configure_ready_live_provider(settings)
        snapshot.payment_modes = ["sbp", "card"]
        snapshot.save(update_fields=["payment_modes", "updated_at"])

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["online_payments_enabled"] is True
        assert response.json()["payment_modes"] == ["sbp"]

    @pytest.mark.parametrize(
        ("failure", "expected_reason"),
        [
            ("missing_readback", "retailer_readback_missing"),
            ("stale_readback", "retailer_readback_stale"),
            ("inactive_retailer", "retailer_not_ready"),
            ("retailer_without_sbp", "retailer_not_ready"),
            ("card_configured", "payment_mode_invalid"),
            ("wrong_return_origin", "return_origin_invalid"),
            ("fiscalization_undecided", "fiscalization_undecided"),
            ("reconciliation_disabled", "reconciliation_disabled"),
        ],
    )
    def test_live_readiness_failures_are_role_safe_and_block_before_financial_writes(
        self,
        settings,
        club,
        owner_user,
        student_user,
        failure,
        expected_reason,
    ):
        snapshot = _configure_ready_live_provider(settings)
        if failure == "missing_readback":
            snapshot.delete()
        elif failure == "stale_readback":
            snapshot.expires_at = timezone.now() - timedelta(seconds=1)
            snapshot.save(update_fields=["expires_at", "updated_at"])
        elif failure == "inactive_retailer":
            snapshot.is_active = False
            snapshot.save(update_fields=["is_active", "updated_at"])
        elif failure == "retailer_without_sbp":
            snapshot.payment_modes = ["card"]
            snapshot.save(update_fields=["payment_modes", "updated_at"])
        elif failure == "card_configured":
            settings.TOCHKA_PAYMENT_MODES = ["sbp", "card"]
        elif failure == "wrong_return_origin":
            settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://payments.example.test"
        elif failure == "fiscalization_undecided":
            settings.TOCHKA_FISCALIZATION_DECISION_ID = ""
        elif failure == "reconciliation_disabled":
            settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = False

        staff_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )
        student_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(student_user, club, role="student"),
        )

        assert staff_response.status_code == student_response.status_code == 200
        assert staff_response.json()["payment_unavailable_reason"] == expected_reason
        assert student_response.json()["payment_unavailable_reason"] == "unavailable"
        assert staff_response.json()["online_payments_enabled"] is False
        assert student_response.json()["online_payments_enabled"] is False

        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        counts_before = (
            BankPaymentOrder.objects.count(),
            Payment.objects.count(),
            Subscription.objects.count(),
        )
        with (
            patch("apps.billing.payment_providers.tochka.TochkaPaymentProvider._post_json") as post,
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            from apps.billing.services import create_bank_payment_order

            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "tochka_payment_creation_not_ready"
        post.assert_not_called()
        assert counts_before == (
            BankPaymentOrder.objects.count(),
            Payment.objects.count(),
            Subscription.objects.count(),
        )

    def test_active_rollout_fails_closed_until_global_new_writes_switch_is_enabled(
        self,
        settings,
        club,
        owner_user,
    ):
        # This test isolates group-selection capability; keep the mock payment
        # provider in its production fail-closed state.
        settings.DEBUG = False
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE
        )

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json() == {
            **_payment_capability_fields(),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.ACTIVE,
            "training_group_payment_selection_mode": "disabled",
            "canonical_group_selection_enabled": False,
        }

        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        enabled_response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert enabled_response.status_code == 200
        assert enabled_response.json() == {
            **_payment_capability_fields(),
            "training_group_rollout_mode": TrainingGroupRolloutState.Mode.ACTIVE,
            "training_group_payment_selection_mode": "canonical",
            "canonical_group_selection_enabled": True,
        }

    @pytest.mark.parametrize(
        ("rollout_mode", "new_writes_enabled", "expected_selection_mode"),
        [
            (TrainingGroupRolloutState.Mode.OFF, False, "legacy"),
            (TrainingGroupRolloutState.Mode.OFF, True, "legacy"),
            (TrainingGroupRolloutState.Mode.SHADOW, True, "legacy"),
            (TrainingGroupRolloutState.Mode.SHADOW, False, "disabled"),
            (TrainingGroupRolloutState.Mode.ACTIVE, True, "canonical"),
            (TrainingGroupRolloutState.Mode.ACTIVE, False, "disabled"),
            (TrainingGroupRolloutState.Mode.RECONCILING, True, "disabled"),
            (TrainingGroupRolloutState.Mode.CONTAINMENT, True, "disabled"),
        ],
    )
    def test_returns_server_owned_group_selection_mode_matrix(
        self,
        settings,
        club,
        owner_user,
        rollout_mode,
        new_writes_enabled,
        expected_selection_mode,
    ):
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=rollout_mode
        )
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = new_writes_enabled

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["training_group_rollout_mode"] == rollout_mode
        assert response.json()["training_group_payment_selection_mode"] == expected_selection_mode
        assert response.json()["canonical_group_selection_enabled"] == (
            expected_selection_mode == "canonical"
        )

    def test_missing_rollout_state_disables_new_group_payment_selection(
        self,
        club,
        owner_user,
    ):
        TrainingGroupRolloutState.objects.for_club(club).delete()

        response = client.get(
            "/billing/payment-capabilities/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["training_group_rollout_mode"] == "missing"
        assert response.json()["training_group_payment_selection_mode"] == "disabled"
        assert response.json()["canonical_group_selection_enabled"] is False


@pytest.mark.django_db
class TestPaymentReturnAPI:
    BROWSER_BINDING = "browser-binding-aaaaaaaaaaaaaaaa"
    OTHER_BROWSER_BINDING = "browser-binding-bbbbbbbbbbbbbbbb"

    @staticmethod
    def _order(*, settings, club, owner_user):
        from apps.billing.services import create_bank_payment_order

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        student = StudentFactory(club=club, first_name="PrivateReturnStudent")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        return create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

    def test_exchange_is_generic_same_browser_idempotent_and_clearable(
        self,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.service_modules.payment_returns import COOKIE_NAME, COOKIE_PATH, create_return_state

        cache.clear()
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
        order = self._order(settings=settings, club=club, owner_user=owner_user)
        raw_state, return_url = create_return_state(order=order)
        state = PaymentReturnState.objects.filter(order=order).latest("id")
        browser = TestClient(api)
        other_browser = TestClient(api)

        exchange = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
        )
        # Ninja's in-process client does not automatically resend a Secure
        # cookie over its synthetic HTTP origin.  Install the returned opaque
        # handle explicitly to model the production HTTPS browser.
        browser.cookies[COOKIE_NAME] = exchange.cookies[COOKIE_NAME].value
        replay = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
        )
        elsewhere = other_browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.OTHER_BROWSER_BINDING},
        )
        status = browser.get("/billing/payment-returns/status/")
        cleared = browser.delete("/billing/payment-returns/session/")
        after_clear = browser.get("/billing/payment-returns/status/")

        assert return_url == f"https://app.jaguar.test/payments/return?state={raw_state}"
        assert state.state_hash != raw_state
        assert exchange.status_code == replay.status_code == status.status_code == 200
        assert exchange.json() == replay.json() == status.json() == {"status": "checking"}
        assert elsewhere.status_code == 404
        cookie = exchange.cookies[COOKIE_NAME]
        assert cookie.value != raw_state
        assert cookie["secure"] is True
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"
        assert cookie["path"] == COOKIE_PATH
        assert cleared.status_code == 204
        assert cleared.cookies[COOKIE_NAME].value == ""
        assert cleared.cookies[COOKIE_NAME]["max-age"] == 0
        assert after_clear.status_code == 404

    @pytest.mark.django_db(transaction=True)
    def test_postgresql_concurrent_same_browser_exchange_converges_to_one_session(
        self,
        settings,
        club,
        owner_user,
    ):
        if connection.vendor != "postgresql":
            pytest.skip("requires PostgreSQL row locking")
        from apps.billing.service_modules.payment_returns import COOKIE_NAME, create_return_state

        cache.clear()
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
        order = self._order(settings=settings, club=club, owner_user=owner_user)
        raw_state, _return_url = create_return_state(order=order)
        gate = Barrier(2)

        def exchange_once():
            close_old_connections()
            browser = TestClient(api)
            gate.wait(timeout=5)
            response = browser.post(
                "/billing/payment-returns/exchange/",
                json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
            )
            result = response.status_code, response.json(), response.cookies[COOKIE_NAME].value
            close_old_connections()
            return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result(timeout=10) for future in [executor.submit(exchange_once) for _ in range(2)]]

        assert {status for status, _body, _cookie in results} == {200}
        assert {tuple(body.items()) for _status, body, _cookie in results} == {(("status", "checking"),)}
        assert len({cookie for _status, _body, cookie in results}) == 1

    def test_invalid_expired_and_wrong_purpose_states_are_non_enumerating(
        self,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.service_modules.payment_returns import create_return_state

        cache.clear()
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
        order = self._order(settings=settings, club=club, owner_user=owner_user)
        raw_state, _return_url = create_return_state(order=order)
        state = PaymentReturnState.objects.filter(order=order).latest("id")
        browser = TestClient(api)

        malformed = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": "x" * 4096, "browser_binding": self.BROWSER_BINDING},
        )
        state.purpose = "wrong_purpose"
        state.save(update_fields=["purpose", "updated_at"])
        wrong_purpose = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
        )
        state.purpose = "payment_return"
        state.expires_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=["purpose", "expires_at", "updated_at"])
        expired = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
        )

        for response in (malformed, wrong_purpose, expired):
            assert response.status_code == 404
            assert response.json()["detail"] == "payment return unavailable"

    def test_terminal_session_grace_starts_on_exchange_and_does_not_slide(
        self,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.service_modules.payment_returns import COOKIE_NAME, create_return_state

        cache.clear()
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
        order = self._order(settings=settings, club=club, owner_user=owner_user)
        order.status = BankPaymentOrder.Status.APPROVED
        order.save(update_fields=["status", "updated_at"])
        raw_state, _return_url = create_return_state(order=order)
        browser = TestClient(api)

        exchange = browser.post(
            "/billing/payment-returns/exchange/",
            json={"state": raw_state, "browser_binding": self.BROWSER_BINDING},
        )
        browser.cookies[COOKIE_NAME] = exchange.cookies[COOKIE_NAME].value
        state = PaymentReturnState.objects.filter(order=order).latest("id")
        first_deadline = state.terminal_grace_expires_at
        status = browser.get("/billing/payment-returns/status/")
        state.refresh_from_db()

        assert exchange.status_code == status.status_code == 200
        assert exchange.json() == status.json() == {"status": "approved"}
        assert set(exchange.json()) == {"status"}
        assert "PrivateReturnStudent" not in exchange.content.decode()
        assert first_deadline is not None
        assert state.session_expires_at == first_deadline
        assert state.terminal_grace_expires_at == first_deadline


@pytest.mark.django_db
class TestPaymentCapabilitiesLogging:
    @pytest.mark.parametrize(
        ("provider", "token", "customer_code", "expected_reason"),
        [
            ("unexpected-provider", "secret-payment-token", "customer-code", "unknown_provider"),
            (
                BankPaymentOrder.Provider.TOCHKA,
                "secret-payment-token",
                "",
                "api_origin_invalid",
            ),
        ],
    )
    def test_logs_safe_reason_code_without_provider_configuration(
        self,
        settings,
        provider,
        token,
        customer_code,
        expected_reason,
    ):
        settings.PAYMENT_PROVIDER = provider
        settings.TOCHKA_JWT_TOKEN = token
        settings.TOCHKA_CUSTOMER_CODE = customer_code
        settings.TOCHKA_API_BASE_URL = "http://private-payment-host.invalid/api"
        settings.TOCHKA_WEBHOOK_PUBLIC_KEY = "test-webhook-public-key"

        handler = _CapturedLogHandler()
        provider_logger = logging.getLogger("apps.billing.payment_providers.base")
        provider_logger.addHandler(handler)
        try:
            assert online_payments_enabled() is False
        finally:
            provider_logger.removeHandler(handler)

        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.getMessage() == "online_payment_capability_unavailable"
        assert record.payment_capability_reason == expected_reason
        logged = f"{record.getMessage()} {record.payment_capability_reason}"
        assert token not in logged
        if customer_code:
            assert customer_code not in logged
        assert settings.TOCHKA_WEBHOOK_PUBLIC_KEY not in logged
        assert "private-payment-host.invalid" not in logged
        assert "unexpected-provider" not in logged


@pytest.mark.django_db
class TestTrainingTypeAPI:
    def test_create_training_type_api(self, club, owner_user):
        grade_system = GradeSystemFactory(club=club, discipline="BJJ")

        response = client.post(
            "/billing/training-types/",
            json={
                "name": "Personal",
                "slug": "personal",
                "kind": TrainingType.Kind.PERSONAL,
                "grade_system_id": grade_system.id,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Personal"
        assert data["slug"] == "personal"
        assert data["kind"] == TrainingType.Kind.PERSONAL
        assert data["is_active"] is True
        assert data["grade_system_id"] == grade_system.id

    def test_list_training_types_api(self, club, owner_user):
        grade_system = GradeSystemFactory(club=club, discipline="BJJ")
        TrainingTypeFactory(club=club, grade_system=grade_system)
        response = client.get(
            "/billing/training-types/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["kind"]
        assert data[0]["grade_system_id"] == grade_system.id

    def test_create_training_type_rejects_foreign_grade_system(self, club, other_club, owner_user):
        foreign_grade_system = GradeSystemFactory(club=other_club, discipline="BJJ")

        response = client.post(
            "/billing/training-types/",
            json={"name": "Group", "slug": "group", "grade_system_id": foreign_grade_system.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "grade_system_not_found"


@pytest.mark.django_db
class TestTariffAPI:
    def test_create_tariff_api(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        response = client.post(
            "/billing/tariffs/",
            json={
                "name": "8 sessions",
                "training_type_id": tt.id,
                "price": "5000.00",
                "trainings_limit": 8,
                "duration_days": 30,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "8 sessions"
        assert Decimal(str(data["price"])) == Decimal("5000.00")

    def test_list_tariffs_api(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        TariffFactory(training_type=tt)
        response = client.get(
            "/billing/tariffs/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_update_tariff_endpoint_updates_allowed_fields(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(
            training_type=tt,
            name="8 sessions",
            price=Decimal("5000"),
            trainings_limit=8,
            duration_days=30,
            description="Old",
            is_active=True,
        )

        response = client.put(
            f"/billing/tariffs/{tariff.id}/",
            json={
                "name": "12 sessions",
                "price": "6500.00",
                "trainings_limit": 12,
                "duration_days": 45,
                "description": "Updated",
                "is_active": False,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "12 sessions"
        assert Decimal(str(data["price"])) == Decimal("6500.00")
        tariff.refresh_from_db()
        assert tariff.trainings_limit == 12
        assert tariff.duration_days == 45
        assert tariff.description == "Updated"
        assert tariff.is_active is False

    def test_create_tariff_api_accepts_package_components(self, club, owner_user):
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP, slug="api-hybrid-group")
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL, slug="api-hybrid-personal")

        response = client.post(
            "/billing/tariffs/",
            json={
                "name": "Гибрид Оптимум",
                "training_type_id": group_type.id,
                "price": "9500.00",
                "duration_days": 30,
                "components": [
                    {
                        "name": "Группа",
                        "training_type_id": group_type.id,
                        "entitlement_kind": "weekly_limit",
                        "weekly_limit": 2,
                        "trainer_payout_policy": "on_payment",
                        "paid_amount_basis": "3500.00",
                    },
                    {
                        "name": "Персоналки",
                        "training_type_id": personal_type.id,
                        "entitlement_kind": "finite_credits",
                        "credits_total": 3,
                        "trainer_payout_policy": "on_checkin",
                        "paid_amount_basis": "6000.00",
                    },
                ],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["payout_timing_hint"] == "mixed"
        assert data["trainer_payout_policy"] == "on_payment"
        assert data["requires_package_owner"] is True
        assert [
            (component["training_type_id"], component["trainer_payout_policy"], component["paid_amount_basis"])
            for component in data["components"]
        ] == [
            (group_type.id, "on_payment", "3500.00"),
            (personal_type.id, "on_checkin", "6000.00"),
        ]
        assert TariffComponent.objects.for_club(club).filter(tariff_id=data["id"]).count() == 2

    def test_create_tariff_api_rejects_component_sum_mismatch(self, club, owner_user):
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP, slug="api-bad-group")
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL, slug="api-bad-personal")

        response = client.post(
            "/billing/tariffs/",
            json={
                "name": "Bad Hybrid",
                "training_type_id": group_type.id,
                "price": "9500.00",
                "duration_days": 30,
                "components": [
                    {
                        "training_type_id": group_type.id,
                        "entitlement_kind": "weekly_limit",
                        "weekly_limit": 2,
                        "trainer_payout_policy": "on_payment",
                        "paid_amount_basis": "3000.00",
                    },
                    {
                        "training_type_id": personal_type.id,
                        "entitlement_kind": "finite_credits",
                        "credits_total": 3,
                        "trainer_payout_policy": "on_checkin",
                        "paid_amount_basis": "6000.00",
                    },
                ],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "component_amount_sum_mismatch"

    def test_update_tariff_blocks_component_changes_with_pending_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000.00"), trainings_limit=5)
        student = StudentFactory(club=club)
        PaymentFactory(club=club, student=student, tariff=tariff, status=Payment.Status.PENDING)

        response = client.put(
            f"/billing/tariffs/{tariff.id}/",
            json={
                "trainer_payout_policy": "on_payment",
                "components": [
                    {
                        "training_type_id": tt.id,
                        "entitlement_kind": "finite_credits",
                        "credits_total": 5,
                        "trainer_payout_policy": "on_payment",
                        "paid_amount_basis": "5000.00",
                    }
                ],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "tariff_contract_change_blocked"

    def test_update_tariff_allows_deactivation_with_pending_payment(self, club, owner_user):
        tariff = TariffFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        PaymentFactory(club=club, student=student, tariff=tariff, status=Payment.Status.PENDING)

        response = client.put(
            f"/billing/tariffs/{tariff.id}/",
            json={"is_active": False},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        tariff.refresh_from_db()
        assert tariff.is_active is False

    def test_update_tariff_allows_unchanged_components_with_pending_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000.00"), trainings_limit=5)
        TariffComponent.objects.create(
            club=club,
            tariff=tariff,
            name=tariff.name,
            training_type=tt,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=5,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis=Decimal("5000.00"),
        )
        student = StudentFactory(club=club)
        PaymentFactory(club=club, student=student, tariff=tariff, status=Payment.Status.PENDING)

        response = client.put(
            f"/billing/tariffs/{tariff.id}/",
            json={
                "name": "Renamed",
                "components": [
                    {
                        "name": tariff.name,
                        "training_type_id": tt.id,
                        "entitlement_kind": "finite_credits",
                        "credits_total": 5,
                        "trainer_payout_policy": "on_checkin",
                        "paid_amount_basis": "5000.00",
                    }
                ],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_list_tariffs_exposes_safe_payout_hint_only(self, club, trainer_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TariffFactory(training_type=tt, trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN)

        response = client.get(
            "/billing/tariffs/",
            **_auth_params(trainer_user, club),
        )

        assert response.status_code == 200
        tariff = response.json()["items"][0]
        assert tariff["payout_timing_hint"] == "per_checkin"
        assert tariff["requires_package_owner"] is True
        assert tariff["trainer_payout_policy"] == ""
        assert tariff["components"] == []


@pytest.mark.django_db
class TestSubscriptionAPI:
    def test_create_subscription_api(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.TRANSFER,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["trainings_left"] == 8
        assert data["status"] == "active"
        payment = Payment.objects.get(subscription_id=data["id"])
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.recorded_by_id == owner_user.id
        assert payment.verified_by_id == owner_user.id
        assert payment.payment_method == Payment.Method.TRANSFER

    def test_create_subscription_api_requires_payment_method_without_side_effects(self, club, owner_user):
        tariff = TariffFactory(
            club=club,
            training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
        )
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/subscriptions/",
            json={"student_id": student.id, "tariff_id": tariff.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 422
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        assert not Payment.objects.for_club(club).filter(student=student).exists()

    @pytest.mark.parametrize("payment_method", [Payment.Method.ONLINE, "crypto"])
    def test_create_subscription_api_rejects_non_manual_payment_method_without_side_effects(
        self,
        payment_method,
        club,
        owner_user,
    ):
        tariff = TariffFactory(
            club=club,
            training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
        )
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": payment_method,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 422
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        assert not Payment.objects.for_club(club).filter(student=student).exists()

    def test_trainer_cannot_create_confirmed_subscription(self, club, trainer_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert Subscription.objects.count() == 0
        assert Payment.objects.count() == 0

    def test_subscription_no_amount_in_input(self, club, owner_user):
        """SubscriptionIn schema has no amount field -- extra fields are ignored."""
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"))
        student = StudentFactory(club=club)
        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
                "amount": 999,
            },
            **_auth_params(owner_user, club),
        )
        # amount is ignored, price comes from tariff
        assert response.status_code == 201
        data = response.json()
        assert Decimal(str(data["tariff"]["price"])) == Decimal("7000")

    def test_create_personal_subscription_requires_package_owner(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert TrainerPackageAllocation.objects.count() == 0

    def test_subscription_list_exposes_immutable_paid_amount(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        SubscriptionFactory(
            tariff=tariff,
            student=student,
            paid_amount=Decimal("5000"),
        )
        tariff.price = Decimal("7000")
        tariff.save(update_fields=["price"])

        response = client.get(
            "/billing/subscriptions/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert Decimal(str(item["paid_amount"])) == Decimal("5000")
        assert Decimal(str(item["tariff"]["price"])) == Decimal("7000")

    def test_create_personal_subscription_exposes_package_owner(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        package_owner = TrainerFactory(club=club, first_name="Owner", last_name="Coach")

        response = client.post(
            "/billing/subscriptions/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
                "package_owner_trainer_id": package_owner.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["training_type_kind"] == TrainingType.Kind.PERSONAL
        assert data["package_owner_trainer_id"] == package_owner.id
        assert data["package_owner_trainer_name"] == str(package_owner)
        allocation = TrainerPackageAllocation.objects.get(subscription_id=data["id"])
        assert allocation.owner_trainer_id == package_owner.id

    def test_subscription_list_does_not_expose_foreign_club_package_allocation(
        self,
        club,
        other_club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            paid_amount=tariff.price,
        )
        foreign_owner = TrainerFactory(club=other_club, first_name="Foreign", last_name="Owner")
        TrainerPackageAllocation.objects.create(
            club=other_club,
            subscription=subscription,
            student=student,
            tariff=tariff,
            training_type=tt,
            owner_trainer=foreign_owner,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=subscription.trainings_left,
            amount_snapshot=tariff.price,
        )

        response = client.get(
            "/billing/subscriptions/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == subscription.id
        assert item["package_owner_trainer_id"] is None
        assert item["package_owner_trainer_name"] == ""

    def test_trainer_cannot_list_unscoped_student_subscriptions(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        SubscriptionFactory(student=unassigned)

        response = client.get(
            f"/billing/subscriptions/?student_id={unassigned.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_with_actual_checkin_can_read_student_subscriptions(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student)
        schedule = ScheduleFactory(club=club, trainer=trainer, training_type=training_type)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=training_type,
        )

        list_response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        detail_response = client.get(
            f"/billing/subscriptions/{subscription.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 200
        assert {item["id"] for item in list_response.json()["items"]} == {subscription.id}
        assert detail_response.status_code == 200
        assert detail_response.json()["id"] == subscription.id

    def test_subscription_payload_exposes_pending_freeze_status(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        SubscriptionFreezeFactory(
            subscription=subscription,
            frozen_by=owner_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        list_response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(owner_user, club),
        )
        detail_response = client.get(
            f"/billing/subscriptions/{subscription.id}/",
            **_auth_params(owner_user, club),
        )

        assert list_response.status_code == 200
        assert list_response.json()["items"][0]["freeze_status"] == SubscriptionFreeze.FreezeStatus.PENDING
        assert detail_response.status_code == 200
        assert detail_response.json()["freeze_status"] == SubscriptionFreeze.FreezeStatus.PENDING

    def test_subscription_payload_exposes_usable_hybrid_personal_component(self, club, owner_user):
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=group_type)
        tariff_component = TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=personal_type,
            credits_total=1,
            paid_amount_basis=Decimal("1500.00"),
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            credits_total=1,
            credits_left=1,
        )

        response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == subscription.id
        assert item["has_components"] is True
        assert item["booking_entitlements"] == [
            {
                "training_type_id": personal_type.id,
                "training_type_name": personal_type.name,
                "training_type_kind": TrainingType.Kind.PERSONAL,
                "credits_left": 1,
                "weekly_limit": None,
                "weekly_used": None,
                "scope": Tariff.Scope.CLUB,
                "location_id": None,
            }
        ]

    def test_subscription_booking_date_annotates_weekly_component_usage(
        self,
        club,
        owner_user,
    ):
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=group_type)
        tariff_component = TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=personal_type,
            entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT,
            credits_total=None,
            weekly_limit=1,
            paid_amount_basis=Decimal("1500.00"),
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        component = SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            credits_total=None,
            credits_left=None,
            weekly_limit=1,
        )
        booking_date = timezone.localdate() + timedelta(days=7)
        CheckinFactory(
            club=club,
            student=student,
            subscription=subscription,
            subscription_component=component,
            date=booking_date,
        )

        response = client.get(
            f"/billing/subscriptions/?student_id={student.id}&booking_date={booking_date.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["booking_date"] == booking_date.isoformat()
        assert item["booking_entitlements"] == [
            {
                "training_type_id": personal_type.id,
                "training_type_name": personal_type.name,
                "training_type_kind": TrainingType.Kind.PERSONAL,
                "credits_left": None,
                "weekly_limit": 1,
                "weekly_used": 1,
                "scope": Tariff.Scope.CLUB,
                "location_id": None,
            }
        ]

    def test_subscription_payload_does_not_prefetch_foreign_club_component(
        self,
        club,
        other_club,
        owner_user,
    ):
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=group_type)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        foreign_type = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.PERSONAL)
        foreign_tariff = TariffFactory(club=other_club, training_type=foreign_type)
        foreign_component = TariffComponentFactory(
            club=other_club,
            tariff=foreign_tariff,
            training_type=foreign_type,
            credits_total=1,
            paid_amount_basis=Decimal("1500.00"),
        )
        SubscriptionComponentFactory(
            club=other_club,
            subscription=subscription,
            tariff_component=foreign_component,
            credits_total=1,
            credits_left=1,
        )

        response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == subscription.id
        assert item["has_components"] is False
        assert item["booking_entitlements"] == []

    def test_subscription_payload_marks_inactive_component_as_non_legacy(self, club, owner_user):
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=personal_type)
        tariff_component = TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=personal_type,
            credits_total=1,
            paid_amount_basis=Decimal("1500.00"),
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=8,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            credits_total=1,
            credits_left=1,
            is_active=False,
        )

        response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == subscription.id
        assert item["has_components"] is True
        assert item["booking_entitlements"] == []

    def test_trainer_package_owner_can_read_student_subscriptions(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        subscription = _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)

        list_response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        detail_response = client.get(
            f"/billing/subscriptions/{subscription.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 200
        assert {item["id"] for item in list_response.json()["items"]} == {subscription.id}
        assert detail_response.status_code == 200
        assert detail_response.json()["id"] == subscription.id

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_package_owner_without_current_subscription_cannot_read_student_subscriptions(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        subscription = _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )

        list_response = client.get(
            f"/billing/subscriptions/?student_id={student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        detail_response = client.get(
            f"/billing/subscriptions/{subscription.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 403
        assert detail_response.status_code == (404 if deleted else 403)

    def test_trainer_cannot_read_unscoped_subscription_detail(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type)
        unassigned = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=unassigned)

        response = client.get(
            f"/billing/subscriptions/{subscription.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestPaymentAPI:
    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_group_sbp_renewal_accepts_exact_frontend_target_and_source(
        self,
        mock_async,
        mock_schedule,
        settings,
        club,
        trainer_user,
    ):
        """The public staff SBP boundary keeps both the group receipt and source."""

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v2",
        )
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
            starts_on=today - timedelta(days=7),
        )
        source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        auth = _auth_params(trainer_user, club, role="trainer")
        payload = {
            "student_id": student.id,
            "tariff_id": tariff.id,
            "discount_ids": [],
            "debt_ids": [],
            "target_training_group_id": group.id,
            "target_schedule_id": schedule.id,
            "target_start_date": today.isoformat(),
            "renewed_from_subscription_id": source.id,
            "idempotency_key": "group-renewal-sbp-k1",
        }

        created = client.post("/billing/bank-payment-orders/", json=payload, **auth)
        assert created.status_code == 201, created.json()
        receipt = created.json()
        assert receipt["renewed_from_subscription_id"] == source.id
        assert receipt["target_training_group_id"] == group.id
        assert receipt["target_schedule_id"] == schedule.id
        assert receipt["group_membership_action_snapshot"] == "renewal"
        assert receipt["debt_ids"] == []

        membership.ends_on = today - timedelta(days=1)
        membership.save(update_fields=["ends_on", "updated_at"])
        replay = client.post("/billing/bank-payment-orders/", json=payload, **auth)
        assert replay.status_code == 200, replay.json()
        assert replay.json()["id"] == receipt["id"]
        assert replay.json()["command_replayed"] is True

        wrong_source = SubscriptionFactory(
            club=club,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        bad_source = client.post(
            "/billing/bank-payment-orders/",
            json={
                **payload,
                "renewed_from_subscription_id": wrong_source.id,
                "idempotency_key": "group-renewal-sbp-wrong-source",
            },
            **auth,
        )
        assert bad_source.status_code == 400
        assert bad_source.json()["code"] == "renewal_source_not_found"

        other_group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        bad_group = client.post(
            "/billing/bank-payment-orders/",
            json={
                **payload,
                "target_training_group_id": other_group.id,
                "idempotency_key": "group-renewal-sbp-wrong-group",
            },
            **auth,
        )
        assert bad_group.status_code == 400
        assert bad_group.json()["code"] == "target_training_group_mismatch"

        debt = DebtFactory(
            club=club,
            student=student,
            checkin=CheckinFactory(club=club, student=student),
        )
        debt_authority = client.post(
            "/billing/bank-payment-orders/",
            json={
                **payload,
                "debt_ids": [debt.id],
                "idempotency_key": "group-renewal-sbp-debt",
            },
            **auth,
        )
        assert debt_authority.status_code == 400
        assert debt_authority.json()["code"] == "renewal_client_terms_forbidden"
        assert Payment.objects.for_club(club).filter(command_idempotency_key="group-renewal-sbp-debt").count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @pytest.mark.parametrize("role", ["owner", "trainer"])
    @pytest.mark.parametrize("target_membership_state", ["missing", "ended"])
    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_v2_exact_group_renewal_rejects_nonrenewal_target_without_financial_artifacts(
        self,
        mock_async,
        mock_schedule,
        role,
        target_membership_state,
        settings,
        club,
        owner_user,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v2",
        )
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        target_group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        target_schedule = ScheduleFactory(
            club=club,
            training_group=target_group,
            trainer=trainer,
            location=target_group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        if target_membership_state == "missing":
            other_group = TrainingGroupFactory(
                club=club,
                training_type=training_type,
                responsible_trainer=trainer,
            )
            TrainingGroupMembershipFactory(
                club=club,
                student=student,
                training_group=other_group,
                starts_on=today,
            )
        else:
            TrainingGroupMembershipFactory(
                club=club,
                student=student,
                training_group=target_group,
                starts_on=today - timedelta(days=7),
                ends_on=today - timedelta(days=1),
            )

        response = client.post(
            "/billing/bank-payment-orders/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "discount_ids": [],
                "debt_ids": [],
                "target_training_group_id": target_group.id,
                "target_schedule_id": target_schedule.id,
                "target_start_date": today.isoformat(),
                "renewed_from_subscription_id": source.id,
                "idempotency_key": f"v2-nonrenewal-{role}-{target_membership_state}",
            },
            **(
                _auth_params(trainer_user, club, role="trainer")
                if role == "trainer"
                else _auth_params(owner_user, club)
            ),
        )

        assert response.status_code == 400, response.json()
        assert response.json()["code"] == "renewal_group_membership_required"
        assert Payment.objects.for_club(club).count() == 0
        assert Subscription.objects.for_club(club).count() == 1
        assert BankPaymentOrder.objects.for_club(club).count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_contextual_renewal_endpoints_reject_client_commercial_terms(
        self,
        mock_async,
        mock_schedule,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        student = StudentFactory(club=club)
        source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=CheckinFactory(club=club, student=student),
        )
        auth = _auth_params(owner_user, club)

        bank_rejected = client.post(
            "/billing/bank-payment-orders/",
            json={
                "student_id": student.id,
                "renewed_from_subscription_id": source.id,
                "idempotency_key": "staff-exact-renewal-bank",
                "tariff_id": tariff.id,
                "debt_ids": [debt.id],
                "target_schedule_id": 999999,
                "target_training_group_id": 999999,
                "target_start_date": "2026-08-14",
            },
            **auth,
        )
        assert bank_rejected.status_code == 400
        assert bank_rejected.json()["code"] == "renewal_client_terms_forbidden"

        bank_payload = {
            "student_id": student.id,
            "renewed_from_subscription_id": source.id,
            "idempotency_key": "staff-exact-renewal-bank",
        }
        bank_created = client.post("/billing/bank-payment-orders/", json=bank_payload, **auth)
        assert bank_created.status_code == 201, bank_created.json()
        assert bank_created.json()["tariff_id"] == tariff.id
        bank_replay = client.post("/billing/bank-payment-orders/", json=bank_payload, **auth)
        assert bank_replay.status_code == 200, bank_replay.json()
        assert bank_replay.json()["id"] == bank_created.json()["id"]
        assert bank_replay.json()["command_replayed"] is True

        manual_source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        manual_rejected = client.post(
            "/billing/payments/renewals/",
            json={
                "student_id": student.id,
                "renewed_from_subscription_id": manual_source.id,
                "payment_method": "cash",
                "idempotency_key": "staff-exact-renewal-manual",
                "tariff_id": tariff.id,
                "debt_ids": [debt.id],
                "amount": "1.00",
            },
            **auth,
        )
        assert manual_rejected.status_code == 422

        manual_payload = {
            "student_id": student.id,
            "renewed_from_subscription_id": manual_source.id,
            "payment_method": "cash",
            "idempotency_key": "staff-exact-renewal-manual",
        }
        manual_created = client.post("/billing/payments/renewals/", json=manual_payload, **auth)
        assert manual_created.status_code == 201, manual_created.json()
        assert manual_created.json()["tariff"]["id"] == tariff.id
        manual_replay = client.post("/billing/payments/renewals/", json=manual_payload, **auth)
        assert manual_replay.status_code == 200, manual_replay.json()
        assert manual_replay.json()["id"] == manual_created.json()["id"]
        assert manual_replay.json()["command_replayed"] is True

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_create_payment_endpoint(self, mock_async, mock_schedule, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert Decimal(str(data["amount"])) == Decimal("5000")
        assert data["status"] == "pending"
        assert data["payment_method"] == "cash"

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_unified_contextual_group_payment_rejects_a_missing_command_key_before_writes(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        settings,
    ):
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": 1,
                "tariff_id": 1,
                "payment_method": Payment.Method.CASH,
                "target_schedule_id": 1,
                "target_start_date": "2030-01-07",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "idempotency_key_required"
        assert Payment.objects.for_club(club).count() == 0
        assert Subscription.objects.for_club(club).count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_v1_contextual_group_payment_requires_client_upgrade_but_replays_after_v2_activation(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        settings,
    ):
        """A tenant flip cannot turn a legacy keyed replay into a new write."""

        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v1",
        )
        payload = {
            "student_id": student.id,
            "tariff_id": tariff.id,
            "payment_method": Payment.Method.CASH,
            "target_schedule_id": schedule.id,
            "target_start_date": "2030-01-07",
            "idempotency_key": "v1-contextual-group-replay-after-v2",
        }
        created = client.post("/billing/payments/", json=payload, **_auth_params(owner_user, club))
        assert created.status_code == 201, created.json()
        assert Payment.objects.for_club(club).count() == 1

        settings_row = ClubSettingsFactory._meta.model.objects.get(club=club)
        settings_row.commercial_journey_protocol_version = "v2"
        settings_row.save(update_fields=["commercial_journey_protocol_version", "updated_at"])

        replay = client.post("/billing/payments/", json=payload, **_auth_params(owner_user, club))
        assert replay.status_code == 200, replay.json()
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["command_replayed"] is True

        denied = client.post(
            "/billing/payments/",
            json=payload | {"idempotency_key": "v1-contextual-group-new-after-v2"},
            **_auth_params(owner_user, club),
        )
        assert denied.status_code == 400
        assert denied.json()["code"] == "client_upgrade_required"
        assert Payment.objects.for_club(club).count() == 1
        assert Subscription.objects.for_club(club).count() == 1
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_non_unified_v1_contextual_group_payment_retains_pending_admission_evidence(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        settings,
    ):
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=False,
            commercial_journey_protocol_version="v1",
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        target_date = timezone.localdate() + timedelta(days=7)
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=target_date.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": lead.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
                "target_training_group_id": group.id,
                "target_schedule_id": schedule.id,
                "target_start_date": target_date.isoformat(),
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201, response.json()
        payment = Payment.objects.for_club(club).get(id=response.json()["id"])
        lead.refresh_from_db()
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        assert LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
            actor=owner_user,
            metadata__payment_id=payment.id,
        ).count() == 1
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

        confirmation = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )

        assert confirmation.status_code == 200, confirmation.json()
        payment.refresh_from_db()
        lead.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.conversion_enrollment_id is not None
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_contextual_group_payment_without_durable_protocol_remains_generic_pending(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        settings,
    ):
        """A missing capability row must not opt a legacy request into v1 admission."""

        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        assert not ClubSettings.objects.filter(club=club).exists()
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        target_date = timezone.localdate() + timedelta(days=7)
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=target_date.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
        lead = LeadFactory(club=club, assigned_trainer=trainer)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": lead.id,
                "tariff_id": tariff.id,
                "payment_method": Payment.Method.CASH,
                "target_training_group_id": group.id,
                "target_schedule_id": schedule.id,
                "target_start_date": target_date.isoformat(),
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201, response.json()
        payment = Payment.objects.for_club(club).get(id=response.json()["id"])
        assert payment.status == Payment.Status.PENDING
        assert payment.conversion_enrollment_id is None
        assert payment.conversion_group_membership_id is None
        assert not LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
        ).exists()
        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.NEW
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_v1_contextual_group_bank_order_replays_after_v2_activation_but_rejects_new_key(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        settings,
    ):
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        settings_row = ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v1",
        )
        payload = {
            "student_id": student.id,
            "tariff_id": tariff.id,
            "target_schedule_id": schedule.id,
            "target_start_date": "2030-01-07",
            "idempotency_key": "v1-contextual-bank-k1",
        }

        created = client.post(
            "/billing/bank-payment-orders/",
            json=payload,
            **_auth_params(owner_user, club),
        )
        assert created.status_code == 201, created.json()

        settings_row.commercial_journey_protocol_version = "v2"
        settings_row.save(update_fields=["commercial_journey_protocol_version", "updated_at"])

        replay = client.post(
            "/billing/bank-payment-orders/",
            json=payload,
            **_auth_params(owner_user, club),
        )
        assert replay.status_code == 200, replay.json()
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["command_replayed"] is True

        denied = client.post(
            "/billing/bank-payment-orders/",
            json={**payload, "idempotency_key": "v1-contextual-bank-k2"},
            **_auth_params(owner_user, club),
        )
        assert denied.status_code == 400
        assert denied.json()["code"] == "client_upgrade_required"
        assert Payment.objects.for_club(club).count() == 1
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_personal_payment_requires_package_owner_trainer(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "package_owner_trainer_required"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_personal_payment_stores_package_owner_separately_from_seller(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        seller = TrainerFactory(club=club, first_name="Seller", last_name="Coach")
        package_owner = TrainerFactory(club=club, first_name="Owner", last_name="Coach")

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": seller.id,
                "package_owner_trainer_id": package_owner.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["training_type_kind"] == TrainingType.Kind.PERSONAL
        assert data["seller_trainer_id"] == seller.id
        assert data["seller_trainer_name"] == str(seller)
        assert data["package_owner_trainer_id"] == package_owner.id
        assert data["package_owner_trainer_name"] == str(package_owner)
        payment = Payment.objects.get(id=data["id"])
        assert payment.seller_trainer_id == seller.id
        assert payment.package_owner_trainer_id == package_owner.id
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_ignores_spoofed_seller_and_uses_current_trainer(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        spoofed_trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=trainer)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": spoofed_trainer.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        assert payment.status == Payment.Status.PENDING
        assert payment.recorded_by_id == trainer_user.id
        assert payment.seller_trainer_id == trainer.id

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_group_target_payment_derives_seller_from_target_schedule(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
        settings,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        recorder_trainer = TrainerFactory(club=club, user=trainer_user)
        target_trainer = TrainerFactory(club=club, first_name="Target", last_name="Coach")
        spoofed_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=recorder_trainer)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v1",
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": spoofed_trainer.id,
                "target_schedule_id": target_schedule.id,
                "target_start_date": "2030-01-07",
                "idempotency_key": "trainer-group-target-derived-seller",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["seller_trainer_id"] == target_trainer.id
        assert data["target_schedule_id"] == target_schedule.id
        assert data["target_trainer_id_snapshot"] == target_trainer.id
        assert data["sale_trainer_id_snapshot"] == target_trainer.id
        assert data["conversion_enrollment_id"] is not None
        payment = Payment.objects.get(id=data["id"])
        assert payment.recorded_by_id == trainer_user.id
        assert payment.seller_trainer_id == target_trainer.id
        assert payment.sale_attribution_source == "target_group_regular_trainer"
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_exact_group_key_replays_across_trainer_and_owner_without_seller_conflict(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
        trainer_user,
        settings,
    ):
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
            club=club,
            defaults={"mode": TrainingGroupRolloutState.Mode.ACTIVE},
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        trainer = TrainerFactory(club=club, user=trainer_user)
        spoofed_seller = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        target_date = timezone.localdate() + timedelta(days=7)
        target_schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=target_date.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=trainer)
        trainer_payload = {
            "student_id": student.id,
            "tariff_id": tariff.id,
            "payment_method": "cash",
            "idempotency_key": "exact-group-cross-actor-k1",
            "seller_trainer_id": spoofed_seller.id,
            "target_schedule_id": target_schedule.id,
            "target_training_group_id": group.id,
            "target_start_date": target_date.isoformat(),
        }
        created = client.post(
            "/billing/payments/",
            json=trainer_payload,
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert created.status_code == 201, created.json()
        assert created.json()["command_replayed"] is False
        assert created.json()["seller_trainer_id"] == trainer.id

        owner_payload = trainer_payload | {"seller_trainer_id": None}
        replay = client.post("/billing/payments/", json=owner_payload, **_auth_params(owner_user, club))
        assert replay.status_code == 200, replay.json()
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["command_replayed"] is True
        assert Payment.objects.for_club(club).filter(command_idempotency_key="exact-group-cross-actor-k1").count() == 1
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_manual_group_payment_disabled_leaves_trainer_sheet_request_without_mutations(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
        settings,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v1",
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "target_schedule_id": target_schedule.id,
                "target_start_date": "2030-01-07",
                "idempotency_key": "manual-group-payment-disabled",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "manual_operational_admission_disabled"
        assert not Payment.objects.for_club(club).filter(student=student).exists()
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_group_payment_requires_target_schedule_and_date(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "target_schedule_required"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_group_payment_cannot_turn_renewal_into_group_transfer(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        current_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        other_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, assigned_trainer=trainer)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=current_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "target_schedule_id": other_schedule.id,
                "target_start_date": today.isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_group_transfer_requires_management"
        assert Payment.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_group_projection_cannot_be_hidden_to_turn_renewal_into_transfer(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        current_schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        other_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
            starts_on=today,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=current_schedule,
            training_group_membership=membership,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "target_schedule_id": other_schedule.id,
                "target_start_date": today.isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_group_transfer_requires_management"
        assert Payment.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_personal_payment_ignores_spoofed_package_owner(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        spoofed_seller = TrainerFactory(club=club)
        spoofed_owner = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=trainer)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": spoofed_seller.id,
                "package_owner_trainer_id": spoofed_owner.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["seller_trainer_id"] == trainer.id
        assert data["package_owner_trainer_id"] == trainer.id
        payment = Payment.objects.get(id=data["id"])
        assert payment.seller_trainer_id == trainer.id
        assert payment.package_owner_trainer_id == trainer.id
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_package_owner_can_accept_payment_for_student(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
        settings,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        spoofed_seller = TrainerFactory(club=club)
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=False,
            commercial_journey_protocol_version="v1",
        )
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            lead_status=None,
        )
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
        target_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=0,
        )
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": spoofed_seller.id,
                "target_schedule_id": target_schedule.id,
                "target_start_date": "2030-01-07",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        assert payment.status == Payment.Status.PENDING
        assert payment.recorded_by_id == trainer_user.id
        assert payment.seller_trainer_id == trainer.id
        assert payment.target_schedule_id == target_schedule.id
        assert payment.conversion_enrollment_id is None
        mock_async.assert_called_once()
        mock_schedule.assert_not_called()

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_package_owner_without_current_subscription_cannot_accept_payment_for_student(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert Payment.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_rejects_unscoped_student_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_rejects_checkin_only_student_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer, training_type=tt)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=tt,
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_rejects_unassigned_lead_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        lead = StudentFactory(club=club, status=Student.Status.LEAD)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": lead.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_applies_one_active_same_club_discount(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("999.99"))
        student = StudentFactory(club=club, assigned_trainer=trainer)
        discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("12.34"))

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "discount_ids": [discount.id],
                "seller_trainer_id": trainer.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        assert payment.status == Payment.Status.PENDING
        assert payment.original_amount == Decimal("999.99")
        assert payment.amount == Decimal("876.59")
        assert list(payment.applied_discounts.values_list("id", flat=True)) == [discount.id]
        assert payment.seller_trainer_id == trainer.id
        assert mock_async.call_count == 1
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_rejects_multiple_discounts_without_creating_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=trainer)
        first_discount = DiscountFactory(club=club)
        second_discount = DiscountFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "transfer",
                "discount_ids": [first_discount.id, second_discount.id],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_multiple_discounts_not_allowed"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @pytest.mark.parametrize("discount_scenario", ["inactive", "foreign"])
    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_rejects_ineligible_single_discount_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        discount_scenario,
        club,
        other_club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club, assigned_trainer=trainer)
        discount = DiscountFactory(
            club=other_club if discount_scenario == "foreign" else club,
            is_active=discount_scenario != "inactive",
        )

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "discount_ids": [discount.id],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_discount_ids"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_payment_keeps_explicit_seller_trainer(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        seller = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "seller_trainer_id": seller.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        assert payment.seller_trainer_id == seller.id

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_payment_allows_discounts_with_explicit_seller_trainer(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        seller = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        discount = DiscountFactory(club=club, discount_type="fixed", value=Decimal("500"))

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "discount_ids": [discount.id],
                "seller_trainer_id": seller.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        assert payment.seller_trainer_id == seller.id
        assert payment.amount == Decimal("4500")
        assert list(payment.applied_discounts.values_list("id", flat=True)) == [discount.id]
        assert mock_async.call_count == 1
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_payment_rejects_invalid_discount_id_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "discount_ids": [999999],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_discount_ids"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_owner_payment_rejects_invalid_debt_id_without_financial_records(
        self,
        mock_async,
        mock_schedule,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "debt_ids": [999999],
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "debt_not_found"
        assert Payment.objects.count() == 0
        assert Subscription.objects.count() == 0
        mock_async.assert_not_called()
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_create_payment_no_amount_in_request(self, mock_async, mock_schedule, club, owner_user):
        """PaymentIn schema has no amount field -- extra amount field is ignored."""
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "amount": 1,  # This should be ignored
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        # Amount comes from tariff, not from input
        assert Decimal(str(response.json()["amount"])) == Decimal("5000")

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_trainer_payment_can_settle_selected_existing_debt_after_owner_confirm(
        self,
        mock_async,
        mock_schedule,
        club,
        trainer_user,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"), trainings_limit=8)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        response = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "payment_method": "cash",
                "debt_ids": [debt.id],
                "seller_trainer_id": trainer.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        payment = Payment.objects.get(id=response.json()["id"])
        debt.refresh_from_db()
        checkin.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert payment.seller_trainer_id == trainer.id
        assert debt.settlement_payment_id == payment.id
        assert debt.resolved_at is None
        assert checkin.subscription_id is None

        verify_response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )

        assert verify_response.status_code == 200
        debt.refresh_from_db()
        checkin.refresh_from_db()
        payment.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert debt.resolution_type == "payment"
        assert debt.resolved_at is not None
        assert debt.settlement_payment_id == payment.id
        assert checkin.subscription_id == payment.subscription_id
        assert checkin.is_debt is False
        mock_async.assert_any_call(
            "apps.attendance.tasks.calculate_salary",
            checkin.id,
            club_id=club.id,
        )
        mock_schedule.assert_not_called()

    def test_verify_payment_endpoint(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(tariff=tariff, student=student, subscription=subscription, recorded_by=owner_user)
        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "confirmed"

    def test_verify_payment_response_does_not_open_or_expose_account_access(self, club, owner_user):
        generated_password = "Generated-Access-Password-For-Test-123"
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="8 900 123 45 67",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        subscription = SubscriptionFactory(tariff=tariff, student=lead, status=Subscription.Status.PENDING)
        payment = PaymentFactory(tariff=tariff, student=lead, subscription=subscription, recorded_by=owner_user)

        with patch(
            "apps.students.access_services._generate_temporary_password",
            return_value=generated_password,
        ) as generate_password:
            response = client.post(
                f"/billing/payments/{payment.id}/verify/",
                json={"action": "confirm"},
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 200
        response_text = response.content.decode()
        assert "temporary_password" not in response_text
        assert "password" not in response_text
        assert generated_password not in response_text
        assert lead.user_id is None
        assert not Student.objects.filter(id=lead.id, user__isnull=False).exists()
        assert not generate_password.called

    def test_confirmed_personal_payment_exposes_seller_and_package_owner(self, club, owner_user):
        seller = TrainerFactory(club=club, first_name="Seller", last_name="Coach")
        package_owner = TrainerFactory(club=club, first_name="Owner", last_name="Coach")
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
            seller_trainer=seller,
            package_owner_trainer=package_owner,
        )

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["training_type_kind"] == TrainingType.Kind.PERSONAL
        assert data["seller_trainer_id"] == seller.id
        assert data["seller_trainer_name"] == str(seller)
        assert data["package_owner_trainer_id"] == package_owner.id
        assert data["package_owner_trainer_name"] == str(package_owner)

    def test_reject_payment_endpoint(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(tariff=tariff, student=student, subscription=subscription, recorded_by=owner_user)
        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "reject", "rejection_reason": "suspicious"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        payment.refresh_from_db()
        assert payment.rejection_reason == "suspicious"

    def test_reject_payment_requires_reason(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(tariff=tariff, student=student, subscription=subscription, recorded_by=owner_user)

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "reject", "rejection_reason": "   "},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "payment_rejection_reason_required"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert payment.rejection_reason == ""
        assert subscription.deleted_at is None

    def test_verify_payment_endpoint_rejects_online_payment_manual_confirm(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            recorded_by=owner_user,
        )

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "online_payment_manual_verification_forbidden"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert subscription.status == Subscription.Status.PENDING

    def test_verify_payment_rejects_invalid_action(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(tariff=tariff, student=student, subscription=subscription, recorded_by=owner_user)

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "maybe"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_payment_verify_action"
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PENDING

    def test_payment_requires_auth(self):
        response = client.get("/billing/payments/")
        assert response.status_code == 401

    def test_payment_tenant_isolation_api(self, club, owner_user):
        other_club = ClubFactory()
        tt_other = TrainingTypeFactory(club=other_club)
        tariff_other = TariffFactory(training_type=tt_other)
        student_other = StudentFactory(club=other_club)
        other_user = UserFactory()
        PaymentFactory(tariff=tariff_other, student=student_other, recorded_by=other_user)

        response = client.get(
            "/billing/payments/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_list_payments_filters_by_student_and_status(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        target_student = StudentFactory(club=club)
        other_student = StudentFactory(club=club)
        target_payment = PaymentFactory(
            tariff=tariff,
            student=target_student,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
        )
        PaymentFactory(
            tariff=tariff,
            student=target_student,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        PaymentFactory(
            tariff=tariff,
            student=other_student,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
        )

        response = client.get(
            f"/billing/payments/?student_id={target_student.id}&status={Payment.Status.CONFIRMED}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == target_payment.id

    def test_get_payment_detail_endpoint_returns_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
        )

        response = client.get(
            f"/billing/payments/{payment.id}/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == payment.id
        assert data["student_id"] == student.id
        assert data["status"] == Payment.Status.CONFIRMED


@pytest.mark.django_db
class TestDiscountAPI:
    def test_create_discount_endpoint(self, club, owner_user):
        response = client.post(
            "/billing/discounts/",
            json={"name": "Family", "discount_type": "percent", "value": "15.00"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Family"
        assert data["discount_type"] == "percent"

    def test_list_discounts_endpoint(self, club, owner_user):
        DiscountFactory(club=club)
        DiscountFactory(club=club, is_active=False)
        response = client.get(
            "/billing/discounts/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        # Only active discounts returned
        assert len(response.json()) == 1

    def test_update_discount_endpoint_updates_allowed_fields(self, club, owner_user):
        discount = DiscountFactory(
            club=club,
            name="Family",
            discount_type="percent",
            value=Decimal("10"),
            is_active=True,
        )

        response = client.put(
            f"/billing/discounts/{discount.id}/",
            json={"name": "Retention", "value": "20.00", "is_active": False},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Retention"
        assert Decimal(str(data["value"])) == Decimal("20.00")
        assert data["is_active"] is False
        discount.refresh_from_db()
        assert discount.name == "Retention"
        assert discount.value == Decimal("20.00")
        assert discount.is_active is False


@pytest.mark.django_db
class TestExpenseAPI:
    def test_expense_crud_api(self, club, owner_user):
        expense_date = timezone.localdate()

        create_response = client.post(
            "/billing/expenses/",
            json={
                "name": "Rent",
                "amount": "10000.00",
                "date": expense_date.isoformat(),
                "category": "facility",
                "is_recurring": True,
            },
            **_auth_params(owner_user, club),
        )

        assert create_response.status_code == 201
        expense_id = create_response.json()["id"]

        list_response = client.get(
            "/billing/expenses/",
            **_auth_params(owner_user, club),
        )
        assert list_response.status_code == 200
        assert list_response.json()["count"] == 1

        update_response = client.patch(
            f"/billing/expenses/{expense_id}/",
            json={
                "name": "Mat repair",
                "amount": "2500.00",
                "category": "equipment",
                "is_recurring": False,
            },
            **_auth_params(owner_user, club),
        )
        assert update_response.status_code == 200
        data = update_response.json()
        assert data["name"] == "Mat repair"
        assert Decimal(str(data["amount"])) == Decimal("2500.00")
        assert data["category"] == "equipment"
        assert data["is_recurring"] is False

        delete_response = client.delete(
            f"/billing/expenses/{expense_id}/",
            **_auth_params(owner_user, club),
        )
        assert delete_response.status_code == 204
        assert Expense.objects.get(id=expense_id).deleted_at is not None

        final_list_response = client.get(
            "/billing/expenses/",
            **_auth_params(owner_user, club),
        )
        assert final_list_response.status_code == 200
        assert final_list_response.json()["count"] == 0


@pytest.mark.django_db
class TestDebtAPI:
    def test_open_debts_expose_personal_drop_in_booking_and_required_tariff(self, club, owner_user):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("1750.00"))
        schedule = ScheduleFactory(club=club, training_type=training_type)
        target_date = timezone.localdate()
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        personal_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=target_date,
        )
        personal_debt = DebtFactory(
            club=club,
            student=student,
            checkin=personal_checkin,
            required_tariff=tariff,
            reason="personal_drop_in",
            tariff_price=tariff.price,
        )
        booking = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.ATTENDED,
            checkin=personal_checkin,
            debt=personal_debt,
            created_by=owner_user,
            idempotency_key="debt-api-personal-drop-in",
        )
        ordinary_checkin = CheckinFactory(club=club, student=student)
        ordinary_debt = DebtFactory(club=club, student=student, checkin=ordinary_checkin)

        response = client.get(
            f"/billing/debts/?student_id={student.id}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        debts = {item["id"]: item for item in response.json()["items"]}
        assert debts[personal_debt.id]["booking_id"] == booking.id
        assert debts[personal_debt.id]["required_tariff_id"] == tariff.id
        assert debts[ordinary_debt.id]["booking_id"] is None
        assert debts[ordinary_debt.id]["required_tariff_id"] is None

    def test_owner_can_list_all_open_debts(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        response = client.get(
            "/billing/debts/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == debt.id

    def test_trainer_can_list_open_debts_for_scoped_student(self, club, trainer_user):
        from django.utils import timezone

        from apps.attendance.tests.factories import CheckinFactory

        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        other_student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student, is_debt=True)
        other_checkin = CheckinFactory(club=club, student=other_student, is_debt=True)
        resolved_checkin = CheckinFactory(club=club, student=student, is_debt=False)
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=Decimal("1200"),
        )
        DebtFactory(club=club, student=other_student, checkin=other_checkin)
        DebtFactory(
            club=club,
            student=student,
            checkin=resolved_checkin,
            resolved_at=timezone.now(),
            resolution_type="payment",
        )

        response = client.get(
            f"/billing/debts/?student_id={student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == debt.id
        assert Decimal(str(data["items"][0]["tariff_price"])) == Decimal("1200")

    def test_trainer_package_owner_can_list_open_debts_for_student(self, club, trainer_user):
        from apps.attendance.tests.factories import CheckinFactory

        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        other_student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        checkin = CheckinFactory(club=club, student=student, is_debt=True)
        other_checkin = CheckinFactory(club=club, student=other_student, is_debt=True)
        debt = DebtFactory(club=club, student=student, checkin=checkin, tariff_price=Decimal("900"))
        DebtFactory(club=club, student=other_student, checkin=other_checkin)

        response = client.get(
            f"/billing/debts/?student_id={student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == debt.id

    def test_trainer_cannot_list_open_debts_for_unscoped_student(self, club, trainer_user):
        from apps.attendance.tests.factories import CheckinFactory

        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=unassigned, is_debt=True)
        DebtFactory(club=club, student=unassigned, checkin=checkin)

        response = client.get(
            f"/billing/debts/?student_id={unassigned.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_cannot_list_all_debts_broadly(self, club, trainer_user):
        response = client.get(
            "/billing/debts/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_debt_list_is_club_scoped(self, club, other_club, trainer_user):
        from apps.attendance.tests.factories import CheckinFactory

        TrainerFactory(club=club, user=trainer_user)
        other_student = StudentFactory(club=other_club)
        other_checkin = CheckinFactory(club=other_club, student=other_student, is_debt=True)
        DebtFactory(club=other_club, student=other_student, checkin=other_checkin)

        response = client.get(
            f"/billing/debts/?student_id={other_student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_write_off_debt_endpoint_records_actor_and_reason(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        response = client.post(
            f"/billing/debts/{debt.id}/write-off/",
            json={"reason": "Admin adjustment"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["resolution_type"] == "writeoff"
        assert data["resolved_at"] is not None
        event = DebtWriteOffEvent.objects.for_club(club.id).get(debt=debt)
        assert event.written_off_by_id == owner_user.id
        assert event.reason == "Admin adjustment"

    def test_write_off_debt_endpoint_requires_reason(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        response = client.post(
            f"/billing/debts/{debt.id}/write-off/",
            json={"reason": "   "},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        debt.refresh_from_db()
        assert debt.resolved_at is None
        assert DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).count() == 0


@pytest.mark.django_db
class TestBillingTenantIsolation:
    def test_billing_tenant_isolation_api(self, club, owner_user):
        other_club = ClubFactory()
        tt_other = TrainingTypeFactory(club=other_club)
        TariffFactory(training_type=tt_other)

        response = client.get(
            "/billing/tariffs/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["count"] == 0


@pytest.mark.django_db
class TestFreezeAPI:
    def test_freeze_subscription_endpoint(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 10, "reason": "vacation"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["days"] == 10
        assert data["reason"] == "vacation"
        assert data["ends_at"] is None

    def test_trainer_cannot_freeze_unscoped_student_subscription(self, club, trainer_user):
        ClubSettingsFactory(club=club)
        TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        unassigned = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=unassigned, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 10, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert SubscriptionFreeze.objects.filter(subscription=sub).count() == 0

    def test_trainer_package_owner_can_request_freeze_for_student_subscription(self, club, trainer_user):
        ClubSettingsFactory(club=club)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        subscription = _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)

        response = client.post(
            f"/billing/subscriptions/{subscription.id}/freeze/",
            json={"days": 10, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert response.json()["status"] == "pending"
        assert SubscriptionFreeze.objects.filter(subscription=subscription).count() == 1

    def test_trainer_cannot_freeze_checkin_only_student_subscription(self, club, trainer_user):
        ClubSettingsFactory(club=club)
        trainer = TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, trainer=trainer, training_type=tt)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=tt,
        )

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 10, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert SubscriptionFreeze.objects.filter(subscription=sub).count() == 0

    def test_freeze_rejects_invalid_reason_api(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 10, "reason": "not-a-valid-reason"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert SubscriptionFreeze.objects.filter(subscription=sub).count() == 0

    def test_unfreeze_endpoint(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.FROZEN)
        freeze = SubscriptionFreezeFactory(subscription=sub, days=10, frozen_by=owner_user)

        response = client.post(
            f"/billing/freezes/{freeze.id}/unfreeze/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ends_at"] is not None

    def test_trainer_cannot_unfreeze_subscription(self, club, owner_user, trainer_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.FROZEN)
        freeze = SubscriptionFreezeFactory(subscription=sub, days=10, frozen_by=owner_user)

        response = client.post(
            f"/billing/freezes/{freeze.id}/unfreeze/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        freeze.refresh_from_db()
        assert freeze.ends_at is None

    def test_freeze_disabled_returns_400(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_enabled=False)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 10, "reason": "vacation"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 400

    def test_freeze_rejects_zero_days_api(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 0, "reason": "vacation"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400

    def test_freeze_tenant_isolation_api(self, club, owner_user):
        other_club = ClubFactory()
        ClubSettingsFactory(club=other_club)
        tt = TrainingTypeFactory(club=other_club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=other_club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 5, "reason": "vacation"},
            **_auth_params(owner_user, club),
        )
        # Should fail -- subscription belongs to other club
        assert response.status_code in (400, 404)

    def test_list_subscription_freezes(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student)
        SubscriptionFreezeFactory(subscription=sub, frozen_by=owner_user)

        response = client.get(
            f"/billing/subscriptions/{sub.id}/freezes/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_trainer_cannot_list_unscoped_subscription_freezes(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        unassigned = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=unassigned)

        response = client.get(
            f"/billing/subscriptions/{sub.id}/freezes/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestDebtorsAPI:
    def test_debtors_list_endpoint(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin)

        response = client.get(
            "/billing/debtors/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_debtors_export_endpoint(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin, tariff_price=5000)

        response = client.get(
            "/billing/debtors/export/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.django_db
class TestClubSettingsAPI:
    def test_club_settings_endpoint(self, club, owner_user):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        response = client.get(
            "/billing/settings/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["freeze_enabled"] is True
        assert data["freeze_max_days"] == 30
        assert data["freeze_max_count"] is None
        assert data["timezone"] == "Asia/Yekaterinburg"

    def test_update_club_settings(self, club, owner_user):
        response = client.put(
            "/billing/settings/",
            json={"freeze_enabled": False, "freeze_max_days": 15},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["freeze_enabled"] is False
        assert data["freeze_max_days"] == 15


@pytest.mark.django_db
class TestFreezeApprovalWorkflow:
    """D-16/D-17: Trainer initiates freeze, owner approves."""

    def test_trainer_freeze_creates_pending(self, club, trainer_user):
        """Trainer-initiated freeze should have status=pending, not immediately applied."""
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201
        assert response.json()["status"] == "pending"

    def test_owner_freeze_creates_approved(self, club, owner_user):
        """Owner-initiated freeze should be approved immediately."""
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        response = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        assert response.json()["status"] == "approved"
        assert response.json()["approved_by_id"] == owner_user.id
        assert response.json()["decision_at"] is not None
        assert response.json()["decision_reason"] == ""
        freeze = SubscriptionFreeze.objects.get(id=response.json()["id"])
        assert freeze.approved_by_id == owner_user.id
        assert freeze.rejected_by_id is None
        assert freeze.decision_at is not None
        assert freeze.decision_reason == ""

    def test_owner_can_approve_freeze(self, club, owner_user, trainer_user):
        """Owner can approve a pending freeze."""
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        # Trainer creates pending freeze
        create_resp = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert create_resp.status_code == 201
        freeze_id = create_resp.json()["id"]
        # Owner approves
        response = client.patch(
            f"/billing/freezes/{freeze_id}/approve/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        freeze = SubscriptionFreeze.objects.get(id=freeze_id)
        assert freeze.approved_by_id == owner_user.id
        assert freeze.decision_at is not None

    def test_owner_can_reject_freeze(self, club, owner_user, trainer_user):
        """Owner can reject a pending freeze."""
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        # Trainer creates pending freeze
        create_resp = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert create_resp.status_code == 201
        freeze_id = create_resp.json()["id"]
        # Owner rejects
        response = client.patch(
            f"/billing/freezes/{freeze_id}/reject/",
            json={"decision_reason": "Просьба без подтверждения"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        freeze = SubscriptionFreeze.objects.get(id=freeze_id)
        assert freeze.rejected_by_id == owner_user.id
        assert freeze.decision_at is not None
        assert freeze.decision_reason == "Просьба без подтверждения"

    def test_owner_can_reject_freeze_without_reason(self, club, owner_user, trainer_user):
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        create_resp = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert create_resp.status_code == 201
        freeze_id = create_resp.json()["id"]

        response = client.patch(
            f"/billing/freezes/{freeze_id}/reject/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        freeze = SubscriptionFreeze.objects.get(id=freeze_id)
        assert freeze.rejected_by_id == owner_user.id
        assert freeze.decision_reason == ""

    def test_trainer_cannot_approve_or_reject_freeze(self, club, owner_user, trainer_user):
        from apps.clubs.tests.factories import ClubSettingsFactory

        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        sub = SubscriptionFactory(tariff=tariff, student=student, status="active")
        create_resp = client.post(
            f"/billing/subscriptions/{sub.id}/freeze/",
            json={"days": 7, "reason": "vacation"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert create_resp.status_code == 201
        freeze_id = create_resp.json()["id"]

        approve_response = client.patch(
            f"/billing/freezes/{freeze_id}/approve/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        reject_response = client.patch(
            f"/billing/freezes/{freeze_id}/reject/",
            json={"decision_reason": "no"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert approve_response.status_code == 403
        assert reject_response.status_code == 403
        assert SubscriptionFreeze.objects.get(id=freeze_id).status == SubscriptionFreeze.FreezeStatus.PENDING


@pytest.mark.django_db
class TestGroupEnrollmentOptions:
    def test_trainer_sees_safe_compatible_groups_and_latest_trial_first(
        self,
        club,
        trainer_user,
    ):
        recorder = TrainerFactory(club=club, user=trainer_user)
        target_trainer = TrainerFactory(club=club, first_name="Target", last_name="Coach")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        trial_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
            group_name="Trial Group",
        )
        ScheduleFactory(
            club=club,
            trainer=recorder,
            training_type=training_type,
            day_of_week=today.weekday(),
            group_name="Another Group",
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=recorder)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=trial_schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=today,
            ends_on=today,
            trial_at=timezone.now(),
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["schedule_id"] == trial_schedule.id
        assert data[0]["is_latest_trial_group"] is True
        assert data[0]["trainer_name"] == "Target Coach"
        assert data[0]["next_occurrence_date"] == today.isoformat()
        assert data[0]["occurrence_dates"][0] == today.isoformat()
        assert "students" not in data[0]
        assert "phone" not in data[0]

    def test_mapped_slots_return_one_canonical_group_card(self, settings, club, trainer_user):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        recorder = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=recorder,
            name="Canonical evening group",
        )
        first_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=recorder,
            training_type=training_type,
            day_of_week=today.weekday(),
            group_name="Stale slot label",
        )
        second_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
            start_time=(timezone.localtime().replace(hour=19, minute=0, second=0, microsecond=0)).time(),
            group_name="Other stale slot label",
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=recorder)
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE
        )

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert len(response.json()) == 1
        card = response.json()[0]
        assert card["training_group_id"] == group.id
        assert card["is_canonical_group_card"] is True
        assert card["group_name"] == group.name
        assert card["responsible_trainer_id"] == recorder.id
        assert card["slot_schedule_ids"] == [first_slot.id, second_slot.id]
        assert card["group_membership_action"] == "new_admission"
        assert card["renewed_from_subscription_id"] is None
        assert {item["schedule_id"] for item in card["upcoming_occurrences"]} == {
            first_slot.id,
            second_slot.id,
        }

    def test_canonical_group_renewal_card_exposes_only_one_exact_source(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        responsible = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible,
        )
        ScheduleFactory(
            club=club,
            training_group=group,
            trainer=responsible,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=responsible)
        TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
            starts_on=today,
        )
        source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        auth = _auth_params(trainer_user, club, role="trainer")

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **auth,
        )

        assert response.status_code == 200
        card = response.json()[0]
        assert card["group_membership_action"] == "renewal"
        assert card["renewed_from_subscription_id"] == source.id

        # A duplicate live legacy source is deliberately not client-resolved.
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        ambiguous = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **auth,
        )
        assert ambiguous.status_code == 200
        ambiguous_card = ambiguous.json()[0]
        assert ambiguous_card["group_membership_action"] == "renewal"
        assert ambiguous_card["renewed_from_subscription_id"] is None

    def test_group_renewal_option_does_not_disclose_foreign_student_source(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=other_trainer,
        )
        ScheduleFactory(
            club=club,
            training_group=group,
            trainer=other_trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        foreign_student = StudentFactory(club=club, assigned_trainer=other_trainer)
        TrainingGroupMembershipFactory(
            club=club,
            student=foreign_student,
            training_group=group,
            starts_on=today,
        )
        SubscriptionFactory(
            club=club,
            student=foreign_student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={foreign_student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_canonical_group_occurrences_keep_reschedule_and_substitute_details(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        responsible = TrainerFactory(club=club, user=trainer_user)
        regular_trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(
            club=club,
            first_name="Substitute",
            last_name="Coach",
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible,
        )
        rescheduled_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=regular_trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
            start_time=time(10, 0),
        )
        substitute_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=regular_trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
            start_time=time(18, 0),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=rescheduled_slot,
            date=today,
            exception_type="rescheduled",
            new_date=today + timedelta(days=1),
            new_start_time=time(15, 30),
            new_end_time=time(16, 45),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=substitute_slot,
            date=today,
            exception_type="substitute",
            substitute_trainer=substitute,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=responsible)
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE
        )

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        card = response.json()[0]
        occurrences = card["upcoming_occurrences"]
        moved = next(
            item for item in occurrences
            if item["schedule_id"] == rescheduled_slot.id
            and item["date"] == (today + timedelta(days=1)).isoformat()
        )
        substituted = next(
            item for item in occurrences
            if item["schedule_id"] == substitute_slot.id
            and item["date"] == today.isoformat()
        )
        assert moved["start_time"] == "15:30:00"
        assert moved["end_time"] == "16:45:00"
        assert moved["is_rescheduled"] is True
        assert substituted["trainer_id"] == substitute.id
        assert substituted["trainer_name"] == "Substitute Coach"
        assert substituted["is_substitute"] is True

    def test_shadow_rollout_keeps_mapped_slots_compatible_and_schedule_shaped(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        recorder = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=recorder,
        )
        first_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=recorder,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        second_slot = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=recorder)
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        options = response.json()
        assert [option["schedule_id"] for option in options] == [first_slot.id, second_slot.id]
        assert all(option["is_canonical_group_card"] is False for option in options)
        assert all(option["training_group_id"] is None for option in options)

    def test_shadow_schedule_option_keeps_each_effective_occurrence(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        responsible = TrainerFactory(club=club, user=trainer_user)
        regular_trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(
            club=club,
            first_name="Shadow",
            last_name="Substitute",
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=regular_trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        substitute_date = today + timedelta(days=7)
        moved_from = today + timedelta(days=14)
        moved_to = moved_from + timedelta(days=1)
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=substitute_date,
            exception_type="substitute",
            substitute_trainer=substitute,
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=moved_from,
            exception_type="rescheduled",
            new_date=moved_to,
            new_start_time=time(15, 30),
            new_end_time=time(16, 45),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=responsible)
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        option = response.json()[0]
        assert option["is_canonical_group_card"] is False
        occurrences = option["upcoming_occurrences"]
        substituted = next(item for item in occurrences if item["date"] == substitute_date.isoformat())
        moved = next(item for item in occurrences if item["date"] == moved_to.isoformat())
        assert substituted["trainer_id"] == substitute.id
        assert substituted["trainer_name"] == "Shadow Substitute"
        assert substituted["is_substitute"] is True
        assert moved["start_time"] == "15:30:00"
        assert moved["end_time"] == "16:45:00"
        assert moved["is_rescheduled"] is True

    @pytest.mark.parametrize(
        ("rollout_mode", "new_writes_enabled"),
        [
            (TrainingGroupRolloutState.Mode.SHADOW, False),
            (TrainingGroupRolloutState.Mode.ACTIVE, False),
            (TrainingGroupRolloutState.Mode.RECONCILING, True),
            (TrainingGroupRolloutState.Mode.CONTAINMENT, True),
        ],
    )
    def test_disabled_rollout_modes_hide_new_group_enrollment_options(
        self,
        settings,
        club,
        trainer_user,
        rollout_mode,
        new_writes_enabled,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=timezone.localdate().weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=rollout_mode
        )
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = new_writes_enabled

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_missing_rollout_state_hides_new_group_enrollment_options(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=timezone.localdate().weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        TrainingGroupRolloutState.objects.for_club(club).delete()

        response = client.get(
            f"/billing/group-enrollment-options/?student_id={student.id}&tariff_id={tariff.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_trainer_options_lock_renewal_to_existing_permanent_group(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        existing_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
            group_name="Current Group",
        )
        ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
            group_name="Other Group",
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=existing_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today + timedelta(days=7),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [item["schedule_id"] for item in response.json()] == [existing_schedule.id]

    def test_trainer_options_treat_group_projection_as_existing_permanent_group(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        existing_schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
            starts_on=today,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=existing_schedule,
            training_group_membership=membership,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [item["schedule_id"] for item in response.json()] == [
            existing_schedule.id
        ]

    def test_group_options_use_exception_aware_occurrence_dates(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=today,
            exception_type="cancelled",
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, assigned_trainer=trainer)

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        option = response.json()[0]
        assert today.isoformat() not in option["occurrence_dates"]
        assert option["next_occurrence_date"] == (today + timedelta(days=7)).isoformat()

    def test_trainer_options_reject_terminal_student(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(
            club=club,
            status=Student.Status.LOST,
            lead_status=None,
            assigned_trainer=trainer,
        )

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_group_student_not_eligible"

    def test_trainer_options_do_not_cross_tenant_student_scope(
        self,
        club,
        other_club,
        trainer_user,
    ):
        TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        foreign_student = StudentFactory(club=other_club)

        response = client.get(
            (
                "/billing/group-enrollment-options/"
                f"?student_id={foreign_student.id}&tariff_id={tariff.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
