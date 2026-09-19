from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.notifications.models import PushSubscription
from apps.notifications.services import TRAINER_RETENTION_TASK
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for retention task lifecycle E2E."

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
                "Prepared retention task lifecycle E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['target_student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        numeric = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"retention-task-lifecycle-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"RetentionTaskLifecycle-{unique}-pass"
        last_visit_date = today - timedelta(days=15)

        club = Club.objects.create(
            name=f"Jaguar Retention E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Retention E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Retention Hall {fixture_id}",
            address="Retention lifecycle E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Retention",
            last_name="Trainer",
            phone=f"+155570{numeric}",
            user=trainer_user,
        )
        trainer_push = PushSubscription.objects.create(
            user=trainer_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/trainer-retention",
            key_p256dh=f"p256dh-trainer-retention-{unique}",
            key_auth=f"auth-trainer-retention-{unique}",
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        other_trainer_user = self._create_user(fixture_id=fixture_id, role="other-trainer", password=None)
        ClubMembership.objects.create(user=other_trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="Other",
            last_name="Retention",
            phone=f"+155571{numeric}",
            user=other_trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=other_trainer, location=location)

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Retention Grade {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="White",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Retention Group {fixture_id}",
            slug=f"retention-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("30.00"),
        )
        Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Retention Tariff {fixture_id}",
            price=Decimal("6000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        tariff = Tariff.objects.for_club(club).get(name=f"Retention Tariff {fixture_id}")

        target_student = Student.objects.create(
            club=club,
            first_name="RetentionE2E",
            last_name="Target",
            phone=f"+155572{numeric}",
            email="",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            last_visit_date=last_visit_date,
        )
        StudentGrade.objects.create(
            club=club,
            student=target_student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=3,
        )
        Subscription.objects.create(
            club=club,
            student=target_student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=5,
            trainings_used=3,
            expires_at=now + timedelta(days=20),
            scope=tariff.scope,
            location=None,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=last_visit_date.weekday(),
            start_time=timezone.datetime.min.time().replace(hour=10),
            end_time=timezone.datetime.min.time().replace(hour=11),
            group_name=f"Retention Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        for offset in (25, 20, 15):
            checkin_date = today - timedelta(days=offset)
            Checkin.objects.create(
                club=club,
                student=target_student,
                schedule=schedule,
                training_type=training_type,
                trainer=trainer,
                location=location,
                date=checkin_date,
                source=Checkin.Source.MANUAL,
                is_debt=False,
            )

        other_student = Student.objects.create(
            club=club,
            first_name="HiddenRetention",
            last_name="Student",
            phone=f"+155573{numeric}",
            email="",
            status=Student.Status.AT_RISK,
            source=Student.Source.OTHER,
            last_visit_date=today - timedelta(days=18),
        )
        other_task = RetentionTask.objects.create(
            club=club,
            student=other_student,
            trainer=other_trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=today,
            task_type=RetentionTask.TaskType.RETENTION,
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
            "target_student": {
                "student_id": target_student.id,
                "name": str(target_student),
                "phone": target_student.phone,
                "last_visit_date": last_visit_date.isoformat(),
            },
            "other_task_id": other_task.id,
            "push_subscription_ids": {
                "trainer": trainer_push.id,
            },
            "expected": {
                "days_missed": 15,
                "level": RetentionTask.Level.YELLOW,
                "trainer_notification_type": TRAINER_RETENTION_TASK,
                "status_after_in_progress": RetentionTask.TaskStatus.IN_PROGRESS,
                "status_after_snooze": RetentionTask.TaskStatus.SNOOZED,
                "status_after_close": RetentionTask.TaskStatus.CLOSED,
                "resolution_after_close": RetentionTask.Resolution.CALLED_WILL_COME,
                "comment_text": f"Retention lifecycle comment {fixture_id}",
                "close_notes": f"Retention lifecycle close note {fixture_id}",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@retention-task-lifecycle-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
