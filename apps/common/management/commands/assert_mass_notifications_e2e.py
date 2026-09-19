from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership
from apps.notifications.models import MassNotification, NotificationPreference, PushSubscription
from apps.notifications.selectors import get_recipients_for_segment
from apps.notifications.services import get_mass_notification_recipient_ids
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert mass notification E2E side effects and recipient scoping."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_mass_notifications_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for mass notification state before failing.",
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
                    raise CommandError(f"mass notifications E2E assertion failed: {exc}") from exc
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
            "trainer",
            "owned_schedule_id",
            "foreign_schedule_id",
            "target_student",
            "non_target_student",
            "opted_out_student",
            "push_subscriptions",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        trainer_user_id = int(fixture["trainer"]["user_id"])
        target_user_id = int(fixture["target_student"]["user_id"])
        non_target_user_id = int(fixture["non_target_student"]["user_id"])
        opted_out_user_id = int(fixture["opted_out_student"]["user_id"])
        owned_schedule_id = int(fixture["owned_schedule_id"])
        foreign_schedule_id = int(fixture["foreign_schedule_id"])
        push_subscriptions = fixture["push_subscriptions"]
        expected = fixture["expected"]

        if not ClubMembership.objects.filter(
            club=club,
            user_id=trainer_user_id,
            role=ClubMembership.Role.TRAINER,
            is_active=True,
        ).exists():
            raise CommandError("trainer membership not found")

        target_student = Student.objects.for_club(club).get(id=int(fixture["target_student"]["student_id"]))
        non_target_student = Student.objects.for_club(club).get(id=int(fixture["non_target_student"]["student_id"]))
        opted_out_student = Student.objects.for_club(club).get(id=int(fixture["opted_out_student"]["student_id"]))
        if target_student.user_id != target_user_id:
            raise CommandError("target student user link mismatch")
        if non_target_student.user_id != non_target_user_id:
            raise CommandError("non-target student user link mismatch")
        if opted_out_student.user_id != opted_out_user_id:
            raise CommandError("opted-out student user link mismatch")

        raw_recipients = list(
            get_recipients_for_segment(
                club_id=club.id,
                segment_type="group",
                segment_filter={"schedule_id": owned_schedule_id},
            )
        )
        expected_raw_recipients = {target_user_id, opted_out_user_id}
        if set(raw_recipients) != expected_raw_recipients:
            raise CommandError(f"group raw recipients mismatch: {raw_recipients}")
        recipients = list(
            get_mass_notification_recipient_ids(
                club_id=club.id,
                segment_type="group",
                segment_filter={"schedule_id": owned_schedule_id},
            )
        )
        expected_recipients = [target_user_id, opted_out_user_id]
        if recipients != expected_recipients:
            raise CommandError(f"group deliverable recipients mismatch: {recipients}")
        foreign_recipients = list(
            get_recipients_for_segment(
                club_id=club.id,
                segment_type="group",
                segment_filter={"schedule_id": foreign_schedule_id},
            )
        )
        if target_user_id in foreign_recipients:
            raise CommandError("target user leaked into foreign trainer group")
        if non_target_user_id in recipients:
            raise CommandError("non-target student leaked into owned group recipients")
        if opted_out_user_id not in recipients:
            raise CommandError("training-reminder preference incorrectly suppressed mass recipient")
        if foreign_recipients != [non_target_user_id]:
            raise CommandError(f"foreign group recipients mismatch: {foreign_recipients}")

        target_subscription_count = PushSubscription.objects.filter(user_id=target_user_id, is_active=True).count()
        target_inactive_subscription_count = PushSubscription.objects.filter(
            user_id=target_user_id,
            is_active=False,
        ).count()
        non_target_subscription_count = PushSubscription.objects.filter(
            user_id=non_target_user_id,
            is_active=True,
        ).count()
        opted_out_subscription_count = PushSubscription.objects.filter(
            user_id=opted_out_user_id,
            is_active=True,
        ).count()
        if target_subscription_count != 2:
            raise CommandError("target active push subscriptions missing")
        if target_inactive_subscription_count != 1:
            raise CommandError("target inactive push subscription missing")
        if non_target_subscription_count != 1:
            raise CommandError("non-target push subscription missing")
        if opted_out_subscription_count != 1:
            raise CommandError("opted-out push subscription missing")
        expected_subscription_states = {
            int(push_subscriptions["target_active_id"]): True,
            int(push_subscriptions["target_second_active_id"]): True,
            int(push_subscriptions["target_inactive_id"]): False,
            int(push_subscriptions["non_target_active_id"]): True,
            int(push_subscriptions["opted_out_active_id"]): True,
        }
        actual_subscription_states = dict(
            PushSubscription.objects.filter(id__in=expected_subscription_states).values_list("id", "is_active")
        )
        if actual_subscription_states != expected_subscription_states:
            raise CommandError("push subscription active states mismatch")
        opted_out_preference = NotificationPreference.objects.filter(user_id=opted_out_user_id).first()
        if opted_out_preference is None:
            raise CommandError("opted-out notification preference missing")
        if opted_out_preference.disabled_categories != expected["disabled_categories"]:
            raise CommandError("opted-out notification preference categories mismatch")

        owner_notification = self._get_notification(
            club=club,
            text=expected["owner_message"],
            sent_by_id=owner_user_id,
            schedule_id=owned_schedule_id,
            expected_count=int(expected["recipient_count"]),
        )
        trainer_notification = self._get_notification(
            club=club,
            text=expected["trainer_message"],
            sent_by_id=trainer_user_id,
            schedule_id=owned_schedule_id,
            expected_count=int(expected["recipient_count"]),
        )
        denied_texts = [
            expected["trainer_club_message"],
            expected["trainer_foreign_message"],
        ]
        for text in denied_texts:
            if MassNotification.objects.for_club(club).filter(text=text).exists():
                raise CommandError(f"denied mass notification row was created: {text}")
        if MassNotification.objects.for_club(club).filter(
            text=expected["trainer_message"],
            segment_filter__schedule_id=foreign_schedule_id,
        ).exists():
            raise CommandError("trainer notification was created for a foreign group")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "raw_owned_group_recipients": raw_recipients,
            "owned_group_recipients": recipients,
            "foreign_group_recipients": foreign_recipients,
            "opted_out": {
                "user_id": opted_out_user_id,
                "disabled_categories": opted_out_preference.disabled_categories,
                "included_in_mass_recipients": opted_out_user_id in recipients,
            },
            "owner_notification": {
                "id": owner_notification.id,
                "recipient_count": owner_notification.recipient_count,
            },
            "trainer_notification": {
                "id": trainer_notification.id,
                "recipient_count": trainer_notification.recipient_count,
            },
            "subscriptions": {
                "target_active": target_subscription_count,
                "target_inactive": target_inactive_subscription_count,
                "non_target_active": non_target_subscription_count,
                "opted_out_active": opted_out_subscription_count,
            },
            "denied_notifications_created": False,
        }

    def _get_notification(
        self,
        *,
        club,
        text: str,
        sent_by_id: int,
        schedule_id: int,
        expected_count: int,
    ) -> MassNotification:
        notification = (
            MassNotification.objects.for_club(club)
            .filter(
                text=text,
                sent_by_id=sent_by_id,
                segment_type="group",
                segment_filter__schedule_id=schedule_id,
            )
            .order_by("-created_at")
            .first()
        )
        if notification is None:
            raise CommandError(f"mass notification not found: {text}")
        if notification.recipient_count != expected_count:
            raise CommandError(
                f"mass notification recipient count mismatch: {notification.recipient_count}"
            )
        return notification
