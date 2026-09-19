from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership
from apps.notifications.models import NotificationPreference, PushSubscription
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert push subscription and notification preferences E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_push_notification_preferences_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for push state before failing.",
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
                    raise CommandError(f"push notification preferences E2E assertion failed: {exc}") from exc
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

        required = {"fixture_id", "club_id", "parent", "child", "student", "expected"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        parent_user_id = int(fixture["parent"]["user_id"])
        student_user_id = int(fixture["student"]["user_id"])
        child = Student.objects.for_club(club).get(id=int(fixture["child"]["student_id"]))
        student = Student.objects.for_club(club).get(id=int(fixture["student"]["student_id"]))
        parent_membership = ClubMembership.objects.filter(
            user_id=parent_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.PARENT,
        ).first()
        if parent_membership is None:
            raise CommandError("parent membership not found")
        if child.parent_user_id != parent_user_id:
            raise CommandError("child is not linked to parent")
        student_membership = ClubMembership.objects.filter(
            user_id=student_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.STUDENT,
        ).first()
        if student_membership is None:
            raise CommandError("student membership not found")
        if student.user_id != student_user_id:
            raise CommandError("student is not linked to user")

        expected = fixture["expected"]
        parent_push = self._collect_user_push_evidence(
            user_id=parent_user_id,
            expected=expected["parent"],
        )
        student_push = self._collect_user_push_evidence(
            user_id=student_user_id,
            expected=expected["student"],
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "parent": {
                "user_id": parent_user_id,
                "membership_role": parent_membership.role,
            },
            "child": {
                "student_id": child.id,
                "parent_user_id": child.parent_user_id,
            },
            "student": {
                "student_id": student.id,
                "user_id": student.user_id,
                "membership_role": student_membership.role,
            },
            "subscription": parent_push["subscription"],
            "preferences": parent_push["preferences"],
            "parent_push": parent_push,
            "student_push": student_push,
        }

    def _collect_user_push_evidence(self, *, user_id: int, expected: dict) -> dict:
        subscriptions = list(PushSubscription.objects.filter(user_id=user_id, is_active=True).order_by("id"))
        if not subscriptions:
            raise CommandError("push subscription not found")

        matching_subscription = next(
            (subscription for subscription in subscriptions if subscription.endpoint == expected["endpoint"]),
            None,
        )
        if matching_subscription is None:
            raise CommandError("push subscription endpoint mismatch")
        if matching_subscription.key_p256dh != expected["key_p256dh"]:
            raise CommandError("push subscription p256dh key mismatch")
        if matching_subscription.key_auth != expected["key_auth"]:
            raise CommandError("push subscription auth key mismatch")

        preference = NotificationPreference.objects.filter(user_id=user_id).first()
        if preference is None:
            raise CommandError("notification preferences not found")
        if preference.disabled_categories != expected["disabled_categories"]:
            raise CommandError("notification disabled categories mismatch")

        return {
            "subscription": {
                "active_count": len(subscriptions),
                "endpoint_matches": True,
                "id": matching_subscription.id,
            },
            "preferences": {
                "disabled_categories": preference.disabled_categories,
            },
        }
