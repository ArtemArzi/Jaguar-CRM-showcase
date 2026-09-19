from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.attendance.tests.factories import CheckinFactory
from apps.billing.models import Payment, Subscription
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
)
from apps.clubs.timezones import club_local_day_start
from apps.dashboard.selectors import get_attention_alerts, get_dashboard_metrics
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestDashboardMetrics:
    def test_dashboard_metrics_returns_6_keys(self, club):
        today = date.today()
        result = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        assert set(result.keys()) == {
            "active_subscriptions",
            "expiring_subscriptions",
            "debtors",
            "checkins",
            "revenue",
            "new_students",
        }

    def test_dashboard_active_subscription_uses_strict_dst_expiry_boundary(self, club):
        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        student = StudentFactory(club=club)
        expiry = club_local_day_start(club, date(2027, 3, 16))
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=expiry,
        )

        with patch("apps.dashboard.selectors.timezone.now", return_value=expiry - timedelta(seconds=1)):
            last_valid = get_dashboard_metrics(club=club, date_from=date(2027, 3, 15), date_to=date(2027, 3, 15))
        with patch("apps.dashboard.selectors.timezone.now", return_value=expiry):
            expiry_day = get_dashboard_metrics(club=club, date_from=date(2027, 3, 16), date_to=date(2027, 3, 16))

        assert last_valid["active_subscriptions"] == 1
        assert expiry_day["active_subscriptions"] == 0

    def test_snapshot_metrics_ignore_period(self, club):
        """Snapshot metrics (active/expiring/debtors) are the same for any date range."""
        student = StudentFactory(club=club)
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )

        today = date.today()
        month_ago = today - timedelta(days=30)

        result_today = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        result_month = get_dashboard_metrics(club=club, date_from=month_ago, date_to=today)

        assert result_today["active_subscriptions"] == result_month["active_subscriptions"]
        assert result_today["expiring_subscriptions"] == result_month["expiring_subscriptions"]
        assert result_today["debtors"] == result_month["debtors"]

    def test_dashboard_debtors_ignore_reserved_debts(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        payment = PaymentFactory(club=club, student=student, status=Payment.Status.PENDING)
        DebtFactory(club=club, student=student, checkin=checkin, settlement_payment=payment)

        today = date.today()
        result = get_dashboard_metrics(club=club, date_from=today, date_to=today)

        assert result["debtors"] == 0

    def test_period_metrics_filter_by_date(self, club):
        """Period metrics (checkins/revenue/new_students) change based on date range."""
        today = date.today()
        yesterday = today - timedelta(days=1)

        # Checkin today
        CheckinFactory(club=club, date=today)
        # Checkin yesterday
        CheckinFactory(club=club, date=yesterday)

        result_today = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        result_both = get_dashboard_metrics(club=club, date_from=yesterday, date_to=today)

        assert result_today["checkins"] == 1
        assert result_both["checkins"] == 2

    def test_dashboard_metrics_tenant_isolation(self, club, other_club):
        """Metrics from other club not included."""
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

        today = date.today()
        result = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        assert result["active_subscriptions"] == 1

    def test_revenue_sums_confirmed_payments(self, club):
        today = date.today()
        verified_at = timezone.make_aware(datetime.combine(today, time(12, 0)))
        first = PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("3000"))
        second = PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("2000"))
        Payment.objects.filter(id__in=[first.id, second.id]).update(verified_at=verified_at)
        PaymentFactory(club=club, status=Payment.Status.PENDING, amount=Decimal("9999"))

        result = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        assert result["revenue"] == Decimal("5000")

    def test_revenue_uses_payment_verified_at_not_created_at(self, club):
        january_from = date(2026, 1, 1)
        january_to = date(2026, 1, 31)
        february_from = date(2026, 2, 1)
        february_to = date(2026, 2, 28)
        created_at = timezone.make_aware(datetime(2026, 1, 31, 23, 30))
        verified_at = timezone.make_aware(datetime(2026, 2, 1, 9, 0))

        payment = PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("7500"),
        )
        Payment.objects.filter(id=payment.id).update(
            created_at=created_at,
            verified_at=verified_at,
        )

        january = get_dashboard_metrics(club=club, date_from=january_from, date_to=january_to)
        february = get_dashboard_metrics(club=club, date_from=february_from, date_to=february_to)

        assert january["revenue"] == Decimal("0")
        assert february["revenue"] == Decimal("7500")

    def test_period_metrics_use_club_local_day_bounds(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        local_zone = ZoneInfo("Asia/Yekaterinburg")
        target_date = date(2026, 3, 15)
        included_at = datetime(2026, 3, 15, 0, 30, tzinfo=local_zone)
        excluded_at = datetime(2026, 3, 16, 0, 15, tzinfo=local_zone)

        tariff = TariffFactory(club=club)
        included_student = StudentFactory(club=club)
        excluded_student = StudentFactory(club=club)
        included_payment = PaymentFactory(
            club=club,
            student=included_student,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("3000"),
        )
        excluded_payment = PaymentFactory(
            club=club,
            student=excluded_student,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("9000"),
        )
        Payment.objects.filter(id=included_payment.id).update(verified_at=included_at)
        Payment.objects.filter(id=excluded_payment.id).update(verified_at=excluded_at)
        Student.objects.filter(id=included_student.id).update(created_at=included_at)
        Student.objects.filter(id=excluded_student.id).update(created_at=excluded_at)

        result = get_dashboard_metrics(club=club, date_from=target_date, date_to=target_date)

        assert result["revenue"] == Decimal("3000")
        assert result["new_students"] == 1


@pytest.mark.django_db
class TestAttentionAlerts:
    def test_attention_alerts_expiring_subs(self, club):
        """Subs with expires_at <=3d from now appear in alerts."""
        student = StudentFactory(club=club)
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=2),
        )
        # Sub expiring in 5 days -- should NOT appear
        SubscriptionFactory(
            club=club,
            student=StudentFactory(club=club),
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=5),
        )

        alerts = get_attention_alerts(club=club)
        expiring = [a for a in alerts if a["type"] == "expiring_subscriptions"]
        assert len(expiring) == 1
        assert expiring[0]["count"] == 1

    def test_attention_alerts_unconfirmed_payments(self, club):
        """Payments with status=pending and created_at >12h ago appear."""
        old_payment = PaymentFactory(club=club, status=Payment.Status.PENDING)
        # Manually set created_at to >12h ago
        Payment.objects.filter(id=old_payment.id).update(created_at=timezone.now() - timedelta(hours=13))
        # Recent pending payment -- should NOT appear
        PaymentFactory(club=club, status=Payment.Status.PENDING)

        alerts = get_attention_alerts(club=club)
        unconfirmed = [a for a in alerts if a["type"] == "unconfirmed_payments"]
        assert len(unconfirmed) == 1
        assert unconfirmed[0]["count"] == 1

    def test_attention_alerts_at_risk_students(self, club):
        """Students with status=at_risk appear."""
        StudentFactory(club=club, status="at_risk")
        StudentFactory(club=club, status="active")

        alerts = get_attention_alerts(club=club)
        at_risk = [a for a in alerts if a["type"] == "at_risk_students"]
        assert len(at_risk) == 1
        assert at_risk[0]["count"] == 1

    def test_attention_alerts_overdue_retention(self, club):
        """Unresolved RetentionTasks with due_date < today appear."""
        RetentionTaskFactory(
            club=club,
            due_date=date.today() - timedelta(days=2),
        )
        # Future task -- should NOT appear
        RetentionTaskFactory(
            club=club,
            due_date=date.today() + timedelta(days=2),
        )

        alerts = get_attention_alerts(club=club)
        overdue = [a for a in alerts if a["type"] == "overdue_retention_tasks"]
        assert len(overdue) == 1
        assert overdue[0]["count"] == 1
