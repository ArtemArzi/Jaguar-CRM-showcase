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
from apps.attendance.services import generate_kiosk_pin
from apps.billing.models import Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for owner/admin payment rejection E2E."

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
                "Prepared payment rejection E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, child_student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"payment-reject-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"PaymentRejectE2E-{unique}-pass"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Payment Reject E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Payment Reject E2E",
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        location = Location.objects.create(
            club=club,
            name=f"Reject Hall {fixture_id}",
            address="Payment rejection E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_password = f"PaymentRejectTrainerE2E-{unique}-pass"
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Reject",
            last_name="Seller",
            phone=f"+155580{numeric_suffix}",
            user=trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Group Reject E2E {fixture_id}",
            slug=f"group-reject-e2e-{unique}",
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
            name=f"Payment Reject 8-Pack {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Reject",
            last_name="Child",
            phone="",
            email="",
            is_child=True,
            date_of_birth=timezone.localdate().replace(year=timezone.localdate().year - 10),
            guardian_phone=f"+155590{numeric_suffix}",
            status=Student.Status.LEAD,
            source=Student.Source.WEBSITE,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=trainer,
        )
        target_start_date = today + timedelta(days=1)
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_start_date.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Reject Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        second_schedule_date = target_start_date + timedelta(days=2)
        second_schedule = Schedule.objects.create(
            club=club,
            day_of_week=second_schedule_date.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=owner_user.id,
            schedule_ids=[schedule.id, second_schedule.id],
            canonical_name=schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-canonical-group",
            require_manual_operational_admission=True,
        )
        adult = Student.objects.create(
            club=club,
            first_name="Reject",
            last_name="Adult",
            phone=f"+155591{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.WEBSITE,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=trainer,
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "trainer": {
                "user_id": trainer_user.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "trainer_id": trainer.id,
            "student_id": student.id,
            "child": {
                "id": student.id,
                "name": str(student),
                "parent_phone_input": student.guardian_phone,
            },
            "adult": {
                "id": adult.id,
                "name": str(adult),
            },
            "training_type_id": training_type.id,
            "tariff_id": tariff.id,
            "schedule_id": schedule.id,
            "tariff": {"id": tariff.id, "name": tariff.name},
            "schedule": {
                "id": schedule.id,
                "name": schedule.group_name,
                "start_date": target_start_date.isoformat(),
                "training_group_id": canonical_group["training_group_id"],
                "rollout_mode": canonical_group["mode"],
                "second_schedule_id": second_schedule.id,
                "second_start_date": second_schedule_date.isoformat(),
                "new_writes_enabled": canonical_group["new_writes_enabled"],
                "manual_operational_admission_enabled": canonical_group[
                    "manual_operational_admission_enabled"
                ],
            },
            "kiosk": {"activation_pin": kiosk_pin},
            "expected": {
                "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
                "payment_amount": "4000.00",
                "rejection_reason": "E2E receipt mismatch",
                "sale_rate_percent": "25.00",
                "sale_earning_amount": "1000.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@payment-reject-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
