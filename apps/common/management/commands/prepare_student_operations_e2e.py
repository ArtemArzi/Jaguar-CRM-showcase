"""Disposable, synthetic fixture for the student operations browser pack."""

import json
import os
import secrets
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.attendance.models import Schedule, ScheduleEnrollment, TrainingGroupRolloutState
from apps.billing.models import Payment, Subscription, SubscriptionComponent, Tariff, TariffComponent, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerPackageAllocation, TrainerRate


def assert_disposable_database():
    config = connection.settings_dict
    name = str(config["NAME"]).lower()
    if not any(marker in name for marker in ("test", "e2e", "memory")):
        raise CommandError("Student operations fixtures require a disposable test/e2e database")
    if connection.vendor == "postgresql" and config.get("HOST") not in {"127.0.0.1", "localhost", "::1"}:
        raise CommandError("Student operations fixtures require local PostgreSQL")


def load_owned_fixture(path):
    assert_disposable_database()
    data = json.loads(Path(path).read_text())
    if not str(data.get("fixture_id", "")).startswith("student-operations-e2e-"):
        raise CommandError("Wrong fixture type")
    if not Club.objects.filter(id=data["club_id"], name=data["fixture_id"]).exists():
        raise CommandError("Fixture club identity changed")
    return data


class Command(BaseCommand):
    help = "Prepare synthetic student-card correction, attendance and renewal evidence."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        assert_disposable_database()
        fixture = self.create_fixture()
        target = Path(options["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(fixture, output)
        target.chmod(0o600)
        self.stdout.write(json.dumps({"ok": True, "club_id": fixture["club_id"]}))

    @transaction.atomic
    def create_fixture(self):
        suffix = uuid4().hex[:12]
        fixture_id = f"student-operations-e2e-{suffix}"
        now = timezone.now()
        club = Club.objects.create(name=fixture_id, timezone="UTC")
        ClubSettings.objects.create(club=club, club_name_display="Проверка карточки ученика", freeze_enabled=True)
        TrainingGroupRolloutState.objects.get_or_create(club=club, defaults={"mode": "off"})
        owner_secret = secrets.token_urlsafe(24)
        owner = get_user_model().objects.create_user(
            username=f"owner-{suffix}", email=f"owner-{suffix}@example.invalid", password=owner_secret
        )
        ClubMembership.objects.create(club=club, user=owner, role="owner")
        trainer = Trainer.objects.create(club=club, first_name="Тренер", last_name="Проверка")
        location = Location.objects.create(club=club, name="Тестовый зал")
        training_type = TrainingType.objects.create(
            club=club,
            name="Персональное занятие",
            slug=f"personal-{suffix}",
            kind="personal",
            drop_in_price=Decimal("1000"),
        )
        tariff = Tariff.objects.create(
            club=club,
            name="Проверочный пакет",
            training_type=training_type,
            price=Decimal("8000"),
            trainings_limit=8,
            duration_days=30,
            trainer_payout_policy="on_checkin",
        )
        tariff_component = TariffComponent.objects.create(
            club=club,
            tariff=tariff,
            training_type=training_type,
            name="Персональные",
            entitlement_kind="finite_credits",
            credits_total=8,
            paid_amount_basis=Decimal("8000"),
            trainer_payout_policy="on_checkin",
        )
        student = Student.objects.create(
            club=club,
            first_name="Ученик",
            last_name=f"Проверка-{suffix}",
            phone="+79990000001",
            status="active",
            lead_status=None,
            became_student_at=now,
        )
        control = Student.objects.create(
            club=club,
            first_name="Контроль",
            last_name="Другой ученик",
            phone="+79990000002",
            status="active",
            lead_status=None,
            became_student_at=now,
        )
        foreign_club = Club.objects.create(name=f"{fixture_id}-foreign", timezone="UTC")
        foreign = Student.objects.create(
            club=foreign_club,
            first_name="Контроль",
            last_name="Другой клуб",
            phone="+79990000003",
            status="active",
            lead_status=None,
            became_student_at=now,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
            trainer_payout_policy_snapshot="on_checkin",
        )
        Subscription.objects.for_club(club).filter(id=subscription.id).update(activated_at=now - timedelta(days=30))
        component = SubscriptionComponent.objects.create(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            name_snapshot="Персональные",
            training_type=training_type,
            entitlement_kind="finite_credits",
            credits_total=8,
            credits_left=7,
            credits_used=1,
            paid_amount_basis_snapshot=Decimal("8000"),
            unit_amount_basis_snapshot=Decimal("1000"),
            trainer_payout_policy_snapshot="on_checkin",
        )
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("8000"),
            original_amount=Decimal("8000"),
            payment_method="cash",
            status="confirmed",
            recorded_by=owner,
            verified_by=owner,
            verified_at=now - timedelta(days=25),
            package_owner_trainer=trainer,
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            student=student,
            subscription=subscription,
            payment=payment,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=trainer,
            amount_snapshot=Decimal("8000"),
            sessions_total_snapshot=8,
            sessions_remaining_snapshot=7,
            source="payment",
            activated_at=now - timedelta(days=25),
            created_by=owner,
        )
        TrainerRate.objects.create(
            club=club, trainer=trainer, location=location, training_type=training_type, percent=Decimal("50")
        )
        schedules = []
        for offset in (2, 3):
            day = (now - timedelta(days=offset)).date()
            schedule = Schedule.objects.create(
                club=club,
                trainer=trainer,
                location=location,
                training_type=training_type,
                group_name="Проверочное занятие",
                one_time_date=day,
                day_of_week=day.weekday(),
                start_time=time(18),
                end_time=time(19),
            )
            for person in (student, control):
                ScheduleEnrollment.objects.create(
                    club=club,
                    student=person,
                    schedule=schedule,
                    starts_on=day - timedelta(days=30),
                    created_from="manual",
                )
            schedules.append({"id": schedule.id, "date": day.isoformat()})
        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {"id": owner.id, "email": owner.email, "password": owner_secret},
            "student": {"id": student.id, "name": str(student)},
            "control_student_id": control.id,
            "foreign_student_id": foreign.id,
            "subscription_id": subscription.id,
            "component_id": component.id,
            "schedule": schedules[0],
            "concurrent_schedule": schedules[1],
        }
