"""Synthetic mixed workbook and audited group rollout for the import browser gate."""

import json
import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule
from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.opening_terms import OpeningEntitlementTerms
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.common.management.commands.prepare_student_operations_e2e import assert_disposable_database
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.students.imports.parsing import ENTITLEMENTS_SHEET, SETTLEMENTS_SHEET, WorkbookRow
from apps.students.imports.schemas import COLUMNS
from apps.students.imports.storage import private_path, save_private
from apps.students.imports.workbooks import workbook_bytes
from apps.trainers.models import Trainer


def load_import_fixture(path):
    assert_disposable_database()
    data = json.loads(Path(path).read_text())
    if not str(data.get("fixture_id", "")).startswith("student-opening-e2e-"):
        raise CommandError("Wrong import fixture type")
    if not Club.objects.filter(id=data["club_id"], name=data["fixture_id"]).exists():
        raise CommandError("Fixture club identity changed")
    return data


class Command(BaseCommand):
    help = "Prepare private synthetic mixed opening workbook without issuing its rows."

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
        fixture_id = f"student-opening-e2e-{suffix}"
        now = timezone.now()
        day = now.date()
        first_day = day + timedelta(days=3)
        club = Club.objects.create(name=fixture_id, timezone="UTC")
        ClubSettings.objects.create(club=club, club_name_display="Проверка переноса")
        password = secrets.token_urlsafe(24)
        owner = get_user_model().objects.create_user(
            username=f"import-{suffix}", email=f"import-{suffix}@example.invalid", password=password
        )
        ClubMembership.objects.create(club=club, user=owner, role="owner")
        location = Location.objects.create(club=club, name="Тестовый зал")
        assigned = Trainer.objects.create(club=club, first_name="Ответственный", last_name="А")
        package_owner = Trainer.objects.create(club=club, first_name="Владелец пакета", last_name="Б")
        actual = Trainer.objects.create(club=club, first_name="Тренер занятия", last_name="В")
        seller = Trainer.objects.create(club=club, first_name="Получатель комиссии", last_name="Г")
        personal = TrainingType.objects.create(
            club=club, name="Персональная", slug=f"personal-{suffix}", kind="personal"
        )
        group_type = TrainingType.objects.create(club=club, name="Групповая", slug=f"group-{suffix}", kind="group")
        personal_tariff = Tariff.objects.create(
            club=club,
            name="Исходные 12 персональных",
            training_type=personal,
            price=Decimal("15000"),
            trainings_limit=12,
            duration_days=30,
            trainer_payout_policy="on_checkin",
        )
        TariffComponent.objects.create(
            club=club,
            tariff=personal_tariff,
            training_type=personal,
            name="Персональные",
            entitlement_kind="finite_credits",
            credits_total=12,
            paid_amount_basis=Decimal("15000"),
            trainer_payout_policy="on_checkin",
        )
        group_tariff = Tariff.objects.create(
            club=club,
            name="Исходный групповой",
            training_type=group_type,
            price=Decimal("7000"),
            trainings_limit=8,
            duration_days=30,
            trainer_payout_policy="on_payment",
        )
        TariffComponent.objects.create(
            club=club,
            tariff=group_tariff,
            training_type=group_type,
            name="Групповые",
            entitlement_kind="finite_credits",
            credits_total=8,
            paid_amount_basis=Decimal("7000"),
            trainer_payout_policy="on_payment",
        )
        personal_schedule = Schedule.objects.create(
            club=club,
            trainer=actual,
            location=location,
            training_type=personal,
            one_time_date=first_day,
            day_of_week=first_day.weekday(),
            start_time=time(18),
            end_time=time(19),
        )
        group_schedule = Schedule.objects.create(
            club=club,
            trainer=actual,
            location=location,
            training_type=group_type,
            group_name="Перенесённая группа",
            day_of_week=first_day.weekday(),
            start_time=time(18),
            end_time=time(19),
        )
        canonical = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=owner.id,
            schedule_ids=[group_schedule.id],
            canonical_name="Перенесённая группа",
            responsible_trainer_id=actual.id,
            idempotency_prefix=suffix,
        )
        namespace = uuid4().hex
        terms = OpeningEntitlementTerms(
            source_namespace=namespace,
            student_source_key="personal-student",
            entitlement_source_key="personal-package",
            payment_source_key="personal-payment",
            first_name="Персональный",
            last_name="Перенос",
            is_child=False,
            phone="+79990000011",
            guardian_phone="",
            date_of_birth=None,
            tariff_id=personal_tariff.id,
            started_on=day - timedelta(days=10),
            expires_on=day + timedelta(days=20),
            effective_on=day - timedelta(days=10),
            covered_through=now - timedelta(days=1),
            operational_cutover=timezone.make_aware(datetime.combine(first_day, time(18)), UTC),
            original_total=12,
            original_used=5,
            original_left=7,
            paid_amount=Decimal("12000"),
            payout_policy="on_checkin",
            payment_method="unknown",
            external_gap_confirmed=True,
            past_training_confirmed=True,
            assigned_trainer_id=assigned.id,
            package_owner_trainer_id=package_owner.id,
            cutover_schedule_id=personal_schedule.id,
        )
        group_terms = replace(
            terms,
            student_source_key="group-student",
            entitlement_source_key="group-package",
            payment_source_key="group-payment",
            first_name="Групповой",
            phone="+79990000012",
            tariff_id=group_tariff.id,
            original_total=8,
            original_used=2,
            original_left=6,
            paid_amount=Decimal("6500"),
            payout_policy="on_payment",
            package_owner_trainer_id=None,
            cutover_schedule_id=None,
            schedule_id=group_schedule.id,
            training_group_id=canonical["training_group_id"],
            sale_trainer_id=seller.id,
            sale_rate_percent=Decimal("20"),
        )
        question = replace(
            terms,
            student_source_key="review-student",
            entitlement_source_key="review-package",
            payment_source_key="review-payment",
            first_name="Уточнение",
            phone="+79990000013",
        )
        rows = []
        for index, source in enumerate((group_terms, terms, question), start=2):
            payload = source.as_payload()
            values = {column: payload[field] for field, column in COLUMNS.items()}
            values.update(
                {
                    "Исходная цена": payload["paid_amount"],
                    "Валюта": "RUB",
                    "Исторический долг": 0,
                    "Заморожен": source == question,
                    "Тип пакета": "Групповой" if source == group_terms else "Персональный",
                }
            )
            rows.append(WorkbookRow(ENTITLEMENTS_SHEET, index, values))
        rows.extend(
            [
                WorkbookRow(
                    SETTLEMENTS_SHEET,
                    2,
                    {
                        "source_key": "baseline",
                        "Вид записи": "Начальный расчёт",
                        "ID тренера": seller.id,
                        "Дата": day.isoformat(),
                        "Сумма": "1000",
                        "Причина": "Подтверждённый начальный долг",
                        "Подтверждено": True,
                        "Ключи абонементов": "group-package, personal-package",
                    },
                ),
                WorkbookRow(
                    SETTLEMENTS_SHEET,
                    3,
                    {
                        "source_key": "paid",
                        "Вид записи": "Выплата",
                        "ID тренера": seller.id,
                        "Дата": day.isoformat(),
                        "Сумма": "200",
                        "Причина": "Деньги уже выданы",
                        "Подтверждено": True,
                        "Способ выплаты": "Наличные",
                    },
                ),
            ]
        )
        book = save_private(
            content=workbook_bytes(club_id=club.id, actor_user_id=owner.id, namespace=namespace, rows=rows),
            suffix="xlsx",
        )
        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {"id": owner.id, "email": owner.email, "password": password},
            "workbook_path": str(private_path(name=book)),
            "namespace": namespace,
            "seller_id": seller.id,
            "assigned_id": assigned.id,
            "package_owner_id": package_owner.id,
            "actual_trainer_id": actual.id,
            "group_id": canonical["training_group_id"],
        }
