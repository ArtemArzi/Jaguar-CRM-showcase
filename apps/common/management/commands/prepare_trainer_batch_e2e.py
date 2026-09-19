from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment, TrainingGroupRolloutState
from apps.attendance.services import create_checkin, enroll_student_in_schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for trainer batch check-in E2E."

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
                "Prepared trainer batch E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, schedule_id={fixture['schedule_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"trainer-batch-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        password = f"TrainerBatchE2E-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Batch E2E {fixture_id}",
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
            club_name_display="Jaguar Trainer Batch E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Batch Hall {fixture_id}",
            address="Trainer batch E2E fixture",
        )
        form_location = Location.objects.create(
            club=club,
            name=f"Batch Form Hall {fixture_id}",
            address="Trainer batch form E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Batch",
            last_name="Trainer",
            phone=f"+15553{unique[:7]}",
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
            name=f"Group E2E {fixture_id}",
            slug=f"group-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        form_training_type = TrainingType.objects.create(
            club=club,
            name=f"Form Group E2E {fixture_id}",
            slug=f"form-group-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1300.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerLocation.objects.create(club=club, trainer=trainer, location=form_location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=form_location,
            training_type=form_training_type,
            percent=Decimal("55.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Trainer Batch 10-Pack {fixture_id}",
            price=Decimal("9000.00"),
            trainings_limit=10,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        schedule_start_time, schedule_end_time = self._finished_today_times(now=now)
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=schedule_start_time,
            end_time=schedule_end_time,
            group_name=f"Trainer Batch Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        unclosed_date = today - timedelta(days=1)
        unclosed_schedule = Schedule.objects.create(
            club=club,
            day_of_week=unclosed_date.weekday(),
            start_time=time(9, 0),
            end_time=time(10, 0),
            group_name=f"Trainer Unclosed Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=unclosed_date,
            is_active=True,
        )

        active_students = [
            self._create_student_with_subscription(
                club=club,
                fixture_id=fixture_id,
                index=index,
                tariff=tariff,
                grade_system=grade_system,
                grade=grade,
                schedule=schedule,
                enrollment_status=ScheduleEnrollment.Status.ACTIVE,
                trainings_left_before=5,
                today=today,
            )
            for index in (1, 2)
        ]
        frozen_student = self._create_student_with_subscription(
            club=club,
            fixture_id=fixture_id,
            index=3,
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=schedule,
            enrollment_status=ScheduleEnrollment.Status.FROZEN,
            trainings_left_before=5,
            today=today,
        )
        unclosed_student = self._create_student_with_subscription(
            club=club,
            fixture_id=fixture_id,
            index=4,
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=unclosed_schedule,
            enrollment_status=ScheduleEnrollment.Status.ACTIVE,
            trainings_left_before=5,
            today=unclosed_date,
        )
        for student in active_students:
            self._create_checkin_without_enqueue(
                club_id=club.id,
                student_id=student["student_id"],
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source=Checkin.Source.BATCH,
                checkin_date=today,
            )
        self._create_checkin_without_enqueue(
            club_id=club.id,
            student_id=unclosed_student["student_id"],
            schedule_id=unclosed_schedule.id,
            training_type_id=training_type.id,
            source=Checkin.Source.BATCH,
            checkin_date=unclosed_date,
        )
        schedule_form_date = today

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer_id": trainer.id,
            "trainer": {
                "email": trainer_user.email,
                "password": password,
                "user_id": trainer_user.id,
            },
            "trainer_user_id": trainer_user.id,
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "checkin_date": today.isoformat(),
            "active_student_ids": [student["student_id"] for student in active_students],
            "active_subscriptions": [
                {
                    "student_id": student["student_id"],
                    "subscription_id": student["subscription_id"],
                    "trainings_left_before": 5,
                    "trainings_left_after": 4,
                }
                for student in active_students
            ],
            "frozen_student_id": frozen_student["student_id"],
            "unclosed": {
                "date": unclosed_date.isoformat(),
                "schedule_id": unclosed_schedule.id,
                "group_name": unclosed_schedule.group_name,
                "student_id": unclosed_student["student_id"],
                "subscription_id": unclosed_student["subscription_id"],
                "trainings_left_after": 4,
            },
            "schedule_form": {
                "date": schedule_form_date.isoformat(),
                "created_group_name": f"Trainer Form Draft {fixture_id}",
                "created_start_time": "23:00",
                "created_end_time": "23:45",
                "created_training_type_id": training_type.id,
                "created_training_type_name": training_type.name,
                "created_location_id": location.id,
                "created_location_name": location.name,
                "edited_group_name": f"Trainer Form Edited {fixture_id}",
                "edited_start_time": "23:10",
                "edited_end_time": "23:55",
                "edited_training_type_id": form_training_type.id,
                "edited_training_type_name": form_training_type.name,
                "edited_location_id": form_location.id,
                "edited_location_name": form_location.name,
            },
            "expected": {
                "active_student_count": 2,
                "frozen_student_count": 1,
                "group_session_attendee_count": 2,
            },
            "created_at": now.isoformat(),
        }

    def _create_student_with_subscription(
        self,
        *,
        club: Club,
        fixture_id: str,
        index: int,
        tariff: Tariff,
        grade_system: GradeSystem,
        grade: Grade,
        schedule: Schedule,
        enrollment_status: str,
        trainings_left_before: int,
        today,
    ) -> dict:
        student = Student.objects.create(
            club=club,
            first_name=f"Batch{index}",
            last_name="Student",
            phone=f"+15554{club.id:04d}{index:02d}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=trainings_left_before,
            trainings_used=0,
            expires_at=timezone.now() + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=enrollment_status,
            starts_on=today,
        )
        return {
            "student_id": student.id,
            "subscription_id": subscription.id,
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@trainer-batch-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)

    def _finished_today_times(self, *, now) -> tuple[time, time]:
        club_tz = ZoneInfo("Europe/Moscow")
        club_now = now.astimezone(club_tz)
        finished_at = club_now - timedelta(minutes=5)

        if finished_at.date() != club_now.date():
            return time(0, 0), time(0, 0)

        end_time = finished_at.time().replace(second=0, microsecond=0)
        start_at = datetime.combine(finished_at.date(), end_time, club_tz) - timedelta(hours=1)
        if start_at.date() != club_now.date():
            return time(0, 0), end_time

        return start_at.time().replace(second=0, microsecond=0), end_time

    def _create_checkin_without_enqueue(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        training_type_id: int,
        source: str,
        checkin_date,
    ) -> dict:
        with patch("apps.attendance.services.async_task", return_value=None):
            return create_checkin(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                source=source,
                checkin_date=checkin_date,
                _skip_group_analytics=True,
            )
