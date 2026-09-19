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
from apps.attendance.services import (
    enroll_student_in_schedule,
    reassign_training_group_responsibility,
)
from apps.attendance.services import (
    substitute_trainer as substitute_session_trainer,
)
from apps.billing.models import Tariff, TrainingType
from apps.billing.services import create_payment, verify_payment
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for schedule exception visibility E2E."

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
                "Prepared schedule exception visibility E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"schedule-exception-visibility-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        week_start = today + timedelta(days=7 - today.weekday())
        cancel_date = week_start
        reschedule_old_date = week_start + timedelta(days=1)
        reschedule_new_date = week_start + timedelta(days=2)
        substitute_date = week_start + timedelta(days=3)

        owner_password = f"ScheduleOwner-{unique}-pass"
        trainer_password = f"ScheduleTrainer-{unique}-pass"
        substitute_password = f"ScheduleSubstitute-{unique}-pass"
        student_password = f"ScheduleStudent-{unique}-pass"
        parent_password = f"ScheduleParent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Schedule Exception E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Schedule Exception E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Exception Hall {fixture_id}",
            address="Schedule exception visibility E2E fixture",
        )

        owner_user = self._create_user(
            fixture_id=fixture_id,
            role="owner",
            password=owner_password,
        )
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        substitute_user = self._create_user(
            fixture_id=fixture_id,
            role="substitute-trainer",
            password=substitute_password,
        )
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=student_password,
        )
        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=parent_password,
        )
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=substitute_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Exception",
            last_name="Trainer",
            phone=f"+1555600{phone_seed[:4]}",
            user=trainer_user,
        )
        substitute_trainer = Trainer.objects.create(
            club=club,
            first_name="Backup",
            last_name="Trainer",
            phone=f"+1555700{phone_seed[:4]}",
            user=substitute_user,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Exception Group {fixture_id}",
            slug=f"exception-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        for assigned_trainer in (trainer, substitute_trainer):
            TrainerLocation.objects.create(club=club, trainer=assigned_trainer, location=location)
            TrainerRate.objects.create(
                club=club,
                trainer=assigned_trainer,
                location=location,
                training_type=training_type,
                percent=Decimal("50.00"),
            )

        student = Student.objects.create(
            club=club,
            first_name="Exception",
            last_name="Student",
            phone=f"+1555800{phone_seed[:4]}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
        )
        canonical_student = Student.objects.create(
            club=club,
            first_name="Canonical",
            last_name="Membership",
            phone=f"+1555900{phone_seed[:4]}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )

        cancel_schedule = self._create_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            fixture_id=fixture_id,
            label="Cancelled",
            session_date=cancel_date,
            start=time(16, 0),
            end=time(17, 0),
        )
        reschedule_schedule = self._create_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            fixture_id=fixture_id,
            label="Moved",
            session_date=reschedule_old_date,
            start=time(17, 0),
            end=time(18, 0),
        )
        substitute_schedule = self._create_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            fixture_id=fixture_id,
            label="Substitute",
            session_date=substitute_date,
            start=time(18, 0),
            end=time(19, 0),
            recurring=True,
        )

        for schedule in (cancel_schedule, reschedule_schedule):
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                starts_on=today,
            )

        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=owner_user.id,
            schedule_ids=[substitute_schedule.id],
            canonical_name=substitute_schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-substitute-group",
            require_manual_operational_admission=True,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=substitute_schedule.id,
            starts_on=today,
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Substitute Canonical Group {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=canonical_student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            target_schedule_id=substitute_schedule.id,
            target_training_group_id=canonical_group["training_group_id"],
            target_start_date=substitute_date,
            create_manual_operational_admission=True,
        )
        if payment.conversion_group_membership_id is None:
            raise CommandError("substitute fixture payment did not create its canonical membership")

        reassign_training_group_responsibility(
            club_id=club.id,
            training_group_id=canonical_group["training_group_id"],
            responsible_trainer_id=substitute_trainer.id,
        )
        payment = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        substitute_reason = f"Substitute reason {fixture_id}"
        substitute_session_trainer(
            club_id=club.id,
            schedule_id=substitute_schedule.id,
            date=substitute_date,
            substitute_trainer_id=substitute_trainer.id,
            reason=substitute_reason,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "trainer": {
                "email": trainer_user.email,
                "password": trainer_password,
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "name": "Exception Trainer",
            },
            "substitute_trainer": {
                "email": substitute_user.email,
                "password": substitute_password,
                "user_id": substitute_user.id,
                "trainer_id": substitute_trainer.id,
                "name": "Backup Trainer",
            },
            "student": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
                "student_id": student.id,
                "name": "Exception Student",
            },
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "cancel_schedule_id": cancel_schedule.id,
            "reschedule_schedule_id": reschedule_schedule.id,
            "substitute_schedule_id": substitute_schedule.id,
            "cancel_date": cancel_date.isoformat(),
            "reschedule_old_date": reschedule_old_date.isoformat(),
            "reschedule_new_date": reschedule_new_date.isoformat(),
            "substitute_date": substitute_date.isoformat(),
            "week_start": week_start.isoformat(),
            "training_group_substitute": {
                "training_group_id": canonical_group["training_group_id"],
                "membership_id": payment.conversion_group_membership_id,
                "student_id": canonical_student.id,
                "payment_id": payment.id,
                "schedule_id": substitute_schedule.id,
                "original_responsible_trainer_id": trainer.id,
                "replacement_responsible_trainer_id": substitute_trainer.id,
                "rollout_mode": canonical_group["mode"],
                "new_writes_enabled": canonical_group["new_writes_enabled"],
                "manual_operational_admission_enabled": canonical_group[
                    "manual_operational_admission_enabled"
                ],
                "seller_trainer_id": trainer.id,
                "sale_trainer_id_snapshot": trainer.id,
                "sale_attribution_source": "training_group_responsible_trainer",
            },
            "expected": {
                "cancel_group_name": cancel_schedule.group_name,
                "reschedule_group_name": reschedule_schedule.group_name,
                "substitute_group_name": substitute_schedule.group_name,
                "cancel_reason": f"Cancel reason {fixture_id}",
                "reschedule_reason": f"Reschedule reason {fixture_id}",
                "substitute_reason": substitute_reason,
                "reschedule_new_start_time": "20:00",
                "reschedule_new_end_time": "21:00",
            },
            "created_at": now.isoformat(),
        }

    def _create_schedule(
        self,
        *,
        club: Club,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        fixture_id: str,
        label: str,
        session_date,
        start: time,
        end: time,
        recurring: bool = False,
    ) -> Schedule:
        return Schedule.objects.create(
            club=club,
            day_of_week=session_date.weekday(),
            start_time=start,
            end_time=end,
            group_name=f"{label} Exception Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None if recurring else session_date,
            is_active=True,
        )

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@real-stack-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
