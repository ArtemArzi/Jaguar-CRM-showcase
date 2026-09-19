"""Tests for admin push verification flow (D-04, D-08)."""

from unittest.mock import patch

import pytest
from ninja.testing import TestClient

from apps.billing.models import Payment
from apps.billing.tests.factories import (
    PaymentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import ClubMembershipFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.notifications.tests.factories import PushSubscriptionFactory
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestSendPushWithActions:
    """Verify send_push_to_user passes actions and data to async_task."""

    @patch("apps.notifications.services.async_task")
    def test_push_with_actions_parameter(self, mock_async):
        user = UserFactory()
        PushSubscriptionFactory(user=user)
        actions = [
            {"action": "confirm", "title": "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c"},
            {"action": "reject", "title": "\u041e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c"},
        ]

        from apps.notifications.services import send_push_to_user

        send_push_to_user(
            user_id=user.id,
            title=(
                "\u0412\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044f "
                "\u043e\u043f\u043b\u0430\u0442\u044b"
            ),
            body="Test -- 1000 \u0440\u0443\u0431.",
            actions=actions,
        )

        mock_async.assert_called_once()
        call_args = mock_async.call_args[0]
        # args: task_path, sub_id, title, body, url, actions, data
        assert call_args[5] == actions

    @patch("apps.notifications.services.async_task")
    def test_push_with_data_parameter(self, mock_async):
        user = UserFactory()
        PushSubscriptionFactory(user=user)
        data = {"payment_id": 42, "tag": "payment-verify"}

        from apps.notifications.services import send_push_to_user

        send_push_to_user(
            user_id=user.id,
            title="\u0412\u0435\u0440\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044f",
            body="Test",
            data=data,
        )

        mock_async.assert_called_once()
        call_args = mock_async.call_args[0]
        assert call_args[6] == data


@pytest.mark.django_db
class TestPaymentVerifyEndpoint:
    """Test payment verify endpoint used by admin SW action buttons."""

    def test_confirm_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            recorded_by=owner_user,
        )

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "confirmed"
        payment.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED

    def test_reject_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            recorded_by=owner_user,
        )

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "reject", "rejection_reason": "receipt mismatch"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        payment.refresh_from_db()
        assert payment.status == Payment.Status.REJECTED

    def test_verify_requires_owner_role(self, club):
        """Non-owner (trainer) gets 403 on verify endpoint."""
        trainer = UserFactory()
        ClubMembershipFactory(user=trainer, club=club, role="trainer")
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            recorded_by=trainer,
        )

        response = client.post(
            f"/billing/payments/{payment.id}/verify/",
            json={"action": "confirm"},
            **_auth_params(trainer, club, role="trainer"),
        )

        assert response.status_code == 403
