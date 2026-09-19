from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.tasks import log_parent_event, reverse_parent_checkin_push
from apps.feedback.tasks import send_churned_survey_push, send_trial_feedback_push
from apps.notifications.tasks import (
    check_missed_trainings,
    check_subscription_expiry,
    check_trainings_left_push,
    send_training_reminders,
    send_training_reminders_24h,
)


class Command(BaseCommand):
    help = "Trigger automatic notification tasks for the automatic notification lifecycle E2E fixture."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())

        with patch("apps.notifications.services.async_task") as mock_async:
            expiry_result = check_subscription_expiry()
            check_trainings_left_push(
                int(fixture["trainings_left_checkin_id"]),
                int(fixture["club_id"]),
            )
            check_trainings_left_push(
                int(fixture["last_training_checkin_id"]),
                int(fixture["club_id"]),
            )
            occurrence_at = datetime.fromisoformat(fixture["reminder_occurrence_at"])
            with patch(
                "apps.notifications.tasks.timezone.now",
                return_value=occurrence_at - timedelta(hours=24),
            ):
                reminder_24h_result = send_training_reminders_24h()
            with patch(
                "apps.notifications.tasks.timezone.now",
                return_value=occurrence_at - timedelta(hours=1),
            ):
                reminder_1h_result = send_training_reminders()
            missed_result = check_missed_trainings()
            before_child_checkin = mock_async.call_count
            log_parent_event(
                int(fixture["trainings_left_checkin_id"]),
                int(fixture["club_id"]),
            )
            reverse_parent_checkin_push(
                int(fixture["last_training_checkin_id"]),
                int(fixture["club_id"]),
            )
            child_checkin_queued_push_count = mock_async.call_count - before_child_checkin

        with patch("apps.notifications.services.async_task") as feedback_async:
            send_trial_feedback_push(
                int(fixture["student"]["student_id"]),
                int(fixture["club_id"]),
            )
            send_churned_survey_push(
                int(fixture["student"]["student_id"]),
                int(fixture["club_id"]),
            )

        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "fixture_id": fixture["fixture_id"],
                    "checked_at": timezone.now().isoformat(),
                    "queued_push_count": mock_async.call_count,
                    "subscription_expiry": expiry_result,
                    "training_reminder_24h": reminder_24h_result,
                    "training_reminder_1h": reminder_1h_result,
                    "missed_training": missed_result,
                    "child_checkin": {
                        "queued_push_count": child_checkin_queued_push_count,
                    },
                    "feedback_surveys": {
                        "queued_push_count": feedback_async.call_count,
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
        required = {
            "fixture_id",
            "club_id",
            "student",
            "parent",
            "trainings_left_checkin_id",
            "last_training_checkin_id",
            "reminder_occurrence_at",
            "expected",
            "feedback_form_ids",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture
