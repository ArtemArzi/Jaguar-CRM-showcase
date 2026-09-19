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

from apps.billing.models import Subscription, SubscriptionFreeze, Tariff, TrainingType
from apps.billing.services import freeze_subscription
from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for owner/admin freeze lifecycle E2E."

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
                "Prepared freeze lifecycle E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, approve_freeze_id={fixture['approve_freeze_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"freeze-lifecycle-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"FreezeLifecycleE2E-{unique}-pass"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Freeze Lifecycle E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Freeze Lifecycle E2E",
            freeze_enabled=True,
            freeze_max_days=60,
            freeze_max_count=None,
            min_trainings_to_freeze=0,
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        Trainer.objects.create(
            club=club,
            first_name="Freeze",
            last_name="Trainer",
            phone=f"+155540{numeric_suffix}",
            user=trainer_user,
        )
        student_user = self._create_user(fixture_id=fixture_id, role="student", password=None)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=None)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Freeze Lifecycle E2E {fixture_id}",
            slug=f"freeze-lifecycle-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Freeze Lifecycle 8-Pack {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )

        approve_subscription = self._create_subscription(
            club=club,
            tariff=tariff,
            first_name="ApproveFreeze",
            last_name="Student",
            phone=f"+155550{numeric_suffix}",
        )
        approve_expires_at_before = approve_subscription.expires_at
        approve_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=approve_subscription.id,
            days=5,
            reason=SubscriptionFreeze.Reason.INJURY,
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
        )

        reject_subscription = self._create_subscription(
            club=club,
            tariff=tariff,
            first_name="RejectFreeze",
            last_name="Student",
            phone=f"+155560{numeric_suffix}",
        )
        reject_expires_at_before = reject_subscription.expires_at
        reject_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=reject_subscription.id,
            days=4,
            reason=SubscriptionFreeze.Reason.VACATION,
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
        )

        unfreeze_subscription = self._create_subscription(
            club=club,
            tariff=tariff,
            first_name="Unfreeze",
            last_name="Student",
            phone=f"+155570{numeric_suffix}",
        )
        unfreeze_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=unfreeze_subscription.id,
            days=10,
            reason=SubscriptionFreeze.Reason.ILLNESS,
            frozen_by_id=owner_user.id,
        )
        unfreeze_freeze.starts_at = now - timedelta(days=1, hours=1)
        unfreeze_freeze.save(update_fields=["starts_at", "updated_at"])
        unfreeze_subscription.refresh_from_db()
        unfreeze_expires_at_before = unfreeze_subscription.expires_at

        denial_subscription = self._create_subscription(
            club=club,
            tariff=tariff,
            first_name="DenyFreeze",
            last_name="Student",
            phone=f"+155580{numeric_suffix}",
        )
        denial_expires_at_before = denial_subscription.expires_at
        denial_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=denial_subscription.id,
            days=6,
            reason=SubscriptionFreeze.Reason.OTHER,
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
        )

        denial_unfreeze_subscription = self._create_subscription(
            club=club,
            tariff=tariff,
            first_name="DenyUnfreeze",
            last_name="Student",
            phone=f"+155590{numeric_suffix}",
        )
        denial_unfreeze_freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=denial_unfreeze_subscription.id,
            days=7,
            reason=SubscriptionFreeze.Reason.ILLNESS,
            frozen_by_id=owner_user.id,
        )
        denial_unfreeze_subscription.refresh_from_db()
        denial_unfreeze_expires_at_before = denial_unfreeze_subscription.expires_at

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "trainer_user_id": trainer_user.id,
            "student_user_id": student_user.id,
            "parent_user_id": parent_user.id,
            "approve_subscription_id": approve_subscription.id,
            "approve_freeze_id": approve_freeze.id,
            "reject_subscription_id": reject_subscription.id,
            "reject_freeze_id": reject_freeze.id,
            "unfreeze_subscription_id": unfreeze_subscription.id,
            "unfreeze_freeze_id": unfreeze_freeze.id,
            "denial_subscription_id": denial_subscription.id,
            "denial_freeze_id": denial_freeze.id,
            "denial_unfreeze_subscription_id": denial_unfreeze_subscription.id,
            "denial_unfreeze_freeze_id": denial_unfreeze_freeze.id,
            "expected": {
                "approve_days": 5,
                "reject_days": 4,
                "unfreeze_original_days": 10,
                "denial_days": 6,
                "denial_unfreeze_days": 7,
                "unfreeze_actual_days_after": 1,
                "reject_decision_reason": "E2E needs document",
                "approve_expires_at_before": approve_expires_at_before.isoformat(),
                "reject_expires_at_before": reject_expires_at_before.isoformat(),
                "unfreeze_expires_at_before": unfreeze_expires_at_before.isoformat(),
                "unfreeze_expires_at_after": (
                    unfreeze_expires_at_before - timedelta(days=9)
                ).isoformat(),
                "denial_expires_at_before": denial_expires_at_before.isoformat(),
                "denial_unfreeze_expires_at_before": denial_unfreeze_expires_at_before.isoformat(),
            },
            "created_at": now.isoformat(),
        }

    def _create_subscription(
        self,
        *,
        club: Club,
        tariff: Tariff,
        first_name: str,
        last_name: str,
        phone: str,
    ) -> Subscription:
        student = Student.objects.create(
            club=club,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        return Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=tariff.trainings_limit,
            trainings_used=0,
            expires_at=timezone.now() + timedelta(days=30),
            scope=tariff.scope,
        )

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@freeze-lifecycle-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
