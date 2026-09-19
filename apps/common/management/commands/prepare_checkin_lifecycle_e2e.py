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
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for same-checkin create/cancel lifecycle E2E."

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
                "Prepared check-in lifecycle E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_suffix = str(int(unique, 16) % 10_000).zfill(4)
        fixture_id = f"checkin-lifecycle-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"CheckinLifecycleOwner-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Checkin Lifecycle E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Checkin Lifecycle E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Lifecycle Hall {fixture_id}",
            address="Check-in lifecycle E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        student_user = self._create_user(fixture_id=fixture_id, role="student", password=None)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=None)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Lifecycle",
            last_name="Trainer",
            phone=f"+1555900{phone_suffix}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Lifecycle Grade {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Lifecycle Personal {fixture_id}",
            slug=f"lifecycle-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=Decimal("1500.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Lifecycle 5-Pack {fixture_id}",
            price=Decimal("10000.00"),
            trainings_limit=10,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Lifecycle",
            last_name="Student",
            phone=f"+1555910{phone_suffix}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
        )
        student_grade = StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
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
            group_name=f"Lifecycle Checkin Proof {fixture_id}",
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
            status="active",
            starts_on=today,
        )
        retention_task = RetentionTask.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=today,
            task_type=RetentionTask.TaskType.RETENTION,
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "phone_suffix": phone_suffix,
            "owner": {
                "email": owner_user.email,
                "password": owner_password,
                "user_id": owner_user.id,
            },
            "student": {
                "student_id": student.id,
                "name": str(student),
            },
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "subscription_id": subscription.id,
            "student_grade_id": student_grade.id,
            "retention_task_id": retention_task.id,
            "checkin_date": today.isoformat(),
            "expected": {
                "group_name": schedule.group_name,
                "trainings_left_before": trainings_left_before,
                "trainings_left_after_checkin": trainings_left_before - 1,
                "trainings_left_after_cancel": trainings_left_before,
                "trainings_used_after_checkin": 1,
                "trainings_used_after_cancel": 0,
                "group_session_attendee_count_after_checkin": 1,
                "group_session_attendee_count_after_cancel": 0,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@checkin-lifecycle-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
