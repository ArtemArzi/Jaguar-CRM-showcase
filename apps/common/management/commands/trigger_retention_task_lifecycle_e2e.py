from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club
from apps.retention.models import RetentionTask
from apps.retention.tasks import daily_retention_check


class Command(BaseCommand):
    help = "Trigger the real retention scan for a retention task lifecycle E2E fixture."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        club_id = int(fixture["club_id"])
        student_id = int(fixture["target_student"]["student_id"])

        if RetentionTask.objects.for_club(club_id).filter(
            student_id=student_id,
            task_type=RetentionTask.TaskType.RETENTION,
            resolved_at__isnull=True,
        ).exists():
            raise CommandError("target retention task already exists before trigger")

        Club.objects.filter(id=club_id).update(is_active=True)
        with patch("apps.notifications.services.async_task") as mock_async:
            result = daily_retention_check()
        task = RetentionTask.objects.for_club(club_id).filter(
            student_id=student_id,
            trainer_id=int(fixture["trainer"]["trainer_id"]),
            task_type=RetentionTask.TaskType.RETENTION,
            resolved_at__isnull=True,
        ).first()
        if task is None:
            raise CommandError("daily retention check did not create target task")

        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "fixture_id": fixture["fixture_id"],
                    "checked_at": timezone.now().isoformat(),
                    "task_id": task.id,
                    "level": task.level,
                    "status": task.status,
                    "trainer_queued_push_count": mock_async.call_count,
                    "daily_retention_check": result,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc
        required = {"fixture_id", "club_id", "trainer", "target_student", "expected"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture
