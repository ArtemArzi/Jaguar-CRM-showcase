"""Private synthetic trainer settlement fixture; never use against live data."""

import json
import os
import secrets
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Payment, Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.common.management.commands.prepare_student_operations_e2e import assert_disposable_database
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerEarning
from apps.trainers.settlement_services import record_trainer_settlement


def load_fixture(path):
    assert_disposable_database()
    data = json.loads(Path(path).read_text())
    if (
        not str(data.get("fixture_id", "")).startswith("trainer-settlements-e2e-")
        or not Club.objects.filter(
            id=data.get("club_id"),
            name=data.get("fixture_id"),
        ).exists()
    ):
        raise CommandError("Wrong trainer settlement fixture")
    return data


class Command(BaseCommand):
    help = "Prepare synthetic settlement opening, payout, reversal and historical correction evidence."

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
        self.stdout.write(json.dumps({"ok": True}))

    @transaction.atomic
    def create_fixture(self):
        suffix = uuid4().hex[:12]
        fixture_id = f"trainer-settlements-e2e-{suffix}"
        now = timezone.now()
        club = Club.objects.create(name=fixture_id, timezone="UTC")
        ClubSettings.objects.create(club=club, club_name_display="Проверка расчётов")
        owner_secret = secrets.token_urlsafe(24)
        owner = get_user_model().objects.create_user(
            username=f"owner-{suffix}",
            email=f"owner-{suffix}@example.invalid",
            password=owner_secret,
        )
        ClubMembership.objects.create(club=club, user=owner, role="owner")
        trainer = Trainer.objects.create(club=club, first_name="Тренер", last_name="Расчёты")
        second = Trainer.objects.create(club=club, first_name="Тренер", last_name="Сверка")
        location = Location.objects.create(club=club, name="Проверочный зал")
        group = TrainingType.objects.create(club=club, name="Группа", kind="group", slug=f"group-{suffix}")
        personal = TrainingType.objects.create(
            club=club, name="Персональные", kind="personal", slug=f"personal-{suffix}"
        )
        tariff = Tariff.objects.create(
            club=club, training_type=group, name="Групповой", price=Decimal("6500"), trainings_limit=8, duration_days=30
        )
        student = Student.objects.create(
            club=club,
            first_name="Ученик",
            last_name="Синтетический",
            phone="+79990000011",
            status="active",
            lead_status=None,
            became_student_at=now,
        )
        sub = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
        )
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=sub,
            amount=Decimal("6500"),
            original_amount=Decimal("6500"),
            payment_method="cash",
            status="confirmed",
            verified_at=now - timedelta(days=7),
            recorded_by=owner,
            verified_by=owner,
        )
        historical = TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            payment=payment,
            earning_source="sale",
            earning_type="group",
            amount=Decimal("1300"),
            rate_percent=Decimal("20"),
            subscription_price=Decimal("6500"),
        )
        schedule = Schedule.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal,
            day_of_week=now.date().weekday(),
            one_time_date=now.date(),
            start_time=time(10),
            end_time=time(11),
            group_name="Синтетическое занятие",
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            location=location,
            training_type=personal,
            date=now.date(),
            source="manual",
            subscription=sub,
        )
        TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            checkin=checkin,
            amount=Decimal("500"),
            earning_type="personal",
            rate_percent=Decimal("50"),
            subscription_price=Decimal("1000"),
        )
        opening_on = now.date() - timedelta(days=1)
        record_trainer_settlement(
            club_id=club.id,
            actor_user_id=owner.id,
            trainer_id=second.id,
            kind="opening",
            effective_on=opening_on,
            balance_delta=0,
            reason="Начальная сверка второго тренера",
            source_namespace="fixture",
            source_key="second",
        )
        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer_id": trainer.id,
            "second_trainer_id": second.id,
            "historical_earning_id": historical.id,
            "opening_on": str(opening_on),
            "today": str(now.date()),
            "owner": {"id": owner.id, "email": owner.email, "password": owner_secret},
        }
