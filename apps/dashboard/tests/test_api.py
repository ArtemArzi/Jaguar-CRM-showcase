from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from ninja.testing import TestClient

from apps.billing.models import Payment, Subscription
from apps.billing.tests.factories import (
    ExpenseFactory,
    PaymentFactory,
    SubscriptionFactory,
)
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestDashboardMetricsAPI:
    def test_dashboard_metrics_api(self, club, owner_user):
        today = date.today()
        response = client.get(
            f"/dashboard/metrics/?date_from={today}&date_to={today}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {
            "active_subscriptions",
            "expiring_subscriptions",
            "debtors",
            "checkins",
            "revenue",
            "new_students",
        }

    def test_dashboard_alerts_api(self, club, owner_user):
        response = client.get(
            "/dashboard/alerts/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_pnl_api(self, club, owner_user):
        today = date.today()
        PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("5000"))

        response = client.get(
            f"/dashboard/pnl/?date_from={today}&date_to={today}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert "gross_income" in data
        assert "refunded_income" in data
        assert "income" in data
        assert "margin" in data

    def test_business_metrics_api(self, club, owner_user):
        today = date.today()
        response = client.get(
            f"/dashboard/business-metrics/?date_from={today}&date_to={today}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert "arpm" in data
        assert "churn_rate" in data

    def test_dashboard_requires_owner_role(self, club, trainer_user):
        today = date.today()
        response = client.get(
            f"/dashboard/metrics/?date_from={today}&date_to={today}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_dashboard_isolated(self, club, other_club, owner_user):
        """Two clubs, only own club data visible."""
        today = date.today()
        from django.utils import timezone

        student = StudentFactory(club=club)
        other_student = StudentFactory(club=other_club)
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )
        SubscriptionFactory(
            club=other_club,
            student=other_student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )

        response = client.get(
            f"/dashboard/metrics/?date_from={today}&date_to={today}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["active_subscriptions"] == 1


@pytest.mark.django_db
class TestExpenseAPI:
    def test_create_expense(self, club, owner_user):
        today = str(date.today())
        response = client.post(
            "/billing/expenses/",
            json={"name": "Rent", "amount": "50000", "date": today, "category": "rent"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Rent"
        assert Decimal(data["amount"]) == Decimal("50000")

    def test_list_expenses(self, club, owner_user):
        ExpenseFactory(club=club)
        ExpenseFactory(club=club)
        response = client.get(
            "/billing/expenses/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_update_expense(self, club, owner_user):
        expense = ExpenseFactory(club=club, name="Old Name")
        response = client.patch(
            f"/billing/expenses/{expense.id}/",
            json={"name": "New Name"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["name"] == "New Name"

    def test_delete_expense(self, club, owner_user):
        expense = ExpenseFactory(club=club)
        response = client.delete(
            f"/billing/expenses/{expense.id}/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 204
