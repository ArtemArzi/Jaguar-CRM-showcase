from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent
from apps.attendance.services import create_checkin
from apps.billing.models import Subscription
from apps.clubs.models import Club
from apps.dashboard.services import get_pnl_report
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment, TrainerPackageAllocation


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


class Command(BaseCommand):
    help = "Assert trainer package transfer E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_package_transfer_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for UI-created subscription before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"trainer package transfer E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {
            "fixture_id",
            "club_id",
            "owner",
            "student_id",
            "training_type_id",
            "tariff_id",
            "schedule_id",
            "package_owner_trainer_id",
            "actual_trainer_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        club_id = club.id
        student_id = int(fixture["student_id"])
        tariff_id = int(fixture["tariff_id"])
        training_type_id = int(fixture["training_type_id"])
        schedule_id = int(fixture["schedule_id"])
        package_owner_trainer_id = int(fixture["package_owner_trainer_id"])
        actual_trainer_id = int(fixture["actual_trainer_id"])
        expected = fixture["expected"]
        try:
            checkin_date = date.fromisoformat(
                fixture.get("checkin_date") or timezone.localdate().isoformat()
            )
        except ValueError as exc:
            raise CommandError(f"fixture checkin_date is not valid ISO date: {fixture.get('checkin_date')}") from exc

        existing_checkin = (
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                date=checkin_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .select_related("subscription")
            .order_by("-created_at", "-id")
            .first()
        )
        if existing_checkin is not None and existing_checkin.subscription_id is not None:
            subscription = existing_checkin.subscription
        else:
            subscription = (
                Subscription.objects.for_club(club_id)
                .filter(student_id=student_id, tariff_id=tariff_id, deleted_at__isnull=True)
                .order_by("-created_at", "-id")
                .first()
            )
        if subscription is None:
            raise CommandError("subscription not created")
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"subscription not active: got {subscription.status}")

        allocation = (
            TrainerPackageAllocation.objects.for_club(club_id)
            .filter(subscription=subscription, is_active=True)
            .select_related("owner_trainer")
            .first()
        )
        if allocation is None:
            raise CommandError("active package allocation not found")
        if allocation.owner_trainer_id != package_owner_trainer_id:
            raise CommandError(
                "package owner mismatch: "
                f"expected {package_owner_trainer_id}, got {allocation.owner_trainer_id}"
            )

        checkin_result = create_checkin(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
            source=Checkin.Source.MANUAL,
            checkin_date=checkin_date,
            _skip_group_analytics=True,
        )
        checkin = Checkin.objects.for_club(club_id).get(id=checkin_result["checkin_id"])
        if checkin.trainer_id != actual_trainer_id:
            raise CommandError(f"checkin trainer mismatch: expected {actual_trainer_id}, got {checkin.trainer_id}")
        if checkin.subscription_id != subscription.id:
            raise CommandError(
                f"checkin subscription mismatch: expected {subscription.id}, got {checkin.subscription_id}"
            )

        salary_event = (
            CheckinCascadeEvent.objects.for_club(club_id)
            .filter(
                checkin=checkin,
                effect=CheckinCascadeEvent.Effect.SALARY,
                expected=True,
            )
            .first()
        )
        if salary_event is None:
            raise CommandError("salary cascade event not queued")
        if salary_event.task_name != "apps.attendance.tasks.calculate_salary":
            raise CommandError(f"salary cascade task mismatch: got {salary_event.task_name}")
        salary_payload = salary_event.payload or {}
        if salary_payload.get("checkin_id") != checkin.id or salary_payload.get("club_id") != club_id:
            raise CommandError("salary cascade payload does not target the checkin")
        if salary_payload.get("calculation_basis") != "checkin_salary_snapshot":
            raise CommandError("salary cascade payload is missing salary snapshot basis")
        if salary_payload.get("snapshot_provenance") != "checkin_queue":
            raise CommandError("salary cascade payload is missing queue provenance")

        earning = TrainerEarning.objects.for_club(club_id).filter(checkin=checkin, cancelled=False).first()
        if earning is None:
            raise CommandError("trainer earning not created")
        expected_salary = Decimal(expected["salary_amount"])
        if earning.trainer_id != actual_trainer_id:
            raise CommandError(f"earning trainer mismatch: expected {actual_trainer_id}, got {earning.trainer_id}")
        if earning.amount != expected_salary:
            raise CommandError(f"earning amount mismatch: expected {expected_salary}, got {earning.amount}")

        transfer = (
            TrainerEarningAdjustment.objects.for_club(club_id)
            .filter(
                source_checkin=checkin,
                trainer_id=actual_trainer_id,
                kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
                direction=TrainerEarningAdjustment.Direction.INFO,
            )
            .select_related("counterparty_trainer")
            .first()
        )
        if transfer is None:
            raise CommandError("package transfer adjustment not created")
        if transfer.counterparty_trainer_id != package_owner_trainer_id:
            raise CommandError(
                "package transfer counterparty mismatch: "
                f"expected {package_owner_trainer_id}, got {transfer.counterparty_trainer_id}"
            )
        if transfer.payable_amount_delta != Decimal("0.00"):
            raise CommandError(f"package transfer payable delta is not zero: got {transfer.payable_amount_delta}")
        if transfer.affects_payroll:
            raise CommandError("package transfer unexpectedly affects payroll")

        payable_total = (
            TrainerEarning.objects.for_club(club_id).filter(cancelled=False).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        payroll_adjustment_total = (
            TrainerEarningAdjustment.objects.for_club(club_id)
            .filter(affects_payroll=True)
            .aggregate(total=Sum("payable_amount_delta"))["total"]
            or Decimal("0.00")
        )
        if payable_total + payroll_adjustment_total != expected_salary:
            raise CommandError("salary total changed by package transfer adjustment")

        subscription.refresh_from_db()
        expected_left = int(expected["subscription_trainings_left_after_checkin"])
        if subscription.trainings_left != expected_left:
            raise CommandError(
                f"subscription trainings_left mismatch: expected {expected_left}, got {subscription.trainings_left}"
            )

        pnl = get_pnl_report(club=club, date_from=checkin_date, date_to=checkin_date)
        if pnl["salary_expenses"] != expected_salary:
            raise CommandError(
                f"P&L salary mismatch: expected {expected_salary}, got {pnl['salary_expenses']}"
            )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "subscription": {
                "id": subscription.id,
                "status": subscription.status,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
            "allocation": {
                "id": allocation.id,
                "owner_trainer_id": allocation.owner_trainer_id,
                "owner_trainer_name": str(allocation.owner_trainer),
                "source": allocation.source,
            },
            "checkin": {
                "id": checkin.id,
                "date": str(checkin.date),
                "trainer_id": checkin.trainer_id,
                "subscription_id": checkin.subscription_id,
                "created": checkin_result["created"],
            },
            "salary_cascade": {
                "id": salary_event.id,
                "task_name": salary_event.task_name,
                "expected": salary_event.expected,
                "calculation_basis": salary_payload.get("calculation_basis"),
                "snapshot_provenance": salary_payload.get("snapshot_provenance"),
            },
            "earning": {
                "id": earning.id,
                "trainer_id": earning.trainer_id,
                "amount": str(earning.amount),
                "rate_percent": str(earning.rate_percent),
            },
            "package_transfer": {
                "id": transfer.id,
                "counterparty_trainer_id": transfer.counterparty_trainer_id,
                "counterparty_trainer_name": str(transfer.counterparty_trainer),
                "amount_basis_snapshot": str(transfer.amount_basis_snapshot),
                "payable_amount_delta": str(transfer.payable_amount_delta),
                "affects_payroll": transfer.affects_payroll,
            },
            "salary": {
                "payable_total": _money(payable_total),
                "payroll_adjustment_total": _money(payroll_adjustment_total),
            },
            "pnl": {
                "income": _money(pnl["income"]),
                "salary_expenses": _money(pnl["salary_expenses"]),
                "margin": _money(pnl["margin"]),
            },
        }
