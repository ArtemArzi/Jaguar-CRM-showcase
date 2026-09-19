from __future__ import annotations

import json
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import create_checkin
from apps.attendance.tasks import calculate_salary
from apps.billing.models import Debt, Payment, SubscriptionComponent, Tariff
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert hybrid package entitlement real-stack E2E side effects."

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
                    raise CommandError(f"hybrid package entitlements E2E assertion failed: {exc}") from exc
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
            "payment_id",
            "subscription_id",
            "student_id",
            "group_trainer_id",
            "personal_trainer_id",
            "group_training_type_id",
            "personal_training_type_id",
            "group_schedule_id",
            "debt_checkin_id",
            "debt_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        payment_id = int(fixture["payment_id"])
        subscription_id = int(fixture["subscription_id"])
        student_id = int(fixture["student_id"])
        group_trainer_id = int(fixture["group_trainer_id"])
        personal_trainer_id = int(fixture["personal_trainer_id"])
        group_training_type_id = int(fixture["group_training_type_id"])
        personal_training_type_id = int(fixture["personal_training_type_id"])
        group_schedule_id = int(fixture["group_schedule_id"])
        debt_checkin_id = int(fixture["debt_checkin_id"])
        debt_id = int(fixture["debt_id"])
        expected = fixture["expected"]

        payment = Payment.objects.for_club(club_id).get(id=payment_id)
        if payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"payment not confirmed: got {payment.status}")
        if payment.subscription_id != subscription_id:
            raise CommandError(
                f"payment subscription mismatch: expected {subscription_id}, got {payment.subscription_id}"
            )

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
                "hybrid group sale earning count mismatch: "
                f"expected 1, got {len(sale_earnings)}"
            )
        sale = sale_earnings[0]
        if sale.subscription_component_id is None:
            raise CommandError("hybrid group sale earning is missing component snapshot")
        self._assert_money("sale amount", Decimal(expected["sale_amount"]), sale.amount)
        self._assert_money("sale basis", Decimal(expected["sale_basis"]), sale.subscription_price)
        if sale.trainer_id != group_trainer_id:
            raise CommandError(f"sale trainer mismatch: expected {group_trainer_id}, got {sale.trainer_id}")
        if sale.payout_policy_snapshot != Tariff.PayoutPolicy.ON_PAYMENT:
            raise CommandError(f"sale payout policy mismatch: got {sale.payout_policy_snapshot}")

        debt = Debt.objects.for_club(club_id).get(id=debt_id)
        debt_checkin = Checkin.objects.for_club(club_id).get(id=debt_checkin_id)
        if debt.resolved_at is None or debt.resolution_type != "payment":
            raise CommandError(f"debt not resolved by payment: got {debt.resolution_type}")
        if debt.settlement_payment_id != payment_id:
            raise CommandError(
                f"debt settlement payment mismatch: expected {payment_id}, got {debt.settlement_payment_id}"
            )
        if debt_checkin.subscription_component_id is None:
            raise CommandError("debt check-in subscription component is missing")
        personal_component = SubscriptionComponent.objects.for_club(club_id).get(
            id=debt_checkin.subscription_component_id
        )
        if personal_component.training_type_id != personal_training_type_id:
            raise CommandError(
                "debt attached to wrong component: "
                f"expected type {personal_training_type_id}, got {personal_component.training_type_id}"
            )
        if personal_component.trainer_payout_policy_snapshot != Tariff.PayoutPolicy.ON_CHECKIN:
            raise CommandError(
                f"personal component payout policy mismatch: {personal_component.trainer_payout_policy_snapshot}"
            )
        self._assert_money(
            "personal component paid basis",
            Decimal(expected["personal_component_paid_basis"]),
            personal_component.paid_amount_basis_snapshot,
        )
        if personal_component.credits_left != 2:
            raise CommandError(
                "personal component credits mismatch: "
                f"expected 2, got {personal_component.credits_left}"
            )

        calculate_salary(debt_checkin.id, club_id)
        personal_earning = TrainerEarning.objects.for_club(club_id).get(checkin_id=debt_checkin.id)
        self._assert_money(
            "personal salary amount",
            Decimal(expected["personal_salary_amount"]),
            personal_earning.amount,
        )
        self._assert_money(
            "personal salary basis",
            Decimal(expected["personal_salary_basis"]),
            personal_earning.subscription_price,
        )
        if personal_earning.trainer_id != personal_trainer_id:
            raise CommandError(
                f"personal earning trainer mismatch: expected {personal_trainer_id}, got {personal_earning.trainer_id}"
            )

        group_checkins = self._ensure_group_weekly_checkins(
            club_id=club_id,
            student_id=student_id,
            schedule_id=group_schedule_id,
            training_type_id=group_training_type_id,
        )
        group_component = SubscriptionComponent.objects.for_club(club_id).get(
            subscription_id=subscription_id,
            training_type_id=group_training_type_id,
        )
        self._assert_money(
            "group component paid basis",
            Decimal(expected["group_component_paid_basis"]),
            group_component.paid_amount_basis_snapshot,
        )
        if group_component.trainer_payout_policy_snapshot != Tariff.PayoutPolicy.ON_PAYMENT:
            raise CommandError(
                f"group component payout policy mismatch: {group_component.trainer_payout_policy_snapshot}"
            )
        if group_component.credits_used != 2:
            raise CommandError(f"group component usage mismatch: expected 2, got {group_component.credits_used}")
        if TrainerEarning.objects.for_club(club_id).filter(
            checkin_id__in=[checkin.id for checkin in group_checkins]
        ).exists():
            raise CommandError("group on-payment component created check-in salary")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "payment": {"id": payment.id, "status": payment.status},
            "sale_earning": {
                "id": sale.id,
                "amount": str(sale.amount),
                "basis": str(sale.subscription_price),
            },
            "debt": {
                "id": debt.id,
                "resolution_type": debt.resolution_type,
                "subscription_component_id": debt_checkin.subscription_component_id,
            },
            "personal_component": {
                "id": personal_component.id,
                "credits_left": personal_component.credits_left,
                "credits_used": personal_component.credits_used,
            },
            "personal_earning": {
                "id": personal_earning.id,
                "amount": str(personal_earning.amount),
                "basis": str(personal_earning.subscription_price),
            },
            "group_component": {
                "id": group_component.id,
                "credits_used": group_component.credits_used,
                "weekly_limit": group_component.weekly_limit,
            },
            "group_checkin_ids": [checkin.id for checkin in group_checkins],
        }

    def _ensure_group_weekly_checkins(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        training_type_id: int,
    ) -> list[Checkin]:
        existing = list(
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                subscription__isnull=False,
                cancelled_at__isnull=True,
            )
            .order_by("date", "id")
        )
        if len(existing) >= 2:
            return existing[:2]

        today = timezone.localdate()
        start_date = today - timedelta(days=today.weekday())
        for offset in range(len(existing), 2):
            result = create_checkin(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                source=Checkin.Source.MANUAL,
                checkin_date=start_date + timedelta(days=offset),
            )
            existing.append(Checkin.objects.for_club(club_id).get(id=result["checkin_id"]))

        try:
            create_checkin(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                source=Checkin.Source.MANUAL,
                checkin_date=start_date + timedelta(days=2),
            )
        except BusinessLogicError as exc:
            if exc.code != "subscription_component_limit_exceeded":
                raise CommandError(f"unexpected weekly limit error: {exc.code}") from exc
        else:
            raise CommandError("third group check-in was not blocked by weekly component limit")

        return existing

    def _assert_money(self, label: str, expected: Decimal, actual: Decimal) -> None:
        if actual != expected:
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")
