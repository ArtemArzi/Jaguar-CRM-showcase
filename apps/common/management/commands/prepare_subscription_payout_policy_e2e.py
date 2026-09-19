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

from apps.attendance.models import Schedule, TrainingGroupRolloutState
from apps.billing.models import Payment, Tariff, TrainingType
from apps.billing.services import create_payment, create_subscription, create_tariff
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare a real-stack E2E fixture for subscription payout policies."

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
                "Prepared subscription payout policy E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, payment_id={fixture['payment_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"subscription-payout-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"SubscriptionPayoutE2E-{unique}-pass"
        suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Subscription Payout E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Subscription Payout E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Payout Hall {fixture_id}",
            address="Subscription payout E2E fixture",
        )
        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)

        personal_owner = self._create_trainer(
            club=club,
            fixture_id=fixture_id,
            role="personal-owner",
            phone=f"+155561{suffix}",
            first_name="Policy",
            last_name="Owner",
        )
        mini_trainer = self._create_trainer(
            club=club,
            fixture_id=fixture_id,
            role="mini-trainer",
            phone=f"+155562{suffix}",
            first_name="Mini",
            last_name="Coach",
        )
        for trainer in (personal_owner, mini_trainer):
            TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Personal Payout E2E {fixture_id}",
            slug=f"personal-payout-e2e-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("2000.00"),
            trial_free=False,
        )
        mini_type = TrainingType.objects.create(
            club=club,
            name=f"Mini Group Payout E2E {fixture_id}",
            slug=f"mini-payout-e2e-{unique}",
            kind=TrainingType.Kind.MINI_GROUP,
            drop_in_price=Decimal("1600.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=personal_owner,
            location=location,
            training_type=personal_type,
            percent=Decimal("20.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=mini_trainer,
            location=location,
            training_type=mini_type,
            percent=Decimal("30.00"),
        )

        personal_tariff = create_tariff(
            club_id=club.id,
            name=f"Personal Upfront {fixture_id}",
            training_type_id=personal_type.id,
            price=Decimal("10000.00"),
            trainings_limit=5,
            duration_days=30,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
        )
        personal_checkin_tariff = create_tariff(
            club_id=club.id,
            name=f"Personal Checkin {fixture_id}",
            training_type_id=personal_type.id,
            price=Decimal("10000.00"),
            trainings_limit=5,
            duration_days=30,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        mini_tariff = create_tariff(
            club_id=club.id,
            name=f"Mini Checkin {fixture_id}",
            training_type_id=mini_type.id,
            price=Decimal("8000.00"),
            trainings_limit=4,
            duration_days=30,
        )
        personal_student = Student.objects.create(
            club=club,
            first_name="Payout",
            last_name="Upfront",
            phone=f"+155571{suffix}",
            status=Student.Status.ACTIVE,
        )
        personal_checkin_student = Student.objects.create(
            club=club,
            first_name="Payout",
            last_name="Checkin",
            phone=f"+155574{suffix}",
            status=Student.Status.ACTIVE,
        )
        mini_student = Student.objects.create(
            club=club,
            first_name="Payout",
            last_name="Mini",
            phone=f"+155572{suffix}",
            status=Student.Status.ACTIVE,
        )
        mini_subscription = create_subscription(
            club_id=club.id,
            student_id=mini_student.id,
            tariff_id=mini_tariff.id,
            package_owner_trainer_id=mini_trainer.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )
        personal_checkin_subscription = create_subscription(
            club_id=club.id,
            student_id=personal_checkin_student.id,
            tariff_id=personal_checkin_tariff.id,
            package_owner_trainer_id=personal_owner.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )
        personal_checkin_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Personal Checkin Proof {fixture_id}",
            trainer=personal_owner,
            location=location,
            training_type=personal_type,
            one_time_date=today,
            is_active=True,
        )
        mini_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Mini Payout Proof {fixture_id}",
            trainer=mini_trainer,
            location=location,
            training_type=mini_type,
            one_time_date=today,
            is_active=True,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=personal_student.id,
            tariff_id=personal_tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            seller_trainer_id=personal_owner.id,
            package_owner_trainer_id=personal_owner.id,
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
            "personal_owner_trainer_id": personal_owner.id,
            "personal_student_name": "Payout Upfront",
            "personal_subscription_id": payment.subscription_id,
            "personal_checkin_student_id": personal_checkin_student.id,
            "personal_checkin_schedule_id": personal_checkin_schedule.id,
            "personal_checkin_training_type_id": personal_type.id,
            "personal_checkin_subscription_id": personal_checkin_subscription.id,
            "mini_trainer_id": mini_trainer.id,
            "mini_student_id": mini_student.id,
            "mini_schedule_id": mini_schedule.id,
            "mini_training_type_id": mini_type.id,
            "mini_subscription_id": mini_subscription.id,
            "expected": {
                "personal_sale_amount": "2000.00",
                "personal_sale_basis": "10000.00",
                "personal_checkin_salary_amount": "400.00",
                "personal_checkin_salary_basis": "2000.00",
                "mini_salary_amount": "600.00",
                "mini_salary_basis": "2000.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@subscription-payout-e2e.local"
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
