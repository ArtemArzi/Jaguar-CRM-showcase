import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client as DjangoClient
from django.urls import resolve
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupRolloutEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.services import (
    close_personal_booking_payment_reservation_for_order,
    create_personal_booking_payment_reservation,
    create_personal_drop_in_bank_payment_order,
)
from apps.attendance.services.training_groups import transition_training_group_rollout
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupRolloutStateFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    BankPaymentProviderEvent,
    BankPaymentReconciliationAttempt,
    Debt,
    DebtSettlementEvent,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    PaymentReturnState,
    Subscription,
    SubscriptionRenewalEvent,
    TrainingType,
)
from apps.billing.payment_providers.base import ProviderLinkResult, ProviderOperationInfo, ProviderRetailerInfo
from apps.billing.service_modules.bank_orders import (
    apply_provider_creation_result,
    mark_provider_creation_unknown,
    recover_unknown_bank_payment_order,
)
from apps.billing.service_modules.payment_readiness import record_authenticated_retailer_readback
from apps.billing.service_modules.payment_returns import _hash, build_return_url
from apps.billing.services import (
    cancel_bank_payment_order,
    create_bank_payment_order,
    expire_bank_payment_orders,
    process_bank_payment_webhook,
    replay_deferred_bank_payment_provider_events,
    resolve_bank_payment_order_manual_review,
)
from apps.billing.tests.factories import (
    DebtFactory,
    DiscountFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.fixture(autouse=True)
def _enable_training_group_new_writes_for_existing_canonical_order_scenarios(settings):
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True


def _webhook_body(order: BankPaymentOrder, *, status: str, **overrides) -> bytes:
    payload = {
        "webhookType": "acquiringInternetPayment",
        "event_id": f"evt-{order.id}-{status.lower()}",
        "status": status,
        "paymentLinkId": order.provider_payment_link_id,
        "operationId": overrides.pop("operation_id", f"op-{order.id}"),
        "amount": str(order.amount_snapshot),
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _tariff_for_club(club, **kwargs):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    return TariffFactory(training_type=training_type, **kwargs)


def _open_debt_for_tariff(*, club, student, tariff):
    checkin = CheckinFactory(
        club=club,
        student=student,
        training_type=tariff.training_type,
        subscription=None,
        is_debt=True,
    )
    return DebtFactory(club=club, student=student, checkin=checkin)


@pytest.mark.django_db
class TestBankPaymentOrders:
    def _tochka_lost_creation_order(self, *, club, owner_user):
        """Create the pending financial roots using mock, then model a lost live reply."""
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        order.provider = BankPaymentOrder.Provider.TOCHKA
        order.provider_operation_id = ""
        order.provider_payment_url = ""
        order.provider_customer_code = "test-customer"
        order.provider_merchant_id = "test-merchant"
        order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
        order.link_creation_dispatched_at = timezone.now()
        order.save(
            update_fields=[
                "provider",
                "provider_operation_id",
                "provider_payment_url",
                "provider_customer_code",
                "provider_merchant_id",
                "link_creation_state",
                "link_creation_dispatched_at",
                "updated_at",
            ]
        )
        return order

    def test_timeout_marker_does_not_regress_concurrent_authenticated_approval(
        self,
        club,
        owner_user,
    ):
        stale_order = self._tochka_lost_creation_order(club=club, owner_user=owner_user)
        BankPaymentOrder.objects.filter(id=stale_order.id).update(
            status=BankPaymentOrder.Status.APPROVED,
            provider_operation_id="approved-operation",
            link_creation_state=BankPaymentOrder.LinkCreationState.DISPATCHED,
            last_error_code="",
            last_error_message="",
        )
        Payment.objects.filter(id=stale_order.payment_id).update(status=Payment.Status.CONFIRMED)
        Subscription.objects.filter(id=stale_order.subscription_id).update(status=Subscription.Status.ACTIVE)

        mark_provider_creation_unknown(stale_order, error_code="tochka_timeout")

        stale_order.refresh_from_db()
        assert stale_order.status == BankPaymentOrder.Status.APPROVED
        assert stale_order.provider_operation_id == "approved-operation"
        assert stale_order.link_creation_state == BankPaymentOrder.LinkCreationState.DISPATCHED
        assert stale_order.last_error_code == ""

    def test_personal_tochka_expiry_cap_under_two_minutes_fails_before_financial_writes(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        provider = Mock()
        before = (
            Payment.objects.count(),
            Subscription.objects.count(),
            BankPaymentOrder.objects.count(),
        )

        with (
            patch("apps.billing.payment_providers.base.online_payments_enabled", return_value=True),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
                expires_at_cap=timezone.now() + timedelta(seconds=119),
            )

        assert exc_info.value.code == "bank_payment_order_ttl_too_short"
        assert (
            exc_info.value.message
            == "До окончания записи недостаточно времени для безопасной ссылки СБП"
        )
        assert (
            Payment.objects.count(),
            Subscription.objects.count(),
            BankPaymentOrder.objects.count(),
        ) == before
        provider.create_payment_link.assert_not_called()

    def test_tochka_creation_rechecks_kill_switch_immediately_before_dispatch(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        provider = Mock()

        with (
            patch(
                "apps.billing.payment_providers.base.online_payments_enabled",
                side_effect=[True, False],
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "tochka_payment_creation_not_ready"
        assert exc_info.value.message == "Онлайн-оплата Точки отключена перед отправкой"
        provider.create_payment_link.assert_not_called()
        order = BankPaymentOrder.objects.for_club(club).get(student=student)
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.FAILED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None

    def test_lost_creation_recovery_claim_blocks_concurrent_worker_and_stale_claim_is_reclaimed(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
        order = self._tochka_lost_creation_order(club=club, owner_user=owner_user)
        order.creation_recovery_claim_token = "active-claim"
        order.creation_recovery_claimed_at = timezone.now()
        order.save(update_fields=["creation_recovery_claim_token", "creation_recovery_claimed_at", "updated_at"])

        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider") as get_provider,
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "in_progress"
        get_provider.assert_not_called()

        order.creation_recovery_claimed_at = timezone.now() - timedelta(minutes=3)
        order.save(update_fields=["creation_recovery_claimed_at", "updated_at"])
        provider = Mock()
        provider.find_payment_operation_by_link.return_value = None
        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "unknown"

        order.refresh_from_db()
        assert order.link_creation_state == BankPaymentOrder.LinkCreationState.UNKNOWN
        assert order.creation_recovery_claim_token == ""
        assert order.creation_recovery_claimed_at is None
        provider.find_payment_operation_by_link.assert_called_once()

    def test_lost_creation_recovery_persists_list_match_then_uses_existing_reconciler(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
        order = self._tochka_lost_creation_order(club=club, owner_user=owner_user)
        provider = Mock()
        provider.find_payment_operation_by_link.return_value = ProviderOperationInfo(
            status="PENDING",
            operation_id="recovered-operation",
            payment_link_id=order.provider_payment_link_id,
            payment_url="https://payment.tochka.example/recovered",
            amount=order.amount_snapshot,
            customer_code="test-customer",
            merchant_id="test-merchant",
            payment_modes=["sbp"],
        )
        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
            patch(
                "apps.billing.service_modules.provider_events.request_provider_reconciliation"
            ) as request_reconciliation,
            patch(
                "apps.billing.service_modules.provider_events.reconcile_provider_payment_order",
                return_value="pending",
            ) as reconcile,
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "pending"

        order.refresh_from_db()
        assert order.provider_operation_id == "recovered-operation"
        assert order.provider_payment_url == "https://payment.tochka.example/recovered"
        assert order.link_creation_state == BankPaymentOrder.LinkCreationState.DISPATCHED
        request_reconciliation.assert_called_once_with(club_id=club.id, order_id=order.id, provider_event_id=None)
        reconcile.assert_called_once_with(club_id=club.id, order_id=order.id)

    def test_lost_creation_recovery_expires_only_after_two_distinct_post_cutoff_absence_scans(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
        order = self._tochka_lost_creation_order(club=club, owner_user=owner_user)
        order.expires_at = timezone.now() - timedelta(hours=25)
        order.save(update_fields=["expires_at", "updated_at"])
        provider = Mock()
        provider.find_payment_operation_by_link.return_value = None

        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "unknown"

        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.PENDING
        assert order.creation_absence_count == 1
        order.creation_last_absence_at = timezone.now() - timedelta(seconds=61)
        order.save(update_fields=["creation_last_absence_at", "updated_at"])

        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "expired"

        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.EXPIRED
        assert order.link_creation_state == BankPaymentOrder.LinkCreationState.UNKNOWN
        assert order.creation_absence_count == 2
        assert order.payment.status == Payment.Status.REJECTED

    def test_lost_creation_recovery_rejects_same_link_with_mismatched_financial_evidence(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
        order = self._tochka_lost_creation_order(club=club, owner_user=owner_user)
        provider = Mock()
        provider.find_payment_operation_by_link.return_value = ProviderOperationInfo(
            status="EXPIRED",
            operation_id="mismatched-operation",
            payment_link_id=order.provider_payment_link_id,
            payment_url="https://payment.tochka.example/mismatched",
            amount=order.amount_snapshot + Decimal("1.00"),
            customer_code="test-customer",
            merchant_id="test-merchant",
            payment_modes=["sbp"],
        )

        with (
            patch(
                "apps.billing.service_modules.payment_readiness.get_online_payment_capability",
                return_value=SimpleNamespace(reconciliation_available=True),
            ),
            patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        ):
            assert recover_unknown_bank_payment_order(club_id=club.id, order_id=order.id) == "manual_review"

        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.link_creation_state == BankPaymentOrder.LinkCreationState.UNKNOWN
        assert order.last_error_code == "bank_payment_amount_mismatch"
        assert order.payment.status == Payment.Status.PENDING

    def test_global_creation_switch_fails_before_provider_selection_or_financial_writes(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)

        with (
            patch("apps.billing.payment_providers.get_payment_provider") as mock_get_provider,
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "online_payment_order_creation_disabled"
        assert exc_info.value.message == "Создание онлайн-оплаты временно отключено"
        mock_get_provider.assert_not_called()
        assert not BankPaymentOrder.objects.for_club(club).exists()
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()

    def test_containment_approves_preexisting_canonical_order_and_blocks_new_intent(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_start_date = timezone.localdate() + timedelta(days=7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        responsible_trainer = TrainerFactory(club=club)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible_trainer,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=responsible_trainer,
            training_type=training_type,
            location=group.location,
            day_of_week=target_start_date.weekday(),
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        assert rollout.mode == TrainingGroupRolloutState.Mode.OFF
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=owner_user.id,
            rationale="Enter reconciliation before a containment approval proof.",
            idempotency_key="containment-preexisting-off-to-reconciling",
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.SHADOW,
            actor_id=owner_user.id,
            rationale="A clean forward audit permits the shadow admission proof.",
            idempotency_key="containment-preexisting-reconciling-to-shadow",
            forward_audit_passed=True,
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.ACTIVE,
            actor_id=owner_user.id,
            rationale="Activate before creating the pre-existing canonical order.",
            idempotency_key="containment-preexisting-shadow-to-active",
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
        )

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
            actor_id=owner_user.id,
            rationale="Contain new intent while allowing existing approval proof.",
            idempotency_key="containment-preexisting-enter",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=StudentFactory(club=club).id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
                target_schedule_id=schedule.id,
                target_training_group_id=group.id,
                target_start_date=target_start_date,
            )
        assert exc_info.value.code == "training_group_writes_disabled"

        approved_body = _webhook_body(
            order,
            status="APPROVED",
            paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
        )
        first_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=approved_body,
            headers={},
            request_id="containment-preexisting-approved",
        )
        duplicate_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=approved_body,
            headers={},
            request_id="containment-preexisting-approved-duplicate",
        )
        order.refresh_from_db()
        order.payment.refresh_from_db()

        assert first_event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert duplicate_event.id == first_event.id
        assert duplicate_event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert BankPaymentProviderEvent.objects.for_club(club).filter(order=order).count() == 1
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.payment.status == Payment.Status.CONFIRMED
        memberships = TrainingGroupMembership.objects.for_club(club).filter(
            student=student,
            training_group=group,
        )
        assert memberships.count() == 1
        membership = memberships.get(id=order.payment.conversion_group_membership_id)
        assert membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
        projections = ScheduleEnrollment.objects.for_club(club).filter(
            training_group_membership=membership,
        )
        assert projections.count() == 1
        assert set(projections.values_list("schedule_id", flat=True)) == {schedule.id}
        assert set(projections.values_list("created_from", flat=True)) == {
            ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        }
        assert BankPaymentOrder.objects.for_club(club).filter(student=student).count() == 1
        assert Payment.objects.for_club(club).filter(student=student).count() == 1
        assert Subscription.objects.for_club(club).filter(student=student).count() == 1
        assert TrainingGroupRolloutEvent.objects.for_club(club).filter(
            idempotency_key__in={
                "containment-preexisting-off-to-reconciling",
                "containment-preexisting-reconciling-to-shadow",
                "containment-preexisting-shadow-to-active",
                "containment-preexisting-enter",
            }
        ).count() == 4

    def test_signed_tochka_webhook_verification_round_trip(self, settings):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from apps.billing.payment_providers.tochka import TochkaPaymentProvider

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        settings.TOCHKA_WEBHOOK_PUBLIC_KEY = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        token = jwt.encode(
            {
                "webhookType": "acquiringInternetPayment",
                "status": "APPROVED",
                "paymentLinkId": "test-link",
                "operationId": "test-operation",
                "transactionId": "test-transaction",
                "amount": "5000.00",
            },
            private_key,
            algorithm="RS256",
        )

        webhook = TochkaPaymentProvider().verify_webhook(
            request_body=token.encode(),
            headers={},
        )

        assert webhook.payment_link_id == "test-link"
        assert webhook.event_id == "test-transaction"
        assert webhook.amount == Decimal("5000.00")

    @patch("django_q.tasks.async_task")
    def test_create_order_creates_online_payment_and_no_receipt_mock_link(self, mock_async, settings, club, owner_user):
        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, price=Decimal("5500"))

        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        assert order.status == BankPaymentOrder.Status.PENDING
        assert order.provider == BankPaymentOrder.Provider.MOCK
        payment_url = urlsplit(order.provider_payment_url)
        assert payment_url.scheme == "https"
        assert payment_url.netloc == "pay.example.test"
        assert payment_url.path == f"/mock-payments/{order.provider_payment_link_id}"
        return_url = parse_qs(payment_url.query)["return_url"][0]
        assert return_url.startswith(f"{settings.JAGUAR_PAYMENT_RETURN_ORIGIN}/payments/return?state=")
        return_state = parse_qs(urlsplit(return_url).query)["state"][0]
        assert PaymentReturnState.objects.filter(
            order=order,
            state_hash=_hash(return_state),
            expires_at__gt=timezone.now(),
        ).exists()
        assert resolve(payment_url.path).func.__name__ == "debug_mock_payment_view"
        checkout = DjangoClient().get(payment_url.path, {"return_url": return_url})
        assert checkout.status_code == 200
        assert b"mock-payload" in checkout.content
        assert checkout["Cache-Control"] == "no-store"
        assert len(order.provider_payment_link_id) <= 45
        assert order.receipt_mode == BankPaymentOrder.ReceiptMode.NONE
        assert order.receipt_status == BankPaymentOrder.ReceiptStatus.NOT_REQUIRED
        assert order.payment.payment_method == Payment.Method.ONLINE
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert order.amount_snapshot == Decimal("5500.00")
        assert order.provider_payment_modes == ["sbp"]
        assert mock_async.call_count == 0
        settings.DEBUG = False
        assert DjangoClient().get(payment_url.path, {"return_url": return_url}).status_code == 404

    def test_non_debug_mock_creation_fails_before_financial_writes(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.DEBUG = False
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        before = (
            Payment.objects.count(),
            Subscription.objects.count(),
            BankPaymentOrder.objects.count(),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "mock_payment_order_creation_disabled"
        assert (
            exc_info.value.message
            == "Mock-провайдер не может создавать платёжные ссылки в этом окружении"
        )
        assert (
            Payment.objects.count(),
            Subscription.objects.count(),
            BankPaymentOrder.objects.count(),
        ) == before

    def test_stale_mock_link_is_not_actionable_outside_local_mock_runtime(
        self,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.schemas import BankPaymentOrderOut

        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=StudentFactory(club=club).id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        order.payment_action_mode = "staff"
        assert BankPaymentOrderOut.resolve_can_share(order) is True

        settings.DEBUG = False
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = False

        assert BankPaymentOrderOut.resolve_can_share(order) is False
        assert BankPaymentOrderOut.resolve_can_copy(order) is False
        assert BankPaymentOrderOut.resolve_can_show_qr(order) is False

    def test_loopback_return_origin_is_available_only_to_explicit_local_mock(self, settings):
        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "http://127.0.0.1:4174"

        assert build_return_url("local-state") == (
            "http://127.0.0.1:4174/payments/return?state=local-state"
        )

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        with pytest.raises(BusinessLogicError) as provider_error:
            build_return_url("local-state")
        assert provider_error.value.code == "payment_return_origin_invalid"

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.DEBUG = False
        with pytest.raises(BusinessLogicError) as debug_error:
            build_return_url("local-state")
        assert debug_error.value.code == "payment_return_origin_invalid"

        settings.DEBUG = True
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "http://0.0.0.0:4174"
        with pytest.raises(BusinessLogicError) as host_error:
            build_return_url("local-state")
        assert host_error.value.code == "payment_return_origin_invalid"

    @patch("apps.billing.payment_providers.tochka.TochkaPaymentProvider._post_json")
    @patch("django_q.tasks.async_task")
    def test_create_order_uses_tochka_data_wrapped_payment_request(
        self,
        mock_async,
        mock_post_json,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.TOCHKA_JWT_TOKEN = "test-jwt-token"
        settings.TOCHKA_CUSTOMER_CODE = "customer-123"
        settings.TOCHKA_MERCHANT_ID = "merchant-456"
        settings.TOCHKA_API_BASE_URL = "https://enter.tochka.com/uapi"
        settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"
        settings.JAGUAR_PAYMENT_RETURN_ORIGIN = settings.JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN
        record_authenticated_retailer_readback(
            retailer_info=ProviderRetailerInfo(
                status="REG",
                is_active=True,
                merchant_id="merchant-456",
                payment_modes=["sbp"],
                checked_at=timezone.now(),
            )
        )
        mock_post_json.return_value = {
            "Data": {
                "paymentLink": "https://pay.tochka.test/link-1",
                "operationId": "operation-1",
                "status": "CREATED",
            }
        }
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, price=Decimal("5500"))

        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        assert order.status == BankPaymentOrder.Status.PENDING
        assert order.provider == BankPaymentOrder.Provider.TOCHKA
        assert order.provider_payment_url == "https://pay.tochka.test/link-1"
        assert order.provider_operation_id == "operation-1"
        assert order.provider_status == "CREATED"
        assert order.provider_customer_code == "customer-123"
        assert order.provider_merchant_id == "merchant-456"

        mock_post_json.assert_called_once()
        call_kwargs = mock_post_json.call_args.kwargs
        assert call_kwargs["path"] == "/acquiring/v1.0/payments"
        assert call_kwargs["token"] == "test-jwt-token"
        assert set(call_kwargs["payload"].keys()) == {"Data"}
        payload = call_kwargs["payload"]["Data"]
        assert payload["customerCode"] == "customer-123"
        assert payload["merchantId"] == "merchant-456"
        assert payload["amount"] == "5500.00"
        assert payload["paymentMode"] == ["sbp"]
        assert payload["paymentLinkId"] == order.provider_payment_link_id
        assert order.provider_payment_modes == ["sbp"]
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        "payment_modes",
        [
            [],
            ["card"],
            ["card", "sbp"],
            ["sbp", "sbp"],
            ["SBP"],
            {"mode": "sbp"},
        ],
    )
    @patch("apps.billing.payment_providers.tochka.TochkaPaymentProvider._post_json")
    def test_tochka_adapter_rejects_noncanonical_payment_modes_before_network(
        self,
        mock_post_json,
        payment_modes,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.payment_providers.tochka import TochkaPaymentProvider

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=StudentFactory(club=club).id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        settings.TOCHKA_JWT_TOKEN = "test-jwt-token"
        settings.TOCHKA_CUSTOMER_CODE = "customer-123"
        order.provider_payment_modes = payment_modes

        with pytest.raises(BusinessLogicError) as exc_info:
            TochkaPaymentProvider().create_payment_link(order=order)

        assert exc_info.value.code == "sbp_only_payment_mode_required"
        mock_post_json.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_ambiguous_tochka_create_error_keeps_financial_family_pending_for_recovery(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
        settings.TOCHKA_CUSTOMER_CODE = "customer-test"
        settings.TOCHKA_MERCHANT_ID = "merchant-test"
        provider = Mock()
        provider.create_payment_link.side_effect = TimeoutError("provider reply lost")
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)

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

        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.CREATED
        assert order.link_creation_state == BankPaymentOrder.LinkCreationState.UNKNOWN
        assert order.last_error_code == "provider_creation_unknown"
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert mock_async.call_count == 0


    @patch("django_q.tasks.async_task")
    def test_duplicate_pending_request_reuses_existing_order(self, mock_async, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)

        first_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        second_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        assert second_order.id == first_order.id
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert Payment.objects.for_club(club).count() == 1
        assert Subscription.objects.for_club(club).filter(student=student, tariff=tariff).count() == 1
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        ("status", "payment_modes"),
        [
            (BankPaymentOrder.Status.CREATED, ["card", "sbp"]),
            (BankPaymentOrder.Status.PENDING, []),
            (BankPaymentOrder.Status.AUTHORIZED, ["SBP"]),
            (BankPaymentOrder.Status.MANUAL_REVIEW, ["card"]),
        ],
    )
    def test_noncanonical_live_order_blocks_new_family_when_reuse_is_disabled(
        self,
        status,
        payment_modes,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        BankPaymentOrder.objects.for_club(club).filter(id=order.id).update(
            status=status,
            provider_payment_modes=payment_modes,
        )
        counts_before = {
            "orders": BankPaymentOrder.objects.for_club(club).filter(student=student).count(),
            "payments": Payment.objects.for_club(club).filter(student=student).count(),
            "subscriptions": Subscription.objects.for_club(club).filter(student=student).count(),
        }

        with (
            patch(
                "apps.billing.payment_providers.mock.MockPaymentProvider.create_payment_link"
            ) as mock_create_link,
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
                allow_reuse=False,
            )

        assert exc_info.value.code == "bank_payment_order_legacy_payment_mode"
        assert (
            exc_info.value.message
            == "Активная ссылка использует недоступный способ оплаты. Отмените её и создайте новую"
        )
        mock_create_link.assert_not_called()
        assert (
            BankPaymentOrder.objects.for_club(club).filter(student=student).count()
            == counts_before["orders"]
        )
        assert Payment.objects.for_club(club).filter(student=student).count() == counts_before["payments"]
        assert (
            Subscription.objects.for_club(club).filter(student=student).count()
            == counts_before["subscriptions"]
        )

    @patch("django_q.tasks.async_task")
    def test_compatible_pending_request_reuses_canonical_order_across_sources(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )

        trainer_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        student_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=owner_user.id,
        )

        assert student_order.id == trainer_order.id
        assert student_order.payment_id == trainer_order.payment_id
        assert student_order.subscription_id == trainer_order.subscription_id
        assert student_order.payment_intent_key == trainer_order.payment_intent_key
        # Creator/source remains an immutable snapshot; reuse does not rewrite
        # the staff-created order into a student-created financial family.
        assert student_order.source == BankPaymentOrder.Source.TRAINER
        assert trainer_order.source == BankPaymentOrder.Source.TRAINER
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert Payment.objects.for_club(club).count() == 1
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_trainer_cannot_receive_a_private_self_service_link_while_it_blocks_duplicates(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        SubscriptionFactory(club=club, student=student, tariff=tariff, status=Subscription.Status.ACTIVE)
        private_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "bank_payment_order_private_intent_exists"
        assert exc_info.value.message == "У ученика уже есть самостоятельная ссылка на эту оплату"
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert BankPaymentOrder.objects.for_club(club).get().id == private_order.id
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_changed_amount_or_purpose_never_reuses_an_old_bearer_link(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, name="Original", price=Decimal("5000.00"))
        original = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        tariff.name = "Changed"
        tariff.price = Decimal("6000.00")
        tariff.save(update_fields=["name", "price", "updated_at"])

        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
            )

        assert exc_info.value.code == "bank_payment_order_pending_exists"
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert BankPaymentOrder.objects.for_club(club).get().id == original.id
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_expired_manual_review_intent_still_blocks_a_replacement(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.MANUAL_REVIEW
        order.expires_at = timezone.now() - timedelta(days=1)
        order.save(update_fields=["status", "expires_at", "updated_at"])

        replay = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )

        assert replay.id == order.id
        assert replay.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_late_create_response_cannot_regress_an_authenticated_terminal_state(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=StudentFactory(club=club).id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        original_url = order.provider_payment_url
        BankPaymentOrder.objects.for_club(club).filter(id=order.id).update(
            status=BankPaymentOrder.Status.APPROVED,
            paid_at=timezone.now(),
        )
        Payment.objects.for_club(club).filter(id=order.payment_id).update(status=Payment.Status.CONFIRMED)
        Subscription.objects.for_club(club).filter(id=order.subscription_id).update(
            status=Subscription.Status.ACTIVE
        )

        result = apply_provider_creation_result(
            club_id=club.id,
            order_id=order.id,
            link=ProviderLinkResult(
                payment_url="https://pay.example.test/late",
                payment_link_id=order.provider_payment_link_id,
                provider_status="CREATED",
                operation_id="late-operation",
                payment_modes=["sbp"],
            ),
            provider_payment_modes=["sbp"],
        )

        assert result.status == BankPaymentOrder.Status.APPROVED
        assert result.provider_payment_url == original_url
        assert result.provider_operation_id != "late-operation"
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_pending_order_with_different_debt_selection_blocks_second_order(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        first_debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)
        second_debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)

        create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            debt_ids=[first_debt.id],
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
                debt_ids=[second_debt.id],
            )

        assert exc_info.value.code == "bank_payment_order_pending_exists"
        assert exc_info.value.message == "У ученика уже есть активная ссылка на оплату с другим составом"
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_validated_webhook_is_deferred_during_group_reconciliation(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        TrainingGroupRolloutStateFactory(club=club)
        transition_training_group_rollout(
            club_id=club.id,
            target_mode="reconciling",
            actor_id=owner_user.id,
            rationale="defer validated provider event",
            idempotency_key="s5-provider-event-defer",
        )
        paid_at = timezone.now() - timedelta(minutes=1)

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-reconciling-deferred",
        )

        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
        assert event.normalized_status_snapshot == "approved"
        assert event.provider_paid_at_snapshot == paid_at
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_authenticated_duplicate_redelivery_replays_deferred_event_after_exit(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        TrainingGroupRolloutStateFactory(club=club)
        transition_training_group_rollout(
            club_id=club.id,
            target_mode="reconciling",
            actor_id=owner_user.id,
            rationale="defer until an authenticated redelivery",
            idempotency_key="deferred-redelivery-enter",
        )
        body = _webhook_body(
            order,
            status="APPROVED",
            paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
        )
        deferred = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id="deferred-redelivery-first",
        )
        assert deferred.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
        transition_training_group_rollout(
            club_id=club.id,
            target_mode="off",
            actor_id=owner_user.id,
            rationale="leave reconciliation before provider redelivery",
            idempotency_key="deferred-redelivery-exit",
        )

        replayed = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id="deferred-redelivery-second",
        )

        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert replayed.id == deferred.id
        assert replayed.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.payment.status == Payment.Status.CONFIRMED
        assert order.subscription.status == Subscription.Status.ACTIVE
        assert BankPaymentProviderEvent.objects.for_club(club).filter(order=order).count() == 1
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_owner_recovery_replays_deferred_approval_after_reconciliation_exit(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        TrainingGroupRolloutStateFactory(club=club)
        transition_training_group_rollout(
            club_id=club.id,
            target_mode="reconciling",
            actor_id=owner_user.id,
            rationale="defer then replay",
            idempotency_key="s5-provider-event-replay-enter",
        )
        paid_at = timezone.now() - timedelta(minutes=1)
        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-reconciling-replay",
        )
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED

        transition_training_group_rollout(
            club_id=club.id,
            target_mode="off",
            actor_id=owner_user.id,
            rationale="reconciliation completed",
            idempotency_key="s5-provider-event-replay-exit",
        )
        replay_deferred_bank_payment_provider_events(club_id=club.id)

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.payment.status == Payment.Status.CONFIRMED
        assert order.subscription.status == Subscription.Status.ACTIVE

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S5 suite",
    )
    @patch("django_q.tasks.async_task")
    def test_postgresql_concurrent_deferred_group_replay_has_one_owned_family(
        self,
        _mock_async,
        settings,
        club,
        owner_user,
    ):
        from apps.attendance.models import TrainingGroupRolloutState
        from apps.attendance.tests.factories import TrainingGroupFactory
        from apps.trainers.tests.factories import TrainerFactory

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        responsible_trainer = TrainerFactory(club=club)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible_trainer,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=responsible_trainer,
            training_type=training_type,
            day_of_week=timezone.localdate().weekday(),
        )
        rollout = TrainingGroupRolloutStateFactory(club=club)
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=timezone.localdate(),
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.RECONCILING
        )
        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(
                order,
                status="APPROVED",
                paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
            ),
            headers={},
            request_id="s5-pg-race-defer",
        )
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )

        gate = Barrier(2)

        def replay_once():
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return replay_deferred_bank_payment_provider_events(club_id=club.id)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: replay_once(), range(2)))

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert any(outcome["processed"] == 1 for outcome in outcomes)
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.payment.status == Payment.Status.CONFIRMED
        assert order.payment.conversion_group_membership_id is not None
        assert order.payment.conversion_enrollment_id is not None
        assert ScheduleEnrollment.objects.for_club(club).filter(
            training_group_membership_id=order.payment.conversion_group_membership_id
        ).count() == 1

    @patch("django_q.tasks.async_task")
    def test_approved_webhook_confirms_payment_with_provider_paid_at_idempotently(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        paid_at = timezone.now() - timedelta(days=1)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        body = _webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat())
        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id="req-approved",
        )
        duplicate = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id="req-approved-duplicate",
        )

        assert duplicate.id == event.id
        assert BankPaymentProviderEvent.objects.count() == 1
        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.paid_at == paid_at
        assert order.payment.status == Payment.Status.CONFIRMED
        assert order.payment.verified_at == paid_at
        assert order.subscription.status == Subscription.Status.ACTIVE
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_authorized_webhook_does_not_confirm_payment(self, mock_async, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="AUTHORIZED"),
            headers={},
            request_id="req-authorized",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.status == BankPaymentOrder.Status.AUTHORIZED
        assert order.payment.status == Payment.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_amount_mismatch_moves_order_to_manual_review(self, mock_async, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", amount="1.00"),
            headers={},
            request_id="req-mismatch",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
        assert event.failure_code == "bank_payment_amount_mismatch"
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_personal_reservation_amount_mismatch_enters_review_and_reject_releases_slot(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        starts_at = timezone.now() + timedelta(days=14)
        ends_at = starts_at + timedelta(hours=1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
            idempotency_key="personal-payment-review-reject",
        )
        order = reservation.bank_payment_order

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", amount="1.00"),
            headers={},
            request_id="req-personal-amount-mismatch",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        reservation.refresh_from_db()
        slot.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
        assert event.failure_code == "bank_payment_amount_mismatch"
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert reservation.status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
        assert reservation.last_error_code == "bank_payment_amount_mismatch"
        assert slot.status == PersonalAvailabilitySlot.Status.HELD

        resolved = resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=BankPaymentOrderReviewEvent.Resolution.REJECT,
            reason="Bank amount does not match",
        )

        resolved.payment.refresh_from_db()
        resolved.subscription.refresh_from_db()
        reservation.refresh_from_db()
        slot.refresh_from_db()
        assert resolved.status == BankPaymentOrder.Status.FAILED
        assert resolved.payment.status == Payment.Status.REJECTED
        assert resolved.subscription.deleted_at is not None
        assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
        assert reservation.last_error_message == "Bank amount does not match"
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot.booked_enrollment_id is None
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_payment_link_mismatch_moves_order_to_manual_review(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.provider_operation_id = f"op-known-{order.id}"
        order.save(update_fields=["provider_operation_id", "updated_at"])

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(
                order,
                status="APPROVED",
                paymentLinkId="different-link",
                operation_id=order.provider_operation_id,
                paid_at=timezone.now().isoformat(),
            ),
            headers={},
            request_id="req-link-mismatch",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
        assert event.failure_code == "bank_payment_link_mismatch"
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_approved_order_without_paid_at_defers_to_provider_reconciliation(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.expires_at = timezone.now() - timedelta(minutes=1)
        order.save(update_fields=["expires_at", "updated_at"])

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED"),
            headers={},
            request_id="req-late",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
        assert event.failure_code == "bank_payment_reconciliation_pending"
        assert order.status != BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        attempt = BankPaymentReconciliationAttempt.objects.for_club(club).get(order=order)
        assert attempt.provider_event_id == event.id
        assert attempt.status == BankPaymentReconciliationAttempt.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_tochka_manual_review_confirm_paid_requires_provider_reconciliation(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        BankPaymentOrder.objects.for_club(club).filter(id=order.id).update(
            provider=BankPaymentOrder.Provider.TOCHKA,
            status=BankPaymentOrder.Status.MANUAL_REVIEW,
        )
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW

        with pytest.raises(BusinessLogicError) as exc_info:
            resolve_bank_payment_order_manual_review(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
                resolution=BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
                reason="Bank statement checked",
                evidence={"provider_event_id": "evt-safe", "raw_payload": "must-not-store"},
            )

        assert exc_info.value.code == "tochka_manual_confirm_denied"
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert BankPaymentOrderReviewEvent.objects.for_club(club).filter(order=order).count() == 0
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        "resolution",
        [
            BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
            BankPaymentOrderReviewEvent.Resolution.REJECT,
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
            BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
        ],
    )
    def test_manual_review_requires_reason_before_mutation(
        self,
        settings,
        club,
        owner_user,
        resolution,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.MANUAL_REVIEW
        order.save(update_fields=["status", "updated_at"])

        with pytest.raises(BusinessLogicError) as exc_info:
            resolve_bank_payment_order_manual_review(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
                resolution=resolution,
                reason=" ",
                evidence={"refund_amount": "1.00"},
            )

        assert exc_info.value.code == "bank_payment_review_reason_required"
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert BankPaymentOrderReviewEvent.objects.for_club(club).filter(order=order).count() == 0

    @patch("django_q.tasks.async_task")
    def test_manual_review_confirm_paid_books_personal_reservation_already_in_manual_review(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        starts_at = timezone.now() + timedelta(days=14)
        ends_at = starts_at + timedelta(hours=1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
            idempotency_key="manual-review-personal-confirm",
        )
        reviewed = close_personal_booking_payment_reservation_for_order(
            club_id=club.id,
            order_id=reservation.bank_payment_order_id,
            status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            reason="booking_failed_once",
        )
        order = reviewed.bank_payment_order
        order.status = BankPaymentOrder.Status.MANUAL_REVIEW
        order.last_error_code = "personal_payment_reservation_manual_review"
        order.last_error_message = "booking_failed_once"
        order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])

        resolved = resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
            reason="Bank statement checked",
        )

        reviewed.refresh_from_db()
        slot.refresh_from_db()
        resolved.payment.refresh_from_db()
        resolved.subscription.refresh_from_db()
        assert resolved.status == BankPaymentOrder.Status.APPROVED
        assert resolved.payment.status == Payment.Status.CONFIRMED
        assert resolved.subscription.status == Subscription.Status.ACTIVE
        assert reviewed.status == PersonalBookingPaymentReservation.Status.BOOKED
        assert reviewed.schedule_id is not None
        assert reviewed.enrollment_id is not None
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == reviewed.enrollment_id
        event = BankPaymentOrderReviewEvent.objects.for_club(club).get(order=resolved)
        assert event.resolution == BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_manual_review_reject_releases_pending_artifacts_and_records_review_event(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            debt_ids=[debt.id],
        )
        BankPaymentOrder.objects.filter(id=order.id).update(status=BankPaymentOrder.Status.MANUAL_REVIEW)

        resolved = resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=BankPaymentOrderReviewEvent.Resolution.REJECT,
            reason="Provider support rejected operation",
            evidence={"payment_link_id": order.provider_payment_link_id, "authorization": "must-not-store"},
        )

        resolved.payment.refresh_from_db()
        resolved.subscription.refresh_from_db()
        debt.refresh_from_db()
        assert resolved.status == BankPaymentOrder.Status.FAILED
        assert resolved.payment.status == Payment.Status.REJECTED
        assert resolved.subscription.deleted_at is not None
        assert debt.settlement_payment_id is None
        event = BankPaymentOrderReviewEvent.objects.for_club(club).get(order=resolved)
        assert event.resolution == BankPaymentOrderReviewEvent.Resolution.REJECT
        assert event.new_payment_status == Payment.Status.REJECTED
        assert event.evidence_metadata == {"payment_link_id": order.provider_payment_link_id}
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_failed_webhook_rejects_pending_artifacts_and_releases_reserved_debt(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            debt_ids=[debt.id],
        )

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="FAILED"),
            headers={},
            request_id="req-failed",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        debt.refresh_from_db()
        events = list(
            DebtSettlementEvent.objects.for_club(club.id)
            .filter(payment=order.payment, debt=debt)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        assert order.status == BankPaymentOrder.Status.FAILED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None
        assert debt.settlement_payment_id is None
        assert events == [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.REJECTED,
        ]
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_expire_bank_payment_orders_rejects_pending_artifacts_and_releases_reserved_debt(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            debt_ids=[debt.id],
        )
        now = timezone.now()
        order.expires_at = now - timedelta(minutes=1)
        order.save(update_fields=["expires_at", "updated_at"])

        expired = expire_bank_payment_orders(now=now)

        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        debt.refresh_from_db()
        events = list(
            DebtSettlementEvent.objects.for_club(club.id)
            .filter(payment=order.payment, debt=debt)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        assert expired == 1
        assert order.status == BankPaymentOrder.Status.EXPIRED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None
        assert debt.settlement_payment_id is None
        assert events == [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.REJECTED,
        ]
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_expire_bank_payment_orders_rejects_created_and_authorized_artifacts(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        created_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        authorized_order = create_bank_payment_order(
            club_id=club.id,
            student_id=StudentFactory(club=club).id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        created_order.status = BankPaymentOrder.Status.CREATED
        created_order.provider_payment_url = ""
        created_order.expires_at = timezone.now() - timedelta(minutes=1)
        created_order.save(update_fields=["status", "provider_payment_url", "expires_at", "updated_at"])
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(authorized_order, status="AUTHORIZED"),
            headers={},
            request_id="req-authorized-expiry-setup",
        )
        authorized_order.expires_at = timezone.now() - timedelta(minutes=1)
        authorized_order.save(update_fields=["expires_at", "updated_at"])

        expired = expire_bank_payment_orders(now=timezone.now())

        created_order.refresh_from_db()
        authorized_order.refresh_from_db()
        created_order.payment.refresh_from_db()
        authorized_order.payment.refresh_from_db()
        created_order.subscription.refresh_from_db()
        authorized_order.subscription.refresh_from_db()
        assert expired == 2
        assert created_order.status == BankPaymentOrder.Status.EXPIRED
        assert authorized_order.status == BankPaymentOrder.Status.EXPIRED
        assert created_order.payment.status == Payment.Status.REJECTED
        assert authorized_order.payment.status == Payment.Status.REJECTED
        assert created_order.subscription.deleted_at is not None
        assert authorized_order.subscription.deleted_at is not None
        assert mock_async.call_count == 0

    @patch(
        "apps.billing.payment_providers.mock.MockPaymentProvider.create_payment_link",
        side_effect=BusinessLogicError("provider rejected", code="provider_rejected"),
    )
    @patch("django_q.tasks.async_task")
    def test_provider_create_failure_rejects_pending_artifacts_and_releases_reserved_debt(
        self,
        mock_async,
        mock_create_link,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        debt = _open_debt_for_tariff(club=club, student=student, tariff=tariff)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
                debt_ids=[debt.id],
            )

        order = BankPaymentOrder.objects.for_club(club).get()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        debt.refresh_from_db()
        assert exc_info.value.code == "provider_rejected"
        assert order.status == BankPaymentOrder.Status.FAILED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None
        assert debt.settlement_payment_id is None
        assert mock_create_link.call_count == 1
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        "payment_modes",
        [
            [],
            ["card"],
            ["sbp", "sbp"],
            ["SBP"],
        ],
    )
    @patch("django_q.tasks.async_task")
    def test_noncanonical_provider_result_fails_order_without_overwriting_sbp_snapshot(
        self,
        mock_async,
        payment_modes,
        settings,
        club,
        owner_user,
    ):
        from apps.billing.payment_providers.base import ProviderLinkResult

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        unsafe_result = ProviderLinkResult(
            payment_url="https://unsafe.example.test/card-link",
            payment_link_id="unsafe-link",
            provider_status="CREATED",
            payment_modes=payment_modes,
        )

        with (
            patch(
                "apps.billing.payment_providers.mock.MockPaymentProvider.create_payment_link",
                return_value=unsafe_result,
            ) as mock_create_link,
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
            )

        order = BankPaymentOrder.objects.for_club(club).get(student=student)
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert exc_info.value.code == "sbp_only_payment_mode_required"
        assert order.status == BankPaymentOrder.Status.FAILED
        assert order.provider_payment_modes == ["sbp"]
        assert order.provider_payment_url == ""
        assert order.provider_operation_id == ""
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None
        mock_create_link.assert_called_once()
        assert mock_async.call_count == 0

    @patch(
        "apps.billing.payment_providers.mock.MockPaymentProvider.create_payment_link",
        side_effect=RuntimeError("network exploded"),
    )
    @patch("django_q.tasks.async_task")
    def test_provider_unexpected_failure_rejects_pending_artifacts(
        self,
        mock_async,
        mock_create_link,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)

        with pytest.raises(RuntimeError):
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                source=BankPaymentOrder.Source.TRAINER,
                created_by_id=owner_user.id,
            )

        order = BankPaymentOrder.objects.for_club(club).get()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.FAILED
        assert order.last_error_code == "provider_unexpected_error"
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None
        assert mock_create_link.call_count == 1
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_created_order_is_reused_while_link_creation_is_in_progress(self, mock_async, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.CREATED
        order.provider_payment_url = ""
        order.save(update_fields=["status", "provider_payment_url", "updated_at"])

        replay = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        assert replay.id == order.id

        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_same_tariff_renewal_adds_old_remaining_days_and_trainings(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8, duration_days=30)
        paid_at = timezone.now()
        old_expires_at = paid_at + timedelta(days=10)
        old_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=3,
            expires_at=old_expires_at,
        )

        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        assert order.renewed_from_subscription_id == old_subscription.id

        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-renewal",
        )

        old_subscription.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert old_subscription.status == Subscription.Status.EXPIRED
        assert order.subscription.status == Subscription.Status.ACTIVE
        assert order.subscription.trainings_left == 11
        assert order.subscription.expires_at == old_expires_at + timedelta(days=tariff.duration_days)
        assert order.payment.verified_at == paid_at
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_renewal_paid_after_old_subscription_exhausted_does_not_carry_leftovers(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8, duration_days=30)
        paid_at = timezone.now()
        old_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=0,
            expires_at=paid_at + timedelta(days=10),
        )

        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-renewal-exhausted",
        )

        old_subscription.refresh_from_db()
        order.subscription.refresh_from_db()
        assert old_subscription.status == Subscription.Status.EXPIRED
        assert order.subscription.status == Subscription.Status.ACTIVE
        assert order.subscription.trainings_left == tariff.trainings_limit
        assert order.subscription.expires_at == paid_at + timedelta(days=tariff.duration_days)
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_renewal_approved_after_source_closed_early_needs_manual_review(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8, duration_days=30)
        paid_at = timezone.now()
        old_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=3,
            expires_at=paid_at + timedelta(days=10),
        )
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        old_subscription.status = Subscription.Status.EXPIRED
        old_subscription.save(update_fields=["status", "updated_at"])

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-renewal-source-closed",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
        assert event.failure_code == "bank_payment_renewal_source_closed"
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.payment.status == Payment.Status.PENDING
        assert order.subscription.status == Subscription.Status.PENDING
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_late_failed_webhook_after_approved_does_not_downgrade_order(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        paid_at = timezone.now()
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-approved-before-late-fail",
        )

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="FAILED"),
            headers={},
            request_id="req-late-failed",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.IGNORED
        assert event.failure_code == "bank_payment_status_after_confirmed_ignored"
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.payment.status == Payment.Status.CONFIRMED
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        ("provider_status", "failure_code"),
        [
            ("REFUNDED", "bank_payment_refunded_requires_review"),
            ("REFUNDED_PARTIALLY", "bank_payment_refunded_partially_requires_review"),
        ],
    )
    @patch("django_q.tasks.async_task")
    def test_refund_webhook_after_approved_requires_manual_accounting_review(
        self,
        mock_async,
        settings,
        club,
        owner_user,
        provider_status,
        failure_code,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        paid_at = timezone.now()
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=paid_at.isoformat()),
            headers={},
            request_id="req-approved-before-refund",
        )

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status=provider_status),
            headers={},
            request_id=f"req-{provider_status.lower()}",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
        assert event.failure_code == failure_code
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert order.last_error_code == failure_code
        assert order.payment.status == Payment.Status.CONFIRMED
        assert order.subscription.status == Subscription.Status.ACTIVE
        refund_case = PaymentRefundCase.objects.for_club(club).get(provider_event=event)
        assert refund_case.order_id == order.id
        assert refund_case.refund_kind == (
            PaymentRefundCase.Kind.FULL
            if provider_status == "REFUNDED"
            else PaymentRefundCase.Kind.PARTIAL
        )
        assert refund_case.status == (
            PaymentRefundCase.Status.DETECTED
            if provider_status == "REFUNDED"
            else PaymentRefundCase.Status.RECONCILIATION_REQUIRED
        )
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_manual_review_partial_refund_requires_refund_amount_snapshot(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=timezone.now().isoformat()),
            headers={},
            request_id="req-approved-before-partial-refund",
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="REFUNDED_PARTIALLY"),
            headers={},
            request_id="req-partial-refund",
        )
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW

        with pytest.raises(BusinessLogicError) as exc_info:
            resolve_bank_payment_order_manual_review(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
                resolution=BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
                reason="Provider sent partial refund without amount",
                evidence={"provider_event_id": "evt-partial"},
            )

        assert exc_info.value.code == "bank_payment_review_refund_amount_required"
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert BankPaymentOrderReviewEvent.objects.for_club(club).count() == 0
        assert mock_async.call_count == 0

    @patch("django_q.tasks.async_task")
    def test_manual_review_partial_refund_rejects_full_refund_amount(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, price=Decimal("5000"))
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=timezone.now().isoformat()),
            headers={},
            request_id="req-approved-before-invalid-partial-refund",
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="REFUNDED_PARTIALLY"),
            headers={},
            request_id="req-invalid-partial-refund",
        )
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW

        with pytest.raises(BusinessLogicError) as exc_info:
            resolve_bank_payment_order_manual_review(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
                resolution=BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
                reason="Provider sent full amount as partial refund",
                evidence={"refund_amount": "5000.00"},
            )

        assert exc_info.value.code == "bank_payment_review_partial_refund_amount_invalid"
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert BankPaymentOrderReviewEvent.objects.for_club(club).count() == 0
        assert mock_async.call_count == 0

    @pytest.mark.parametrize(
        ("provider_status", "resolution", "evidence", "expected_order_status"),
        [
            (
                "REFUNDED",
                BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
                {
                    "provider_event_id": "evt-refund",
                    "entitlement_action": PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
                },
                BankPaymentOrder.Status.REFUNDED,
            ),
            (
                "REFUNDED_PARTIALLY",
                BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
                {"provider_event_id": "evt-partial-refund", "refund_amount": "1000.00"},
                BankPaymentOrder.Status.REFUNDED_PARTIALLY,
            ),
        ],
    )
    @patch("django_q.tasks.async_task")
    def test_manual_review_refund_posts_accounting_event_and_records_dispositions(
        self,
        mock_async,
        settings,
        club,
        owner_user,
        provider_status,
        resolution,
        evidence,
        expected_order_status,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, price=Decimal("5000"))
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=timezone.now().isoformat()),
            headers={},
            request_id=f"req-approved-before-{provider_status.lower()}",
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status=provider_status),
            headers={},
            request_id=f"req-{provider_status.lower()}",
        )
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW

        resolved = resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=resolution,
            reason="Provider statement checked",
            evidence=evidence,
        )

        resolved.payment.refresh_from_db()
        resolved.subscription.refresh_from_db()
        event = BankPaymentOrderReviewEvent.objects.for_club(club).get(order=order)
        refund = PaymentRefund.objects.for_club(club).get(order=order)
        assert resolved.status == expected_order_status
        assert resolved.payment.status == Payment.Status.CONFIRMED
        assert resolved.subscription.status == Subscription.Status.ACTIVE
        assert event.previous_payment_status == Payment.Status.CONFIRMED
        assert event.new_payment_status == Payment.Status.CONFIRMED
        assert event.previous_subscription_status == Subscription.Status.ACTIVE
        assert event.new_subscription_status == Subscription.Status.ACTIVE
        assert event.evidence_metadata["accounting_effect"] == "payment_refund_posted"
        assert event.evidence_metadata["refund_id"] == str(refund.id)
        assert event.evidence_metadata["refund_case_id"] == str(refund.refund_case_id)
        assert refund.amount == (
            Decimal("1000.00")
            if resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY
            else Decimal("5000.00")
        )
        assert refund.entitlement_disposition == (
            PaymentRefund.EntitlementDisposition.KEPT_PARTIAL
            if resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY
            else PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS
        )
        if resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY:
            assert event.evidence_metadata["refund_amount"] == "1000.00"
        assert mock_async.call_count == 0


@pytest.mark.django_db
class TestBankPaymentOrderAPI:
    @patch("django_q.tasks.async_task")
    def test_owner_lists_and_approves_detected_partial_refund_case(
        self,
        mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club, price=Decimal("5000"))
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="APPROVED", paid_at=timezone.now().isoformat()),
            headers={},
            request_id="api-refund-approved",
        )
        provider_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="REFUNDED_PARTIALLY"),
            headers={},
            request_id="api-refund-detected",
        )
        refund_case = PaymentRefundCase.objects.for_club(club).get(provider_event=provider_event)

        list_response = client.get(
            "/billing/payment-refund-cases/",
            **_auth_params(owner_user, club),
        )
        approve_response = client.post(
            f"/billing/payment-refund-cases/{refund_case.id}/approve/",
            json={
                "idempotency_key": "api-partial-refund",
                "amount": "1000.00",
                "refund_kind": "partial",
                "reason": "Provider statement checked",
            },
            **_auth_params(owner_user, club),
        )

        assert list_response.status_code == 200
        assert list_response.json() == [
            {
                "id": refund_case.id,
                "order_id": order.id,
                "payment_id": order.payment_id,
                "student_id": order.student_id,
                "refund_kind": PaymentRefundCase.Kind.PARTIAL,
                "detected_amount": None,
                "provider_refunded_at": list_response.json()[0]["provider_refunded_at"],
                "status": PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
                "provider_event_id": provider_event.id,
                "legacy_review_event_id": None,
            }
        ]
        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == PaymentRefund.Status.COMPLETED
        assert approve_response.json()["entitlement_disposition"] == (
            PaymentRefund.EntitlementDisposition.KEPT_PARTIAL
        )
        assert PaymentRefund.objects.for_club(club).get(id=approve_response.json()["id"]).amount == Decimal(
            "1000.00"
        )
        assert mock_async.call_count == 0

    def test_owner_can_create_bank_payment_order(self, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)

        response = client.post(
            "/billing/bank-payment-orders/",
            json={"student_id": student.id, "tariff_id": tariff.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        order = BankPaymentOrder.objects.get(id=data["id"])
        assert data["provider_payment_url"] == order.provider_payment_url
        assert data["source"] == BankPaymentOrder.Source.OWNER
        assert order.payment.payment_method == Payment.Method.ONLINE

    def test_owner_can_resolve_manual_review_bank_payment_order(self, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
        )
        BankPaymentOrder.objects.filter(id=order.id).update(status=BankPaymentOrder.Status.MANUAL_REVIEW)

        response = client.post(
            f"/billing/bank-payment-orders/{order.id}/review/",
            json={
                "resolution": BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
                "reason": "Statement checked",
                "evidence": {"payment_link_id": order.provider_payment_link_id},
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == BankPaymentOrder.Status.APPROVED
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert order.payment.status == Payment.Status.CONFIRMED
        assert BankPaymentOrderReviewEvent.objects.for_club(club).filter(order=order).exists()

    def test_trainer_cannot_create_bank_payment_order_with_discount(self, settings, club, trainer_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        tariff = _tariff_for_club(club)
        discount = DiscountFactory(club=club)

        response = client.post(
            "/billing/bank-payment-orders/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "discount_ids": [discount.id],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_discounts_not_allowed"
        assert BankPaymentOrder.objects.count() == 0

    def test_trainer_group_bank_order_requires_target_before_legacy_reuse(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        tariff = _tariff_for_club(club)
        legacy_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=trainer_user.id,
            seller_trainer_id=trainer.id,
        )

        response = client.post(
            "/billing/bank-payment-orders/",
            json={"student_id": student.id, "tariff_id": tariff.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "target_schedule_required"
        assert BankPaymentOrder.objects.count() == 1
        assert BankPaymentOrder.objects.get() == legacy_order

    def test_trainer_group_bank_order_response_exposes_target_context(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        recorder = TrainerFactory(club=club, user=trainer_user)
        target_trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club, assigned_trainer=recorder)
        tariff = _tariff_for_club(club)
        target_date = timezone.localdate()
        schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=tariff.training_type,
            day_of_week=target_date.weekday(),
        )

        response = client.post(
            "/billing/bank-payment-orders/",
            json={
                "student_id": student.id,
                "tariff_id": tariff.id,
                "target_schedule_id": schedule.id,
                "target_start_date": target_date.isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert response.json()["target_schedule_id"] == schedule.id
        assert response.json()["target_start_date"] == target_date.isoformat()

    def test_trainer_cannot_access_another_trainers_booking_linked_orders_after_reassignment(
        self,
        settings,
        club,
        owner_user,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        owning_trainer = TrainerFactory(club=club, user=trainer_user)
        foreign_trainer_user = UserFactory()
        foreign_trainer = TrainerFactory(club=club, user=foreign_trainer_user)
        student = StudentFactory(
            club=club,
            status="active",
            assigned_trainer=owning_trainer,
        )
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("1000.00"),
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("1000.00"),
            trainings_limit=1,
        )
        TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=training_type,
            credits_total=1,
            paid_amount_basis=tariff.price,
        )
        TrainerLocationFactory(club=club, trainer=owning_trainer, location=location)

        booking_date = timezone.localdate()
        schedule = ScheduleFactory(
            club=club,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            one_time_date=booking_date,
            day_of_week=booking_date.weekday(),
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=booking_date,
            ends_on=booking_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            date=booking_date,
            is_debt=True,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=tariff.price,
            required_tariff=tariff,
            reason="personal_drop_in",
        )
        booking = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.ATTENDED,
            checkin=checkin,
            debt=debt,
            created_by=trainer_user,
            idempotency_key="trainer-booking-linked-drop-in-order",
        )
        drop_in_link = create_personal_drop_in_bank_payment_order(
            club_id=club.id,
            booking_id=booking.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=trainer_user.id,
            idempotency_key="trainer-booking-linked-drop-in-order-payment",
        )
        reservation_starts_at = timezone.now() + timedelta(days=14)
        reservation_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
        )
        reservation_payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=reservation_subscription,
            amount=tariff.price,
            original_amount=tariff.price,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=trainer_user,
            seller_trainer=owning_trainer,
            package_owner_trainer=owning_trainer,
        )
        reservation_order = BankPaymentOrder.objects.create(
            club=club,
            payment=reservation_payment,
            subscription=reservation_subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.TRAINER,
            status=BankPaymentOrder.Status.PENDING,
            amount_snapshot=reservation_payment.amount,
            purpose_snapshot="Personal booking reservation",
            provider_payment_url="https://pay.example.test/personal-reservation",
            expires_at=timezone.now() + timedelta(hours=1),
            created_by=trainer_user,
        )
        reservation = PersonalBookingPaymentReservation.objects.create(
            club=club,
            student=student,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            tariff=tariff,
            payment=reservation_order.payment,
            bank_payment_order=reservation_order,
            subscription=reservation_order.subscription,
            starts_at=reservation_starts_at,
            ends_at=reservation_starts_at + timedelta(hours=1),
            status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            expires_at=reservation_order.expires_at,
            idempotency_key="trainer-booking-linked-reservation-order",
            created_by=trainer_user,
        )
        drop_in_order = drop_in_link.bank_payment_order
        debt.refresh_from_db()
        assert debt.settlement_payment_id == drop_in_order.payment_id
        assert reservation.bank_payment_order_id == reservation_order.id

        owned_list = client.get(
            f"/billing/bank-payment-orders/?student_id={student.id}&status=live",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert owned_list.status_code == 200
        assert {drop_in_order.id, reservation_order.id} <= {
            item["id"] for item in owned_list.json()["items"]
        }

        student.assigned_trainer = foreign_trainer
        student.save(update_fields=["assigned_trainer", "updated_at"])

        foreign_list = client.get(
            f"/billing/bank-payment-orders/?student_id={student.id}&status=live",
            **_auth_params(foreign_trainer_user, club, role="trainer"),
        )
        assert foreign_list.status_code == 200
        assert {
            item["id"] for item in foreign_list.json()["items"]
        }.isdisjoint({drop_in_order.id, reservation_order.id})

        for order in (drop_in_order, reservation_order):
            detail_response = client.get(
                f"/billing/bank-payment-orders/{order.id}/",
                **_auth_params(foreign_trainer_user, club, role="trainer"),
            )
            cancel_response = client.post(
                f"/billing/bank-payment-orders/{order.id}/cancel/",
                json={},
                **_auth_params(foreign_trainer_user, club, role="trainer"),
            )
            refresh_response = client.post(
                f"/billing/bank-payment-orders/{order.id}/refresh/",
                json={},
                **_auth_params(foreign_trainer_user, club, role="trainer"),
            )
            assert detail_response.status_code == 404
            assert cancel_response.status_code == 404
            assert refresh_response.status_code == 404

        for order in (drop_in_order, reservation_order):
            order.refresh_from_db()
            order.payment.refresh_from_db()
            order.subscription.refresh_from_db()
            assert order.status == BankPaymentOrder.Status.PENDING
            assert order.payment.status == Payment.Status.PENDING
            assert order.subscription.status == Subscription.Status.PENDING
        debt.refresh_from_db()
        reservation.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.settlement_payment_id == drop_in_order.payment_id
        assert DebtSettlementEvent.objects.for_club(club).filter(
            debt=debt,
            payment_id=drop_in_order.payment_id,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        ).exists()
        assert reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
        assert reservation.bank_payment_order_id == reservation_order.id
        assert reservation.payment_id == reservation_order.payment_id
        assert reservation.subscription_id == reservation_order.subscription_id

        assert client.get(
            f"/billing/bank-payment-orders/{drop_in_order.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        ).status_code == 200
        assert client.post(
            f"/billing/bank-payment-orders/{drop_in_order.id}/refresh/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        ).status_code == 200
        assert BankPaymentReconciliationAttempt.objects.for_club(club).filter(
            order=drop_in_order
        ).count() == 1
        assert client.get(
            f"/billing/bank-payment-orders/{reservation_order.id}/",
            **_auth_params(owner_user, club),
        ).status_code == 200

    def test_trainer_bank_payment_order_list_hides_self_service_sources(self, settings, club, trainer_user, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        student_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        BankPaymentOrder.objects.filter(id=student_order.id).update(source=BankPaymentOrder.Source.STUDENT)
        student_order.refresh_from_db()
        trainer_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=trainer_user.id,
        )

        response = client.get(
            f"/billing/bank-payment-orders/?student_id={student.id}&status=live",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["items"]}
        assert trainer_order.id in ids
        assert student_order.id not in ids

    def test_trainer_cannot_cancel_self_service_bank_payment_order(self, settings, club, trainer_user, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=_tariff_for_club(club).id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        BankPaymentOrder.objects.filter(id=order.id).update(source=BankPaymentOrder.Source.STUDENT)
        order.refresh_from_db()

        detail_response = client.get(
            f"/billing/bank-payment-orders/{order.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        cancel_response = client.post(
            f"/billing/bank-payment-orders/{order.id}/cancel/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        refresh_response = client.post(
            f"/billing/bank-payment-orders/{order.id}/refresh/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert detail_response.status_code == 404
        assert cancel_response.status_code == 404
        assert refresh_response.status_code == 404
        order.refresh_from_db()
        assert order.status == BankPaymentOrder.Status.PENDING

    def test_provider_webhook_is_public_but_confirms_only_verified_mock_payload(self, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club)
        tariff = _tariff_for_club(club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        response = client.post(
            "/billing/payment-provider-webhooks/mock/",
            json={
                "webhookType": "acquiringInternetPayment",
                "event_id": "evt-api-approved",
                "status": "APPROVED",
                "paymentLinkId": order.provider_payment_link_id,
                "operationId": "op-api-approved",
                "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["order_id"] == order.id
        assert data["processing_status"] == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        order.payment.refresh_from_db()
        assert order.payment.status == Payment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S12 suite",
)
def test_provider_event_replay_lock_order_contract(settings, club, owner_user):
    from django.test.utils import CaptureQueriesContext

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    responsible_trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=responsible_trainer,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=responsible_trainer,
        training_type=training_type,
        day_of_week=timezone.localdate().weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club)
    with patch("django_q.tasks.async_task"):
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=timezone.localdate(),
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.RECONCILING,
        )
        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(
                order,
                status="APPROVED",
                paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
            ),
            headers={},
            request_id="s12-lock-order-defer",
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW,
        )

        with CaptureQueriesContext(connection) as queries:
            outcomes = replay_deferred_bank_payment_provider_events(club_id=club.id)

    assert outcomes["processed"] == 1
    event.refresh_from_db()
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED

    locked_sql = [
        query["sql"].upper()
        for query in queries.captured_queries
        if "FOR UPDATE" in query["sql"].upper()
    ]
    order_clause = f'FOR UPDATE OF "{BankPaymentOrder._meta.db_table.upper()}"'
    event_clause = f'FOR UPDATE OF "{BankPaymentProviderEvent._meta.db_table.upper()}"'
    order_lock = next(
        index for index, statement in enumerate(locked_sql) if order_clause in statement
    )
    event_lock = next(
        index for index, statement in enumerate(locked_sql) if event_clause in statement
    )
    assert order_lock < event_lock


@pytest.mark.django_db(transaction=True)
def test_idempotent_reconciliation_exit_retry_reregisters_failed_replay_enqueue(
    club,
    owner_user,
):
    TrainingGroupRolloutStateFactory(club=club)
    transition_training_group_rollout(
        club_id=club.id,
        target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
        actor_id=owner_user.id,
        rationale="Enter reconciliation before testing enqueue recovery.",
        idempotency_key="retry-replay-enqueue-enter",
    )

    with patch(
        "django_q.tasks.async_task",
        side_effect=[RuntimeError("queue unavailable"), "task-id"],
    ) as mock_async:
        with pytest.raises(RuntimeError, match="queue unavailable"):
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.OFF,
                actor_id=owner_user.id,
                rationale="Exit reconciliation and enqueue deferred replay.",
                idempotency_key="retry-replay-enqueue-exit",
            )

        assert TrainingGroupRolloutState.objects.for_club(club).get().mode == (
            TrainingGroupRolloutState.Mode.OFF
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.OFF,
            actor_id=owner_user.id,
            rationale="Exit reconciliation and enqueue deferred replay.",
            idempotency_key="retry-replay-enqueue-exit",
        )

    assert mock_async.call_count == 2
    assert (
        TrainingGroupRolloutEvent.objects.for_club(club)
        .filter(idempotency_key="retry-replay-enqueue-exit")
        .count()
        == 1
    )


@pytest.mark.parametrize(
    (
        "reconciliation_exit",
        "provider_status",
        "expected_order_status",
        "expected_event_status",
        "expected_replay_outcome",
        "expects_canonical_membership",
    ),
    [
        pytest.param(
            "off",
            "APPROVED",
            BankPaymentOrder.Status.APPROVED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            False,
            id="off-legacy-approved",
        ),
        pytest.param(
            "off",
            "FAILED",
            BankPaymentOrder.Status.FAILED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            False,
            id="off-legacy-failed",
        ),
        pytest.param(
            "off",
            "REFUNDED",
            BankPaymentOrder.Status.MANUAL_REVIEW,
            BankPaymentProviderEvent.ProcessingStatus.FAILED,
            "failed",
            False,
            id="off-legacy-refunded",
        ),
        pytest.param(
            "shadow",
            "APPROVED",
            BankPaymentOrder.Status.APPROVED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            True,
            id="shadow-approved",
        ),
        pytest.param(
            "shadow",
            "FAILED",
            BankPaymentOrder.Status.FAILED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            False,
            id="shadow-failed",
        ),
        pytest.param(
            "shadow",
            "REFUNDED",
            BankPaymentOrder.Status.MANUAL_REVIEW,
            BankPaymentProviderEvent.ProcessingStatus.FAILED,
            "failed",
            False,
            id="shadow-refunded",
        ),
        pytest.param(
            "containment",
            "APPROVED",
            BankPaymentOrder.Status.APPROVED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            True,
            id="containment-approved",
        ),
        pytest.param(
            "containment",
            "FAILED",
            BankPaymentOrder.Status.FAILED,
            BankPaymentProviderEvent.ProcessingStatus.PROCESSED,
            "processed",
            False,
            id="containment-failed",
        ),
        pytest.param(
            "containment",
            "REFUNDED",
            BankPaymentOrder.Status.MANUAL_REVIEW,
            BankPaymentProviderEvent.ProcessingStatus.FAILED,
            "failed",
            False,
            id="containment-refunded",
        ),
    ],
)
@patch("django_q.tasks.async_task")
def test_deferred_provider_events_replay_once_on_every_allowed_reconciliation_exit(
    _mock_async,
    reconciliation_exit,
    provider_status,
    expected_order_status,
    expected_event_status,
    expected_replay_outcome,
    expects_canonical_membership,
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupMembership
    from apps.attendance.tests.factories import TrainingGroupFactory
    from apps.trainers.tests.factories import TrainerFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    TrainingGroupRolloutStateFactory(club=club)

    def transition(target_mode, key, **kwargs):
        return transition_training_group_rollout(
            club_id=club.id,
            target_mode=target_mode,
            actor_id=owner_user.id,
            rationale="Exercise a deferred provider event through an audited reconciliation exit.",
            idempotency_key=f"provider-replay-{reconciliation_exit}-{provider_status.lower()}-{key}",
            **kwargs,
        )

    target_kwargs = {}
    if reconciliation_exit == "off":
        # An OFF exit is legal only for legacy work that began from OFF, so it
        # deliberately proves that a pre-existing non-canonical order remains operable.
        tariff = _tariff_for_club(club)
    else:
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "initial-enter")
        transition(
            TrainingGroupRolloutState.Mode.SHADOW,
            "initial-shadow",
            forward_audit_passed=True,
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        responsible_trainer = TrainerFactory(club=club)
        group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=responsible_trainer,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            trainer=responsible_trainer,
            training_type=training_type,
            location=group.location,
            day_of_week=target_start_date.weekday(),
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        target_kwargs = {
            "target_schedule_id": schedule.id,
            "target_training_group_id": group.id,
            "target_start_date": target_start_date,
        }
        transition(TrainingGroupRolloutState.Mode.ACTIVE, "initial-active")

    student = StudentFactory(club=club)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        **target_kwargs,
    )
    if reconciliation_exit == "off":
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "off-enter")
    elif reconciliation_exit == "shadow":
        transition(TrainingGroupRolloutState.Mode.CONTAINMENT, "shadow-containment")
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "shadow-enter")
    else:
        transition(TrainingGroupRolloutState.Mode.CONTAINMENT, "containment-enter")
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "containment-reconciling")

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=_webhook_body(
            order,
            status=provider_status,
            paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
        ),
        headers={},
        request_id=f"deferred-{reconciliation_exit}-{provider_status.lower()}",
    )
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
    assert not TrainingGroupMembership.objects.for_club(club).exists()
    assert not ScheduleEnrollment.objects.for_club(club).exists()

    if reconciliation_exit == "off":
        transition(TrainingGroupRolloutState.Mode.OFF, "off-exit")
    elif reconciliation_exit == "shadow":
        transition(
            TrainingGroupRolloutState.Mode.SHADOW,
            "shadow-exit",
            forward_audit_passed=True,
        )
    else:
        transition(
            TrainingGroupRolloutState.Mode.CONTAINMENT,
            "containment-exit",
            canonical_mutations_committed=True,
            forward_audit_failed=True,
        )
    first_replay = replay_deferred_bank_payment_provider_events(club_id=club.id)
    second_replay = replay_deferred_bank_payment_provider_events(club_id=club.id)

    event.refresh_from_db()
    order.refresh_from_db()
    order.payment.refresh_from_db()
    assert event.processing_status == expected_event_status
    assert order.status == expected_order_status
    assert first_replay == {
        "processed": int(expected_replay_outcome == "processed"),
        "failed": int(expected_replay_outcome == "failed"),
        "ignored": 0,
        "deferred": 0,
        "skipped": 0,
    }
    assert second_replay == {
        "processed": 0,
        "failed": 0,
        "ignored": 0,
        "deferred": 0,
        "skipped": 0,
    }
    assert BankPaymentProviderEvent.objects.for_club(club).filter(order=order).count() == 1
    assert BankPaymentOrder.objects.for_club(club).filter(student=student).count() == 1
    assert Payment.objects.for_club(club).filter(student=student).count() == 1
    memberships = TrainingGroupMembership.objects.for_club(club).filter(student=student)
    projections = ScheduleEnrollment.objects.for_club(club).filter(student=student)
    assert memberships.count() == int(expects_canonical_membership)
    assert projections.count() == int(expects_canonical_membership)
    if expects_canonical_membership:
        membership = memberships.get()
        assert membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
        assert set(projections.values_list("schedule_id", flat=True)) == {order.payment.target_schedule_id}
    if provider_status == "REFUNDED":
        assert PaymentRefundCase.objects.for_club(club).filter(order=order).exists()
    else:
        expected_payment_status = (
            Payment.Status.CONFIRMED if provider_status == "APPROVED" else Payment.Status.REJECTED
        )
        assert order.payment.status == expected_payment_status


@pytest.mark.django_db
def test_mapped_webhook_locks_leading_group_scope_before_creating_provider_event(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.services import training_group_memberships
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
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
        day_of_week=target_start_date.weekday(),
    )
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )

    calls: list[str] = []
    original_lock = training_group_memberships.lock_training_group_payment_scope
    original_create = BankPaymentProviderEvent.objects.create

    def lock_scope(*, club_id):
        calls.append("leading_scope")
        return original_lock(club_id=club_id)

    def create_event(*args, **kwargs):
        calls.append("provider_event")
        return original_create(*args, **kwargs)

    with (
        patch(
            "apps.attendance.services.training_group_memberships.lock_training_group_payment_scope",
            side_effect=lock_scope,
        ),
        patch.object(BankPaymentProviderEvent.objects, "create", side_effect=create_event),
    ):
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=_webhook_body(order, status="AUTHORIZED"),
            headers={},
            request_id="mapped-webhook-lock-order",
        )

    assert calls.index("leading_scope") < calls.index("provider_event")


@pytest.mark.django_db
def test_identical_canonical_bank_order_request_reuses_exact_pending_order(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    slot_trainer = TrainerFactory(club=club)
    responsible_trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=responsible_trainer,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=slot_trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)

    first_order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )
    retry = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )

    assert retry.id == first_order.id
    first_order.payment.refresh_from_db()
    assert first_order.payment.seller_trainer_id == responsible_trainer.id
    assert first_order.payment.target_training_group_id == group.id


@pytest.mark.django_db
def test_canonical_bank_order_reuse_rejects_each_changed_same_source_dimension(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    responsible_trainer = TrainerFactory(club=club)
    changed_responsible_trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=responsible_trainer,
    )
    anchor = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=responsible_trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    alternate_anchor = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=responsible_trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    other_group = TrainingGroup.objects.create(
        club=club,
        name="Other canonical reuse group",
        training_type=training_type,
        location=group.location,
        responsible_trainer=responsible_trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    other_group_anchor = ScheduleFactory(
        club=club,
        training_group=other_group,
        trainer=responsible_trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    first_discount = DiscountFactory(club=club, value=Decimal("10"))
    changed_discount = DiscountFactory(club=club, value=Decimal("20"))
    base_request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
        "target_schedule_id": anchor.id,
        "target_training_group_id": group.id,
        "target_start_date": target_start_date,
        "discount_ids": [first_discount.id],
    }
    first_order = create_bank_payment_order(**base_request)
    first_order.payment.refresh_from_db()
    assert first_order.payment.seller_trainer_id == responsible_trainer.id

    changed_requests = [
        (
            "group",
            {
                "target_schedule_id": other_group_anchor.id,
                "target_training_group_id": other_group.id,
            },
        ),
        ("anchor", {"target_schedule_id": alternate_anchor.id}),
        ("date", {"target_start_date": target_start_date + timedelta(days=7)}),
        ("discount", {"discount_ids": [changed_discount.id]}),
    ]
    for _dimension, overrides in changed_requests:
        with pytest.raises(BusinessLogicError) as exc_info:
            create_bank_payment_order(**(base_request | overrides))

        assert exc_info.value.code == "bank_payment_order_pending_exists"
        assert BankPaymentOrder.objects.for_club(club).count() == 1
        assert Payment.objects.for_club(club).count() == 1

    TrainingGroup.objects.for_club(club).filter(id=group.id).update(
        responsible_trainer=changed_responsible_trainer
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        create_bank_payment_order(**base_request)

    assert exc_info.value.code == "bank_payment_order_pending_exists"
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_canonical_bank_order_reuse_includes_target_membership_identity(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    responsible_trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=responsible_trainer,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=responsible_trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    prior_membership = TrainingGroupMembership.objects.create(
        club=club,
        student=student,
        training_group=group,
        starts_on=target_start_date - timedelta(days=14),
        source=TrainingGroupMembership.Source.MANUAL,
    )
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
        "target_schedule_id": schedule.id,
        "target_training_group_id": group.id,
        "target_start_date": target_start_date,
    }

    first_order = create_bank_payment_order(**request)
    first_order.payment.refresh_from_db()
    assert first_order.payment.target_group_membership_id == prior_membership.id

    TrainingGroupMembership.objects.for_club(club).filter(id=prior_membership.id).update(
        ends_on=target_start_date - timedelta(days=1)
    )
    current_membership = TrainingGroupMembership.objects.create(
        club=club,
        student=student,
        training_group=group,
        starts_on=target_start_date,
        source=TrainingGroupMembership.Source.MANUAL,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        create_bank_payment_order(**request)

    assert exc_info.value.code == "bank_payment_order_pending_exists"
    assert current_membership.id != prior_membership.id
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_legacy_bank_order_reuse_rejects_changed_package_owner(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    package_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=package_training_type)
    student = StudentFactory(club=club)
    package_owner = TrainerFactory(club=club)
    changed_package_owner = TrainerFactory(club=club)
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
        "package_owner_trainer_id": package_owner.id,
    }
    first_order = create_bank_payment_order(**request)

    with pytest.raises(BusinessLogicError) as exc_info:
        create_bank_payment_order(
            **(request | {"package_owner_trainer_id": changed_package_owner.id})
        )

    assert exc_info.value.code == "bank_payment_order_pending_exists"
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert first_order.payment.package_owner_trainer_id == package_owner.id


@pytest.mark.django_db
def test_terminal_canonical_order_allows_new_same_source_family_and_tariff_stays_distinct(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
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
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    first_tariff = TariffFactory(club=club, training_type=training_type)
    second_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    second_group = TrainingGroupFactory(
        club=club,
        training_type=second_training_type,
        responsible_trainer=trainer,
    )
    second_schedule = ScheduleFactory(
        club=club,
        training_group=second_group,
        trainer=trainer,
        training_type=second_training_type,
        location=second_group.location,
        day_of_week=target_start_date.weekday(),
    )
    second_tariff = TariffFactory(club=club, training_type=second_training_type)
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": first_tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
        "target_schedule_id": schedule.id,
        "target_training_group_id": group.id,
        "target_start_date": target_start_date,
    }
    first_order = create_bank_payment_order(**request)
    different_tariff_order = create_bank_payment_order(
        **(
            request
            | {
                "tariff_id": second_tariff.id,
                "target_schedule_id": second_schedule.id,
                "target_training_group_id": second_group.id,
            }
        )
    )

    assert different_tariff_order.id != first_order.id
    assert different_tariff_order.payment.tariff_id == second_tariff.id

    cancel_bank_payment_order(
        club_id=club.id,
        order_id=first_order.id,
        actor_user_id=owner_user.id,
        reason="test terminal lifecycle permits a fresh canonical order",
    )
    replacement = create_bank_payment_order(**request)

    assert replacement.id not in {first_order.id, different_tariff_order.id}
    assert replacement.source == first_order.source
    assert replacement.payment.tariff_id == first_tariff.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection lock semantics; run in the PostgreSQL S5 suite",
)
@patch("django_q.tasks.async_task")
def test_postgresql_concurrent_identical_canonical_order_creation_keeps_one_financial_family(
    _mock_async,
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupMembership, TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
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
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
        "target_schedule_id": schedule.id,
        "target_training_group_id": group.id,
        "target_start_date": target_start_date,
    }
    gate = Barrier(2)

    def submit() -> tuple[str, int | str]:
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return "order", create_bank_payment_order(**request).id
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: submit(), range(2)))

    orders = list(BankPaymentOrder.objects.for_club(club).order_by("id"))
    assert len(orders) == 1
    order = orders[0]
    assert Payment.objects.for_club(club).filter(id=order.payment_id).count() == 1
    assert Subscription.objects.for_club(club).filter(id=order.subscription_id).count() == 1
    assert TrainingGroupMembership.objects.for_club(club).filter(
        student=student,
        training_group=group,
    ).count() == 0
    assert any(kind == "order" and value == order.id for kind, value in outcomes)
    assert all(
        (kind == "order" and value == order.id)
        or (kind == "business_error" and value == "bank_payment_order_creating")
        for kind, value in outcomes
    )

    retry = create_bank_payment_order(**request)
    assert retry.id == order.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection command-arbitration semantics",
)
@patch("django_q.tasks.async_task")
def test_postgresql_schedule_owned_seller_normalization_replays_same_key(
    _mock_async,
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    schedule_trainer = TrainerFactory(club=club)
    owner_supplied_seller = TrainerFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=schedule_trainer,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club)
    key = "pg-schedule-owned-seller-key"
    gate = Barrier(2)
    transaction.commit()

    def submit(seller_id: int | None):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return (
                "order",
                create_bank_payment_order(
                    club_id=club.id,
                    student_id=student.id,
                    tariff_id=tariff.id,
                    source=BankPaymentOrder.Source.OWNER,
                    created_by_id=owner_user.id,
                    seller_trainer_id=seller_id,
                    target_schedule_id=schedule.id,
                    target_start_date=target_start_date,
                    command_idempotency_key=key,
                ).id,
            )
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, (None, owner_supplied_seller.id)))

    assert all(kind == "order" for kind, _value in outcomes), outcomes
    assert len({value for _kind, value in outcomes}) == 1
    assert Payment.objects.for_club(club).filter(command_idempotency_key=key).count() == 1
    assert BankPaymentOrder.objects.for_club(club).filter(payment__command_idempotency_key=key).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL duplicate-provider confirmation lock semantics",
)
@patch("django_q.tasks.async_task")
def test_postgresql_duplicate_provider_renewal_confirmation_has_one_event_and_leaf(
    _mock_async,
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = _tariff_for_club(club, duration_days=30, trainings_limit=8)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=3,
        expires_at=timezone.now() + timedelta(days=7),
    )
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=None,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        renewed_from_subscription_id=source.id,
        command_idempotency_key="pg-duplicate-provider-renewal",
    )
    body = _webhook_body(
        order,
        status="APPROVED",
        paid_at=(timezone.now() - timedelta(seconds=1)).isoformat(),
    )
    gate = Barrier(2)
    transaction.commit()

    def confirm():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            event = process_bank_payment_webhook(
                provider=BankPaymentOrder.Provider.MOCK,
                request_body=body,
                headers={},
                request_id="pg-duplicate-provider-renewal",
            )
            return "event", event.processing_status
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: confirm(), range(2)))

    order.refresh_from_db()
    order.payment.refresh_from_db()
    source.refresh_from_db()
    assert not {
        code
        for kind, code in outcomes
        if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }, outcomes
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert source.status == Subscription.Status.EXPIRED
    assert SubscriptionRenewalEvent.objects.for_club(club).filter(payment=order.payment).count() == 1
    assert Subscription.objects.for_club(club).filter(
        renewed_from=source,
        status=Subscription.Status.ACTIVE,
        deleted_at__isnull=True,
    ).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection lock semantics; run in the PostgreSQL S5 suite",
)
@patch("django_q.tasks.async_task")
def test_postgresql_canonical_approval_and_cancel_race_has_one_coherent_terminal_family(
    _mock_async,
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroupMembership, TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=trainer,
    )
    anchor = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    sibling = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=anchor.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )
    gate = Barrier(2)

    def approve() -> tuple[str, str]:
        close_old_connections()
        try:
            gate.wait(timeout=10)
            event = process_bank_payment_webhook(
                provider=BankPaymentOrder.Provider.MOCK,
                request_body=_webhook_body(
                    order,
                    status="APPROVED",
                    paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
                ),
                headers={},
                request_id="s5-pg-approved-cancel-race",
            )
            return "approved_webhook", event.processing_status
        except BusinessLogicError as exc:
            return "approved_webhook_error", exc.code
        finally:
            close_old_connections()

    def cancel() -> tuple[str, str]:
        close_old_connections()
        try:
            gate.wait(timeout=10)
            cancelled = cancel_bank_payment_order(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
                reason="S5 PostgreSQL approval/cancel lock race",
            )
            return "cancel", cancelled.status
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve), executor.submit(cancel)]
        outcomes = [future.result(timeout=20) for future in futures]

    order.refresh_from_db()
    order.payment.refresh_from_db()
    membership_qs = TrainingGroupMembership.objects.for_club(club).filter(
        student=student,
        training_group=group,
        authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
    )
    projection_qs = ScheduleEnrollment.objects.for_club(club).filter(
        training_group_membership__in=membership_qs,
        created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
    )
    assert not {
        code
        for kind, code in outcomes
        if kind.endswith("_error") and code in {"deadlock_detected", "database_locked"}
    }
    assert membership_qs.count() <= 1
    assert projection_qs.count() <= 2
    assert Debt.objects.for_club(club).filter(
        settlement_payment=order.payment,
        resolved_at__isnull=True,
    ).count() == 0
    if order.payment.status == Payment.Status.CONFIRMED:
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert membership_qs.count() == 1
        assert set(projection_qs.values_list("schedule_id", flat=True)) == {anchor.id, sibling.id}
    else:
        assert order.payment.status == Payment.Status.REJECTED
        assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        assert membership_qs.count() == 0
        assert projection_qs.count() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S11 suite",
)
def test_bank_order_reuse_lock_order_contract(
    settings,
    club,
    owner_user,
):
    from django.test.utils import CaptureQueriesContext

    from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
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
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    first_order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )

    calls: list[str] = []
    original_order_for_club = BankPaymentOrder.objects.for_club
    original_payment_for_club = Payment.objects.for_club
    original_subscription_for_club = Subscription.objects.for_club
    original_student_for_club = Student.objects.for_club
    original_group_for_club = TrainingGroup.objects.for_club

    def record_order(*args, **kwargs):
        calls.append("bank_order")
        return original_order_for_club(*args, **kwargs)

    def record_payment(*args, **kwargs):
        calls.append("payment")
        return original_payment_for_club(*args, **kwargs)

    def record_subscription(*args, **kwargs):
        calls.append("subscription")
        return original_subscription_for_club(*args, **kwargs)

    def record_student(*args, **kwargs):
        calls.append("student")
        return original_student_for_club(*args, **kwargs)

    def record_group(*args, **kwargs):
        calls.append("training_group")
        return original_group_for_club(*args, **kwargs)

    with (
        patch.object(BankPaymentOrder.objects, "for_club", side_effect=record_order),
        patch.object(Payment.objects, "for_club", side_effect=record_payment),
        patch.object(Subscription.objects, "for_club", side_effect=record_subscription),
        patch.object(Student.objects, "for_club", side_effect=record_student),
        patch.object(TrainingGroup.objects, "for_club", side_effect=record_group),
        CaptureQueriesContext(connection) as queries,
    ):
        retry = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
        )

    assert retry.id == first_order.id
    assert calls.index("student") < calls.index("bank_order")
    assert calls.index("bank_order") < calls.index("payment") < calls.index("subscription")
    assert calls.index("subscription") < calls.index("training_group")

    locked_sql = [
        query["sql"].upper()
        for query in queries.captured_queries
        if "FOR UPDATE" in query["sql"].upper()
    ]
    financial_lock_clauses = [
        f'FOR UPDATE OF "{model._meta.db_table.upper()}"'
        for model in (BankPaymentOrder, Payment, Subscription)
    ]
    first_lock_indexes = [
        next(
            index
            for index, statement in enumerate(locked_sql)
            if f'"{Student._meta.db_table.upper()}"' in statement
        ),
        *[
            next(
                index
                for index, statement in enumerate(locked_sql)
                if clause in statement
            )
            for clause in financial_lock_clauses
        ],
        next(
            index
            for index, statement in enumerate(locked_sql)
            if f'"{TrainingGroup._meta.db_table.upper()}"' in statement
        ),
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL Slice 13 suite",
)
def test_manual_review_lock_order_contract(
    settings,
    club,
    owner_user,
):
    from django.test.utils import CaptureQueriesContext

    from apps.attendance.services import training_group_memberships

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = _tariff_for_club(club)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    BankPaymentOrder.objects.for_club(club).filter(id=order.id).update(
        status=BankPaymentOrder.Status.MANUAL_REVIEW,
    )

    calls: list[str] = []
    original_scope_lock = training_group_memberships.lock_training_group_mutation_scope
    original_order_for_club = BankPaymentOrder.objects.for_club
    original_payment_for_club = Payment.objects.for_club

    def record_scope(*args, **kwargs):
        calls.append("leading_scope")
        return original_scope_lock(*args, **kwargs)

    def record_order(*args, **kwargs):
        calls.append("bank_order")
        return original_order_for_club(*args, **kwargs)

    def record_payment(*args, **kwargs):
        calls.append("payment")
        return original_payment_for_club(*args, **kwargs)

    with (
        patch.object(
            training_group_memberships,
            "lock_training_group_mutation_scope",
            side_effect=record_scope,
        ),
        patch.object(BankPaymentOrder.objects, "for_club", side_effect=record_order),
        patch.object(Payment.objects, "for_club", side_effect=record_payment),
        CaptureQueriesContext(connection) as queries,
    ):
        resolved = resolve_bank_payment_order_manual_review(
            club_id=club.id,
            order_id=order.id,
            actor_user_id=owner_user.id,
            resolution=BankPaymentOrderReviewEvent.Resolution.REJECT,
            reason="Operator rejected after review",
        )

    assert resolved.status == BankPaymentOrder.Status.FAILED
    assert calls.index("leading_scope") < calls.index("bank_order") < calls.index("payment")

    locked_sql = [
        query["sql"].upper()
        for query in queries.captured_queries
        if "FOR UPDATE" in query["sql"].upper()
    ]
    first_lock_indexes = [
        next(
            index
            for index, statement in enumerate(locked_sql)
            if f'"{model._meta.db_table.upper()}"' in statement
        )
        for model in (TrainingGroupRolloutState, BankPaymentOrder, Payment)
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL Slice 13 suite",
)
def test_legacy_expiry_lock_order_contract(settings, club, owner_user):
    from django.test.utils import CaptureQueriesContext

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = _tariff_for_club(club)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    order.expires_at = timezone.now() - timedelta(seconds=1)
    order.save(update_fields=["expires_at", "updated_at"])

    with CaptureQueriesContext(connection) as queries:
        assert expire_bank_payment_orders(now=timezone.now()) == 1

    locked_sql = [
        query["sql"].upper()
        for query in queries.captured_queries
        if "FOR UPDATE" in query["sql"].upper()
    ]
    first_lock_indexes = [
        next(
            index
            for index, statement in enumerate(locked_sql)
            if f'"{model._meta.db_table.upper()}"' in statement
        )
        for model in (TrainingGroupRolloutState, BankPaymentOrder, Payment)
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL Slice 13 suite",
)
def test_legacy_deferred_replay_lock_order_contract(settings, club, owner_user):
    from django.test.utils import CaptureQueriesContext

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    tariff = _tariff_for_club(club)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    rollout = TrainingGroupRolloutState.objects.for_club(club).get()
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.RECONCILING,
    )
    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=_webhook_body(
            order,
            status="APPROVED",
            paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(),
        ),
        headers={},
        request_id="slice13-legacy-replay-lock-order",
    )
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.SHADOW,
    )

    with CaptureQueriesContext(connection) as queries:
        outcomes = replay_deferred_bank_payment_provider_events(club_id=club.id)

    assert outcomes["processed"] == 1
    locked_sql = [
        query["sql"].upper()
        for query in queries.captured_queries
        if "FOR UPDATE" in query["sql"].upper()
    ]
    first_lock_indexes = [
        next(
            index
            for index, statement in enumerate(locked_sql)
            if f'"{TrainingGroupRolloutState._meta.db_table.upper()}"' in statement
        ),
        *[
            next(
                index
                for index, statement in enumerate(locked_sql)
                if f'FOR UPDATE OF "{model._meta.db_table.upper()}"' in statement
            )
            for model in (BankPaymentOrder, BankPaymentProviderEvent, Payment)
        ],
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)

@pytest.mark.django_db
def test_canonical_reuse_locks_student_then_financial_family_then_group(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
    from apps.attendance.tests.factories import TrainingGroupFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
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
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    first_order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=schedule.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )

    calls: list[str] = []
    original_order_for_club = BankPaymentOrder.objects.for_club
    original_payment_for_club = Payment.objects.for_club
    original_subscription_for_club = Subscription.objects.for_club
    original_student_for_club = Student.objects.for_club
    original_group_for_club = TrainingGroup.objects.for_club

    def record_order(*args, **kwargs):
        calls.append("bank_order")
        return original_order_for_club(*args, **kwargs)

    def record_payment(*args, **kwargs):
        calls.append("payment")
        return original_payment_for_club(*args, **kwargs)

    def record_subscription(*args, **kwargs):
        calls.append("subscription")
        return original_subscription_for_club(*args, **kwargs)

    def record_student(*args, **kwargs):
        calls.append("student")
        return original_student_for_club(*args, **kwargs)

    def record_group(*args, **kwargs):
        calls.append("training_group")
        return original_group_for_club(*args, **kwargs)

    with (
        patch.object(BankPaymentOrder.objects, "for_club", side_effect=record_order),
        patch.object(Payment.objects, "for_club", side_effect=record_payment),
        patch.object(Subscription.objects, "for_club", side_effect=record_subscription),
        patch.object(Student.objects, "for_club", side_effect=record_student),
        patch.object(TrainingGroup.objects, "for_club", side_effect=record_group),
    ):
        retry = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
        )

    assert retry.id == first_order.id
    assert calls.index("student") < calls.index("bank_order")
    assert calls.index("bank_order") < calls.index("payment") < calls.index("subscription")
    assert calls.index("subscription") < calls.index("training_group")

@pytest.mark.django_db
def test_group_target_mapping_race_keeps_leading_scope_before_identity_locks(
    settings,
    club,
    owner_user,
):
    from apps.attendance.models import Schedule, TrainingGroupRolloutState
    from apps.attendance.services import training_group_memberships
    from apps.attendance.tests.factories import TrainingGroupFactory
    from apps.billing.service_modules import group_payments

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    target_start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    trainer = TrainerFactory(club=club)
    group = TrainingGroupFactory(
        club=club,
        training_type=training_type,
        responsible_trainer=trainer,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=None,
        trainer=trainer,
        training_type=training_type,
        location=group.location,
        day_of_week=target_start_date.weekday(),
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)

    calls: list[str] = []
    original_scope_lock = training_group_memberships.lock_training_group_mutation_scope
    original_student_for_club = Student.objects.for_club
    original_validate_target = group_payments._validate_group_conversion_target
    validation_calls = 0

    def lock_scope(*, club_id):
        calls.append("leading_scope")
        return original_scope_lock(club_id=club_id)

    def record_student(*args, **kwargs):
        calls.append("student")
        return original_student_for_club(*args, **kwargs)

    def rebind_after_preflight(*args, **kwargs):
        nonlocal validation_calls

        schedule_result = original_validate_target(*args, **kwargs)
        validation_calls += 1
        if validation_calls == 1 and not kwargs.get("lock_schedule", False):
            Schedule.objects.for_club(club).filter(id=schedule.id).update(training_group=group)
        return schedule_result

    with (
        patch(
            "apps.attendance.services.training_group_memberships.lock_training_group_mutation_scope",
            side_effect=lock_scope,
        ),
        patch.object(Student.objects, "for_club", side_effect=record_student),
        patch(
            "apps.billing.service_modules.group_payments._validate_group_conversion_target",
            side_effect=rebind_after_preflight,
        ),
    ):
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
        )

    assert validation_calls >= 2
    # One scope lock protects claim creation; a second protects application of
    # the provider response in its later transaction.
    assert calls.count("leading_scope") == 2
    assert calls.index("leading_scope") < calls.index("student")
    order.payment.refresh_from_db()
    assert order.payment.target_training_group_id == group.id
