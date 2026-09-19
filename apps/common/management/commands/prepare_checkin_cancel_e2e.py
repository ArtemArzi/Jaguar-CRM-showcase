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

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    Schedule,
    ScheduleEnrollment,
    TrainingGroupRolloutState,
)
from apps.attendance.services import enroll_student_in_schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeProgressEvent, GradeSystem, StudentGrade
from apps.notifications.models import SentNotification
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerEarning, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for check-in cancellation E2E."

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
                "Prepared check-in cancel E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, checkin_id={fixture['checkin_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"checkin-cancel-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"CheckinCancelOwner-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Checkin Cancel E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Checkin Cancel E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Cancel Hall {fixture_id}",
            address="Check-in cancel E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=None)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Cancel",
            last_name="Trainer",
            phone=f"+155580{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Cancel Grade {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Cancel Personal {fixture_id}",
            slug=f"cancel-personal-{unique}",
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
            name=f"Cancel 5-Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=5,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Cancel",
            last_name="Student",
            phone=f"+155581{phone_seed}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
            last_visit_date=today,
        )
        student_grade = StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=1,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=4,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Cancel Checkin Proof {fixture_id}",
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
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=subscription,
            is_debt=False,
        )
        CheckinCascadeEvent.objects.create(
            club=club,
            checkin=checkin,
            effect=CheckinCascadeEvent.Effect.SALARY,
            status=CheckinCascadeEvent.Status.QUEUED,
            expected=True,
            task_name="apps.attendance.tasks.calculate_salary",
            payload={
                "calculation_basis": "checkin_salary_snapshot",
                "snapshot_provenance": "checkin_queue",
                "training_type_kind_snapshot": training_type.kind,
                "rate_percent_snapshot": "50.00",
                "subscription_price_snapshot": str(tariff.price),
                "trainer_id_snapshot": trainer.id,
            },
        )
        earning = TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            checkin=checkin,
            earning_source=TrainerEarning.Source.CHECKIN,
            earning_type=TrainerEarning.EarningType.PERSONAL,
            amount=Decimal("2500.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=tariff.price,
            cancelled=False,
        )
        GradeProgressEvent.objects.create(club=club, student_grade=student_grade, checkin=checkin)
        GroupSession.objects.create(
            club=club,
            schedule=schedule,
            date=today,
            trainer=trainer,
            attendee_count=1,
        )
        retention_task = RetentionTask.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.CLOSED,
            due_date=today,
            resolved_at=checkin.created_at + timedelta(seconds=1),
            resolution=RetentionTask.Resolution.AUTO_CHECKIN,
            task_type=RetentionTask.TaskType.RETENTION,
        )
        SentNotification.objects.create(
            club=club,
            student=student,
            notification_type=f"parent_checkin:{checkin.id}",
            sent_date=today,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "student": {
                "student_id": student.id,
                "name": str(student),
            },
            "schedule_id": schedule.id,
            "checkin_id": checkin.id,
            "subscription_id": subscription.id,
            "earning_id": earning.id,
            "student_grade_id": student_grade.id,
            "retention_task_id": retention_task.id,
            "checkin_date": today.isoformat(),
            "expected": {
                "group_name": schedule.group_name,
                "trainings_left_before_cancel": 4,
                "trainings_left_after_cancel": 5,
                "trainings_used_after_cancel": 0,
                "group_session_attendee_count_after_cancel": 0,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@checkin-cancel-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
