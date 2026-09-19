from __future__ import annotations

import json
import uuid
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule, TrainingGroupRolloutState
from apps.billing.models import Debt, Payment, Tariff, TariffComponent, TrainingType
from apps.billing.services import create_payment, create_tariff
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare a real-stack E2E fixture for hybrid package entitlements."

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
                "Prepared hybrid package entitlements E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, payment_id={fixture['payment_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"hybrid-entitlements-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"HybridEntitlementsE2E-{unique}-pass"
        suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Hybrid Entitlements E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Hybrid Entitlements E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Hybrid Hall {fixture_id}",
            address="Hybrid entitlements E2E fixture",
        )
        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        group_trainer = self._create_trainer(
            club=club,
            fixture_id=fixture_id,
            role="group-trainer",
            phone=f"+155563{suffix}",
            first_name="Hybrid",
            last_name="Seller",
        )
        personal_trainer = self._create_trainer(
            club=club,
            fixture_id=fixture_id,
            role="personal-trainer",
            phone=f"+155564{suffix}",
            first_name="Hybrid",
            last_name="Personal",
        )
        for trainer in (group_trainer, personal_trainer):
            TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        group_type = TrainingType.objects.create(
            club=club,
            name=f"Hybrid Group E2E {fixture_id}",
            slug=f"hybrid-group-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Hybrid Personal E2E {fixture_id}",
            slug=f"hybrid-personal-e2e-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("2000.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=group_trainer,
            location=location,
            training_type=group_type,
            percent=Decimal("1.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=personal_trainer,
            location=location,
            training_type=personal_type,
            percent=Decimal("2.00"),
        )
        tariff = create_tariff(
            club_id=club.id,
            name=f"Гибрид Оптимум {fixture_id}",
            training_type_id=group_type.id,
            price=Decimal("9500.00"),
            trainings_limit=None,
            duration_days=30,
            components=[
                {
                    "name": "Группа",
                    "training_type_id": group_type.id,
                    "entitlement_kind": TariffComponent.EntitlementKind.WEEKLY_LIMIT,
                    "weekly_limit": 2,
                    "trainer_payout_policy": Tariff.PayoutPolicy.ON_PAYMENT,
                    "paid_amount_basis": Decimal("3500.00"),
                },
                {
                    "name": "Персоналки",
                    "training_type_id": personal_type.id,
                    "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
                    "credits_total": 3,
                    "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
                    "paid_amount_basis": Decimal("6000.00"),
                },
            ],
        )
        student = Student.objects.create(
            club=club,
            first_name="Hybrid",
            last_name="Student",
            phone=f"+155573{suffix}",
            status=Student.Status.ACTIVE,
        )
        personal_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Hybrid Personal Debt {fixture_id}",
            trainer=personal_trainer,
            location=location,
            training_type=personal_type,
            one_time_date=today,
            is_active=True,
        )
        group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Hybrid Group Weekly {fixture_id}",
            trainer=group_trainer,
            location=location,
            training_type=group_type,
            one_time_date=None,
            is_active=True,
        )
        debt_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=personal_schedule,
            training_type=personal_type,
            trainer=personal_trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        debt = Debt.objects.create(
            club=club,
            student=student,
            checkin=debt_checkin,
            tariff_price=personal_type.drop_in_price,
            reason="no_subscription",
        )
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
            seller_trainer_id=group_trainer.id,
            package_owner_trainer_id=personal_trainer.id,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "payment_id": payment.id,
            "subscription_id": payment.subscription_id,
            "student_id": student.id,
            "student_name": "Hybrid Student",
            "group_trainer_id": group_trainer.id,
            "personal_trainer_id": personal_trainer.id,
            "group_training_type_id": group_type.id,
            "personal_training_type_id": personal_type.id,
            "group_schedule_id": group_schedule.id,
            "debt_checkin_id": debt_checkin.id,
            "debt_id": debt.id,
            "expected": {
                "sale_amount": "35.00",
                "sale_basis": "3500.00",
                "personal_salary_amount": "40.00",
                "personal_salary_basis": "2000.00",
                "personal_component_paid_basis": "6000.00",
                "group_component_paid_basis": "3500.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@hybrid-entitlements-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)

    def _create_trainer(
        self,
        *,
        club: Club,
        fixture_id: str,
        role: str,
        phone: str,
        first_name: str,
        last_name: str,
    ) -> Trainer:
        user = self._create_user(fixture_id=fixture_id, role=role, password=None)
        ClubMembership.objects.create(user=user, club=club, role=ClubMembership.Role.TRAINER)
        return Trainer.objects.create(
            club=club,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            user=user,
        )
