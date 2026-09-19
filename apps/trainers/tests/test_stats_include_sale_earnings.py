"""After T1, sale earnings (checkin=None) must show up in trainer stats."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing.models import Payment, TrainingType
from apps.billing.services import create_payment, verify_payment
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.selectors import (
    get_salary_summary,
    get_trainer_earnings,
    get_trainer_earnings_summary,
    get_trainer_revenue_summary,
    get_trainer_salary_ledger_rows,
    get_trainers_with_stats,
)
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


@pytest.fixture(autouse=True)
def _disable_payment_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


@pytest.fixture
def group_sale(db, club):
    """Create one confirmed group sale → triggers T1 sale earning."""
    location = LocationFactory(club=club)
    tt = TrainingTypeFactory(club=club, slug="group", kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(
        club=club, training_type=tt, price=Decimal("4000"),
        scope="location", location=location,
    )
    trainer = TrainerFactory(club=club)
    TrainerLocationFactory(
        club=club, trainer=trainer, location=location,
        rate_group=Decimal("20.00"),
    )
    student = StudentFactory(club=club)
    user = UserFactory()

    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        seller_trainer_id=trainer.id,
    )
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
        verify_payment(
            payment_id=payment.id, club_id=club.id,
            verified_by_id=user.id, action="confirm",
        )
    create_sale_earning(payment.id, club.id)
    return {"trainer": trainer, "payment": payment, "amount": Decimal("800.00")}


@pytest.mark.django_db
class TestSaleEarningsInTrainerStats:
    def test_get_trainers_with_stats_counts_sale_earnings(self, club, group_sale):
        today = date.today()
        rows = list(get_trainers_with_stats(
            club=club, date_from=today, date_to=today,
        ))
        target = next(r for r in rows if r.id == group_sale["trainer"].id)
        assert target.monthly_sessions == 1
        assert target.monthly_salary == group_sale["amount"]

    def test_get_trainer_earnings_includes_sale(self, club, group_sale):
        today = date.today()
        qs = get_trainer_earnings(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=today, date_to=today,
        )
        assert qs.count() == 1
        e = qs.first()
        assert e.checkin_id is None
        assert e.payment_id == group_sale["payment"].id

    def test_get_trainer_earnings_summary_total_includes_sale(self, club, group_sale):
        today = date.today()
        s = get_trainer_earnings_summary(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=today, date_to=today,
        )
        assert s["total_amount"] == group_sale["amount"]
        assert s["total_sessions"] == 1

    def test_get_salary_summary_includes_sale(self, club, group_sale):
        today = date.today()
        rows = get_salary_summary(club=club, date_from=today, date_to=today)
        target = next(r for r in rows if r["trainer_id"] == group_sale["trainer"].id)
        assert target["total"] == group_sale["amount"]
        assert target["sessions"] == 1

    def test_sale_earnings_use_club_local_day_boundary(self, club, group_sale):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        local_sale_day = date(2026, 6, 29)
        Payment.objects.filter(id=group_sale["payment"].id).update(
            verified_at=datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
        )

        stats_rows = list(
            get_trainers_with_stats(
                club=club,
                date_from=local_sale_day,
                date_to=local_sale_day,
            )
        )
        target_stats = next(row for row in stats_rows if row.id == group_sale["trainer"].id)
        earnings = get_trainer_earnings(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=local_sale_day,
            date_to=local_sale_day,
        )
        summary = get_trainer_earnings_summary(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=local_sale_day,
            date_to=local_sale_day,
        )
        salary_rows = get_salary_summary(
            club=club,
            date_from=local_sale_day,
            date_to=local_sale_day,
        )
        ledger_rows = get_trainer_salary_ledger_rows(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=local_sale_day,
            date_to=local_sale_day,
        )

        assert target_stats.monthly_sessions == 1
        assert target_stats.monthly_salary == group_sale["amount"]
        assert earnings.count() == 1
        assert summary["total_amount"] == group_sale["amount"]
        assert summary["total_sessions"] == 1
        assert next(row for row in salary_rows if row["trainer_id"] == group_sale["trainer"].id)[
            "sessions"
        ] == 1
        assert ledger_rows[0].checkin_date == local_sale_day.isoformat()

    def test_period_excludes_old_sale(self, club, group_sale):
        # Move the verified_at backwards by 60 days, then query for "today"
        Payment.objects.filter(id=group_sale["payment"].id).update(
            verified_at=timezone.now() - timedelta(days=60),
        )
        today = date.today()
        s = get_trainer_earnings_summary(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=today, date_to=today,
        )
        assert s["total_amount"] == 0
        assert s["total_sessions"] == 0


@pytest.mark.django_db
class TestTrainerRevenueSummary:
    def test_revenue_counts_full_payment_amount_for_sale(self, club, group_sale):
        today = date.today()
        r = get_trainer_revenue_summary(
            club=club,
            trainer_id=group_sale["trainer"].id,
            date_from=today, date_to=today,
        )
        # Trainer brought in the FULL 4000 (not the 800 cut): 800 × 100 / 20
        assert r["total_revenue"] == Decimal("4000.00")
        assert r["sales_count"] == 1

    def test_revenue_includes_personal_checkin_earnings(self, club):
        """Revenue = deal value, not just group sales. Personal classes count too."""
        from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
        from apps.attendance.tasks import calculate_salary
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
        from apps.billing.tests.factories import SubscriptionFactory

        location = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club, slug="personal", kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(
            club=club, training_type=tt, price=Decimal("2000"),
            scope="location", location=location,
        )
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(
            club=club, trainer=trainer, location=location,
            rate_personal=Decimal("50.00"),
        )
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, trainer=trainer, location=location)
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=tt, subscription=sub, location=location,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)

        today = date.today()
        r = get_trainer_revenue_summary(
            club=club, trainer_id=trainer.id,
            date_from=today, date_to=today,
        )
        # Earning = 2000 × 50% = 1000. Revenue = 1000 × 100 / 50 = 2000.
        assert r["total_revenue"] == Decimal("2000.00")
        assert r["sales_count"] == 1

    def test_revenue_excludes_other_trainers(self, club, group_sale):
        other = TrainerFactory(club=club)
        today = date.today()
        r = get_trainer_revenue_summary(
            club=club, trainer_id=other.id,
            date_from=today, date_to=today,
        )
        assert r["total_revenue"] == 0
        assert r["sales_count"] == 0
