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
from apps.attendance.services import activate_kiosk, enroll_student_in_schedule, generate_kiosk_pin
from apps.billing.models import Debt, Payment, Subscription, SubscriptionFreeze, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.documents.models import DocumentType, StudentDocument
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated two-club fixture for tenant negative matrix E2E."

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
                "Prepared tenant negative matrix E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_a_id={fixture['club_a']['club_id']}, club_b_id={fixture['club_b']['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"tenant-negative-matrix-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        club_a = self._create_club_side(
            fixture_id=fixture_id,
            unique=unique,
            label="A",
            phone_suffix="1111",
        )
        club_b = self._create_club_side(
            fixture_id=fixture_id,
            unique=unique,
            label="B",
            phone_suffix="4242",
        )

        return {
            "fixture_id": fixture_id,
            "club_a": club_a,
            "club_b": club_b,
            "created_at": now.isoformat(),
        }

    def _create_club_side(self, *, fixture_id: str, unique: str, label: str, phone_suffix: str) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        # The dashboard schedule grid renders Monday-Saturday; keep UI markers visible on Sundays.
        schedule_date = today if today.weekday() < 6 else today - timedelta(days=1)
        club = Club.objects.create(
            name=f"Jaguar Tenant {label} E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display=f"Tenant {label} E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Tenant {label} Hall {fixture_id}",
            address=f"Tenant {label} matrix fixture",
        )

        owner_password = f"Tenant{label}Owner-{unique}-pass"
        parent_password = f"Tenant{label}Parent-{unique}-pass"
        trainer_password = f"Tenant{label}Trainer-{unique}-pass"
        owner_user = self._create_user(fixture_id=fixture_id, label=label, role="owner", password=owner_password)
        parent_user = self._create_user(fixture_id=fixture_id, label=label, role="parent", password=parent_password)
        trainer_user = self._create_user(fixture_id=fixture_id, label=label, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)

        trainer = Trainer.objects.create(
            club=club,
            first_name=f"Tenant{label}",
            last_name="Trainer",
            phone=f"+15550{club.id:04d}{phone_suffix[:3]}",
            user=trainer_user,
        )
        grade_system = GradeSystem.objects.create(club=club, discipline=f"Tenant {label} Muay Thai {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name=f"Tenant{label} Start",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Tenant {label} Group {fixture_id}",
            slug=f"tenant-{label.lower()}-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Tenant {label} 5-Pack {fixture_id}",
            price=Decimal("6000.00"),
            trainings_limit=5,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        child = Student.objects.create(
            club=club,
            first_name=f"Tenant{label}",
            last_name="Child",
            phone=f"+15551{club.id:04d}{phone_suffix}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        lead = Student.objects.create(
            club=club,
            first_name=f"Tenant{label}",
            last_name="Lead",
            phone=f"+15552{club.id:04d}{phone_suffix}",
            email="",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=trainer,
        )
        StudentGrade.objects.create(
            club=club,
            student=child,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=5,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        payment = Payment.objects.create(
            club=club,
            student=child,
            tariff=tariff,
            subscription=subscription,
            amount=tariff.price,
            original_amount=tariff.price,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        freeze = SubscriptionFreeze.objects.create(
            club=club,
            subscription=subscription,
            days=3,
            reason=SubscriptionFreeze.Reason.OTHER,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
            frozen_by=owner_user,
            starts_at=now,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=schedule_date.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Tenant {label} Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=schedule_date,
            is_active=True,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=child.id,
            schedule_id=schedule.id,
            starts_on=schedule_date,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=schedule_date,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        debt = Debt.objects.create(
            club=club,
            student=child,
            checkin=checkin,
            tariff_price=training_type.drop_in_price,
            reason="no_subscription",
        )
        document_type = DocumentType.objects.create(
            club=club,
            name=f"Tenant {label} Medical {fixture_id}",
            scope=DocumentType.Scope.CHILDREN,
            is_required=True,
        )
        document = StudentDocument.objects.create(
            club=club,
            student=child,
            document_type=document_type,
            is_provided=True,
            notes=f"Tenant {label} private document note {fixture_id}",
        )
        task = RetentionTask.objects.create(
            club=club,
            student=lead,
            trainer=trainer,
            due_date=today,
            task_type=RetentionTask.TaskType.NEW_LEAD,
            notes=f"Tenant {label} retention note {fixture_id}",
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)
        kiosk_token = activate_kiosk(pin=kiosk_pin)["token"]

        return {
            "club_id": club.id,
            "owner": {
                "email": owner_user.email,
                "password": owner_password,
                "user_id": owner_user.id,
            },
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "trainer": {
                "email": trainer_user.email,
                "password": trainer_password,
                "user_id": trainer_user.id,
            },
            "kiosk_token": kiosk_token,
            "child_id": child.id,
            "lead_id": lead.id,
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "subscription_id": subscription.id,
            "payment_id": payment.id,
            "freeze_id": freeze.id,
            "checkin_id": checkin.id,
            "debt_id": debt.id,
            "document_type_id": document_type.id,
            "document_id": document.id,
            "retention_task_id": task.id,
            "checkin_date": schedule_date.isoformat(),
            "markers": {
                "student_name": str(child),
                "lead_name": str(lead),
                "group_name": schedule.group_name,
                "training_type_name": training_type.name,
                "document_type_name": document_type.name,
                "phone_suffix": phone_suffix,
            },
        }

    def _create_user(self, *, fixture_id: str, label: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{label.lower()}-{role}"
        email = f"{role}-{label.lower()}-{fixture_id}@tenant-negative-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
