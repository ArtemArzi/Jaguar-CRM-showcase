"""T4 — cancel_checkin reverse cascade: re-open retention + parent push."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.retention.models import RetentionTask
from apps.retention.services import (
    auto_close_retention_tasks,
    reopen_retention_tasks_auto_closed,
)
from apps.retention.tasks import (
    auto_close_retention_on_checkin,
    create_post_trial_task,
    reverse_auto_close_retention,
)
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestReopenRetentionAutoClosed:
    def test_reopens_auto_closed_after_timestamp(self, club):
        student = StudentFactory(club=club)
        t = RetentionTaskFactory(club=club, student=student)
        auto_close_retention_tasks(student_id=student.id, club_id=club.id)
        t.refresh_from_db()
        assert t.resolved_at is not None
        assert t.resolution == RetentionTask.Resolution.AUTO_CHECKIN

        before = timezone.now() - timedelta(minutes=1)
        n = reopen_retention_tasks_auto_closed(
            student_id=student.id, club_id=club.id, after=before,
        )
        assert n == 1
        t.refresh_from_db()
        assert t.resolved_at is None
        assert t.resolution == ""

    def test_does_not_reopen_older_auto_closed(self, club):
        student = StudentFactory(club=club)
        t = RetentionTaskFactory(club=club, student=student)
        auto_close_retention_tasks(student_id=student.id, club_id=club.id)

        future = timezone.now() + timedelta(hours=1)
        n = reopen_retention_tasks_auto_closed(
            student_id=student.id, club_id=club.id, after=future,
        )
        assert n == 0
        t.refresh_from_db()
        assert t.resolved_at is not None  # untouched

    def test_does_not_touch_non_auto_resolutions(self, club):
        student = StudentFactory(club=club)
        t = RetentionTaskFactory(club=club, student=student)
        t.resolved_at = timezone.now()
        t.resolution = RetentionTask.Resolution.MANUAL_CONTACTED
        t.save(update_fields=["resolved_at", "resolution"])

        before = timezone.now() - timedelta(minutes=1)
        n = reopen_retention_tasks_auto_closed(
            student_id=student.id, club_id=club.id, after=before,
        )
        assert n == 0
        t.refresh_from_db()
        assert t.resolved_at is not None
        assert t.resolution == RetentionTask.Resolution.MANUAL_CONTACTED


@pytest.mark.django_db
class TestReverseAutoCloseRetentionTask:
    def test_reverse_via_task_wrapper(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        # Create checkin first, then RetentionTask, then auto-close (after checkin.created_at)
        checkin = CheckinFactory(club=club, student=student, schedule=schedule)
        t = RetentionTaskFactory(club=club, student=student)
        auto_close_retention_tasks(student_id=student.id, club_id=club.id)

        reverse_auto_close_retention(checkin.id, club.id)

        t.refresh_from_db()
        assert t.resolved_at is None

    def test_stale_auto_close_task_skips_cancelled_checkin(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            cancelled_at=timezone.now(),
            deleted_at=timezone.now(),
        )
        task = RetentionTaskFactory(club=club, student=student)

        auto_close_retention_on_checkin(checkin.id, club.id)

        task.refresh_from_db()
        assert task.resolved_at is None
        assert task.status == RetentionTask.TaskStatus.OPEN


@pytest.mark.django_db
class TestCreatePostTrialTaskWrapper:
    def test_skips_cancelled_trial_checkin(self, club):
        student = StudentFactory(club=club, status="trial")
        schedule = ScheduleFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            cancelled_at=timezone.now(),
            deleted_at=timezone.now(),
        )

        create_post_trial_task(
            student.id,
            club.id,
            schedule.trainer_id,
            checkin_id=checkin.id,
        )

        assert not RetentionTask.objects.for_club(club).filter(
            student=student,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        ).exists()
