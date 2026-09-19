from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student


class Command(BaseCommand):
    help = "Prepare an isolated fixture for push subscription and notification preferences E2E."

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
                "Prepared push notification preferences E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, child_id={fixture['child']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"push-notification-preferences-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        parent_password = f"PushPreferenceParent-{unique}-pass"
        student_password = f"PushPreferenceStudent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Push Preference E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Push Preference E2E",
        )

        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=parent_password,
        )
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=student_password,
        )
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)

        child = Student.objects.create(
            club=club,
            first_name="Push",
            last_name="Child",
            phone=f"+15559{phone_seed}1",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        student = Student.objects.create(
            club=club,
            first_name="Push",
            last_name="Student",
            phone=f"+15559{phone_seed}2",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "child": {
                "student_id": child.id,
                "name": str(child),
            },
            "student": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
                "student_id": student.id,
                "name": str(student),
            },
            "expected": {
                "parent": {
                    "endpoint": f"https://push-e2e.invalid/{fixture_id}/parent-device",
                    "key_p256dh": f"p256dh-parent-{unique}",
                    "key_auth": f"auth-parent-{unique}",
                    "disabled_categories": ["child_checkin"],
                    "enabled_category": "subscription_alerts",
                },
                "student": {
                    "endpoint": f"https://push-e2e.invalid/{fixture_id}/student-device",
                    "key_p256dh": f"p256dh-student-{unique}",
                    "key_auth": f"auth-student-{unique}",
                    "disabled_categories": ["training_reminders"],
                    "enabled_category": "subscription_alerts",
                },
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@push-notification-preferences-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
