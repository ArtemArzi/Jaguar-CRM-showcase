from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import create_checkin
from apps.attendance.tasks import calculate_salary
from apps.billing.models import Payment, SubscriptionComponent, Tariff
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert subscription payout policy real-stack E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for async sale earning before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        deadline = time.monotonic() + max(float(options["timeout_seconds"]), 0)

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"subscription payout policy E2E assertion failed: {exc}") from exc
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
            "payment_id",
            "personal_owner_trainer_id",
            "personal_checkin_student_id",
            "personal_checkin_schedule_id",
            "personal_checkin_training_type_id",
            "personal_checkin_subscription_id",
            "mini_trainer_id",
            "mini_student_id",
            "mini_schedule_id",
            "mini_training_type_id",
            "mini_subscription_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        payment_id = int(fixture["payment_id"])
        personal_owner_trainer_id = int(fixture["personal_owner_trainer_id"])
        personal_checkin_student_id = int(fixture["personal_checkin_student_id"])
        personal_checkin_schedule_id = int(fixture["personal_checkin_schedule_id"])
        personal_checkin_training_type_id = int(fixture["personal_checkin_training_type_id"])
        personal_checkin_subscription_id = int(fixture["personal_checkin_subscription_id"])
        mini_trainer_id = int(fixture["mini_trainer_id"])
        mini_student_id = int(fixture["mini_student_id"])
        mini_schedule_id = int(fixture["mini_schedule_id"])
        mini_training_type_id = int(fixture["mini_training_type_id"])
        mini_subscription_id = int(fixture["mini_subscription_id"])
        expected = fixture["expected"]

        payment = Payment.objects.for_club(club_id).select_related("subscription").get(id=payment_id)
        if payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"payment not confirmed: got {payment.status}")
        if payment.subscription_id is None:
            raise CommandError("payment subscription is missing")

        sale_earnings = list(
            TrainerEarning.objects.for_club(club_id)
            .filter(
                payment_id=payment_id,
                earning_source=TrainerEarning.Source.SALE,
            )
            .order_by("id")
        )
        if len(sale_earnings) != 1:
            raise CommandError(
                "personal on-payment sale earning count mismatch: "
                f"expected 1, got {len(sale_earnings)}"
            )
        sale = sale_earnings[0]
        if sale.subscription_component_id is None:
            raise CommandError("personal on-payment sale earning is missing component snapshot")
        if sale.trainer_id != personal_owner_trainer_id:
            raise CommandError(
                f"sale trainer mismatch: expected {personal_owner_trainer_id}, got {sale.trainer_id}"
            )
        if sale.payout_policy_snapshot != Tariff.PayoutPolicy.ON_PAYMENT:
            raise CommandError(f"sale payout policy mismatch: got {sale.payout_policy_snapshot}")
        expected_sale_amount = Decimal(expected["personal_sale_amount"])
        expected_sale_basis = Decimal(expected["personal_sale_basis"])
        if sale.amount != expected_sale_amount:
            raise CommandError(f"sale amount mismatch: expected {expected_sale_amount}, got {sale.amount}")
        if sale.subscription_price != expected_sale_basis:
            raise CommandError(f"sale basis mismatch: expected {expected_sale_basis}, got {sale.subscription_price}")

        personal_checkin = self._ensure_checkin(
            club_id=club_id,
            student_id=personal_checkin_student_id,
            schedule_id=personal_checkin_schedule_id,
            training_type_id=personal_checkin_training_type_id,
        )
        calculate_salary(personal_checkin.id, club_id)
        personal_checkin_earning = TrainerEarning.objects.for_club(club_id).get(checkin_id=personal_checkin.id)
        expected_personal_checkin_amount = Decimal(expected["personal_checkin_salary_amount"])
        expected_personal_checkin_basis = Decimal(expected["personal_checkin_salary_basis"])
        if personal_checkin.subscription_id != personal_checkin_subscription_id:
            raise CommandError(
                "personal check-in subscription mismatch: "
                f"expected {personal_checkin_subscription_id}, got {personal_checkin.subscription_id}"
            )
        if personal_checkin_earning.trainer_id != personal_owner_trainer_id:
            raise CommandError(
                "personal check-in salary trainer mismatch: "
                f"expected {personal_owner_trainer_id}, got {personal_checkin_earning.trainer_id}"
            )
        if personal_checkin_earning.payout_policy_snapshot != Tariff.PayoutPolicy.ON_CHECKIN:
            raise CommandError(
                f"personal check-in salary payout policy mismatch: {personal_checkin_earning.payout_policy_snapshot}"
            )
        if personal_checkin_earning.amount != expected_personal_checkin_amount:
            raise CommandError(
                "personal check-in salary amount mismatch: "
                f"expected {expected_personal_checkin_amount}, got {personal_checkin_earning.amount}"
            )
        if personal_checkin_earning.subscription_price != expected_personal_checkin_basis:
            raise CommandError(
                "personal check-in salary basis mismatch: "
                f"expected {expected_personal_checkin_basis}, got {personal_checkin_earning.subscription_price}"
            )

        mini_checkin = self._ensure_mini_group_checkin(
            club_id=club_id,
            student_id=mini_student_id,
            schedule_id=mini_schedule_id,
            training_type_id=mini_training_type_id,
        )
        calculate_salary(mini_checkin.id, club_id)
        mini_earning = TrainerEarning.objects.for_club(club_id).get(checkin_id=mini_checkin.id)
        expected_mini_amount = Decimal(expected["mini_salary_amount"])
        expected_mini_basis = Decimal(expected["mini_salary_basis"])
        if mini_earning.trainer_id != mini_trainer_id:
            raise CommandError(
                f"mini salary trainer mismatch: expected {mini_trainer_id}, got {mini_earning.trainer_id}"
            )
        if mini_earning.payout_policy_snapshot != Tariff.PayoutPolicy.ON_CHECKIN:
            raise CommandError(f"mini salary payout policy mismatch: got {mini_earning.payout_policy_snapshot}")
        if mini_earning.amount != expected_mini_amount:
            raise CommandError(
                f"mini salary amount mismatch: expected {expected_mini_amount}, got {mini_earning.amount}"
            )
        if mini_earning.subscription_price != expected_mini_basis:
            raise CommandError(
                f"mini salary basis mismatch: expected {expected_mini_basis}, got {mini_earning.subscription_price}"
            )

        mini_component = SubscriptionComponent.objects.for_club(club_id).get(
            subscription_id=mini_subscription_id,
            training_type_id=mini_training_type_id,
        )
        if mini_component.credits_left != 3:
            raise CommandError(f"mini component credits mismatch: expected 3, got {mini_component.credits_left}")

        student = Student.objects.for_club(club_id).get(id=mini_student_id)
        if student.status != Student.Status.ACTIVE:
            raise CommandError(f"mini student status mismatch: got {student.status}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "payment": {
                "id": payment.id,
                "status": payment.status,
                "subscription_id": payment.subscription_id,
            },
            "sale_earning": {
                "id": sale.id,
                "amount": str(sale.amount),
                "basis": str(sale.subscription_price),
                "payout_policy": sale.payout_policy_snapshot,
            },
            "personal_checkin": {
                "id": personal_checkin.id,
                "subscription_component_id": personal_checkin.subscription_component_id,
            },
            "personal_checkin_earning": {
                "id": personal_checkin_earning.id,
                "amount": str(personal_checkin_earning.amount),
                "basis": str(personal_checkin_earning.subscription_price),
                "payout_policy": personal_checkin_earning.payout_policy_snapshot,
            },
            "mini_checkin": {
                "id": mini_checkin.id,
                "subscription_component_id": mini_checkin.subscription_component_id,
            },
            "mini_earning": {
                "id": mini_earning.id,
                "amount": str(mini_earning.amount),
                "basis": str(mini_earning.subscription_price),
                "payout_policy": mini_earning.payout_policy_snapshot,
            },
            "mini_component": {
                "id": mini_component.id,
                "credits_left": mini_component.credits_left,
                "credits_used": mini_component.credits_used,
            },
        }

    def _ensure_checkin(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        training_type_id: int,
    ) -> Checkin:
        existing = (
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                subscription__isnull=False,
                cancelled_at__isnull=True,
            )
            .first()
        )
        if existing is not None:
            return existing

        result = create_checkin(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
            source=Checkin.Source.MANUAL,
            checkin_date=date.today(),
        )
        return Checkin.objects.for_club(club_id).get(id=result["checkin_id"])

    def _ensure_mini_group_checkin(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        training_type_id: int,
    ) -> Checkin:
        return self._ensure_checkin(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
        )
