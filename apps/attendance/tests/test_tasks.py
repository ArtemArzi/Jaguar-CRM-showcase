from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.attendance.models import Checkin, GroupSession
from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import (
    calculate_salary,
    log_parent_event,
    reverse_grade_progress,
    reverse_parent_checkin_push,
    reverse_salary,
    update_grade_progress,
    update_group_analytics,
)
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.tests.factories import (
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.grades.models import GradeProgressEvent
from apps.grades.tests.factories import GradeFactory, GradeSystemFactory, StudentGradeFactory
from apps.notifications.models import NotificationPreference, SentNotification
from apps.notifications.tests.factories import PushSubscriptionFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment, TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory, TrainerRateFactory


@pytest.mark.django_db
class TestCalculateSalary:
    def test_calculate_salary_task(self, club):
        from apps.billing.models import TrainingType
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)

        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            rate_personal=Decimal("20.00"),
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            is_debt=False,
        )

        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)

        earning = TrainerEarning.objects.get(checkin=checkin)
        assert earning.amount == Decimal("1000.00")  # 5000 * 20%
        assert earning.rate_percent == Decimal("20.00")
        assert earning.earning_type == "personal"

    def test_calculate_salary_idempotent(self, club):
        from apps.billing.models import TrainingType
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)

        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
        )

        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        calculate_salary(checkin.id, club.id)

        assert TrainerEarning.objects.filter(checkin=checkin).count() == 1

    def test_calculate_salary_rejects_closed_payroll_period(self, club):
        from apps.billing.models import TrainingType
        from apps.trainers.services import close_trainer_payroll_period

        target_date = timezone.localdate()
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=UserFactory().id,
        )
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-closed", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            rate_personal=Decimal("20.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            date=target_date,
            is_debt=False,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)

        with pytest.raises(BusinessLogicError) as exc_info:
            calculate_salary(checkin.id, club.id)

        assert exc_info.value.code == "payroll_period_closed"
        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    def test_calculate_salary_handles_duplicate_create_race(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)

        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
        )
        original_create = TrainerEarning.objects.create

        def create_then_raise(*args, **kwargs):
            original_create(*args, **kwargs)
            raise IntegrityError("duplicate trainer earning")

        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        with patch("apps.attendance.tasks.TrainerEarning.objects.create", side_effect=create_then_raise):
            calculate_salary(checkin.id, club.id)

        assert TrainerEarning.objects.filter(checkin=checkin).count() == 1

    def test_calculate_salary_records_package_transfer_for_different_owner(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        owner_trainer = TrainerFactory(club=club)
        actual_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-transfer", kind=TrainingType.Kind.PERSONAL
        )
        schedule = ScheduleFactory(club=club, trainer=actual_trainer, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("6000.00"))
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerRateFactory(
            club=club,
            trainer=actual_trainer,
            location=schedule.location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=actual_trainer,
            subscription=sub,
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=sub,
            student=student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=owner_trainer,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=sub.trainings_left,
            amount_snapshot=tariff.price,
        )

        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)

        earning = TrainerEarning.objects.get(checkin=checkin)
        transfer = TrainerEarningAdjustment.objects.get(
            source_checkin=checkin,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
        )
        assert earning.trainer == actual_trainer
        assert transfer.trainer == actual_trainer
        assert transfer.counterparty_trainer == owner_trainer
        assert transfer.amount_basis_snapshot == earning.amount
        assert transfer.affects_payroll is False
        assert transfer.payable_amount_delta == Decimal("0.00")

    def test_calculate_salary_repairs_missing_package_transfer_when_earning_exists(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        owner_trainer = TrainerFactory(club=club)
        actual_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-transfer-retry", kind=TrainingType.Kind.PERSONAL
        )
        schedule = ScheduleFactory(club=club, trainer=actual_trainer, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("6000.00"))
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=actual_trainer,
            subscription=sub,
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=sub,
            student=student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=owner_trainer,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=sub.trainings_left,
            amount_snapshot=tariff.price,
        )
        TrainerEarning.objects.create(
            club=club,
            trainer=actual_trainer,
            checkin=checkin,
            earning_type=TrainingType.Kind.PERSONAL,
            amount=Decimal("3000.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=tariff.price,
        )

        calculate_salary(checkin.id, club.id)

        assert TrainerEarningAdjustment.objects.filter(
            source_checkin=checkin,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
        ).count() == 1

    def test_calculate_salary_debt_no_earning(self, club):
        from apps.billing.models import TrainingType
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
            subscription=None,
        )

        calculate_salary(checkin.id, club.id)

        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    def test_calculate_salary_skips_cancelled_checkin(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-cancelled", kind=TrainingType.Kind.PERSONAL
        )
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            rate_personal=Decimal("20.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            cancelled_at=timezone.now(),
            deleted_at=timezone.now(),
        )

        calculate_salary(checkin.id, club.id)

        assert not TrainerEarning.objects.filter(checkin=checkin).exists()


@pytest.mark.django_db
class TestUpdateGradeProgress:
    def test_update_grade_progress_task(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs)

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        update_grade_progress(checkin.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 1

    def test_update_grade_progress_idempotent(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs)

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        update_grade_progress(checkin.id, club.id)
        update_grade_progress(checkin.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 1

    def test_update_grade_progress_skips_old_retry_after_newer_checkin(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs)
        first = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        second = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club),
            training_type=TrainingTypeFactory(club=club, grade_system=gs),
        )

        update_grade_progress(first.id, club.id)
        update_grade_progress(second.id, club.id)
        update_grade_progress(first.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 2
        assert sg.last_counted_checkin_id == second.id

    def test_update_grade_progress_skips_cancelled_checkin_after_reverse(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        update_grade_progress(checkin.id, club.id)
        Checkin.objects.filter(id=checkin.id).update(
            cancelled_at=timezone.now(),
            deleted_at=timezone.now(),
        )
        reverse_grade_progress(checkin.id, club.id)
        update_grade_progress(checkin.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 0

    def test_update_grade_progress_counts_only_mapped_grade_system(self, club):
        student = StudentFactory(club=club)
        bjj_system = GradeSystemFactory(club=club, discipline="BJJ")
        boxing_system = GradeSystemFactory(club=club, discipline="Boxing")
        bjj_training_type = TrainingTypeFactory(club=club, name="BJJ", grade_system=bjj_system)
        bjj_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=bjj_system,
            trainings_since_last_grade=4,
        )
        boxing_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=boxing_system,
            trainings_since_last_grade=7,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=bjj_training_type),
            training_type=bjj_training_type,
        )

        update_grade_progress(checkin.id, club.id)

        bjj_grade.refresh_from_db()
        boxing_grade.refresh_from_db()
        assert bjj_grade.trainings_since_last_grade == 5
        assert boxing_grade.trainings_since_last_grade == 7
        assert GradeProgressEvent.objects.filter(student_grade=bjj_grade, checkin=checkin).exists()
        assert not GradeProgressEvent.objects.filter(student_grade=boxing_grade, checkin=checkin).exists()

    def test_update_grade_progress_skips_unmapped_training_type(self, club):
        student = StudentFactory(club=club)
        grade_system = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=None)
        student_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=grade_system,
            trainings_since_last_grade=3,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )

        update_grade_progress(checkin.id, club.id)

        student_grade.refresh_from_db()
        assert student_grade.trainings_since_last_grade == 3
        assert not GradeProgressEvent.objects.filter(checkin=checkin).exists()


@pytest.mark.django_db
class TestUpdateGroupAnalytics:
    def test_update_group_analytics_task(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        update_group_analytics(checkin.id, club.id)

        session = GroupSession.objects.get(schedule=schedule, date=checkin.date)
        assert session.attendee_count == 1
        assert session.closed_at is None
        assert session.closed_by_id is None


@pytest.mark.django_db
class TestReverseSalary:
    def test_reverse_salary_task(self, club):
        from apps.billing.models import TrainingType
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)

        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
        )

        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        reverse_salary(checkin.id, club.id)

        earning = TrainerEarning.objects.get(checkin=checkin)
        assert earning.cancelled is True

    def test_reverse_salary_rejects_closed_payroll_period(self, club):
        from apps.billing.models import TrainingType
        from apps.trainers.services import close_trainer_payroll_period

        target_date = timezone.localdate()
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-reverse-closed", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=5000)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            rate_personal=Decimal("20.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            date=target_date,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=UserFactory().id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            reverse_salary(checkin.id, club.id)

        earning = TrainerEarning.objects.get(checkin=checkin)
        assert exc_info.value.code == "payroll_period_closed"
        assert earning.cancelled is False

    def test_reverse_salary_idempotent_after_closed_period_when_already_reversed(self, club):
        from apps.billing.models import TrainingType
        from apps.trainers.services import close_trainer_payroll_period

        target_date = timezone.localdate()
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            date=target_date,
            training_type=TrainingTypeFactory(
                club=club,
                slug="personal-reverse-idempotent",
                kind=TrainingType.Kind.PERSONAL,
            ),
        )
        TrainerEarning.objects.create(
            club=club,
            trainer=checkin.trainer,
            checkin=checkin,
            earning_type=TrainingType.Kind.PERSONAL,
            amount=Decimal("1000.00"),
            rate_percent=Decimal("20.00"),
            subscription_price=Decimal("5000.00"),
            cancelled=True,
        )
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=UserFactory().id,
        )

        reverse_salary(checkin.id, club.id)

        assert TrainerEarning.objects.get(checkin=checkin).cancelled is True

    def test_reverse_salary_disables_manual_corrections_for_cancelled_checkin(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        source_trainer = TrainerFactory(club=club)
        target_trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=source_trainer)
        training_type = TrainingTypeFactory(
            club=club,
            slug="personal-manual-correction-cancel",
            kind=TrainingType.Kind.PERSONAL,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=source_trainer,
        )
        earning = TrainerEarning.objects.create(
            club=club,
            trainer=source_trainer,
            checkin=checkin,
            earning_type=TrainingType.Kind.PERSONAL,
            amount=Decimal("3000.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=Decimal("6000.00"),
        )
        group_id = "33333333-3333-4333-8333-333333333333"
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=source_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("-3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=checkin.date,
            source_checkin=checkin,
            counterparty_trainer=target_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="cancelled-checkin-correction",
        )
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=target_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("3000.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=checkin.date,
            source_checkin=checkin,
            counterparty_trainer=source_trainer,
            reason="manual correction",
            correction_group_id=group_id,
            idempotency_key="cancelled-checkin-correction",
        )

        reverse_salary(checkin.id, club.id)

        earning.refresh_from_db()
        assert earning.cancelled is True
        assert not TrainerEarningAdjustment.objects.for_club(club).filter(
            source_checkin=checkin,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            affects_payroll=True,
        ).exists()

    def test_reverse_salary_creates_package_transfer_reversal(self, club):
        from apps.billing.models import TrainingType

        student = StudentFactory(club=club)
        owner_trainer = TrainerFactory(club=club)
        actual_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club, slug="personal-transfer-cancel", kind=TrainingType.Kind.PERSONAL
        )
        schedule = ScheduleFactory(club=club, trainer=actual_trainer, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("6000.00"))
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerRateFactory(
            club=club,
            trainer=actual_trainer,
            location=schedule.location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=actual_trainer,
            subscription=sub,
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=sub,
            student=student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=owner_trainer,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=sub.trainings_left,
            amount_snapshot=tariff.price,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        transfer = TrainerEarningAdjustment.objects.get(
            source_checkin=checkin,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
        )

        reverse_salary(checkin.id, club.id)

        reversal = TrainerEarningAdjustment.objects.get(
            reversal_of=transfer,
            kind=TrainerEarningAdjustment.Kind.REVERSAL,
        )
        assert reversal.affects_payroll is False
        assert reversal.payable_amount_delta == Decimal("0.00")


@pytest.mark.django_db
class TestReverseGradeProgress:
    def test_reverse_grade_progress_reverts_counted_checkin(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs, trainings_since_last_grade=5)

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        update_grade_progress(checkin.id, club.id)
        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 6
        assert GradeProgressEvent.objects.filter(student_grade=sg, checkin=checkin).exists()

        reverse_grade_progress(checkin.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 5
        assert not GradeProgressEvent.objects.filter(student_grade=sg, checkin=checkin).exists()

    def test_reverse_grade_progress_skips_checkin_that_was_never_counted(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        gs = GradeSystemFactory(club=club)
        training_type = TrainingTypeFactory(club=club, grade_system=gs)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs, trainings_since_last_grade=5)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        reverse_grade_progress(checkin.id, club.id)

        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 5

    def test_reverse_grade_progress_reverts_only_recorded_events(self, club):
        student = StudentFactory(club=club)
        bjj_system = GradeSystemFactory(club=club, discipline="BJJ")
        boxing_system = GradeSystemFactory(club=club, discipline="Boxing")
        bjj_training_type = TrainingTypeFactory(club=club, name="BJJ", grade_system=bjj_system)
        bjj_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=bjj_system,
            trainings_since_last_grade=4,
        )
        boxing_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=boxing_system,
            trainings_since_last_grade=7,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=bjj_training_type),
            training_type=bjj_training_type,
        )

        update_grade_progress(checkin.id, club.id)
        reverse_grade_progress(checkin.id, club.id)

        bjj_grade.refresh_from_db()
        boxing_grade.refresh_from_db()
        assert bjj_grade.trainings_since_last_grade == 4
        assert boxing_grade.trainings_since_last_grade == 7
        assert not GradeProgressEvent.objects.filter(checkin=checkin).exists()

    def test_reverse_grade_progress_does_not_decrement_current_period_for_pre_promotion_event(self, club):
        from apps.grades.services import promote_student

        student = StudentFactory(club=club)
        grade_system = GradeSystemFactory(club=club)
        old_grade = GradeFactory(club=club, grade_system=grade_system, order=0)
        new_grade = GradeFactory(club=club, grade_system=grade_system, order=1)
        training_type = TrainingTypeFactory(club=club, grade_system=grade_system)
        student_grade = StudentGradeFactory(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=old_grade,
            trainings_since_last_grade=4,
        )
        pre_promotion_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )
        update_grade_progress(pre_promotion_checkin.id, club.id)
        pre_promotion_event = GradeProgressEvent.objects.get(checkin=pre_promotion_checkin)

        promote_student(club_id=club.id, student_grade_id=student_grade.id, new_grade_id=new_grade.id)
        student_grade.refresh_from_db()
        GradeProgressEvent.objects.filter(id=pre_promotion_event.id).update(
            created_at=student_grade.promoted_at - timedelta(seconds=1),
        )
        post_promotion_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )
        update_grade_progress(post_promotion_checkin.id, club.id)

        reverse_grade_progress(pre_promotion_checkin.id, club.id)

        student_grade.refresh_from_db()
        assert student_grade.trainings_since_last_grade == 1
        assert not GradeProgressEvent.objects.filter(checkin=pre_promotion_checkin).exists()
        assert GradeProgressEvent.objects.filter(checkin=post_promotion_checkin).exists()


@pytest.mark.django_db
class TestLogParentEvent:
    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_push_with_subscription(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Masha")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=10)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff, trainings_used=8)
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=training_type, subscription=sub,
        )

        log_parent_event(checkin.id, club.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["user_id"] == parent.id
        assert call_kwargs["url"] == f"/parent/child/{student.id}"
        assert "Masha" in call_kwargs["body"]
        assert "8 из 10" in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_push_uses_checkin_subscription(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Masha")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=10)
        checkin_sub = SubscriptionFactory(club=club, student=student, tariff=tariff, trainings_used=3)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=checkin_sub,
        )
        SubscriptionFactory(club=club, student=student, tariff=tariff, trainings_used=9)

        log_parent_event(checkin.id, club.id)

        call_kwargs = mock_push.call_args.kwargs
        assert "3 из 10" in call_kwargs["body"]
        assert "9 из 10" not in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_push_skips_cancelled_checkin(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            cancelled_at=timezone.now(),
            deleted_at=timezone.now(),
        )

        log_parent_event(checkin.id, club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_push_no_subscription(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Dima")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=training_type, subscription=None, is_debt=True,
        )

        log_parent_event(checkin.id, club.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert "Dima" in call_kwargs["body"]
        # No progress info when no subscription
        assert "из" not in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_no_parent_user(self, mock_push, club):
        student = StudentFactory(club=club, is_child=True, parent_user=None)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=training_type,
        )

        log_parent_event(checkin.id, club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_checkin_non_child_student(self, mock_push, club):
        student = StudentFactory(club=club, is_child=False)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=training_type,
        )

        log_parent_event(checkin.id, club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_parent_checkin_push_respects_child_checkin_opt_out(self, mock_async, club):
        parent = UserFactory()
        NotificationPreference.objects.create(
            user=parent,
            disabled_categories=["child_checkin"],
        )
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        log_parent_event(checkin.id, club.id)

        mock_async.assert_not_called()
        assert not SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=f"parent_checkin:{checkin.id}",
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_parent_checkin_push_is_idempotent_per_checkin(self, mock_async, club):
        parent = UserFactory()
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        log_parent_event(checkin.id, club.id)
        log_parent_event(checkin.id, club.id)

        assert mock_async.call_count == 1
        assert SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=f"parent_checkin:{checkin.id}",
        ).count() == 1

    @patch("apps.notifications.services.async_task")
    def test_parent_checkin_push_sends_each_distinct_checkin_same_day(self, mock_async, club):
        parent = UserFactory()
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        training_type = TrainingTypeFactory(club=club)
        first_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )
        second_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )

        log_parent_event(first_checkin.id, club.id)
        log_parent_event(second_checkin.id, club.id)

        assert mock_async.call_count == 2
        assert set(
            SentNotification.objects.filter(club=club, student=student).values_list(
                "notification_type",
                flat=True,
            )
        ) == {
            f"parent_checkin:{first_checkin.id}",
            f"parent_checkin:{second_checkin.id}",
        }


@pytest.mark.django_db
class TestReverseParentCheckinPush:
    @patch("apps.notifications.services.async_task")
    def test_reverse_parent_checkin_push_respects_child_checkin_opt_out(self, mock_async, club):
        parent = UserFactory()
        NotificationPreference.objects.create(
            user=parent,
            disabled_categories=["child_checkin"],
        )
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )

        reverse_parent_checkin_push(checkin.id, club.id)

        mock_async.assert_not_called()
        assert not SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=f"parent_checkin_cancelled:{checkin.id}",
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_reverse_parent_checkin_push_is_idempotent_per_checkin(self, mock_async, club):
        parent = UserFactory()
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )

        reverse_parent_checkin_push(checkin.id, club.id)
        reverse_parent_checkin_push(checkin.id, club.id)

        assert mock_async.call_count == 1
        assert mock_async.call_args.args[4] == f"/parent/child/{student.id}"
        assert SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=f"parent_checkin_cancelled:{checkin.id}",
        ).count() == 1

    @patch("apps.notifications.services.async_task")
    def test_reverse_parent_checkin_push_sends_each_distinct_checkin_same_day(self, mock_async, club):
        parent = UserFactory()
        PushSubscriptionFactory(user=parent)
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        training_type = TrainingTypeFactory(club=club)
        first_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )
        second_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=ScheduleFactory(club=club, training_type=training_type),
            training_type=training_type,
        )

        reverse_parent_checkin_push(first_checkin.id, club.id)
        reverse_parent_checkin_push(second_checkin.id, club.id)

        assert mock_async.call_count == 2
        assert set(
            SentNotification.objects.filter(club=club, student=student).values_list(
                "notification_type",
                flat=True,
            )
        ) == {
            f"parent_checkin_cancelled:{first_checkin.id}",
            f"parent_checkin_cancelled:{second_checkin.id}",
        }


@pytest.mark.django_db
class TestPromoteStudentParentPush:
    @patch("apps.notifications.services.send_push_to_user")
    def test_promote_sends_parent_push(self, mock_push, club):
        from apps.grades.services import promote_student

        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Masha")
        gs = GradeSystemFactory(club=club)
        old_grade = GradeFactory(club=club, grade_system=gs, name="White Belt", order=0)
        new_grade = GradeFactory(club=club, grade_system=gs, name="Blue Belt", order=1)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs, current_grade=old_grade)

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=new_grade.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["user_id"] == parent.id
        assert call_kwargs["url"] == f"/parent/child/{student.id}"
        assert "Masha" in call_kwargs["body"]
        assert "Blue Belt" in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_promote_no_push_without_parent(self, mock_push, club):
        from apps.grades.services import promote_student

        student = StudentFactory(club=club, is_child=False)
        gs = GradeSystemFactory(club=club)
        old_grade = GradeFactory(club=club, grade_system=gs, order=0)
        new_grade = GradeFactory(club=club, grade_system=gs, order=1)
        sg = StudentGradeFactory(club=club, student=student, grade_system=gs, current_grade=old_grade)

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=new_grade.id)

        mock_push.assert_not_called()
