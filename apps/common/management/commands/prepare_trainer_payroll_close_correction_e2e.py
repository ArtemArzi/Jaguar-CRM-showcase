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

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerEarning, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated trainer payroll close/correction E2E fixture."

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
                "Prepared trainer payroll close/correction E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, trainer_id={fixture['trainer']['trainer_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-payroll-close-correction-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"OwnerPayrollClose-{unique}-pass"
        admin_password = f"AdminPayrollClose-{unique}-pass"
        trainer_password = f"TrainerPayrollClose-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Payroll Close E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Payroll Close E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Payroll Close Hall {fixture_id}",
            address="Trainer payroll close E2E fixture",
        )

        owner_user = self._create_user(
            fixture_id=fixture_id,
            role="owner",
            password=owner_password,
        )
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        admin_user = self._create_user(
            fixture_id=fixture_id,
            role="admin",
            password=admin_password,
        )
        ClubMembership.objects.create(user=admin_user, club=club, role=ClubMembership.Role.ADMIN)
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        source_trainer = Trainer.objects.create(
            club=club,
            first_name="Payroll",
            last_name="Source",
            phone=f"+155550{phone_seed}",
            user=trainer_user,
        )
        target_trainer = Trainer.objects.create(
            club=club,
            first_name="Payroll",
            last_name="Target",
            phone=f"+155551{phone_seed}",
        )
        TrainerLocation.objects.create(club=club, trainer=source_trainer, location=location)
        TrainerLocation.objects.create(club=club, trainer=target_trainer, location=location)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Payroll Group {fixture_id}",
            slug=f"payroll-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        for trainer in (source_trainer, target_trainer):
            TrainerRate.objects.create(
                club=club,
                trainer=trainer,
                location=location,
                training_type=training_type,
                percent=Decimal("50.00"),
            )
        personal_training_type = TrainingType.objects.create(
            club=club,
            name=f"Payroll Personal {fixture_id}",
            slug=f"payroll-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("2000.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=source_trainer,
            location=location,
            training_type=personal_training_type,
            percent=Decimal("20.00"),
        )
        personal_tariff = Tariff.objects.create(
            club=club,
            training_type=personal_training_type,
            name=f"Payroll Personal Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=5,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(0, 5),
            group_name=f"Payroll Close Proof {fixture_id}",
            trainer=source_trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        personal_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 10),
            end_time=time(0, 15),
            group_name=f"Payroll Closed Mutation {fixture_id}",
            trainer=source_trainer,
            location=location,
            training_type=personal_training_type,
            one_time_date=today,
            is_active=True,
        )

        corrected_student = self._create_student(
            club=club,
            trainer=source_trainer,
            first_name="PayrollCorrected",
            phone=f"+155552{phone_seed}",
        )
        blocked_student = self._create_student(
            club=club,
            trainer=source_trainer,
            first_name="PayrollBlocked",
            phone=f"+155553{phone_seed}",
        )
        salary_mutation_student = self._create_student(
            club=club,
            trainer=source_trainer,
            first_name="PayrollSalaryBlocked",
            phone=f"+155554{phone_seed}",
        )
        salary_mutation_subscription = Subscription.objects.create(
            club=club,
            student=salary_mutation_student,
            tariff=personal_tariff,
            paid_amount=personal_tariff.price,
            trainings_left=personal_tariff.trainings_limit,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=personal_tariff.scope,
            location=personal_tariff.location,
        )
        corrected_checkin = self._create_checkin(
            club=club,
            student=corrected_student,
            schedule=schedule,
            training_type=training_type,
            trainer=source_trainer,
            location=location,
            checkin_date=today,
        )
        blocked_checkin = self._create_checkin(
            club=club,
            student=blocked_student,
            schedule=schedule,
            training_type=training_type,
            trainer=source_trainer,
            location=location,
            checkin_date=today,
        )
        corrected_earning = TrainerEarning.objects.create(
            club=club,
            trainer=source_trainer,
            checkin=corrected_checkin,
            earning_type=TrainerEarning.EarningType.GROUP,
            amount=Decimal("500.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=Decimal("2500.00"),
            cancelled=False,
        )
        blocked_earning = TrainerEarning.objects.create(
            club=club,
            trainer=source_trainer,
            checkin=blocked_checkin,
            earning_type=TrainerEarning.EarningType.GROUP,
            amount=Decimal("500.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=Decimal("2500.00"),
            cancelled=False,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "admin": {
                "user_id": admin_user.id,
                "email": admin_user.email,
                "password": admin_password,
                "role": ClubMembership.Role.ADMIN,
            },
            "trainer": {
                "trainer_id": source_trainer.id,
                "user_id": trainer_user.id,
                "email": trainer_user.email,
                "password": trainer_password,
                "role": ClubMembership.Role.TRAINER,
                "name": str(source_trainer),
            },
            "target_trainer": {
                "trainer_id": target_trainer.id,
                "name": str(target_trainer),
            },
            "period": {
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "reason": "E2E payroll period approved",
            },
            "earnings": {
                "corrected_id": corrected_earning.id,
                "corrected_checkin_id": corrected_checkin.id,
                "blocked_id": blocked_earning.id,
                "blocked_checkin_id": blocked_checkin.id,
            },
            "salary_mutation": {
                "student_id": salary_mutation_student.id,
                "subscription_id": salary_mutation_subscription.id,
                "schedule_id": personal_schedule.id,
                "training_type_id": personal_training_type.id,
                "location_id": location.id,
                "date": today.isoformat(),
                "expected_error_code": "payroll_period_closed",
            },
            "students": {
                "corrected_name": str(corrected_student),
                "blocked_name": str(blocked_student),
                "salary_mutation_name": str(salary_mutation_student),
            },
            "expected": {
                "correction_reason": "E2E transfer before payroll close",
                "blocked_reason": "E2E blocked correction after payroll close",
                "salary_total_snapshot": "1000.00",
                "source_trainer_total_after_correction": "500.00",
                "target_trainer_total_after_correction": "500.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-payroll-close-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)

    def _create_student(
        self,
        *,
        club: Club,
        trainer: Trainer,
        first_name: str,
        phone: str,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name=first_name,
            last_name="Student",
            phone=phone,
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )

    def _create_checkin(
        self,
        *,
        club: Club,
        student: Student,
        schedule: Schedule,
        training_type: TrainingType,
        trainer: Trainer,
        location: Location,
        checkin_date,
    ) -> Checkin:
        return Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=checkin_date,
            source=Checkin.Source.BATCH,
            is_debt=False,
        )
