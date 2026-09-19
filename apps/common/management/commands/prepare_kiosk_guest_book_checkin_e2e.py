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
from apps.attendance.services import generate_kiosk_pin
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for kiosk guest book-and-check-in E2E."

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
                "Prepared kiosk guest book-and-check-in E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_suffix = f"{int(unique, 16) % 10000:04d}"
        drop_in_phone_suffix = f"{(int(phone_suffix) + 1) % 10000:04d}"
        trial_phone_suffix = f"{(int(phone_suffix) + 2) % 10000:04d}"
        fixture_id = f"kiosk-guest-book-checkin-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        club = Club.objects.create(
            name=f"Jaguar Kiosk Guest E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Kiosk Guest E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Guest Hall {fixture_id}",
            address="Kiosk guest E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer")
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Guest",
            last_name="Trainer",
            phone=f"+1555600{phone_suffix}",
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
            name=f"Guest Group {fixture_id}",
            slug=f"guest-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        drop_in_training_type = TrainingType.objects.create(
            club=club,
            name=f"Drop-In Group {fixture_id}",
            slug=f"drop-in-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("800.00"),
            trial_free=False,
        )
        trial_training_type = TrainingType.objects.create(
            club=club,
            name=f"Trial Free Group {fixture_id}",
            slug=f"trial-free-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=True,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("20.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=drop_in_training_type,
            percent=Decimal("20.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=trial_training_type,
            percent=Decimal("20.00"),
        )

        student_user = self._create_user(fixture_id=fixture_id, role="student")
        parent_user = self._create_user(fixture_id=fixture_id, role="parent")
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        student = Student.objects.create(
            club=club,
            first_name="Guest",
            last_name="Student",
            phone=f"+1555700{phone_suffix}",
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

        drop_in_student_user = self._create_user(fixture_id=fixture_id, role="dropin-student")
        drop_in_parent_user = self._create_user(fixture_id=fixture_id, role="dropin-parent")
        ClubMembership.objects.create(user=drop_in_student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=drop_in_parent_user, club=club, role=ClubMembership.Role.PARENT)
        drop_in_student = Student.objects.create(
            club=club,
            first_name="Dropin",
            last_name="Student",
            phone=f"+1555800{drop_in_phone_suffix}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=drop_in_student_user,
            parent_user=drop_in_parent_user,
        )
        StudentGrade.objects.create(
            club=club,
            student=drop_in_student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )
        trial_student_user = self._create_user(fixture_id=fixture_id, role="trial-student")
        trial_parent_user = self._create_user(fixture_id=fixture_id, role="trial-parent")
        ClubMembership.objects.create(user=trial_student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=trial_parent_user, club=club, role=ClubMembership.Role.PARENT)
        trial_student = Student.objects.create(
            club=club,
            first_name="Trial",
            last_name="Student",
            phone=f"+1555900{trial_phone_suffix}",
            email="",
            is_child=True,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
            source=Student.Source.OTHER,
            user=trial_student_user,
            parent_user=trial_parent_user,
        )
        StudentGrade.objects.create(
            club=club,
            student=trial_student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )

        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Guest Group 5-Pack {fixture_id}",
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
            group_name=f"Guest Booking Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        drop_in_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Drop-In Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=drop_in_training_type,
            one_time_date=None,
            is_active=True,
        )
        trial_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Trial Free Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=trial_training_type,
            one_time_date=None,
            is_active=True,
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
            "checkin_date": today.isoformat(),
            "scenarios": {
                "subscription": {
                    "student_id": student.id,
                    "phone_suffix": phone_suffix,
                    "schedule_id": schedule.id,
                    "training_type_id": training_type.id,
                    "subscription_id": subscription.id,
                    "checkin_date": today.isoformat(),
                    "expected": {
                        "group_name": schedule.group_name,
                        "student_name": "Guest Student",
                        "is_debt": False,
                        "debt_effect": "none",
                        "subscription_effect": "deducted",
                        "enrollment_status": "active",
                        "post_trial_task_expected": False,
                        "trainings_left_after": trainings_left_before - 1,
                        "trainings_used_after": 1,
                    },
                },
                "drop_in_debt": {
                    "student_id": drop_in_student.id,
                    "phone_suffix": drop_in_phone_suffix,
                    "schedule_id": drop_in_schedule.id,
                    "training_type_id": drop_in_training_type.id,
                    "subscription_id": None,
                    "checkin_date": today.isoformat(),
                    "expected": {
                        "group_name": drop_in_schedule.group_name,
                        "student_name": "Dropin Student",
                        "is_debt": True,
                        "debt_effect": "created",
                        "subscription_effect": "none",
                        "enrollment_status": "active",
                        "post_trial_task_expected": False,
                        "debt_amount": "800.00",
                    },
                },
                "trial_free": {
                    "student_id": trial_student.id,
                    "phone_suffix": trial_phone_suffix,
                    "schedule_id": trial_schedule.id,
                    "training_type_id": trial_training_type.id,
                    "subscription_id": None,
                    "checkin_date": today.isoformat(),
                    "expected": {
                        "group_name": trial_schedule.group_name,
                        "student_name": "Trial Student",
                        "is_debt": False,
                        "debt_effect": "none",
                        "subscription_effect": "none",
                        "enrollment_status": "trial",
                        "post_trial_task_expected": True,
                    },
                },
            },
            "expected": {
                "club_name": "Jaguar Kiosk Guest E2E",
                "group_name": schedule.group_name,
                "student_name": "Guest Student",
                "trainings_left_before": trainings_left_before,
                "trainings_left_after": trainings_left_before - 1,
                "trainings_used_after": 1,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@real-stack-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=None)
