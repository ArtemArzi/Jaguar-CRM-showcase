from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, GroupSession
from apps.billing.models import Subscription
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.notifications.models import SentNotification
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert check-in cancellation E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_checkin_cancel_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for cancellation side effects before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"check-in cancel E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {
            "fixture_id",
            "club_id",
            "owner",
            "student",
            "schedule_id",
            "checkin_id",
            "subscription_id",
            "earning_id",
            "student_grade_id",
            "retention_task_id",
            "checkin_date",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        checkin_id = int(fixture["checkin_id"])
        student_id = int(fixture["student"]["student_id"])
        schedule_id = int(fixture["schedule_id"])
        expected = fixture["expected"]

        checkin = Checkin.objects.for_club(club_id).filter(id=checkin_id).first()
        if checkin is None:
            raise CommandError("check-in row not found")
        if checkin.cancelled_at is None:
            raise CommandError("check-in is not cancelled")
        if checkin.deleted_at is None:
            raise CommandError("check-in is not soft-deleted")
        if checkin.cancelled_by_id != int(fixture["owner"]["user_id"]):
            raise CommandError("check-in cancellation actor mismatch")

        subscription = Subscription.objects.for_club(club_id).get(id=int(fixture["subscription_id"]))
        if subscription.trainings_left != int(expected["trainings_left_after_cancel"]):
            raise CommandError("subscription trainings_left was not restored")
        if subscription.trainings_used != int(expected["trainings_used_after_cancel"]):
            raise CommandError("subscription trainings_used was not restored")
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError("subscription status was not restored to active")

        earning = TrainerEarning.objects.for_club(club_id).get(id=int(fixture["earning_id"]))
        if not earning.cancelled:
            raise CommandError("trainer earning was not marked cancelled")

        student_grade = StudentGrade.objects.for_club(club_id).get(id=int(fixture["student_grade_id"]))
        if student_grade.trainings_since_last_grade != 0:
            raise CommandError("grade progress counter was not reversed")
        if GradeProgressEvent.objects.for_club(club_id).filter(checkin_id=checkin_id).exists():
            raise CommandError("grade progress event was not removed")

        group_session = GroupSession.objects.for_club(club_id).get(
            schedule_id=schedule_id,
            date=fixture["checkin_date"],
        )
        if group_session.attendee_count != int(expected["group_session_attendee_count_after_cancel"]):
            raise CommandError("group session attendee count was not recalculated")

        retention_task = RetentionTask.objects.for_club(club_id).get(id=int(fixture["retention_task_id"]))
        if retention_task.resolved_at is not None:
            raise CommandError("retention task was not reopened")
        if retention_task.status != RetentionTask.TaskStatus.OPEN:
            raise CommandError("retention task status was not reopened")
        if retention_task.resolution:
            raise CommandError("retention task resolution was not cleared")

        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date is not None:
            raise CommandError("student last_visit_date was not recalculated")

        parent_checkin_type = f"parent_checkin:{checkin_id}"
        parent_cancel_type = f"parent_checkin_cancelled:{checkin_id}"
        if not SentNotification.objects.for_club(club_id).filter(
            student_id=student_id,
            notification_type=parent_checkin_type,
        ).exists():
            raise CommandError("original parent check-in notification record missing")
        if not SentNotification.objects.for_club(club_id).filter(
            student_id=student_id,
            notification_type=parent_cancel_type,
        ).exists():
            raise CommandError("parent cancellation notification was not recorded")

        live_checkins = Checkin.objects.for_club(club_id).filter(
            student_id=student_id,
            schedule_id=schedule_id,
            date=fixture["checkin_date"],
            deleted_at__isnull=True,
        ).count()
        if live_checkins != 0:
            raise CommandError("cancelled check-in is still visible as live")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkin": {
                "id": checkin.id,
                "cancelled": True,
                "soft_deleted": True,
            },
            "subscription": {
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
                "status": subscription.status,
            },
            "earning": {
                "id": earning.id,
                "cancelled": earning.cancelled,
            },
            "grade": {
                "trainings_since_last_grade": student_grade.trainings_since_last_grade,
            },
            "group_session": {
                "attendee_count": group_session.attendee_count,
            },
            "retention_task": {
                "id": retention_task.id,
                "status": retention_task.status,
                "resolved_at": retention_task.resolved_at,
            },
            "parent_notifications": {
                "checkin": parent_checkin_type,
                "cancelled": parent_cancel_type,
            },
        }
