from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.feedback.models import FeedbackForm
from apps.notifications.models import NotificationPreference, PushSubscription, SentNotification


class Command(BaseCommand):
    help = "Assert automatic notification lifecycle E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_automatic_notification_lifecycle_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for notification side effects before failing.",
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
                    raise CommandError(f"automatic notification lifecycle E2E assertion failed: {exc}") from exc
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
            "student",
            "parent",
            "push_subscription_ids",
            "reminder_schedule_id",
            "reminder_occurrence_date",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        student_id = int(fixture["student"]["student_id"])
        expected_student_types = set(fixture["expected"]["student_notification_types"])
        expected_parent_types = set(fixture["expected"]["parent_notification_types"])
        expected_types = expected_student_types | expected_parent_types

        sent_rows = list(
            SentNotification.objects.for_club(club_id)
            .filter(student_id=student_id)
            .order_by("notification_type")
        )
        sent_types = {row.notification_type for row in sent_rows}
        missing = sorted(expected_types - sent_types)
        if missing:
            raise CommandError(f"expected notification types missing: {', '.join(missing)}")
        extras = sorted(sent_types - expected_types)
        if extras:
            raise CommandError(f"unexpected notification types sent: {', '.join(extras)}")
        suppressed_child_checkin_types = set(
            fixture["expected"].get("child_checkin_suppressed_notification_types", [])
        )
        recorded_suppressed_types = sorted(sent_types & suppressed_child_checkin_types)
        if recorded_suppressed_types:
            raise CommandError(
                "child check-in opt-out did not suppress notification records: "
                + ", ".join(recorded_suppressed_types)
            )

        reminder_rows = list(
            SentNotification.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                occurrence_schedule_id=int(fixture["reminder_schedule_id"]),
                occurrence_date=fixture["reminder_occurrence_date"],
            )
            .order_by("delivery_stage")
        )
        reminder_stage_state = {
            row.delivery_stage: row.delivery_state for row in reminder_rows
        }
        expected_stage_state = {
            SentNotification.DeliveryStage.ONE_HOUR: SentNotification.DeliveryState.QUEUED,
            SentNotification.DeliveryStage.TWENTY_FOUR_HOUR: SentNotification.DeliveryState.QUEUED,
        }
        if reminder_stage_state != expected_stage_state:
            raise CommandError("automatic reminder occurrence stage ledger mismatch")

        student_push = PushSubscription.objects.filter(
            id=int(fixture["push_subscription_ids"]["student"]),
            user_id=int(fixture["student"]["user_id"]),
            is_active=True,
        ).first()
        if student_push is None:
            raise CommandError("active student push subscription missing")
        parent_push = PushSubscription.objects.filter(
            id=int(fixture["push_subscription_ids"]["parent"]),
            user_id=int(fixture["parent"]["user_id"]),
            is_active=True,
        ).first()
        if parent_push is None:
            raise CommandError("active parent push subscription missing")
        preference = NotificationPreference.objects.filter(
            user_id=int(fixture["parent"]["user_id"]),
        ).first()
        if preference is None:
            raise CommandError("parent notification preference missing")
        expected_disabled_categories = fixture["expected"].get(
            "parent_disabled_categories",
            fixture["expected"]["feedback_disabled_categories"],
        )
        if preference.disabled_categories != expected_disabled_categories:
            raise CommandError("parent notification opt-out mismatch")

        feedback_forms = {
            "trial": FeedbackForm.objects.for_club(club_id).filter(
                id=int(fixture["feedback_form_ids"]["trial"]),
                trigger_type=FeedbackForm.TriggerType.TRIAL,
                is_active=True,
            ).exists(),
            "churned": FeedbackForm.objects.for_club(club_id).filter(
                id=int(fixture["feedback_form_ids"]["churned"]),
                trigger_type=FeedbackForm.TriggerType.CHURNED,
                is_active=True,
            ).exists(),
        }
        if not all(feedback_forms.values()):
            raise CommandError("active feedback survey forms missing")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "sent_notification_types": sorted(sent_types),
            "student_notification_types": sorted(expected_student_types),
            "reminder_stage_state": reminder_stage_state,
            "parent_notification_types": sorted(expected_parent_types),
            "push_subscriptions": {
                "student_active": student_push.id,
                "parent_active": parent_push.id,
            },
            "feedback_surveys": {
                "forms": feedback_forms,
                "parent_disabled_categories": preference.disabled_categories,
                "trial_feedback_push_expected": fixture["expected"]["trial_feedback_push_expected"],
                "churned_survey_push_expected": fixture["expected"]["churned_survey_push_expected"],
            },
            "child_checkin": {
                "suppressed_notification_types": sorted(suppressed_child_checkin_types),
                "recorded_suppressed_count": 0,
                "queued_push_expected": fixture["expected"].get("child_checkin_expected_queued_pushes", 0),
            },
        }
