from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.tests.factories import SubscriptionFactory, SubscriptionFreezeFactory
from apps.retention.models import RetentionTask
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.fixture(autouse=True)
def _disable_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


@pytest.mark.django_db
class TestCalculateStudentThresholds:
    def test_frequent_visitor(self):
        from apps.retention.services import calculate_student_thresholds

        yellow, red = calculate_student_thresholds(checkins_last_30=12)
        # avg_gap = 30/12 = 2.5
        # yellow = max(4, min(int(2.5*2.0), 10)) = max(4, min(5, 10)) = 5
        # red = max(7, min(int(2.5*3.5), 21)) = max(7, min(8, 21)) = 8
        assert yellow == 5
        assert red == 8

    def test_infrequent_visitor(self):
        from apps.retention.services import calculate_student_thresholds

        yellow, red = calculate_student_thresholds(checkins_last_30=4)
        # avg_gap = 30/4 = 7.5
        # yellow = max(4, min(int(7.5*2.0), 10)) = max(4, min(15, 10)) = 10
        # red = max(7, min(int(7.5*3.5), 21)) = max(7, min(26, 21)) = 21
        assert yellow == 10
        assert red == 21

    def test_insufficient_data(self):
        from apps.retention.services import calculate_student_thresholds

        yellow, red = calculate_student_thresholds(checkins_last_30=2)
        assert yellow == 10
        assert red == 21


@pytest.mark.django_db
class TestCheckRetentionTriggers:
    def _setup(self, club):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        return trainer, schedule

    def _local_today(self, club):
        return timezone.now().astimezone(zoneinfo.ZoneInfo(club.timezone)).date()

    def test_creates_yellow_task(self, club):
        trainer, schedule = self._setup(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=date.today() - timedelta(days=6),
        )
        # 12 checkins in last 30 days -> yellow_days=5, red_days=8
        for i in range(12):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=i + 1),
            )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)
        assert result["tasks_created"] == 1

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.level == RetentionTask.Level.YELLOW
        assert task.trainer == trainer

    def test_creates_red_task(self, club):
        trainer, schedule = self._setup(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=date.today() - timedelta(days=10),
        )
        for i in range(12):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=i + 1),
            )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)
        assert result["tasks_created"] == 1

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.level == RetentionTask.Level.RED

    def test_churned_30_days(self, club):
        trainer, schedule = self._setup(club)
        today = self._local_today(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=today - timedelta(days=35),
        )
        # Few checkins -> conservative thresholds
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=today - timedelta(days=35),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)
        assert result["tasks_created"] == 1
        assert result["status_changes"] == 1

        student.refresh_from_db()
        assert student.status == Student.Status.CHURNED

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.level == RetentionTask.Level.CHURNED

    def test_churns_student_with_active_subscription(self, club):
        trainer, schedule = self._setup(club)
        today = self._local_today(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=today - timedelta(days=30),
        )
        SubscriptionFactory(
            student=student,
            club=club,
            expires_at=timezone.now() + timedelta(days=10),
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=today - timedelta(days=30),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)

        assert result == {"tasks_created": 1, "status_changes": 1}
        student.refresh_from_db()
        assert student.status == Student.Status.CHURNED

    def test_churns_student_without_active_subscription(self, club):
        trainer, schedule = self._setup(club)
        today = self._local_today(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=today - timedelta(days=30),
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=today - timedelta(days=30),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)

        assert result == {"tasks_created": 1, "status_changes": 1}
        student.refresh_from_db()
        assert student.status == Student.Status.CHURNED

    def test_churns_student_with_active_freeze(self, club):
        trainer, schedule = self._setup(club)
        today = self._local_today(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=today - timedelta(days=30),
        )
        sub = SubscriptionFactory(student=student, club=club)
        SubscriptionFreezeFactory(
            subscription=sub,
            club=club,
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=None,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=today - timedelta(days=30),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)

        assert result == {"tasks_created": 1, "status_changes": 1}
        student.refresh_from_db()
        assert student.status == Student.Status.CHURNED

    def test_no_last_visit_date_does_not_churn(self, club):
        trainer, schedule = self._setup(club)
        today = self._local_today(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=None,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=today - timedelta(days=45),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)

        assert result == {"tasks_created": 0, "status_changes": 0}
        student.refresh_from_db()
        assert student.status == Student.Status.ACTIVE

    def test_churn_boundary_uses_club_timezone_date(self, club, monkeypatch):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        frozen_now = datetime(2026, 6, 5, 19, 30, tzinfo=zoneinfo.ZoneInfo("UTC"))
        monkeypatch.setattr("apps.retention.services.timezone.now", lambda: frozen_now)
        trainer, schedule = self._setup(club)
        local_today = frozen_now.astimezone(zoneinfo.ZoneInfo(club.timezone)).date()
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=local_today - timedelta(days=30),
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            date=local_today - timedelta(days=30),
        )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)

        assert result == {"tasks_created": 1, "status_changes": 1}
        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.due_date == local_today

    def test_status_transition_active_to_at_risk(self, club):
        trainer, schedule = self._setup(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=date.today() - timedelta(days=6),
        )
        for i in range(12):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=i + 1),
            )

        from apps.retention.services import check_retention_triggers

        check_retention_triggers(club=club)

        student.refresh_from_db()
        assert student.status == Student.Status.AT_RISK

    def test_no_duplicate_tasks(self, club):
        trainer, schedule = self._setup(club)
        student = StudentFactory(
            club=club,
            status=Student.Status.AT_RISK,
            last_visit_date=date.today() - timedelta(days=6),
        )
        for i in range(12):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=i + 1),
            )
        # Already has a yellow task
        RetentionTaskFactory(club=club, student=student, trainer=trainer, level=RetentionTask.Level.YELLOW)

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)
        assert result["tasks_created"] == 0

    def test_tenant_isolation(self, club, other_club):
        trainer, schedule = self._setup(club)
        other_trainer, other_schedule = self._setup(other_club)

        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            last_visit_date=date.today() - timedelta(days=6),
        )
        for i in range(12):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=i + 1),
            )

        other_student = StudentFactory(
            club=other_club,
            status=Student.Status.ACTIVE,
            last_visit_date=date.today() - timedelta(days=6),
        )
        for i in range(12):
            CheckinFactory(
                club=other_club,
                student=other_student,
                schedule=other_schedule,
                trainer=other_trainer,
                date=date.today() - timedelta(days=i + 1),
            )

        from apps.retention.services import check_retention_triggers

        result = check_retention_triggers(club=club)
        assert result["tasks_created"] == 1

        # Only club's tasks visible
        assert RetentionTask.objects.for_club(club).count() == 1
        assert RetentionTask.objects.for_club(other_club).count() == 0

    def test_daily_check_processes_all_clubs(self, club, other_club):
        for c in [club, other_club]:
            trainer, schedule = self._setup(c)
            student = StudentFactory(
                club=c,
                status=Student.Status.ACTIVE,
                last_visit_date=date.today() - timedelta(days=35),
            )
            CheckinFactory(
                club=c,
                student=student,
                schedule=schedule,
                trainer=trainer,
                date=date.today() - timedelta(days=35),
            )

        from apps.retention.tasks import daily_retention_check

        result = daily_retention_check()
        assert result["clubs_checked"] >= 2
        assert result["tasks_created"] >= 2


@pytest.mark.django_db
class TestAutoCloseRetentionTasks:
    def test_auto_close_on_checkin(self, club):
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club, status=Student.Status.AT_RISK)
        RetentionTaskFactory(club=club, student=student, trainer=trainer)

        from apps.retention.services import auto_close_retention_tasks

        count = auto_close_retention_tasks(student_id=student.id, club_id=club.id)
        assert count == 1

        task = RetentionTask.objects.for_club(club).get(student=student)
        assert task.resolved_at is not None
        assert task.resolution == RetentionTask.Resolution.AUTO_CHECKIN


@pytest.mark.django_db
class TestCloseRetentionTask:
    def test_close_task(self, club):
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club)
        task = RetentionTaskFactory(club=club, student=student, trainer=trainer)

        from apps.retention.services import close_retention_task

        closed = close_retention_task(
            task_id=task.id,
            club_id=club.id,
            resolution=RetentionTask.Resolution.MANUAL_CONTACTED,
            notes="Called, won't return",
        )
        assert closed.resolved_at is not None
        assert closed.resolution == RetentionTask.Resolution.MANUAL_CONTACTED
        assert closed.notes == "Called, won't return"
