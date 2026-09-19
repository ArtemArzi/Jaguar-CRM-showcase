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

from apps.attendance.models import Schedule, ScheduleEnrollment, TrainingGroupRolloutState
from apps.billing.models import TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation


class Command(BaseCommand):
    help = "Prepare an isolated owner/admin batch check-in lifecycle E2E fixture."

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
                "Prepared owner batch check-in E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        early_date = today + timedelta(days=1)
        finished_date = today - timedelta(days=1)
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"owner-batch-checkin-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        club = Club.objects.create(
            name=f"Jaguar Owner Batch Check-in E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Owner Batch Check-in E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Owner Batch Hall {fixture_id}",
            address="Owner batch check-in E2E fixture",
        )

        owner = self._create_member(
            club=club,
            fixture_id=fixture_id,
            role=ClubMembership.Role.OWNER,
        )
        admin = self._create_member(
            club=club,
            fixture_id=fixture_id,
            role=ClubMembership.Role.ADMIN,
        )
        trainer_user = self._create_member(
            club=club,
            fixture_id=fixture_id,
            role=ClubMembership.Role.TRAINER,
        )
        trainer = Trainer.objects.create(
            club=club,
            first_name="OwnerBatch",
            last_name="Trainer",
            phone=f"+155590{int(unique, 16) % 10_000_000:07d}",
            user=trainer_user["user"],
        )
        TrainerLocation.objects.create(
            club=club,
            trainer=trainer,
            location=location,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Owner Batch Group {fixture_id}",
            slug=f"owner-batch-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("700.00"),
            trial_free=False,
        )
        early_schedule = self._create_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_date=early_date,
            group_name=f"Owner Batch Early {fixture_id}",
        )
        finished_schedule = self._create_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_date=finished_date,
            group_name=f"Owner Batch Finished {fixture_id}",
        )
        early_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            suffix="01",
            schedule=early_schedule,
            target_date=early_date,
        )
        finished_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            suffix="02",
            schedule=finished_schedule,
            target_date=finished_date,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "training_type_id": training_type.id,
            "owner": self._member_payload(owner),
            "admin": self._member_payload(admin),
            "trainer": self._member_payload(trainer_user),
            "early": {
                "schedule_id": early_schedule.id,
                "student_id": early_student.id,
                "date": early_date.isoformat(),
            },
            "finished": {
                "schedule_id": finished_schedule.id,
                "student_id": finished_student.id,
                "date": finished_date.isoformat(),
            },
            "expected": {
                "owner_topic_tags": ["owner-close"],
                "owner_notes": "Owner first close",
                "admin_topic_tags": ["admin-reviewed"],
                "admin_notes": "Admin retry correction",
            },
        }

    def _create_member(self, *, club: Club, fixture_id: str, role: str) -> dict:
        email = f"{role}-{fixture_id}@example.test"
        password = f"OwnerBatch-{role}-{uuid.uuid4().hex[:10]}-pass"
        user = get_user_model().objects.create_user(
            username=email,
            email=email,
            password=password,
        )
        ClubMembership.objects.create(
            user=user,
            club=club,
            role=role,
        )
        return {"user": user, "email": email, "password": password, "role": role}

    def _member_payload(self, member: dict) -> dict:
        return {
            "user_id": member["user"].id,
            "email": member["email"],
            "password": member["password"],
            "role": member["role"],
        }

    def _create_schedule(
        self,
        *,
        club: Club,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        target_date,
        group_name: str,
    ) -> Schedule:
        return Schedule.objects.create(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name=group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            is_active=True,
        )

    def _create_student(
        self,
        *,
        club: Club,
        fixture_id: str,
        suffix: str,
        schedule: Schedule,
        target_date,
    ) -> Student:
        phone_seed = int(fixture_id[-8:], 16) % 10_000_000
        student = Student.objects.create(
            club=club,
            first_name="OwnerBatch",
            last_name=f"Student{suffix}",
            phone=f"+155580{phone_seed:07d}{suffix}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
        )
        return student
