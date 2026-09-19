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

from apps.attendance.models import Schedule, TrainingGroupRolloutState
from apps.attendance.services import enroll_student_in_schedule, generate_kiosk_pin
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for the real-stack browser E2E gate."

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
                "Prepared real-stack E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"rs-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        phone_suffix = f"{(Club.objects.count() + 1) % 10000:04d}"

        club = Club.objects.create(
            name=f"Jaguar Real Stack E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Real Stack E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Main Hall {fixture_id}",
            address="Real stack E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer")
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="RealStack",
            last_name="Trainer",
            phone=f"+1555100{phone_suffix}",
            user=trainer_user,
        )

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Muay Thai {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Personal E2E {fixture_id}",
            slug=f"personal-e2e-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=Decimal("1500.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )

        student_user = self._create_user(fixture_id=fixture_id, role="student")
        parent_user = self._create_user(fixture_id=fixture_id, role="parent")
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        student = Student.objects.create(
            club=club,
            first_name="RealStack",
            last_name="Student",
            phone=f"+1555200{phone_suffix}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
        )
        StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )

        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Real Stack 5-Pack {fixture_id}",
            price=Decimal("10000.00"),
            trainings_limit=10,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        trainings_left_before = 5
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=trainings_left_before,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Real Stack Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            starts_on=today,
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "phone_suffix": phone_suffix,
            "student_id": student.id,
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "subscription_id": subscription.id,
            "expected": {
                "trainings_left_before": trainings_left_before,
                "trainings_left_after": trainings_left_before - 1,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@real-stack-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=None)
