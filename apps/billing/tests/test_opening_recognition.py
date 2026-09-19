from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.billing.models import Payment, Tariff, TrainingType
from apps.billing.recognition import payment_recognition_date
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.dashboard.selectors import get_dashboard_metrics
from apps.dashboard.services import get_pnl_report
from apps.trainers.models import TrainerEarning
from apps.trainers.selectors import (
    get_salary_summary,
    get_trainer_earnings,
    get_trainer_salary_ledger_rows,
    get_trainers_with_stats,
)
from apps.trainers.services import close_trainer_payroll_period, correct_trainer_earning
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def opening(club):
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone"])
    trainer = TrainerFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("6500.00"))
    subscription = SubscriptionFactory(club=club, tariff=tariff, trainings_left=7, trainings_used=5)
    component = SubscriptionComponentFactory(
        club=club, subscription=subscription, training_type=training_type,
        credits_total=12, credits_used=5, credits_left=7,
        paid_amount_basis_snapshot=Decimal("6500.00"),
        trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
        sale_trainer_id_snapshot=trainer.id,
        sale_rate_percent_snapshot=Decimal("20.00"),
        sale_snapshot_provenance=Payment.SaleSnapshotProvenance.OPENING_REVIEWED,
    )
    payment = PaymentFactory(
        club=club, student=subscription.student, tariff=tariff, subscription=subscription,
        origin=Payment.Origin.OPENING, status=Payment.Status.CONFIRMED,
        opening_effective_on=date(2026, 9, 1),
        opening_source_namespace="synthetic-s0", opening_source_key="payment-group-01",
        opening_provenance={"reviewed": True},
        verified_at=datetime(2026, 9, 8, 5, tzinfo=UTC),
        seller_trainer=trainer,
    )
    return payment, component, trainer


def test_opening_reports_and_close_share_original_week(club, opening):
    payment, component, trainer = opening
    create_sale_earning(payment.id, club.id)
    create_sale_earning(payment.id, club.id)
    assert TrainerEarning.objects.for_club(club).filter(payment=payment).count() == 1
    assert payment_recognition_date(payment=payment) == date(2026, 9, 1)
    original = dict(club=club, date_from=date(2026, 8, 31), date_to=date(2026, 9, 6))
    imported = dict(club=club, date_from=date(2026, 9, 7), date_to=date(2026, 9, 13))
    for period, amount in [(original, Decimal("1300.00")), (imported, Decimal("0.00"))]:
        pnl = get_pnl_report(**period)
        assert pnl["salary_expenses"] == amount
        assert pnl["gross_income"] == amount * 5
        assert get_dashboard_metrics(**period)["revenue"] == amount * 5
        rows = get_trainer_earnings(**period, trainer_id=trainer.id)
        assert sum((row.amount for row in rows), Decimal("0")) == amount
        assert sum((row["total"] for row in get_salary_summary(**period)), Decimal("0")) == amount
        stats = get_trainers_with_stats(**period).get(id=trainer.id)
        assert stats.monthly_salary == amount
    ledger = get_trainer_salary_ledger_rows(**original, trainer_id=trainer.id)
    assert ledger[0].sort_date == date(2026, 9, 1)
    closed = close_trainer_payroll_period(
        club_id=club.id, period_start=original["date_from"], period_end=original["date_to"],
        reason="Synthetic acceptance", actor_user_id=payment.recorded_by_id,
    )
    assert closed.salary_total_snapshot == Decimal("1300.00")
    component.refresh_from_db()
    assert (component.credits_total, component.credits_used, component.credits_left) == (12, 5, 7)


def test_pending_opening_earning_blocks_original_period_close(club, opening):
    payment, _, _ = opening
    with pytest.raises(BusinessLogicError) as error:
        close_trainer_payroll_period(
            club_id=club.id, period_start=date(2026, 8, 31), period_end=date(2026, 9, 6),
            reason="Synthetic acceptance", actor_user_id=payment.recorded_by_id,
        )
    assert error.value.code == "payroll_close_pending_salary"


def test_opening_earning_cannot_bypass_closed_source_period(club, opening):
    from apps.trainers.models import TrainerPayrollPeriodClose

    payment, _, _ = opening
    TrainerPayrollPeriodClose.objects.create(
        club=club, period_start=date(2026, 8, 31), period_end=date(2026, 9, 6),
        reason="Synthetic closed source", closed_by_id=payment.recorded_by_id,
    )
    with pytest.raises(BusinessLogicError):
        create_sale_earning(payment.id, club.id)
    assert not TrainerEarning.objects.for_club(club).filter(payment=payment).exists()


def test_ordinary_recognition_retains_local_verified_date(club):
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone"])
    payment = PaymentFactory(
        club=club, status=Payment.Status.CONFIRMED,
        verified_at=datetime(2026, 9, 6, 21, tzinfo=UTC),
    )
    assert payment_recognition_date(payment=payment) == date(2026, 9, 7)
    assert get_pnl_report(club=club, date_from=date(2026, 9, 6), date_to=date(2026, 9, 6))["income"] == 0
    assert get_pnl_report(
        club=club, date_from=date(2026, 9, 7), date_to=date(2026, 9, 7),
    )["income"] == payment.amount


def test_opening_provenance_is_immutable_and_key_unique(club, opening):
    payment, _, _ = opening
    with pytest.raises(ValidationError):
        Payment.objects.for_club(club).filter(id=payment.id).update(opening_effective_on=date(2026, 9, 8))
    payment.opening_effective_on = date(2026, 9, 8)
    with pytest.raises(ValidationError):
        payment.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentFactory(
            club=club, origin=Payment.Origin.OPENING, status=Payment.Status.CONFIRMED,
            opening_effective_on=date(2026, 9, 1), verified_at=datetime(2026, 9, 8, tzinfo=UTC),
            opening_source_namespace="synthetic-s0", opening_source_key="payment-group-01",
        )


@pytest.mark.parametrize("fields", [
    {"opening_effective_on": date(2026, 9, 1)},
    {"payment_method": "unknown"},
    {"opening_provenance": {"reviewed": True}},
    {"origin": "opening"},
])
def test_database_rejects_inconsistent_payment_origin(club, fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentFactory(club=club, **fields)


def test_missing_ordinary_verification_is_not_today(club):
    payment = PaymentFactory(club=club, verified_at=None)
    assert payment_recognition_date(payment=payment) is None


def test_opening_correction_uses_source_date_and_retains_original_snapshots(club, opening):
    payment, component, _ = opening
    create_sale_earning(payment.id, club.id)
    target = TrainerFactory(club=club)
    earning = TrainerEarning.objects.for_club(club).get(payment=payment)
    correct_trainer_earning(
        club_id=club.id, earning_id=earning.id, target_trainer_id=target.id,
        reason="Synthetic recipient correction", actor_user_id=payment.recorded_by_id,
        idempotency_key="opening-recipient-correction",
    )
    from apps.trainers.models import TrainerEarningAdjustment

    adjustments = TrainerEarningAdjustment.objects.for_club(club).filter(source_payment=payment)
    assert adjustments.count() == 2
    assert set(adjustments.values_list("effective_date", flat=True)) == {date(2026, 9, 1)}
    component.refresh_from_db()
    assert component.sale_trainer_id_snapshot == earning.trainer_id
    assert component.sale_rate_percent_snapshot == Decimal("20.00")


def test_opening_worker_requires_reviewed_snapshot_provenance(club, opening):
    payment, component, _ = opening
    component.sale_snapshot_provenance = Payment.SaleSnapshotProvenance.CONFIRM_TIME
    component.save(update_fields=["sale_snapshot_provenance"])
    with pytest.raises(BusinessLogicError) as error:
        create_sale_earning(payment.id, club.id)
    assert error.value.code == "sale_earning_snapshot_missing"
    assert not TrainerEarning.objects.for_club(club).filter(payment=payment).exists()


def test_opening_history_does_not_leak_into_other_club_reports(opening, other_club):
    payment, _, _ = opening
    create_sale_earning(payment.id, payment.club_id)
    period = dict(club=other_club, date_from=date(2026, 8, 31), date_to=date(2026, 9, 6))
    assert get_pnl_report(**period)["income"] == 0
    assert get_dashboard_metrics(**period)["revenue"] == 0
    assert get_salary_summary(**period) == []


@pytest.mark.parametrize("owner", [
    "_capture_sale_earning_snapshot", "_capture_sale_earning_snapshots_for_components",
])
def test_ordinary_snapshot_capture_cannot_rewrite_opening_terms(club, opening, owner):
    from apps.billing.service_modules import sale_earnings

    payment, component, _ = opening
    with pytest.raises(BusinessLogicError) as error:
        getattr(sale_earnings, owner)(payment=payment, subscription=payment.subscription, club_id=club.id)
    assert error.value.code == "opening_snapshot_immutable"
    component.refresh_from_db()
    assert component.sale_snapshot_provenance == Payment.SaleSnapshotProvenance.OPENING_REVIEWED
