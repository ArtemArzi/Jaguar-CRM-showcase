from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.notifications.models import NotificationTemplate, PushSubscription


class Command(BaseCommand):
    help = "Prepare an isolated owner notification template settings E2E fixture."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        output_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared owner notification template settings E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"owner-notification-template-settings-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"OwnerNotificationTemplates-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Notification Templates E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Notification Templates E2E",
            max_push_per_week=3,
            feedback_delay_hours=2,
        )
        control_club = Club.objects.create(
            name=f"Jaguar Notification Templates Control {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=control_club,
            primary_color="#222222",
            accent_color="#222222",
            club_name_display="Control Notification Templates",
            max_push_per_week=3,
            feedback_delay_hours=2,
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        owner_push = PushSubscription.objects.create(
            user=owner_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/owner-admin-device",
            key_p256dh=f"p256dh-{unique}",
            key_auth=f"auth-{unique}",
            is_active=True,
        )

        template = NotificationTemplate.objects.create(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="Initial reminder title",
            body_template="Initial reminder body for {name}",
            is_enabled=True,
        )
        control_template = NotificationTemplate.objects.create(
            club=control_club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="Control reminder title",
            body_template="Control reminder body",
            is_enabled=True,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "control_club_id": control_club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "admin_push": {
                "subscription_id": owner_push.id,
                "endpoint": owner_push.endpoint,
            },
            "template": {
                "template_id": template.id,
                "trigger_type": template.trigger_type,
                "initial_title": template.title_template,
                "initial_body": template.body_template,
            },
            "control_template": {
                "template_id": control_template.id,
                "initial_title": control_template.title_template,
                "initial_body": control_template.body_template,
            },
            "expected": {
                "max_push_per_week": 8,
                "feedback_delay_hours": 5,
                "quiet_hours_start": "22:15",
                "quiet_hours_end": "08:05",
                "updated_title": f"E2E reminder {unique}",
                "updated_body": f"E2E body {unique} for {{name}}",
                "template_is_enabled": False,
                "expiry_days_before": 10,
                "default_template_count": len(NotificationTemplate.TriggerType.values),
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@owner-notification-template-settings-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
