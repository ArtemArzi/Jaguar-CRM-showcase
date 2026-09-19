from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.billing.models import Debt, DebtLifecycleEvent, DebtSettlementEvent, DebtWriteOffEvent, Payment
from apps.trainers.models import TrainerPayrollPeriodClose


class Command(BaseCommand):
    help = "Assert owner/admin debt write-off E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_debt_writeoff_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for write-off side effects before failing.",
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
                    raise CommandError(f"debt write-off E2E assertion failed: {exc}") from exc
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
            "target_checkin_id",
            "target_debt_id",
            "reserved_debt_id",
            "reserved_payment_id",
            "closed_checkin_id",
            "closed_debt_id",
            "payroll_close_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        target_checkin_id = int(fixture["target_checkin_id"])
        target_debt_id = int(fixture["target_debt_id"])
        reserved_debt_id = int(fixture["reserved_debt_id"])
        reserved_payment_id = int(fixture["reserved_payment_id"])
        closed_checkin_id = int(fixture["closed_checkin_id"])
        closed_debt_id = int(fixture["closed_debt_id"])
        payroll_close_id = int(fixture["payroll_close_id"])
        expected = fixture["expected"]

        target_debt = Debt.objects.for_club(club_id).get(id=target_debt_id)
        target_checkin = Checkin.objects.for_club(club_id).get(id=target_checkin_id)
        self._assert_target_written_off(
            debt=target_debt,
            checkin=target_checkin,
            expected=expected,
        )
        writeoff_event = DebtWriteOffEvent.objects.for_club(club_id).filter(debt=target_debt).first()
        if writeoff_event is None:
            raise CommandError("target debt write-off event not found")
        self._assert_writeoff_event(
            event=writeoff_event,
            debt=target_debt,
            expected=expected,
            owner_user_id=owner_user_id,
        )
        target_lifecycle_events = list(
            DebtLifecycleEvent.objects.for_club(club_id)
            .filter(debt=target_debt)
            .order_by("created_at", "id")
        )
        self._assert_target_lifecycle(
            events=target_lifecycle_events,
            debt=target_debt,
            expected=expected,
            owner_user_id=owner_user_id,
        )

        reserved_debt = Debt.objects.for_club(club_id).get(id=reserved_debt_id)
        reserved_payment = Payment.objects.for_club(club_id).get(id=reserved_payment_id)
        self._assert_reserved_debt(
            debt=reserved_debt,
            payment=reserved_payment,
            expected=expected,
        )
        reserved_lifecycle_events = list(
            DebtLifecycleEvent.objects.for_club(club_id)
            .filter(debt=reserved_debt)
            .order_by("created_at", "id")
        )
        self._assert_reserved_lifecycle(events=reserved_lifecycle_events, payment=reserved_payment)
        reserved_settlement_events = list(
            DebtSettlementEvent.objects.for_club(club_id)
            .filter(debt=reserved_debt, payment=reserved_payment)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        if reserved_settlement_events != [DebtSettlementEvent.EventType.RESERVED]:
            raise CommandError(
                "reserved debt settlement events mismatch: "
                f"expected {[DebtSettlementEvent.EventType.RESERVED]}, got {reserved_settlement_events}"
            )
        reserved_writeoff_event_count = DebtWriteOffEvent.objects.for_club(club_id).filter(debt=reserved_debt).count()
        if reserved_writeoff_event_count:
            raise CommandError(
                f"reserved debt unexpectedly has {reserved_writeoff_event_count} write-off event(s)"
            )

        closed_debt = Debt.objects.for_club(club_id).get(id=closed_debt_id)
        closed_checkin = Checkin.objects.for_club(club_id).get(id=closed_checkin_id)
        payroll_close = TrainerPayrollPeriodClose.objects.for_club(club_id).get(id=payroll_close_id)
        self._assert_closed_period_debt(
            debt=closed_debt,
            checkin=closed_checkin,
            payroll_close=payroll_close,
            expected=expected,
        )
        closed_lifecycle_events = list(
            DebtLifecycleEvent.objects.for_club(club_id)
            .filter(debt=closed_debt)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        if DebtLifecycleEvent.EventType.WRITTEN_OFF in closed_lifecycle_events:
            raise CommandError("closed-period debt unexpectedly has a write-off lifecycle event")
        closed_writeoff_event_count = DebtWriteOffEvent.objects.for_club(club_id).filter(debt=closed_debt).count()
        if closed_writeoff_event_count:
            raise CommandError(
                f"closed-period debt unexpectedly has {closed_writeoff_event_count} write-off event(s)"
            )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "target_debt": {
                "id": target_debt.id,
                "resolved": target_debt.resolved_at is not None,
                "resolution_type": target_debt.resolution_type,
                "settlement_payment_id": target_debt.settlement_payment_id,
            },
            "target_checkin": {
                "id": target_checkin.id,
                "subscription_id": target_checkin.subscription_id,
                "is_debt": target_checkin.is_debt,
            },
            "target_writeoff_event": {
                "id": writeoff_event.id,
                "reason": writeoff_event.reason,
                "written_off_by_id": writeoff_event.written_off_by_id,
                "amount_snapshot": str(writeoff_event.amount_snapshot),
            },
            "target_lifecycle_events": [event.event_type for event in target_lifecycle_events],
            "reserved_debt": {
                "id": reserved_debt.id,
                "resolved": reserved_debt.resolved_at is not None,
                "resolution_type": reserved_debt.resolution_type,
                "settlement_payment_id": reserved_debt.settlement_payment_id,
            },
            "reserved_payment": {
                "id": reserved_payment.id,
                "status": reserved_payment.status,
            },
            "reserved_lifecycle_events": [event.event_type for event in reserved_lifecycle_events],
            "reserved_settlement_events": reserved_settlement_events,
            "reserved_writeoff_event_count": reserved_writeoff_event_count,
            "closed_period_debt": {
                "id": closed_debt.id,
                "resolved": closed_debt.resolved_at is not None,
                "resolution_type": closed_debt.resolution_type,
                "writeoff_event_count": closed_writeoff_event_count,
                "lifecycle_events": closed_lifecycle_events,
                "payroll_close_id": payroll_close.id,
            },
        }

    def _assert_target_written_off(self, *, debt: Debt, checkin: Checkin, expected: dict) -> None:
        if debt.resolved_at is None:
            raise CommandError("target debt was not written off")
        if debt.resolution_type != "writeoff":
            raise CommandError(f"target debt resolution_type mismatch: expected writeoff, got {debt.resolution_type}")
        if debt.settlement_payment_id is not None:
            raise CommandError(f"target debt unexpectedly has settlement_payment={debt.settlement_payment_id}")
        if debt.tariff_price != Decimal(expected["target_amount"]):
            raise CommandError(
                f"target debt amount mismatch: expected {expected['target_amount']}, got {debt.tariff_price}"
            )
        if checkin.subscription_id is not None:
            raise CommandError(f"target check-in unexpectedly linked to subscription {checkin.subscription_id}")
        if not checkin.is_debt:
            raise CommandError("target check-in is no longer marked as debt")

    def _assert_writeoff_event(
        self,
        *,
        event: DebtWriteOffEvent,
        debt: Debt,
        expected: dict,
        owner_user_id: int,
    ) -> None:
        expected_reason = expected["writeoff_reason"]
        if event.written_off_by_id != owner_user_id:
            raise CommandError(
                f"write-off actor mismatch: expected {owner_user_id}, got {event.written_off_by_id}"
            )
        if event.reason != expected_reason:
            raise CommandError(f"write-off reason mismatch: expected {expected_reason!r}, got {event.reason!r}")
        if event.decided_at != debt.resolved_at:
            raise CommandError("write-off decided_at does not match debt resolved_at")
        if event.amount_snapshot != Decimal(expected["target_amount"]):
            raise CommandError(
                f"write-off amount snapshot mismatch: expected {expected['target_amount']}, got {event.amount_snapshot}"
            )
        if event.debt_id_snapshot != debt.id:
            raise CommandError("write-off debt snapshot mismatch")
        if event.student_id_snapshot != debt.student_id:
            raise CommandError("write-off student snapshot mismatch")
        if event.checkin_id_snapshot != debt.checkin_id:
            raise CommandError("write-off check-in snapshot mismatch")
        if event.debt_reason_snapshot != debt.reason:
            raise CommandError("write-off debt reason snapshot mismatch")

    def _assert_target_lifecycle(
        self,
        *,
        events: list[DebtLifecycleEvent],
        debt: Debt,
        expected: dict,
        owner_user_id: int,
    ) -> None:
        if [event.event_type for event in events] != [DebtLifecycleEvent.EventType.WRITTEN_OFF]:
            raise CommandError(
                "target debt lifecycle events mismatch: "
                f"expected {[DebtLifecycleEvent.EventType.WRITTEN_OFF]}, got {[event.event_type for event in events]}"
            )
        event = events[0]
        if event.previous_state != "open" or event.new_state != "resolved:writeoff":
            raise CommandError(
                "target debt lifecycle state mismatch: "
                f"expected open->resolved:writeoff, got {event.previous_state}->{event.new_state}"
            )
        if event.actor_id != owner_user_id:
            raise CommandError(f"target debt lifecycle actor mismatch: expected {owner_user_id}, got {event.actor_id}")
        if event.reason != expected["writeoff_reason"]:
            raise CommandError(
                f"target debt lifecycle reason mismatch: expected {expected['writeoff_reason']!r}, got {event.reason!r}"
            )
        if event.debt_id_snapshot != debt.id or event.checkin_id_snapshot != debt.checkin_id:
            raise CommandError("target debt lifecycle immutable snapshots do not match debt/check-in")

    def _assert_reserved_debt(self, *, debt: Debt, payment: Payment, expected: dict) -> None:
        if debt.resolved_at is not None:
            raise CommandError("reserved debt was unexpectedly resolved")
        if debt.resolution_type:
            raise CommandError(f"reserved debt resolution_type mismatch: expected blank, got {debt.resolution_type}")
        if debt.settlement_payment_id != payment.id:
            raise CommandError(
                f"reserved debt settlement_payment mismatch: expected {payment.id}, got {debt.settlement_payment_id}"
            )
        if debt.tariff_price != Decimal(expected["reserved_amount"]):
            raise CommandError(
                f"reserved debt amount mismatch: expected {expected['reserved_amount']}, got {debt.tariff_price}"
            )
        if payment.status != Payment.Status.PENDING:
            raise CommandError(f"reserved payment status mismatch: expected pending, got {payment.status}")

    def _assert_reserved_lifecycle(self, *, events: list[DebtLifecycleEvent], payment: Payment) -> None:
        if [event.event_type for event in events] != [DebtLifecycleEvent.EventType.RESERVED]:
            raise CommandError(
                "reserved debt lifecycle events mismatch: "
                f"expected {[DebtLifecycleEvent.EventType.RESERVED]}, got {[event.event_type for event in events]}"
            )
        event = events[0]
        if event.previous_state != "open" or event.new_state != "reserved":
            raise CommandError(
                "reserved debt lifecycle state mismatch: "
                f"expected open->reserved, got {event.previous_state}->{event.new_state}"
            )
        if event.payment_id != payment.id:
            raise CommandError(
                f"reserved debt lifecycle payment mismatch: expected {payment.id}, got {event.payment_id}"
            )

    def _assert_closed_period_debt(
        self,
        *,
        debt: Debt,
        checkin: Checkin,
        payroll_close: TrainerPayrollPeriodClose,
        expected: dict,
    ) -> None:
        if debt.resolved_at is not None:
            raise CommandError("closed-period debt was unexpectedly resolved")
        if debt.resolution_type:
            raise CommandError(
                f"closed-period debt resolution_type mismatch: expected blank, got {debt.resolution_type}"
            )
        if debt.settlement_payment_id is not None:
            raise CommandError(
                f"closed-period debt unexpectedly has settlement_payment={debt.settlement_payment_id}"
            )
        if debt.tariff_price != Decimal(expected["closed_amount"]):
            raise CommandError(
                f"closed-period debt amount mismatch: expected {expected['closed_amount']}, got {debt.tariff_price}"
            )
        if not (payroll_close.period_start <= checkin.date <= payroll_close.period_end):
            raise CommandError(
                "payroll close does not cover closed-period debt check-in date: "
                f"{payroll_close.period_start}..{payroll_close.period_end} vs {checkin.date}"
            )
