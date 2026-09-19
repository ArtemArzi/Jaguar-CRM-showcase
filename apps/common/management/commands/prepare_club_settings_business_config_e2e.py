from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import TrainingGroupRolloutState
from apps.billing.models import TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Prepare an isolated owner fixture for club settings business config E2E."

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
                "Prepared club settings business config E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"club-settings-business-config-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"ClubSettingsOwner-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Club Settings E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#111111",
            club_name_display="Jaguar Settings Before",
            freeze_enabled=False,
            freeze_max_days=30,
            freeze_max_count=None,
            min_trainings_to_freeze=2,
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )

        control_club = Club.objects.create(
            name=f"Jaguar Club Settings Control {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=control_club,
            primary_color="#222222",
            accent_color="#222222",
            club_name_display="Control Club Settings",
            freeze_enabled=False,
            freeze_max_days=30,
            freeze_max_count=None,
            min_trainings_to_freeze=2,
        )
        TrainingGroupRolloutState.objects.create(
            club=control_club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )

        personal_training_type_name = f"Settings Personal {unique}"
        TrainingType.objects.create(
            club=club,
            name=personal_training_type_name,
            slug=f"settings-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            trial_free=False,
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Settings",
            last_name="Coach",
            phone="",
        )
        phone_seed = str(uuid.uuid4().int % 10_000_000_000).zfill(10)
        sale_student = Student.objects.create(
            club=club,
            first_name="SettingsSale",
            last_name="Student",
            phone=f"+7{phone_seed}",
            status=Student.Status.ACTIVE,
        )
        drop_in_student = Student.objects.create(
            club=club,
            first_name="SettingsDropin",
            last_name="Student",
            phone=f"+8{phone_seed}",
            status=Student.Status.ACTIVE,
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
            "expected": {
                "club_name_display": f"Jaguar Settings {unique}",
                "primary_color": "#185A9D",
                "timezone": "Asia/Yekaterinburg",
                "freeze_enabled": True,
                "freeze_max_days": 14,
                "freeze_max_count": 2,
                "min_trainings_to_freeze": 3,
                "location_initial_name": f"Settings Hall Draft {unique}",
                "location_initial_address": "E2E draft address, 1",
                "location_name": f"Settings Hall {unique}",
                "location_address": "E2E settings address, 12",
                "location_delete_name": f"Settings Temp Hall {unique}",
                "location_delete_address": "E2E temporary address, 99",
                "grade_system_name": f"Settings Muay Thai {unique}",
                "grade_initial_name": f"Settings White {unique}",
                "grade_final_name": f"Settings Yellow {unique}",
                "grade_final_order": 1,
                "grade_final_min_trainings": 12,
                "grade_delete_name": f"Settings Temp Grade {unique}",
                "grade_delete_order": 2,
                "grade_system_delete_name": f"Settings Temp System {unique}",
                "document_type_initial_name": f"Settings Medical Initial {unique}",
                "document_type_name": f"Settings Medical Certificate {unique}",
                "document_type_description": "Edited by club settings E2E",
                "document_type_scope": "children",
                "document_type_is_required": False,
                "document_type_is_active": False,
                "training_type_initial_name": f"Settings Group Initial {unique}",
                "training_type_name": f"Settings Group {unique}",
                "personal_training_type_name": personal_training_type_name,
                "drop_in_price": "750.00",
                "trial_free": False,
                "training_type_is_active": False,
                "tariff_initial_name": f"Settings Trial Pack {unique}",
                "tariff_initial_price": "3000.00",
                "tariff_initial_trainings_limit": 6,
                "tariff_initial_duration_days": 30,
                "tariff_initial_description": "Initial club settings E2E tariff",
                "tariff_name": f"Settings 8-Pack {unique}",
                "tariff_price": "2500.00",
                "tariff_trainings_limit": 8,
                "tariff_duration_days": 45,
                "tariff_scope": "location",
                "tariff_is_active": False,
                "tariff_description": "Created by club settings E2E",
                "personal_tariff_name": f"Settings Personal Single {unique}",
                "personal_tariff_price": "2200.00",
                "personal_tariff_duration_days": 30,
                "discount_initial_name": f"Settings Intro Discount {unique}",
                "discount_initial_value": "5.00",
                "discount_name": f"Settings Family Discount {unique}",
                "discount_type": "percent",
                "discount_value": "15.00",
                "discount_is_active": False,
                "discounted_tariff_amount": "2125.00",
                "drop_in_debt_amount": "750.00",
            },
            "consumption": {
                "sale_student_id": sale_student.id,
                "drop_in_student_id": drop_in_student.id,
                "trainer_id": trainer.id,
                "schedule_group_name": f"Settings Consumption {unique}",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@club-settings-business-config-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
