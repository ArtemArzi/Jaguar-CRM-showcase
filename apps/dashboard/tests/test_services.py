from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.billing.models import Payment, Subscription
from apps.billing.tests.factories import (
    ExpenseFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
)
from apps.dashboard.services import get_business_metrics, get_pnl_report
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment
from apps.trainers.tests.factories import TrainerEarningFactory, TrainerFactory


@pytest.mark.django_db
class TestPnlReport:
    def test_pnl_report(self, club):
        """income - salary - expenses = margin."""
        today = date.today()
        today_at = timezone.make_aware(datetime.combine(today, time(12, 0)))
        PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("10000"), verified_at=today_at)
        TrainerEarningFactory(club=club, amount=Decimal("3000"))
        ExpenseFactory(club=club, amount=Decimal("2000"), date=today)

        result = get_pnl_report(club=club, date_from=today, date_to=today)
        assert result["income"] == Decimal("10000")
        assert result["salary_expenses"] == Decimal("3000")
        assert result["manual_expenses"] == Decimal("2000")
        assert result["margin"] == Decimal("5000")

    def test_pnl_groups_confirmed_cash_and_transfer_by_recorded_method(self, club):
        today = date.today()
        verified_at = timezone.make_aware(datetime.combine(today, time(12, 0)))
        PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            payment_method=Payment.Method.CASH,
            amount=Decimal("3000"),
            verified_at=verified_at,
        )
        PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            payment_method=Payment.Method.TRANSFER,
            amount=Decimal("7000"),
            verified_at=verified_at,
        )

        result = get_pnl_report(club=club, date_from=today, date_to=today)

        assert result["income"] == Decimal("10000")
        assert result["income_breakdown"] == [
            {"label": "Перевод", "amount": Decimal("7000")},
            {"label": "Наличные", "amount": Decimal("3000")},
        ]

    def test_recurring_expenses_in_pnl(self, club):
        """Recurring expenses included for each month in period."""
        # 3-month range
        date_from = date(2026, 1, 1)
        date_to = date(2026, 3, 31)
        ExpenseFactory(club=club, amount=Decimal("1000"), is_recurring=True, date=date(2026, 1, 1))
        # One-time expense within range
        ExpenseFactory(club=club, amount=Decimal("500"), is_recurring=False, date=date(2026, 2, 15))

        result = get_pnl_report(club=club, date_from=date_from, date_to=date_to)
        # recurring: 1000 * 3 months = 3000, one-time: 500
        assert result["manual_expenses"] == Decimal("3500")

    def test_pnl_salary_expenses_include_period_sale_earnings(self, club):
        """Sale earnings use payment verification date when no check-in exists."""
        date_from = date(2026, 1, 1)
        date_to = date(2026, 1, 31)
        in_period = timezone.make_aware(datetime(2026, 1, 10, 12, 0))
        out_of_period = timezone.make_aware(datetime(2026, 2, 1, 12, 0))

        in_period_payment = PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("10000"),
            original_amount=Decimal("10000"),
        )
        out_of_period_payment = PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("7000"),
            original_amount=Decimal("7000"),
        )
        Payment.objects.filter(id=in_period_payment.id).update(
            created_at=in_period,
            verified_at=in_period,
        )
        Payment.objects.filter(id=out_of_period_payment.id).update(
            created_at=out_of_period,
            verified_at=out_of_period,
        )

        TrainerEarningFactory(
            club=club,
            checkin=None,
            payment=in_period_payment,
            earning_source=TrainerEarning.Source.SALE,
            amount=Decimal("1000"),
            rate_percent=Decimal("20.00"),
            subscription_price=Decimal("10000"),
        )
        TrainerEarningFactory(
            club=club,
            checkin=None,
            payment=out_of_period_payment,
            earning_source=TrainerEarning.Source.SALE,
            amount=Decimal("2000"),
            rate_percent=Decimal("20.00"),
            subscription_price=Decimal("7000"),
        )

        result = get_pnl_report(club=club, date_from=date_from, date_to=date_to)

        assert result["income"] == Decimal("10000")
        assert result["salary_expenses"] == Decimal("1000")
        assert result["manual_expenses"] == Decimal("0")
        assert result["total_expenses"] == Decimal("1000")
        assert result["margin"] == Decimal("9000")

    def test_pnl_sale_revenue_and_earnings_use_club_local_day_bounds(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        local_zone = ZoneInfo("Asia/Yekaterinburg")
        target_date = date(2026, 3, 15)
        included_at = datetime(2026, 3, 15, 0, 30, tzinfo=local_zone)
        excluded_at = datetime(2026, 3, 16, 0, 15, tzinfo=local_zone)

        tariff = TariffFactory(club=club)
        included_payment = PaymentFactory(
            club=club,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("10000"),
            original_amount=Decimal("10000"),
        )
        excluded_payment = PaymentFactory(
            club=club,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("9000"),
            original_amount=Decimal("9000"),
        )
        Payment.objects.filter(id=included_payment.id).update(verified_at=included_at)
        Payment.objects.filter(id=excluded_payment.id).update(verified_at=excluded_at)

        TrainerEarningFactory(
            club=club,
            checkin=None,
            payment=included_payment,
            earning_source=TrainerEarning.Source.SALE,
            amount=Decimal("1000"),
            rate_percent=Decimal("10.00"),
            subscription_price=Decimal("10000"),
        )
        TrainerEarningFactory(
            club=club,
            checkin=None,
            payment=excluded_payment,
            earning_source=TrainerEarning.Source.SALE,
            amount=Decimal("900"),
            rate_percent=Decimal("10.00"),
            subscription_price=Decimal("9000"),
        )

        result = get_pnl_report(club=club, date_from=target_date, date_to=target_date)

        assert result["income"] == Decimal("10000")
        assert result["salary_expenses"] == Decimal("1000")
        assert result["margin"] == Decimal("9000")

    def test_pnl_uses_verified_at_for_confirmed_revenue_and_sale_earnings(self, club):
        """Confirmed revenue and related sale earnings share one recognition date."""
        january_from = date(2026, 1, 1)
        january_to = date(2026, 1, 31)
        february_from = date(2026, 2, 1)
        february_to = date(2026, 2, 28)
        created_at = timezone.make_aware(datetime(2026, 1, 31, 23, 30))
        verified_at = timezone.make_aware(datetime(2026, 2, 1, 9, 0))

        delayed_payment = PaymentFactory(
            club=club,
            status=Payment.Status.CONFIRMED,
            amount=Decimal("10000"),
            original_amount=Decimal("10000"),
        )
        Payment.objects.filter(id=delayed_payment.id).update(
            created_at=created_at,
            verified_at=verified_at,
        )
        delayed_payment.refresh_from_db()

        TrainerEarningFactory(
            club=club,
            checkin=None,
            payment=delayed_payment,
            earning_source=TrainerEarning.Source.SALE,
            amount=Decimal("1000"),
            rate_percent=Decimal("10.00"),
            subscription_price=Decimal("10000"),
        )

        january = get_pnl_report(club=club, date_from=january_from, date_to=january_to)
        february = get_pnl_report(club=club, date_from=february_from, date_to=february_to)

        assert january["income"] == Decimal("0")
        assert january["income_breakdown"] == []
        assert january["salary_expenses"] == Decimal("0")
        assert january["margin"] == Decimal("0")
        assert february["income"] == Decimal("10000")
        assert february["income_breakdown"] == [{"label": "Наличные", "amount": Decimal("10000")}]
        assert february["salary_expenses"] == Decimal("1000")
        assert february["margin"] == Decimal("9000")

    def test_pnl_salary_breakdown_includes_manual_adjustment_deltas(self, club):
        today = date.today()
        source_trainer = TrainerFactory(club=club, first_name="Source")
        target_trainer = TrainerFactory(club=club, first_name="Target")
        earning = TrainerEarningFactory(
            club=club,
            trainer=source_trainer,
            amount=Decimal("3000.00"),
            rate_percent=Decimal("50.00"),
        )
        group_id = "22222222-2222-4222-8222-222222222222"
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=source_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("-3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=today,
            source_checkin=earning.checkin,
            counterparty_trainer=target_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="pnl-correction",
        )
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=target_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=today,
            source_checkin=earning.checkin,
            counterparty_trainer=source_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="pnl-correction",
        )

        result = get_pnl_report(club=club, date_from=today, date_to=today)
        by_trainer = {row["trainer_id"]: row for row in result["salary_breakdown"]}

        assert result["salary_expenses"] == Decimal("3000.00")
        assert by_trainer[source_trainer.id]["amount"] == Decimal("0.00")
        assert by_trainer[target_trainer.id]["amount"] == Decimal("3000.00")


@pytest.mark.django_db
class TestBusinessMetrics:
    def test_arpm_calculation(self, club):
        """ARPM = revenue / unique active students."""
        today = date.today()
        student1 = StudentFactory(club=club)
        student2 = StudentFactory(club=club)

        SubscriptionFactory(
            club=club,
            student=student1,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )
        SubscriptionFactory(
            club=club,
            student=student2,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )

        today_at = timezone.make_aware(datetime.combine(today, time(12, 0)))
        PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("6000"), verified_at=today_at)

        result = get_business_metrics(club=club, date_from=today, date_to=today)
        assert result["arpm"] == Decimal("3000")

    def test_churn_rate(self, club):
        """Churn = expired-not-renewed / active-at-start."""
        today = date.today()
        period_start = today - timedelta(days=7)

        student = StudentFactory(club=club)
        # Active at start of period, then expired
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.EXPIRED,
            expires_at=timezone.make_aware(datetime.combine(period_start + timedelta(days=1), time.min)),
        )
        # Override created_at to be before period start
        Subscription.objects.filter(student=student, club=club).update(
            created_at=timezone.make_aware(datetime.combine(period_start - timedelta(days=1), time.min)),
        )

        result = get_business_metrics(club=club, date_from=period_start, date_to=today)
        # 1 expired / 1 active at start = 1.0
        assert result["churn_rate"] is not None
        assert result["churn_rate"] > Decimal("0")

    def test_metrics_empty_club(self, club):
        """Empty club: no members → churn/arpm/ltv all None (no division by zero)."""
        today = date.today()
        result = get_business_metrics(club=club, date_from=today, date_to=today)
        assert result["churn_rate"] is None
        assert result["arpm"] is None
        assert result["ltv"] is None

    def test_ltv_with_zero_churn(self, club):
        """LTV returns None when churn_rate is 0."""
        today = date.today()
        student = StudentFactory(club=club)
        SubscriptionFactory(
            club=club,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=15),
        )
        today_at = timezone.make_aware(datetime.combine(today, time(12, 0)))
        PaymentFactory(club=club, status=Payment.Status.CONFIRMED, amount=Decimal("5000"), verified_at=today_at)

        result = get_business_metrics(club=club, date_from=today, date_to=today)
        # churn_rate = 0, so LTV should be None (can't divide by 0)
        assert result["ltv"] is None

    def test_retention_rate(self, club):
        """retention_rate = 1 - churn_rate."""
        today = date.today()
        result = get_business_metrics(club=club, date_from=today, date_to=today)
        if result["churn_rate"] is not None:
            expected = Decimal("1") - result["churn_rate"]
            assert result["retention_rate"] == expected
