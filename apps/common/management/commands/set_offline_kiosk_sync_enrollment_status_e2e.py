from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment


class Command(BaseCommand):
    help = "Set the prepared offline kiosk sync enrollment status for E2E terminal-failure checks."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")
        parser.add_argument(
            "--status",
            required=True,
            choices=[ScheduleEnrollment.Status.ACTIVE, ScheduleEnrollment.Status.FROZEN],
            help="Enrollment status to set.",
        )
        parser.add_argument(
            "--target",
            choices=["primary", "terminal"],
            default="primary",
            help="Fixture enrollment to change; terminal keeps its idempotency identity separate.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        target_status = options["status"]
        target_name = options["target"]
        target = fixture if target_name == "primary" else fixture["terminal"]

        with transaction.atomic():
            enrollment = (
                ScheduleEnrollment.objects.for_club(int(fixture["club_id"]))
                .select_for_update()
                .filter(
                    id=int(target["enrollment_id"]),
                    student_id=int(target["student_id"]),
                    schedule_id=int(fixture["schedule_id"]),
                )
                .first()
            )
            if enrollment is None:
                raise CommandError("offline kiosk sync enrollment not found")

            previous_status = enrollment.status
            enrollment.status = target_status
            enrollment.save(update_fields=["status", "updated_at"])

        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "fixture_id": fixture["fixture_id"],
                    "target": target_name,
                    "checked_at": timezone.now().isoformat(),
                    "enrollment": {
                        "id": enrollment.id,
                        "previous_status": previous_status,
                        "status": enrollment.status,
                    },
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

        required = {"fixture_id", "club_id", "student_id", "schedule_id", "enrollment_id", "terminal"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        terminal_required = {"student_id", "enrollment_id"}
        terminal_missing = sorted(terminal_required - set(fixture["terminal"]))
        if terminal_missing:
            raise CommandError(
                f"fixture terminal is missing required fields: {', '.join(terminal_missing)}"
            )
        return fixture
