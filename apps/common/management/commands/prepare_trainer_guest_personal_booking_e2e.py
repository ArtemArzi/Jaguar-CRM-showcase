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

from apps.attendance.models import Schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated trainer guest/personal booking E2E fixture."

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
                "Prepared trainer guest/personal booking E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, schedule_id={fixture['group_schedule_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        booking_date = today + timedelta(days=1)
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-guest-personal-booking-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerGuestPersonal-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Guest Personal E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Booking E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Booking Hall {fixture_id}",
            address="Trainer guest personal booking E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Booking",
            last_name="Trainer",
            phone=f"+155510{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        group_training_type = TrainingType.objects.create(
            club=club,
            name=f"Guest Group {fixture_id}",
            slug=f"guest-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        personal_training_type = TrainingType.objects.create(
            club=club,
            name=f"Personal Booking {fixture_id}",
            slug=f"personal-booking-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("2500.00"),
            trial_free=False,
        )
        for training_type in (group_training_type, personal_training_type):
            TrainerRate.objects.create(
                club=club,
                trainer=trainer,
                location=location,
                training_type=training_type,
                percent=Decimal("50.00"),
            )

        group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=booking_date.weekday(),
            start_time=time(0, 0),
            end_time=time(0, 5),
            group_name=f"Guest Booking Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=group_training_type,
            is_active=True,
        )

        guest_student = Student.objects.create(
            club=club,
            first_name="Guestbook",
            last_name="Visitor",
            phone=f"+155520{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        personal_student = Student.objects.create(
            club=club,
            first_name="Personal",
            last_name="Student",
            phone=f"+155530{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        personal_tariff = Tariff.objects.create(
            club=club,
            training_type=personal_training_type,
            name=f"Personal 4-Pack {fixture_id}",
            price=Decimal("9000.00"),
            trainings_limit=4,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        personal_subscription = Subscription.objects.create(
            club=club,
            student=personal_student,
            tariff=personal_tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=personal_tariff.price,
            trainings_left=4,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=personal_tariff.scope,
            location=None,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "location": {
                "id": location.id,
                "name": location.name,
            },
            "group_schedule_id": group_schedule.id,
            "booking_date": booking_date.isoformat(),
            "guest_student": {
                "student_id": guest_student.id,
                "first_name": guest_student.first_name,
                "last_name": guest_student.last_name,
                "name": str(guest_student),
                "search": guest_student.first_name,
            },
            "personal_student": {
                "student_id": personal_student.id,
                "name": str(personal_student),
            },
            "personal_training_type": {
                "id": personal_training_type.id,
                "name": personal_training_type.name,
            },
            "personal_tariff": {
                "id": personal_tariff.id,
                "name": personal_tariff.name,
            },
            "personal_subscription_id": personal_subscription.id,
            "personal_booking": {
                "date": booking_date.isoformat(),
                "start_time": "00:10",
                "end_time": "00:50",
            },
            "expected": {
                "personal_trainings_left": 4,
                "guest_idempotency_key": (
                    f"trainer-walk-in-{group_schedule.id}-{booking_date.isoformat()}-student-{guest_student.id}"
                ),
                "personal_idempotency_key_prefix": "trainer-personal-booking:",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-guest-personal-booking-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
