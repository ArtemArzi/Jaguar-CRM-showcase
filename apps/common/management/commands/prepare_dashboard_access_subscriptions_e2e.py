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

from apps.billing.models import Payment, Subscription, SubscriptionFreeze, Tariff, TrainingType
from apps.billing.services import freeze_subscription
from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Prepare an isolated dashboard subscriptions/access-control E2E fixture."

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
                "Prepared dashboard access subscriptions E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, pending_freeze_id={fixture['pending_freeze_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"dashboard-access-subscriptions-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"DashboardAccessOwner-{unique}-pass"
        admin_password = f"DashboardAccessAdmin-{unique}-pass"
        trainer_password = f"DashboardAccessTrainer-{unique}-pass"
        student_password = f"DashboardAccessStudent-{unique}-pass"
        parent_password = f"DashboardAccessParent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Dashboard Access Subscriptions E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Dashboard Access E2E",
            freeze_enabled=True,
            freeze_max_days=60,
            freeze_max_count=None,
            min_trainings_to_freeze=0,
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        admin_user = self._create_user(fixture_id=fixture_id, role="admin", password=admin_password)
        ClubMembership.objects.create(user=admin_user, club=club, role=ClubMembership.Role.ADMIN)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Access",
            last_name="Trainer",
            phone=f"+155510{phone_seed}",
            user=trainer_user,
        )

        student_user = self._create_user(fixture_id=fixture_id, role="student", password=student_password)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=parent_password)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Dashboard Access Group {fixture_id}",
            slug=f"dashboard-access-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("900.00"),
            trial_free=False,
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Dashboard Access 8-Pack {fixture_id}",
            price=Decimal("3600.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Access",
            last_name="Student",
            phone=f"+155520{phone_seed}",
            email=student_user.email,
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
        )
        sale_student = Student.objects.create(
            club=club,
            first_name="Transfer",
            last_name="Student",
            phone=f"+155521{phone_seed}",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=7,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
        )
        pending_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=subscription.id,
            days=6,
            reason=SubscriptionFreeze.Reason.INJURY,
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
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
            "denied_users": {
                "trainer": {
                    "user_id": trainer_user.id,
                    "email": trainer_user.email,
                    "password": trainer_password,
                    "role": ClubMembership.Role.TRAINER,
                },
                "student": {
                    "user_id": student_user.id,
                    "email": student_user.email,
                    "password": student_password,
                    "role": ClubMembership.Role.STUDENT,
                },
                "parent": {
                    "user_id": parent_user.id,
                    "email": parent_user.email,
                    "password": parent_password,
                    "role": ClubMembership.Role.PARENT,
                },
            },
            "trainer_id": trainer.id,
            "student": {
                "student_id": student.id,
                "name": str(student),
            },
            "sale_student": {
                "student_id": sale_student.id,
                "name": str(sale_student),
            },
            "training_type_id": training_type.id,
            "tariff_id": tariff.id,
            "subscription_id": subscription.id,
            "pending_freeze_id": pending_freeze.id,
            "expected": {
                "pending_freeze_count": 1,
                "freeze_days": 6,
                "freeze_reason": SubscriptionFreeze.Reason.INJURY,
                "tariff_name": tariff.name,
                "paid_amount": "3600.00",
                "trainings_left": 7,
                "trainings_limit": 8,
                "direct_payment_method": Payment.Method.TRANSFER,
                "direct_payment_method_label": Payment.Method.TRANSFER.label,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@dashboard-access-subscriptions-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
