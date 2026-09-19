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
from apps.billing.models import Debt, Payment, Tariff, TrainingType
from apps.billing.services import create_payment
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation
from apps.trainers.services import close_trainer_payroll_period


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for owner/admin debt write-off E2E."

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
                "Prepared debt write-off E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, target_debt_id={fixture['target_debt_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        closed_date = today - timedelta(days=10)
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"debt-writeoff-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"DebtWriteoffE2E-{unique}-pass"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Debt Writeoff E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Debt Writeoff E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Writeoff Hall {fixture_id}",
            address="Debt write-off E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Writeoff",
            last_name="Trainer",
            phone=f"+155510{numeric_suffix}",
            user=trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Debt Writeoff E2E {fixture_id}",
            slug=f"debt-writeoff-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1500.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Debt Writeoff 8-Pack {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Debt Writeoff Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        closed_schedule = Schedule.objects.create(
            club=club,
            day_of_week=closed_date.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Closed Payroll Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=closed_date,
            is_active=True,
        )

        target_student = Student.objects.create(
            club=club,
            first_name="Writeoff",
            last_name="Student",
            phone=f"+155520{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        target_checkin = self._create_debt_checkin(
            club=club,
            student=target_student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            today=today,
        )
        target_debt = Debt.objects.create(
            club=club,
            student=target_student,
            checkin=target_checkin,
            tariff_price=Decimal("1500.00"),
            reason="no_subscription",
        )

        reserved_student = Student.objects.create(
            club=club,
            first_name="Reserved",
            last_name="Student",
            phone=f"+155530{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        reserved_checkin = self._create_debt_checkin(
            club=club,
            student=reserved_student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            today=today,
        )
        reserved_debt = Debt.objects.create(
            club=club,
            student=reserved_student,
            checkin=reserved_checkin,
            tariff_price=Decimal("2500.00"),
            reason="no_subscription",
        )
        reserved_payment = create_payment(
            club_id=club.id,
            student_id=reserved_student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[reserved_debt.id],
            recorded_by_id=owner_user.id,
        )

        closed_student = Student.objects.create(
            club=club,
            first_name="Closed Payroll",
            last_name="Student",
            phone=f"+155540{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        closed_checkin = self._create_debt_checkin(
            club=club,
            student=closed_student,
            schedule=closed_schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            today=closed_date,
        )
        closed_debt = Debt.objects.create(
            club=club,
            student=closed_student,
            checkin=closed_checkin,
            tariff_price=Decimal("1750.00"),
            reason="no_subscription",
        )
        payroll_close = close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_date,
            period_end=closed_date,
            reason="E2E closed period",
            actor_user_id=owner_user.id,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "trainer_id": trainer.id,
            "training_type_id": training_type.id,
            "schedule_id": schedule.id,
            "target_student_id": target_student.id,
            "target_checkin_id": target_checkin.id,
            "target_debt_id": target_debt.id,
            "reserved_student_id": reserved_student.id,
            "reserved_checkin_id": reserved_checkin.id,
            "reserved_debt_id": reserved_debt.id,
            "reserved_payment_id": reserved_payment.id,
            "reserved_subscription_id": reserved_payment.subscription_id,
            "closed_student_id": closed_student.id,
            "closed_checkin_id": closed_checkin.id,
            "closed_debt_id": closed_debt.id,
            "payroll_close_id": payroll_close.id,
            "expected": {
                "writeoff_reason": "E2E manual write-off",
                "reserved_error_text": "ожидающей оплате",
                "closed_error_text": "Период выплат за дату долга уже закрыт",
                "target_amount": "1500.00",
                "reserved_amount": "2500.00",
                "closed_amount": "1750.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_debt_checkin(
        self,
        *,
        club: Club,
        student: Student,
        schedule: Schedule,
        training_type: TrainingType,
        trainer: Trainer,
        location: Location,
        today,
    ) -> Checkin:
        return Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@debt-writeoff-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
