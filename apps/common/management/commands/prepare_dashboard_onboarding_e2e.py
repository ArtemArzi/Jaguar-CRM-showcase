from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import TrainingGroupRolloutState
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.onboarding.models import OnboardingDraft


class Command(BaseCommand):
    help = "Prepare an isolated dashboard onboarding wizard E2E fixture."

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
                "Prepared dashboard onboarding E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"dashboard-onboarding-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"DashboardOnboarding-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Dashboard Onboarding E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Dashboard Onboarding E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name="Onboarding Hall",
            address="E2E",
        )
        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)

        control_club = Club.objects.create(
            name=f"Jaguar Dashboard Onboarding Control {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=control_club,
            primary_color="#222222",
            accent_color="#222222",
            club_name_display="Control Dashboard Onboarding E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=control_club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        control_draft = OnboardingDraft.objects.create(
            club=control_club,
            current_step=3,
            data={"3": {"students": []}},
            is_completed=False,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "location_id": location.id,
            "control_club_id": control_club.id,
            "control_draft_id": control_draft.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "expected": {
                "completed_draft_count": 1,
                "active_draft_count": 0,
                "completed_current_step": 5,
                "control_current_step": 3,
                "trainer_count": 1,
                "schedule_count": 1,
                "schedule_group": "Adults E2E",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@dashboard-onboarding-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
