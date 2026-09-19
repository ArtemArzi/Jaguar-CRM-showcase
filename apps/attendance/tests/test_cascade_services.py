from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession, ScheduleEnrollment
from apps.attendance.services import batch_checkin, cancel_checkin, close_session_from_existing_checkins, create_checkin
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Debt, Subscription
from apps.billing.tests.factories import (
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.grades.tests.factories import GradeFactory, GradeSystemFactory, StudentGradeFactory
from apps.retention.models import RetentionTask
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestCreateCheckin:
    def test_create_checkin_basic(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        checkin = Checkin.objects.get(id=result["checkin_id"])
        assert checkin.student_id == student.id
        assert checkin.schedule_id == schedule.id
        assert checkin.training_type_id == training_type.id
        assert checkin.trainer_id == schedule.trainer_id
        assert checkin.location_id == schedule.location_id
        assert checkin.date == date.today()

    def test_create_checkin_deducts_subscription(self, club):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=8,
            trainings_used=0,
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        sub.refresh_from_db()
        assert sub.trainings_left == 7
        assert sub.trainings_used == 1
        assert result["subscription_id"] == sub.id
        assert result["is_debt"] is False

    def test_create_checkin_no_subscription_creates_debt(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        assert result["is_debt"] is True
        assert result["subscription_id"] is None
        assert Debt.objects.filter(student=student).exists()

    def test_create_checkin_unlimited_subscription(self, club):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=None)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=None,
        )

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        sub.refresh_from_db()
        assert sub.trainings_left is None
        assert sub.trainings_used == 1

    def test_create_checkin_expires_subscription(self, club):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=1,
            trainings_used=7,
        )

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        sub.refresh_from_db()
        assert sub.trainings_left == 0
        assert sub.status == "expired"

    def test_create_checkin_with_zero_left_subscription_creates_debt(self, club):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, drop_in_price=Decimal("1000"))
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=0,
            trainings_used=8,
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        sub.refresh_from_db()
        debt = Debt.objects.get(student=student)
        assert result["subscription_id"] is None
        assert result["is_debt"] is True
        assert debt.tariff_price == Decimal("1000")
        assert sub.trainings_left == 0
        assert sub.trainings_used == 8

    def test_create_checkin_updates_last_visit_date(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        student.refresh_from_db()
        assert student.last_visit_date == date.today()

    def test_create_checkin_duplicate_is_idempotent(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task"):
            first = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )
            second = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )
            # Idempotent: second call returns the same checkin, not a new one.
            assert first["checkin_id"] == second["checkin_id"]

    def test_create_checkin_fires_async_tasks(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task") as mock_async:
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        assert mock_async.call_count == 6
        task_names = [call.args[0] for call in mock_async.call_args_list]
        assert "apps.attendance.tasks.calculate_salary" in task_names
        assert "apps.attendance.tasks.update_grade_progress" in task_names
        assert "apps.attendance.tasks.update_group_analytics" in task_names
        assert "apps.attendance.tasks.log_parent_event" in task_names
        assert "apps.retention.tasks.auto_close_retention_on_checkin" in task_names
        assert "apps.retention.tasks.create_post_trial_task" not in task_names
        assert "apps.notifications.tasks.check_trainings_left_push" in task_names

    def test_create_checkin_records_cascade_events(self, club):
        parent = UserFactory()
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            is_child=True,
            parent_user=parent,
        )
        grade_system = GradeSystemFactory(club=club)
        grade = GradeFactory(club=club, grade_system=grade_system)
        StudentGradeFactory(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
        )
        training_type = TrainingTypeFactory(club=club, grade_system=grade_system)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=4)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=3,
            trainings_used=1,
        )
        RetentionTaskFactory(
            club=club,
            student=student,
            trainer=schedule.trainer,
            task_type=RetentionTask.TaskType.RETENTION,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        events = {
            event.effect: event
            for event in CheckinCascadeEvent.objects.for_club(club).filter(
                checkin_id=result["checkin_id"],
            )
        }
        assert set(events) == {
            CheckinCascadeEvent.Effect.SALARY,
            CheckinCascadeEvent.Effect.GRADE_PROGRESS,
            CheckinCascadeEvent.Effect.GROUP_ANALYTICS,
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION,
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE,
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
            CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH,
        }
        assert all(event.status == CheckinCascadeEvent.Status.QUEUED for event in events.values())
        assert events[CheckinCascadeEvent.Effect.SALARY].expected is True
        assert events[CheckinCascadeEvent.Effect.GRADE_PROGRESS].expected is True
        assert events[CheckinCascadeEvent.Effect.GROUP_ANALYTICS].expected is True
        assert events[CheckinCascadeEvent.Effect.PARENT_NOTIFICATION].expected is True
        assert events[CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE].expected is True
        assert events[CheckinCascadeEvent.Effect.POST_TRIAL_TASK].expected is False
        assert events[CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH].expected is True
        assert all(event.payload["checkin_id"] == result["checkin_id"] for event in events.values())
        assert {
            event.task_name
            for event in events.values()
            if event.effect != CheckinCascadeEvent.Effect.POST_TRIAL_TASK or event.expected
        } == {
            call.args[0] for call in mock_async.call_args_list
        }

    def test_duplicate_checkin_does_not_duplicate_cascade_events(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type

        with patch("apps.attendance.services.async_task"):
            first = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )
            second = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        assert first["checkin_id"] == second["checkin_id"]
        assert (
            CheckinCascadeEvent.objects.for_club(club)
            .filter(checkin_id=first["checkin_id"])
            .count()
        ) == 7

    @pytest.mark.parametrize(
        "created_from",
        [
            ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
            ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
        ],
    )
    def test_booked_trial_checkin_records_post_trial_cascade_event(self, club, created_from):
        student = StudentFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
        )
        training_type = TrainingTypeFactory(club=club, trial_free=True)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        target_date = timezone.localdate()
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=target_date,
            ends_on=target_date,
            created_from=created_from,
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
                checkin_date=target_date,
            )

        event = CheckinCascadeEvent.objects.for_club(club).get(
            checkin_id=result["checkin_id"],
            effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
        )
        assert event.expected is True

    def test_create_checkin_rejects_training_type_mismatch(self, club):
        student = StudentFactory(club=club)
        schedule_type = TrainingTypeFactory(club=club)
        wrong_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=schedule_type)

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=wrong_type.id,
                    source="manual",
                )

        assert exc_info.value.code == "training_type_mismatch"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()

    def test_create_checkin_prefers_location_subscription(self, club):
        student = StudentFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, location=location, training_type=training_type)
        club_tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=8,
            scope="club",
        )
        location_tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=8,
            scope="location",
            location=location,
        )
        club_sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=club_tariff,
            trainings_left=8,
            expires_at=timezone.now() + timedelta(days=1),
        )
        location_sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=location_tariff,
            trainings_left=8,
            expires_at=timezone.now() + timedelta(days=30),
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        club_sub.refresh_from_db()
        location_sub.refresh_from_db()
        assert result["subscription_id"] == location_sub.id
        assert club_sub.trainings_left == 8
        assert location_sub.trainings_left == 7

    def test_create_checkin_uses_checkin_date_for_subscription_expiry(self, club):
        student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=8,
            expires_at=timezone.now() - timedelta(days=1),
        )
        checkin_date = timezone.localdate() - timedelta(days=2)

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
                checkin_date=checkin_date,
            )

        subscription.refresh_from_db()
        assert result["subscription_id"] == subscription.id
        assert result["is_debt"] is False
        assert subscription.trainings_left == 7


@pytest.mark.django_db
class TestCancelCheckin:
    def test_cancel_checkin_restores_subscription(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
            trainings_used=1,
        )
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
        )

        with patch("apps.attendance.services.async_task"):
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        sub.refresh_from_db()
        assert sub.trainings_left == 8
        assert sub.trainings_used == 0
        assert sub.status == "active"

    def test_cancel_checkin_is_idempotent_after_soft_delete(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
            trainings_used=1,
        )
        user = UserFactory()
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        sub.refresh_from_db()
        checkin.refresh_from_db()
        assert sub.trainings_left == 8
        assert sub.trainings_used == 0
        assert checkin.deleted_at is not None
        assert checkin.cancelled_by_id == user.id
        assert mock_async.call_count == 5

    def test_cancel_checkin_removes_debt(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
        )
        Debt.objects.create(
            club=club,
            student=student,
            checkin=checkin,
            reason="no_subscription",
        )

        with patch("apps.attendance.services.async_task"):
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        debt = Debt.objects.get(checkin=checkin)
        assert debt.resolved_at is not None
        assert debt.resolution_type == "cancelled"

    def test_cancel_checkin_soft_deletes(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        with patch("apps.attendance.services.async_task"):
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        checkin.refresh_from_db()
        assert checkin.deleted_at is not None
        assert checkin.cancelled_at is not None
        assert checkin.cancelled_by_id == user.id

    def test_cancel_latest_checkin_recomputes_last_visit_date(self, club):
        student = StudentFactory(club=club, last_visit_date=date.today() - timedelta(days=7))
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type
        user = UserFactory()
        previous_date = date.today() - timedelta(days=3)
        latest_date = date.today()
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=previous_date,
        )
        latest_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=latest_date,
        )
        Student.objects.for_club(club).filter(id=student.id).update(last_visit_date=latest_date)

        with patch("apps.attendance.services.async_task"):
            cancel_checkin(
                checkin_id=latest_checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        student.refresh_from_db()
        assert student.last_visit_date == previous_date

    def test_cancel_checkin_trainer_24h_limit(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        # Backdate created_at to > 24h ago
        Checkin.objects.filter(id=checkin.id).update(created_at=timezone.now() - timedelta(hours=25))

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="trainer",
            )
        assert exc_info.value.code == "cancel_time_limit"

    def test_cancel_checkin_owner_no_limit(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        # Backdate created_at to > 24h ago
        Checkin.objects.filter(id=checkin.id).update(created_at=timezone.now() - timedelta(hours=48))

        with patch("apps.attendance.services.async_task"):
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        checkin.refresh_from_db()
        assert checkin.deleted_at is not None

    def test_cancel_checkin_fires_reverse_tasks(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        user = UserFactory()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=user.id,
                user_role="owner",
            )

        assert mock_async.call_count == 5
        task_names = [call.args[0] for call in mock_async.call_args_list]
        assert "apps.attendance.tasks.reverse_salary" in task_names
        assert "apps.attendance.tasks.reverse_grade_progress" in task_names
        assert "apps.attendance.tasks.reverse_group_analytics" in task_names
        assert "apps.retention.tasks.reverse_auto_close_retention" in task_names
        assert "apps.attendance.tasks.reverse_parent_checkin_push" in task_names

    def test_cancel_checkin_rejects_closed_payroll_period_before_mutation(self, club, owner_user):
        from apps.trainers.services import close_trainer_payroll_period

        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        sub = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
            trainings_used=1,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            date=timezone.localdate(),
        )
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=checkin.date.replace(day=1),
            period_end=checkin.date,
            reason="Payroll closed",
            actor_user_id=owner_user.id,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                cancel_checkin(
                    checkin_id=checkin.id,
                    club_id=club.id,
                    cancelled_by_user_id=owner_user.id,
                    user_role="owner",
                )

        checkin.refresh_from_db()
        sub.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert checkin.cancelled_at is None
        assert checkin.deleted_at is None
        assert sub.trainings_left == 7
        assert sub.trainings_used == 1
        assert mock_async.call_count == 0


@pytest.mark.django_db
class TestBatchCheckin:
    def _enroll_students_for_today(self, *, club, schedule, students):
        for student in students:
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=date.today() - timedelta(days=7),
            )

    def test_batch_checkin(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        students = [StudentFactory(club=club, status=Student.Status.ACTIVE) for _ in range(3)]
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=students)

        with patch("apps.attendance.services.async_task"):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[s.id for s in students],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        assert len(result["checkins"]) == 3
        assert result["group_session_id"] is not None

    def test_batch_checkin_creates_group_session(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        students = [StudentFactory(club=club, status=Student.Status.ACTIVE) for _ in range(5)]
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=students)

        with patch("apps.attendance.services.async_task"):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[s.id for s in students],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
                topic_tags=["striking", "defense"],
                notes="Good session",
            )

        session = GroupSession.objects.get(id=result["group_session_id"])
        assert session.attendee_count == 5
        assert session.topic_tags == ["striking", "defense"]
        assert session.notes == "Good session"
        assert session.closed_by_id == owner_user.id
        assert session.close_source == GroupSession.CloseSource.BATCH

    def test_batch_checkin_records_no_per_checkin_group_analytics_event(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=[student])

        with patch("apps.attendance.services.async_task"):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[student.id],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        checkin_id = result["checkins"][0]["checkin_id"]
        assert not CheckinCascadeEvent.objects.for_club(club).filter(
            checkin_id=checkin_id,
            effect=CheckinCascadeEvent.Effect.GROUP_ANALYTICS,
        ).exists()

    def test_batch_checkin_counts_existing_live_checkins(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        existing_student = StudentFactory(club=club)
        batch_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=[batch_student])
        CheckinFactory(
            club=club,
            student=existing_student,
            schedule=schedule,
            training_type=training_type,
            date=checkin_date,
        )

        with patch("apps.attendance.services.async_task"):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[batch_student.id],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        session = GroupSession.objects.get(id=result["group_session_id"])
        assert session.attendee_count == 2

    @pytest.mark.django_db(transaction=True)
    def test_batch_checkin_defers_async_until_outer_commit(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        students = [StudentFactory(club=club, status=Student.Status.ACTIVE) for _ in range(2)]
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=students)

        with patch("apps.attendance.services.async_task") as mock_async:
            with transaction.atomic():
                batch_checkin(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=checkin_date,
                    present_student_ids=[s.id for s in students],
                    training_type_id=training_type.id,
                    actor_user_id=owner_user.id,
                )
                assert mock_async.call_count == 0

            assert mock_async.call_count == 10

    def test_batch_checkin_deduplicates_present_student_ids(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(club=club, schedule=schedule, students=[student])

        with patch("apps.attendance.services.async_task"):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[student.id, student.id],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        session = GroupSession.objects.get(id=result["group_session_id"])
        assert len(result["checkins"]) == 1
        assert Checkin.objects.filter(student=student, schedule=schedule).count() == 1
        assert session.attendee_count == 1

    @pytest.mark.parametrize("status", [Student.Status.LEAD, Student.Status.LOST])
    def test_batch_checkin_rejects_ineligible_student_status(self, club, owner_user, status):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status=status)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type

        with pytest.raises(BusinessLogicError) as exc_info:
            batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[student.id],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "student_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()

    def test_batch_checkin_rejects_deleted_student(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            deleted_at=timezone.now(),
        )
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type

        with pytest.raises(BusinessLogicError) as exc_info:
            batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[student.id],
                training_type_id=training_type.id,
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "student_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()


@pytest.mark.django_db
class TestCloseSessionFromExistingCheckins:
    def _enroll_students_for_today(self, *, club, schedule, students):
        for student in students:
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=date.today(),
            )

    def _after_today_session_end(self):
        return datetime.combine(date.today(), time(12, 0), tzinfo=ZoneInfo("Europe/Moscow"))

    def test_close_session_summarizes_kiosk_checkins_without_creating_attendance(self, club, owner_user):
        checked_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        waiting_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday())
        training_type = schedule.training_type
        self._enroll_students_for_today(
            club=club,
            schedule=schedule,
            students=[checked_student, waiting_student],
        )
        CheckinFactory(
            club=club,
            student=checked_student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
            source=Checkin.Source.KIOSK,
        )
        checkin_count = Checkin.objects.for_club(club).count()
        cascade_count = CheckinCascadeEvent.objects.for_club(club).count()

        with patch("apps.attendance.selectors.timezone.now", return_value=self._after_today_session_end()):
            session = close_session_from_existing_checkins(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=date.today(),
                actor_user_id=owner_user.id,
                topic_tags=["sparring"],
                notes="Checked at the end",
            )

        assert session.attendee_count == 1
        assert session.topic_tags == ["sparring"]
        assert session.notes == "Checked at the end"
        assert session.closed_at is not None
        assert session.closed_by_id == owner_user.id
        assert session.close_source == GroupSession.CloseSource.TRAINER_REVIEW
        assert Checkin.objects.for_club(club).count() == checkin_count
        assert CheckinCascadeEvent.objects.for_club(club).count() == cascade_count

    def test_close_session_allows_zero_checkins_and_preserves_first_close_metadata(
        self,
        club,
        owner_user,
        admin_user,
    ):
        schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday())

        with patch("apps.attendance.selectors.timezone.now", return_value=self._after_today_session_end()):
            first = close_session_from_existing_checkins(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=date.today(),
                actor_user_id=owner_user.id,
                notes="No one checked in",
            )
            second = close_session_from_existing_checkins(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=date.today(),
                actor_user_id=admin_user.id,
                topic_tags=["updated"],
                notes="Reviewed again",
            )

        assert second.id == first.id
        assert second.attendee_count == 0
        assert second.topic_tags == ["updated"]
        assert second.notes == "Reviewed again"
        assert second.closed_by_id == owner_user.id
        assert second.closed_at == first.closed_at
        assert second.close_source == GroupSession.CloseSource.TRAINER_REVIEW
        assert GroupSession.objects.for_club(club).filter(schedule=schedule, date=date.today()).count() == 1

    def test_close_session_rejects_before_effective_end_time(self, club, owner_user):
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        current_time = datetime.combine(date.today(), time(18, 30), tzinfo=ZoneInfo("Europe/Moscow"))

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            with pytest.raises(BusinessLogicError) as exc_info:
                close_session_from_existing_checkins(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=date.today(),
                    actor_user_id=owner_user.id,
                )

        assert exc_info.value.code == "session_close_not_allowed_yet"
        assert not GroupSession.objects.for_club(club).filter(schedule=schedule, date=date.today()).exists()

    def test_normal_then_batch_retry_preserves_first_close_provenance(
        self,
        club,
        owner_user,
        admin_user,
    ):
        checkin_date = date.today() - timedelta(days=7)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        first_closed_at = datetime.combine(
            checkin_date,
            time(12, 0),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.services.checkin.timezone.now", return_value=first_closed_at):
            first = close_session_from_existing_checkins(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                actor_user_id=owner_user.id,
                notes="Trainer review",
            )
        batch_checkin(
            club_id=club.id,
            schedule_id=schedule.id,
            checkin_date=checkin_date,
            present_student_ids=[],
            training_type_id=schedule.training_type_id,
            actor_user_id=admin_user.id,
            topic_tags=["corrected"],
            notes="Owner correction",
        )

        first.refresh_from_db()
        assert first.closed_at == first_closed_at
        assert first.closed_by_id == owner_user.id
        assert first.close_source == GroupSession.CloseSource.TRAINER_REVIEW
        assert first.topic_tags == ["corrected"]
        assert first.notes == "Owner correction"

    def test_batch_then_normal_retry_preserves_first_close_provenance(
        self,
        club,
        owner_user,
        admin_user,
    ):
        checkin_date = date.today() - timedelta(days=7)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        first_closed_at = datetime.combine(
            checkin_date,
            time(12, 0),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.services.checkin.timezone.now", return_value=first_closed_at):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[],
                training_type_id=schedule.training_type_id,
                actor_user_id=owner_user.id,
                notes="Owner correction",
            )
        session = GroupSession.objects.get(id=result["group_session_id"])
        close_session_from_existing_checkins(
            club_id=club.id,
            schedule_id=schedule.id,
            checkin_date=checkin_date,
            actor_user_id=admin_user.id,
            topic_tags=["reviewed"],
            notes="Trainer-style review",
        )

        session.refresh_from_db()
        assert session.closed_at == first_closed_at
        assert session.closed_by_id == owner_user.id
        assert session.close_source == GroupSession.CloseSource.BATCH
        assert session.topic_tags == ["reviewed"]
        assert session.notes == "Trainer-style review"


@pytest.mark.django_db
class TestDropInPricing:
    def test_trial_student_free_trial_no_debt(self, club):
        """Trial student + trial_free=True → no debt created."""
        training_type = TrainingTypeFactory(club=club, trial_free=True, drop_in_price=None)
        student = StudentFactory(club=club, status=Student.Status.TRIAL)
        schedule = ScheduleFactory(club=club, training_type=training_type)

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        assert not Debt.objects.filter(student=student).exists()
        assert result["is_debt"] is False

    def test_trial_student_paid_trial_creates_debt(self, club):
        """Trial student + trial_free=False → debt with drop_in_price."""
        training_type = TrainingTypeFactory(club=club, trial_free=False, drop_in_price=Decimal("800"))
        student = StudentFactory(club=club, status=Student.Status.TRIAL)
        schedule = ScheduleFactory(club=club, training_type=training_type)

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        debt = Debt.objects.get(student=student)
        assert debt.tariff_price == Decimal("800")
        assert result["is_debt"] is True

    def test_active_student_no_sub_creates_debt_with_price(self, club):
        """Active student without subscription → debt with drop_in_price."""
        training_type = TrainingTypeFactory(club=club, drop_in_price=Decimal("1000"))
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, training_type=training_type)

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
            )

        debt = Debt.objects.get(student=student)
        assert debt.tariff_price == Decimal("1000")
        assert result["is_debt"] is True

    @pytest.mark.parametrize(
        ("student_status", "trial_free"),
        [
            (Student.Status.ACTIVE, True),
            (Student.Status.TRIAL, False),
        ],
    )
    def test_no_subscription_checkin_requires_drop_in_price(
        self,
        club,
        student_status,
        trial_free,
    ):
        """Non-free no-subscription check-in must fail before persisting debt."""
        training_type = TrainingTypeFactory(club=club, drop_in_price=None, trial_free=trial_free)
        student = StudentFactory(club=club, status=student_status)
        schedule = ScheduleFactory(club=club, training_type=training_type)

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="manual",
                )

        assert exc_info.value.code == "drop_in_price_required"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
