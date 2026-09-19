from __future__ import annotations

import json
import time
from datetime import time as dt_time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.notifications.models import NotificationTemplate, PushSubscription


class Command(BaseCommand):
    help = "Assert owner notification template settings E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_owner_notification_template_settings_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for notification settings state before failing.",
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
                    raise CommandError(f"owner notification template settings E2E assertion failed: {exc}") from exc
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

        required = {"fixture_id", "club_id", "control_club_id", "owner", "admin_push", "template", "expected"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        control_club = Club.objects.get(id=int(fixture["control_club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        expected = fixture["expected"]

        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        settings = ClubSettings.objects.get(club=club)
        self._assert_equal("max push per week", settings.max_push_per_week, expected["max_push_per_week"])
        self._assert_equal("feedback delay hours", settings.feedback_delay_hours, expected["feedback_delay_hours"])
        self._assert_equal(
            "quiet hours start",
            settings.quiet_hours_start,
            dt_time.fromisoformat(expected["quiet_hours_start"]),
        )
        self._assert_equal(
            "quiet hours end",
            settings.quiet_hours_end,
            dt_time.fromisoformat(expected["quiet_hours_end"]),
        )

        template_count = NotificationTemplate.objects.for_club(club).count()
        self._assert_equal("default template count", template_count, expected["default_template_count"])
        template = NotificationTemplate.objects.for_club(club).get(id=int(fixture["template"]["template_id"]))
        self._assert_equal("template title", template.title_template, expected["updated_title"])
        self._assert_equal("template body", template.body_template, expected["updated_body"])
        self._assert_equal("template enabled", template.is_enabled, expected["template_is_enabled"])

        expiry_template = (
            NotificationTemplate.objects.for_club(club)
            .filter(trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D)
            .first()
        )
        if expiry_template is None:
            raise CommandError("missing seeded subscription expiry template")
        self._assert_equal("expiry days before", expiry_template.days_before, expected["expiry_days_before"])

        control_settings = ClubSettings.objects.get(club=control_club)
        self._assert_equal("control max push per week", control_settings.max_push_per_week, 3)
        control_template = NotificationTemplate.objects.for_club(control_club).get(
            id=int(fixture["control_template"]["template_id"]),
        )
        self._assert_equal(
            "control template title",
            control_template.title_template,
            fixture["control_template"]["initial_title"],
        )
        self._assert_equal(
            "control template body",
            control_template.body_template,
            fixture["control_template"]["initial_body"],
        )
        self._assert_equal("control template enabled", control_template.is_enabled, True)
        if NotificationTemplate.objects.for_club(control_club).filter(
            title_template=expected["updated_title"],
        ).exists():
            raise CommandError("updated template leaked into control club")

        if PushSubscription.objects.filter(
            id=int(fixture["admin_push"]["subscription_id"]),
            user_id=owner_user_id,
            endpoint=fixture["admin_push"]["endpoint"],
        ).exists():
            raise CommandError("admin push subscription was not unsubscribed")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "settings": {
                "max_push_per_week": settings.max_push_per_week,
                "feedback_delay_hours": settings.feedback_delay_hours,
                "quiet_hours_start": settings.quiet_hours_start,
                "quiet_hours_end": settings.quiet_hours_end,
            },
            "templates": {
                "count": template_count,
                "template_id": template.id,
                "trigger_type": template.trigger_type,
                "is_enabled": template.is_enabled,
                "expiry_days_before": expiry_template.days_before,
            },
            "admin_push": {
                "server_subscription_removed": True,
            },
            "tenant_control": {
                "control_club_id": control_club.id,
                "unchanged": True,
            },
        }

    def _assert_equal(self, label: str, actual, expected) -> None:
        if actual != expected:
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")
