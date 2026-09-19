from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated owner trainer rate grid E2E fixture."

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
                "Prepared owner trainer rate grid E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, trainer_id={fixture['seeded_trainer']['trainer_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"owner-trainer-rate-grid-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"OwnerTrainerRateGrid-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Owner Trainer Rate Grid E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Rates E2E",
        )
        control_club = Club.objects.create(
            name=f"Jaguar Trainer Rate Grid Control {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=control_club,
            primary_color="#222222",
            accent_color="#222222",
            club_name_display="Control Trainer Rates",
        )

        location = Location.objects.create(
            club=club,
            name=f"Rate Hall {unique}",
            address="Owner trainer rate grid E2E fixture",
        )
        group_type = TrainingType.objects.create(
            club=club,
            name=f"Rate Group {unique}",
            slug=f"rate-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("500.00"),
            trial_free=False,
        )
        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Rate Personal {unique}",
            slug=f"rate-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        seeded_trainer = Trainer.objects.create(
            club=club,
            first_name="Seeded",
            last_name="Rates",
            phone=f"+155560{phone_seed}",
        )
        TrainerLocation.objects.create(club=club, trainer=seeded_trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=seeded_trainer,
            location=location,
            training_type=group_type,
            percent=Decimal("20.00"),
        )

        tariff = Tariff.objects.create(
            club=club,
            training_type=personal_type,
            name=f"Personal Salary Proof {unique}",
            price=Decimal("1000.00"),
            trainings_limit=4,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Rate",
            last_name="Student",
            phone=f"+155561{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=Decimal("1000.00"),
            trainings_left=3,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(0, 30),
            group_name=f"Rate Personal Proof {unique}",
            trainer=seeded_trainer,
            location=location,
            training_type=personal_type,
            one_time_date=today,
            is_active=True,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=personal_type,
            trainer=seeded_trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=subscription,
            is_debt=False,
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
            "location": {
                "location_id": location.id,
                "name": location.name,
            },
            "training_types": {
                "group": {"training_type_id": group_type.id, "name": group_type.name},
                "personal": {"training_type_id": personal_type.id, "name": personal_type.name},
            },
            "seeded_trainer": {
                "trainer_id": seeded_trainer.id,
                "name": str(seeded_trainer),
            },
            "student": {
                "student_id": student.id,
            },
            "subscription_id": subscription.id,
            "checkin_id": checkin.id,
            "expected": {
                "new_trainer_first_name": f"Grid{unique[:4]}",
                "new_trainer_last_name": "Coach",
                "new_trainer_group_rate": "22.00",
                "new_trainer_personal_rate": "47.50",
                "seeded_personal_rate": "35.00",
                "salary_amount": "350.00",
                "subscription_price": "1000.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@owner-trainer-rate-grid-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
