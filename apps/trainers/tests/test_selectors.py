from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.tests.factories import TrainingTypeFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarningAdjustment
from apps.trainers.selectors import get_salary_summary, get_trainer_earnings, get_trainer_salary_ledger_rows
from apps.trainers.tests.factories import TrainerEarningFactory, TrainerFactory


@pytest.mark.django_db
class TestGetTrainerEarnings:
    def test_get_trainer_earnings(self, club):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        training_type = TrainingTypeFactory(club=club)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=club, trainer=trainer, checkin=checkin, amount=Decimal("1000.00"))

        earnings = get_trainer_earnings(
            club=club,
            trainer_id=trainer.id,
            date_from=date.today() - timedelta(days=1),
            date_to=date.today() + timedelta(days=1),
        )
        assert earnings.count() == 1
        assert earnings.first().amount == Decimal("1000.00")

    def test_get_trainer_earnings_excludes_cancelled(self, club):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        training_type = TrainingTypeFactory(club=club)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=club, trainer=trainer, checkin=checkin, amount=Decimal("1000.00"), cancelled=True)

        earnings = get_trainer_earnings(
            club=club,
            trainer_id=trainer.id,
            date_from=date.today() - timedelta(days=1),
            date_to=date.today() + timedelta(days=1),
        )
        assert earnings.count() == 0

    def test_salary_ledger_includes_manual_adjustment_without_earnings(self, club, other_club):
        target_date = date.today()
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=other_club)
        adjustment = TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=trainer,
            amount_basis_snapshot=Decimal("0.00"),
            payable_amount_delta=Decimal("750.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=target_date,
            reason="manual bonus",
        )
        TrainerEarningAdjustment.objects.create(
            club=other_club,
            trainer=other_trainer,
            amount_basis_snapshot=Decimal("0.00"),
            payable_amount_delta=Decimal("900.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=target_date,
            reason="foreign bonus",
        )

        rows = get_trainer_salary_ledger_rows(
            club=club,
            trainer_id=trainer.id,
            date_from=target_date,
            date_to=target_date,
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.id == adjustment.id
        assert row.row_type == "adjustment"
        assert row.earning_type == "manual_adjustment"
        assert row.amount == Decimal("750.00")
        assert row.checkin_date == target_date.isoformat()
        assert row.adjustment_direction == TrainerEarningAdjustment.Direction.CREDIT
        assert row.adjustment_reason == "manual bonus"


@pytest.mark.django_db
class TestGetSalarySummary:
    def test_get_salary_summary(self, club):
        trainer1 = TrainerFactory(club=club)
        trainer2 = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer1)
        training_type = TrainingTypeFactory(club=club)

        for trainer in [trainer1, trainer2]:
            student = StudentFactory(club=club)
            checkin = CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                training_type=training_type,
                date=date.today(),
            )
            TrainerEarningFactory(club=club, trainer=trainer, checkin=checkin, amount=Decimal("1000.00"))

        summary = get_salary_summary(
            club=club,
            date_from=date.today() - timedelta(days=1),
            date_to=date.today() + timedelta(days=1),
        )
        assert len(summary) == 2

    def test_salary_tenant_isolation(self, club, other_club):
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=other_club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        other_schedule = ScheduleFactory(club=other_club, trainer=other_trainer)
        training_type = TrainingTypeFactory(club=club)
        other_training_type = TrainingTypeFactory(club=other_club)

        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=club, trainer=trainer, checkin=checkin)

        other_student = StudentFactory(club=other_club)
        other_checkin = CheckinFactory(
            club=other_club,
            student=other_student,
            schedule=other_schedule,
            training_type=other_training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=other_club, trainer=other_trainer, checkin=other_checkin)

        summary = get_salary_summary(
            club=club,
            date_from=date.today() - timedelta(days=1),
            date_to=date.today() + timedelta(days=1),
        )
        assert len(summary) == 1

    def test_salary_summary_includes_manual_adjustment_deltas(self, club):
        target_date = date.today()
        source_trainer = TrainerFactory(club=club, first_name="Source")
        target_trainer = TrainerFactory(club=club, first_name="Target")
        checkin = CheckinFactory(club=club, trainer=source_trainer, date=target_date)
        earning = TrainerEarningFactory(
            club=club,
            trainer=source_trainer,
            checkin=checkin,
            amount=Decimal("3000.00"),
        )
        group_id = "11111111-1111-4111-8111-111111111111"
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=source_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("-3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=target_date,
            source_checkin=checkin,
            counterparty_trainer=target_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="selector-correction",
        )
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=target_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=target_date,
            source_checkin=checkin,
            counterparty_trainer=source_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="selector-correction",
        )

        rows = get_salary_summary(club=club, date_from=target_date, date_to=target_date)
        by_trainer = {row["trainer_id"]: row for row in rows}

        assert by_trainer[source_trainer.id]["total"] == Decimal("0.00")
        assert by_trainer[source_trainer.id]["sessions"] == 1
        assert by_trainer[target_trainer.id]["total"] == Decimal("3000.00")
        assert by_trainer[target_trainer.id]["sessions"] == 0
