from __future__ import annotations

import json
import os
import uuid
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule, ScheduleEnrollment
from apps.billing.models import Payment, Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for account access login real-stack E2E."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        fixture_json = json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(fixture_json)
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared account access login E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"account-access-login-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"AccountAccessTrainer-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Account Access Login E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Account Access E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Account Access Hall {fixture_id}",
            address="Account access login E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="AccessLogin",
            last_name="Trainer",
            phone=f"+79010{phone_seed}",
            user=trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Account Access Group {fixture_id}",
            slug=f"account-access-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Account Access 8-Pack {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="AccessLogin",
            last_name="Student",
            phone=f"+79020{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        child = Student.objects.create(
            club=club,
            first_name="AccessLogin",
            last_name="Child",
            phone=f"+79021{phone_seed}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        target_start_date = club_localdate(club)
        student_payment = self._create_pending_manual_admission(
            club=club,
            student=student,
            tariff=tariff,
            trainer=trainer,
            location=location,
            recorded_by=trainer_user,
            target_start_date=target_start_date,
            group_name=f"Account Access Adult Group {fixture_id}",
        )
        child_payment = self._create_pending_manual_admission(
            club=club,
            student=child,
            tariff=tariff,
            trainer=trainer,
            location=location,
            recorded_by=trainer_user,
            target_start_date=target_start_date,
            group_name=f"Account Access Child Group {fixture_id}",
        )
        parent_phone_input = f"8 903 {phone_seed[:3]} {phone_seed[3:5]} {phone_seed[5:]}"

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "trainer_id": trainer.id,
            "student_id": student.id,
            "student": {
                "name": str(student),
            },
            "child_student_id": child.id,
            "child": {
                "name": str(child),
                "parent_phone_input": parent_phone_input,
                "parent_username": f"+7903{phone_seed}",
            },
            "training_type_id": training_type.id,
            "tariff_id": tariff.id,
            "subscription_id": student_payment.subscription_id,
            "child_subscription_id": child_payment.subscription_id,
            "payment_id": student_payment.id,
            "child_payment_id": child_payment.id,
            "conversion_enrollment_id": student_payment.conversion_enrollment_id,
            "child_conversion_enrollment_id": child_payment.conversion_enrollment_id,
            "expected": {
                "payment_status": Payment.Status.PENDING,
                "subscription_status": Subscription.Status.PENDING,
                "target_start_date": target_start_date.isoformat(),
            },
            "created_at": now.isoformat(),
        }

    def _create_pending_manual_admission(
        self,
        *,
        club,
        student,
        tariff,
        trainer,
        location,
        recorded_by,
        target_start_date,
        group_name: str,
    ) -> Payment:
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=group_name,
            trainer=trainer,
            location=location,
            training_type=tariff.training_type,
            is_active=True,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            paid_amount=None,
            trainings_left=tariff.trainings_limit,
            trainings_used=0,
            expires_at=None,
            scope=tariff.scope,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_start_date,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        return Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=tariff.price,
            original_amount=tariff.price,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=recorded_by,
            seller_trainer=trainer,
            target_schedule=schedule,
            target_start_date=target_start_date,
            target_group_name_snapshot=schedule.group_name,
            target_location_id_snapshot=location.id,
            target_location_name_snapshot=location.name,
            target_trainer_id_snapshot=trainer.id,
            target_trainer_name_snapshot=f"{trainer.first_name} {trainer.last_name}",
            target_training_type_id_snapshot=tariff.training_type_id,
            target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_attribution_source="manual_operational_admission",
            conversion_enrollment=enrollment,
        )

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@account-access-login-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
