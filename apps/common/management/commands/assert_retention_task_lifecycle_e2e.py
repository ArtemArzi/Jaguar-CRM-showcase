from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.notifications.models import PushSubscription, SentNotification
from apps.retention.models import RetentionTask, TaskComment
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert retention task lifecycle E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_retention_task_lifecycle_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for lifecycle side effects before failing.",
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
                    raise CommandError(f"retention task lifecycle E2E assertion failed: {exc}") from exc
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
            "trainer",
            "target_student",
            "other_task_id",
            "push_subscription_ids",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        student_id = int(fixture["target_student"]["student_id"])
        expected = fixture["expected"]

        tasks = list(
            RetentionTask.objects.for_club(club_id)
            .filter(student_id=student_id, task_type=RetentionTask.TaskType.RETENTION)
            .order_by("id")
        )
        if len(tasks) != 1:
            raise CommandError(f"expected exactly one target retention task, got {len(tasks)}")
        task = tasks[0]
        if task.trainer_id != trainer_id:
            raise CommandError("target retention task trainer mismatch")
        if task.level != expected["level"]:
            raise CommandError(f"target retention task level mismatch: expected {expected['level']}, got {task.level}")
        if task.status != expected["status_after_close"]:
            raise CommandError("target retention task was not closed")
        if task.resolution != expected["resolution_after_close"]:
            raise CommandError("target retention task resolution mismatch")
        if task.resolved_at is None:
            raise CommandError("target retention task resolved_at missing")
        if task.notes != expected["close_notes"]:
            raise CommandError("target retention task close notes mismatch")

        comment = TaskComment.objects.for_club(club_id).filter(
            task=task,
            author_id=int(fixture["trainer"]["user_id"]),
            text=expected["comment_text"],
        ).first()
        if comment is None:
            raise CommandError("target retention task comment not found")

        trainer_notification = SentNotification.objects.for_club(club_id).filter(
            student_id=student_id,
            notification_type=expected["trainer_notification_type"],
            sent_date=timezone.now().date(),
        ).first()
        if trainer_notification is None:
            raise CommandError("trainer retention notification record not found")

        trainer_push = PushSubscription.objects.filter(
            id=int(fixture["push_subscription_ids"]["trainer"]),
            user_id=int(fixture["trainer"]["user_id"]),
            is_active=True,
        ).first()
        if trainer_push is None:
            raise CommandError("trainer active push subscription not found")

        other_task = RetentionTask.objects.for_club(club_id).get(id=int(fixture["other_task_id"]))
        if other_task.trainer_id == trainer_id:
            raise CommandError("privacy fixture other task belongs to the target trainer")
        if other_task.resolved_at is not None:
            raise CommandError("foreign trainer task was mutated")

        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.status != Student.Status.AT_RISK:
            raise CommandError("student status was not moved to at_risk by retention scan")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "task": {
                "id": task.id,
                "level": task.level,
                "status": task.status,
                "resolution": task.resolution,
                "trainer_id": task.trainer_id,
                "resolved_at": task.resolved_at,
            },
            "comment": {
                "id": comment.id,
                "author_id": comment.author_id,
            },
            "trainer_notification": {
                "id": trainer_notification.id,
                "notification_type": trainer_notification.notification_type,
                "sent_date": trainer_notification.sent_date,
                "push_subscription_id": trainer_push.id,
            },
            "student": {
                "id": student.id,
                "status": student.status,
                "last_visit_date": student.last_visit_date,
            },
            "privacy": {
                "foreign_task_id": other_task.id,
                "foreign_task_status": other_task.status,
            },
        }
