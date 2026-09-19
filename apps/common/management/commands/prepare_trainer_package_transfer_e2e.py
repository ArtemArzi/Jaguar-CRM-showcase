from __future__ import annotations

import json
import uuid
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule, TrainingGroupRolloutState
from apps.billing.models import Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated trainer package transfer E2E fixture."

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
                "Prepared trainer package transfer E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-package-transfer-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"TrainerPackageTransferOwner-{unique}-pass"
        trainer_password = f"TrainerPackageTransferTrainer-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Package Transfer E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Package Transfer E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Transfer Hall {fixture_id}",
            address="Trainer package transfer E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        package_owner = Trainer.objects.create(
            club=club,
            first_name="Package",
            last_name="Owner",
            phone=f"+155530{phone_seed}",
        )
        actual_trainer = Trainer.objects.create(
            club=club,
            first_name="Actual",
            last_name="Coach",
            phone=f"+155540{phone_seed}",
            user=trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Personal Transfer E2E {fixture_id}",
            slug=f"personal-transfer-e2e-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("1500.00"),
            trial_free=False,
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Transfer Personal 6-Pack {fixture_id}",
            price=Decimal("6000.00"),
            trainings_limit=6,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        for trainer in (package_owner, actual_trainer):
            TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=actual_trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )

        student = Student.objects.create(
            club=club,
            first_name="Transfer",
            last_name="Student",
            phone=f"+155550{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Transfer Personal Session {fixture_id}",
            trainer=actual_trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "student_id": student.id,
            "student_name": str(student),
            "training_type_id": training_type.id,
            "tariff_id": tariff.id,
            "tariff_name": tariff.name,
            "schedule_id": schedule.id,
            "checkin_date": today.isoformat(),
            "package_owner_trainer_id": package_owner.id,
            "package_owner_trainer_name": str(package_owner),
            "actual_trainer_id": actual_trainer.id,
            "actual_trainer_name": str(actual_trainer),
            "actual_trainer": {
                "user_id": trainer_user.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "expected": {
                "subscription_trainings_left_before_checkin": 6,
                "subscription_trainings_left_after_checkin": 5,
                "salary_amount": "500.00",
                "rate_percent": "50.00",
                "payment_amount": "6000.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-package-transfer-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
