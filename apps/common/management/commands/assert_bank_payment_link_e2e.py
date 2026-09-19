from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment, TrainingGroupMembership
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Debt,
    DebtSettlementEvent,
    Payment,
)
from apps.billing.services import expire_bank_payment_orders, process_bank_payment_webhook


class Command(BaseCommand):
    help = "Assert bank payment link real-stack E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_bank_payment_link_e2e.")

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        evidence = self._collect_evidence(fixture)
        self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))

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
            "owner_create_student",
            "owner_manual_review",
            "finance_workspace",
            "trainer",
            "trainer_student",
            "target_group",
            "student",
            "parent",
            "approval",
            "provider_failure",
            "expiry",
            "tariff",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        tariff_id = int(fixture["tariff"]["id"])
        expected = fixture["expected"]
        browser_statuses = expected.get("browser_statuses")
        if not isinstance(browser_statuses, dict):
            raise CommandError("fixture expected.browser_statuses is missing")

        finance_workspace = fixture["finance_workspace"]
        finance_manual_payment = Payment.objects.for_club(club_id).get(
            id=int(finance_workspace["manual_payment_id"])
        )
        if finance_workspace.get("browser_confirms_manual_payment"):
            if (
                finance_manual_payment.status != Payment.Status.CONFIRMED
                or finance_manual_payment.payment_method != Payment.Method.CASH
                or finance_manual_payment.verified_by_id != int(fixture["owner"]["user_id"])
            ):
                raise CommandError("owner finance workspace did not confirm the exact manual payment")
        elif finance_manual_payment.status != Payment.Status.PENDING:
            raise CommandError("non-finance pack changed the manual payment fixture unexpectedly")
        finance_confirmed_online_payment = Payment.objects.for_club(club_id).get(
            id=int(finance_workspace["confirmed_online_payment_id"])
        )
        if (
            finance_confirmed_online_payment.status != Payment.Status.CONFIRMED
            or finance_confirmed_online_payment.payment_method != Payment.Method.ONLINE
        ):
            raise CommandError("confirmed online history fixture changed unexpectedly")
        confirmed_online_order = BankPaymentOrder.objects.for_club(club_id).get(
            payment=finance_confirmed_online_payment
        )
        confirmed_online_event = BankPaymentProviderEvent.objects.for_club(club_id).get(
            order=confirmed_online_order
        )
        if (
            confirmed_online_order.status != BankPaymentOrder.Status.APPROVED
            or confirmed_online_event.processing_status
            != BankPaymentProviderEvent.ProcessingStatus.PROCESSED
        ):
            raise CommandError("confirmed online history lacks provider confirmation evidence")

        manual_review_order = BankPaymentOrder.objects.for_club(club_id).get(
            id=int(fixture["owner_manual_review"]["order_id"])
        )
        manual_review_event = BankPaymentProviderEvent.objects.for_club(club_id).get(
            order=manual_review_order
        )
        if (
            manual_review_order.status != BankPaymentOrder.Status.MANUAL_REVIEW
            or manual_review_event.normalized_status_snapshot != "approved"
            or manual_review_event.processing_status
            != BankPaymentProviderEvent.ProcessingStatus.FAILED
        ):
            raise CommandError("manual review fixture lacks provider mismatch evidence")

        trainer_order = self._get_order(
            club_id=club_id,
            source=BankPaymentOrder.Source.TRAINER,
            student_id=int(fixture["trainer_student"]["id"]),
        )
        student_order = self._get_order(
            club_id=club_id,
            source=BankPaymentOrder.Source.STUDENT,
            student_id=int(fixture["student"]["student_id"]),
        )
        parent_order = self._get_order(
            club_id=club_id,
            source=BankPaymentOrder.Source.PARENT,
            student_id=int(fixture["parent"]["child_id"]),
        )
        owner_order = None
        if "owner" in browser_statuses:
            owner_order = self._get_order(
                club_id=club_id,
                source=BankPaymentOrder.Source.OWNER,
                student_id=int(fixture["owner_create_student"]["id"]),
            )

        orders = [trainer_order, student_order, parent_order]
        if owner_order is not None:
            orders.append(owner_order)
        for order in orders:
            self._assert_common_order(order=order, tariff_id=tariff_id, expected=expected)
        expected_browser_orders = {
            "trainer": trainer_order,
            "student": student_order,
            "parent": parent_order,
        }
        if owner_order is not None:
            expected_browser_orders["owner"] = owner_order
        allowed_browser_statuses = {
            BankPaymentOrder.Status.PENDING,
            BankPaymentOrder.Status.CANCELLED,
            BankPaymentOrder.Status.APPROVED,
        }
        for label, order in expected_browser_orders.items():
            expected_status = browser_statuses.get(label)
            if expected_status not in allowed_browser_statuses:
                raise CommandError(f"fixture browser status is invalid for {label}")
            if order.status != expected_status:
                raise CommandError(
                    f"{label} browser lifecycle mismatch: expected {expected_status}, got {order.status}"
                )

        target_group = fixture["target_group"]
        target_schedule_id = int(target_group["schedule_id"])
        target_start_date = target_group["start_date"]
        if trainer_order.payment.target_schedule_id != target_schedule_id:
            raise CommandError(
                "trainer payment target schedule mismatch: "
                f"expected {target_schedule_id}, got {trainer_order.payment.target_schedule_id}"
            )
        if trainer_order.payment.target_training_group_id != int(target_group["training_group_id"]):
            raise CommandError("trainer bank order did not persist canonical group identity")
        actual_target_start_date = trainer_order.payment.target_start_date
        if actual_target_start_date is None or actual_target_start_date.isoformat() != target_start_date:
            raise CommandError(
                "trainer payment target start date mismatch: "
                f"expected {target_start_date}, got {actual_target_start_date}"
            )
        if trainer_order.payment.target_group_name_snapshot != target_group["name"]:
            raise CommandError(
                "trainer payment target group snapshot mismatch: "
                f"expected {target_group['name']}, got {trainer_order.payment.target_group_name_snapshot}"
            )
        for order in [student_order, parent_order]:
            if order.payment.target_schedule_id is not None or order.payment.target_start_date is not None:
                raise CommandError(f"{order.source} self-service renewal must not target a permanent group")

        pending_enrollment_count = ScheduleEnrollment.objects.for_club(club_id).filter(
            student_id=int(fixture["trainer_student"]["id"]),
            schedule_id=target_schedule_id,
        ).count()
        if pending_enrollment_count:
            raise CommandError(
                "pending bank payment link must not create permanent group enrollment: "
                f"got {pending_enrollment_count}"
            )
        if TrainingGroupMembership.objects.for_club(club_id).filter(
            student_id=int(fixture["trainer_student"]["id"]),
            training_group_id=int(target_group["training_group_id"]),
        ).exists():
            raise CommandError("pending canonical bank payment link created a membership before approval")

        lifecycle_orders = {
            "approval": self._get_order_by_id(
                club_id=club_id,
                order_id=int(fixture["approval"]["order_id"]),
            ),
            "provider_failure": self._get_order_by_id(
                club_id=club_id,
                order_id=int(fixture["provider_failure"]["order_id"]),
            ),
            "expiry": self._get_order_by_id(
                club_id=club_id,
                order_id=int(fixture["expiry"]["order_id"]),
            ),
        }
        for label, order in lifecycle_orders.items():
            self._assert_pending_group_lifecycle_order(
                label=label,
                order=order,
                student_id=int(fixture[label]["student_id"]),
                training_group_id=int(target_group["training_group_id"]),
            )

        self._assert_ttl_minutes(
            order=trainer_order,
            expected_minutes=int(expected["staff_ttl_minutes"]),
            label="trainer",
        )
        self._assert_ttl_minutes(
            order=student_order,
            expected_minutes=int(expected["self_service_ttl_minutes"]),
            label="student",
        )
        self._assert_ttl_minutes(
            order=parent_order,
            expected_minutes=int(expected["self_service_ttl_minutes"]),
            label="parent",
        )
        if owner_order is not None:
            self._assert_ttl_minutes(
                order=owner_order,
                expected_minutes=int(expected["staff_ttl_minutes"]),
                label="owner",
            )

        trainer_debt = Debt.objects.for_club(club_id).get(id=int(fixture["trainer_student"]["debt_id"]))
        trainer_debt_event_types = list(
            DebtSettlementEvent.objects.for_club(club_id)
            .filter(debt=trainer_debt, payment=trainer_order.payment)
            # This debt/payment stream is serialized by its financial locks.
            # Append order survives a backwards adjustment of the wall clock.
            .order_by("id")
            .values_list("event_type", flat=True)
        )
        if trainer_order.status == BankPaymentOrder.Status.CANCELLED:
            if trainer_debt.settlement_payment_id is not None:
                raise CommandError("cancelled trainer order did not release the selected debt")
            if trainer_debt_event_types != [
                DebtSettlementEvent.EventType.RESERVED,
                DebtSettlementEvent.EventType.REJECTED,
            ]:
                raise CommandError(
                    "cancelled trainer order did not preserve reserve/reject debt evidence: "
                    f"got {trainer_debt_event_types}"
                )
        elif trainer_debt.settlement_payment_id != trainer_order.payment_id:
            raise CommandError(
                "trainer debt settlement payment mismatch: "
                f"expected {trainer_order.payment_id}, got {trainer_debt.settlement_payment_id}"
            )
        self_service_debt_count = Debt.objects.for_club(club_id).filter(
            settlement_payment_id__in=[student_order.payment_id, parent_order.payment_id]
        ).count()
        if self_service_debt_count:
            raise CommandError(f"self-service orders must not reserve debts, got {self_service_debt_count}")
        if trainer_order.payment_id == student_order.payment_id or parent_order.payment_id == student_order.payment_id:
            raise CommandError("bank payment orders must have distinct payments")

        lifecycle_evidence = self._exercise_group_lifecycle_orders(
            fixture=fixture,
            lifecycle_orders=lifecycle_orders,
            training_group_id=int(target_group["training_group_id"]),
        )

        order_evidence = {
            "trainer": self._order_evidence(trainer_order),
            "student": self._order_evidence(student_order),
            "parent": self._order_evidence(parent_order),
        }
        if owner_order is not None:
            order_evidence["owner"] = self._order_evidence(owner_order)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "orders": order_evidence,
            "trainer_debt": {
                "id": trainer_debt.id,
                "settlement_payment_id": trainer_debt.settlement_payment_id,
                "settlement_event_types": trainer_debt_event_types,
            },
            "pending_group_enrollment_count": pending_enrollment_count,
            "pending_group_membership_count": 0,
            "group_lifecycle": lifecycle_evidence,
        }

    def _get_order(self, *, club_id: int, source: str, student_id: int) -> BankPaymentOrder:
        orders = list(
            BankPaymentOrder.objects.for_club(club_id)
            .filter(source=source, student_id=student_id)
            .select_related("payment", "subscription")
            .order_by("created_at", "id")
        )
        if len(orders) != 1:
            raise CommandError(
                f"expected exactly one {source} bank payment order for student {student_id}, got {len(orders)}"
            )
        return orders[0]

    def _get_order_by_id(self, *, club_id: int, order_id: int) -> BankPaymentOrder:
        return (
            BankPaymentOrder.objects.for_club(club_id)
            .select_related("payment", "subscription")
            .get(id=order_id)
        )

    def _assert_pending_group_lifecycle_order(
        self,
        *,
        label: str,
        order: BankPaymentOrder,
        student_id: int,
        training_group_id: int,
    ) -> None:
        if order.status != BankPaymentOrder.Status.PENDING or order.payment.status != Payment.Status.PENDING:
            raise CommandError(f"{label} group order must be pending before provider processing")
        if order.student_id != student_id or order.payment.target_training_group_id != training_group_id:
            raise CommandError(f"{label} group order target mismatch")
        membership_count = TrainingGroupMembership.objects.for_club(order.club_id).filter(
            student_id=student_id,
            training_group_id=training_group_id,
        ).count()
        if membership_count:
            raise CommandError(f"{label} group order created membership before approval")

    def _exercise_group_lifecycle_orders(
        self,
        *,
        fixture: dict,
        lifecycle_orders: dict[str, BankPaymentOrder],
        training_group_id: int,
    ) -> dict:
        now = timezone.now()
        approval_order = lifecycle_orders["approval"]
        failure_order = lifecycle_orders["provider_failure"]
        expiry_order = lifecycle_orders["expiry"]

        with (
            patch(
                "apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"
            ),
            patch("django_q.tasks.async_task"),
        ):
            approval_event = self._send_provider_event(
                order=approval_order,
                status="APPROVED",
                event_id=f"{fixture['fixture_id']}-group-approved",
                operation_id=f"{fixture['fixture_id']}-group-approved-operation",
                paid_at=now,
            )
            approval_retry_event = self._send_provider_event(
                order=approval_order,
                status="APPROVED",
                event_id=f"{fixture['fixture_id']}-group-approved",
                operation_id=f"{fixture['fixture_id']}-group-approved-operation",
                paid_at=now,
            )
            failure_event = self._send_provider_event(
                order=failure_order,
                status="FAILED",
                event_id=f"{fixture['fixture_id']}-group-failed",
                operation_id=f"{fixture['fixture_id']}-group-failed-operation",
            )
            expiry_order.expires_at = now - timedelta(minutes=1)
            expiry_order.save(update_fields=["expires_at", "updated_at"])
            expire_bank_payment_orders(now=now)

        if approval_retry_event.id != approval_event.id:
            raise CommandError("provider approval retry did not deduplicate the original event")
        approval_order.refresh_from_db()
        approval_order.payment.refresh_from_db()
        failure_order.refresh_from_db()
        failure_order.payment.refresh_from_db()
        expiry_order.refresh_from_db()
        expiry_order.payment.refresh_from_db()

        approval_memberships = list(
            TrainingGroupMembership.objects.for_club(approval_order.club_id)
            .filter(
                student_id=approval_order.student_id,
                training_group_id=training_group_id,
            )
            .order_by("id")
        )
        if len(approval_memberships) != 1:
            raise CommandError(
                f"provider approval retry must create exactly one group membership, got {len(approval_memberships)}"
            )
        membership = approval_memberships[0]
        if (
            membership.status != TrainingGroupMembership.Status.ACTIVE
            or membership.source != TrainingGroupMembership.Source.PAID_CONVERSION
            or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
            or approval_order.payment.conversion_group_membership_id != membership.id
        ):
            raise CommandError("provider approval did not establish the expected payment-owned membership")
        projection_schedule_ids = set(
            ScheduleEnrollment.objects.for_club(approval_order.club_id)
            .filter(training_group_membership_id=membership.id)
            .values_list("schedule_id", flat=True)
        )
        if len(projection_schedule_ids) != 2:
            raise CommandError("provider approval did not project every canonical group schedule")
        for label, order in {"provider_failure": failure_order, "expiry": expiry_order}.items():
            if order.status not in {BankPaymentOrder.Status.FAILED, BankPaymentOrder.Status.EXPIRED}:
                raise CommandError(f"{label} order did not close after its terminal lifecycle")
            if order.payment.status != Payment.Status.REJECTED:
                raise CommandError(f"{label} payment did not reject after its terminal lifecycle")
            if TrainingGroupMembership.objects.for_club(order.club_id).filter(
                student_id=order.student_id,
                training_group_id=training_group_id,
            ).exists():
                raise CommandError(f"{label} group order created a membership")

        return {
            "approval": {
                "order_id": approval_order.id,
                "provider_event_id": approval_event.id,
                "membership_id": membership.id,
                "membership_count": len(approval_memberships),
                "projection_schedule_ids": sorted(projection_schedule_ids),
            },
            "provider_failure": {
                "order_id": failure_order.id,
                "provider_event_id": failure_event.id,
                "status": failure_order.status,
                "membership_count": 0,
            },
            "expiry": {
                "order_id": expiry_order.id,
                "status": expiry_order.status,
                "membership_count": 0,
            },
        }

    def _send_provider_event(
        self,
        *,
        order: BankPaymentOrder,
        status: str,
        event_id: str,
        operation_id: str,
        paid_at=None,
    ):
        payload = {
            "webhookType": "acquiringInternetPayment",
            "event_id": event_id,
            "status": status,
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": operation_id,
            "amount": str(order.amount_snapshot),
        }
        if paid_at is not None:
            payload["paid_at"] = paid_at.isoformat()
        return process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(payload).encode(),
            headers={},
            request_id=f"e2e-{event_id}",
        )

    def _assert_common_order(self, *, order: BankPaymentOrder, tariff_id: int, expected: dict) -> None:
        if order.provider != expected["provider"]:
            raise CommandError(
                f"provider mismatch for {order.source}: "
                f"expected {expected['provider']}, got {order.provider}"
            )
        if order.status not in {
            expected["status"],
            BankPaymentOrder.Status.CANCELLED,
            BankPaymentOrder.Status.APPROVED,
        }:
            raise CommandError(
                f"status mismatch for {order.source}: expected pending, cancelled, or approved, got {order.status}"
            )
        if order.receipt_mode != expected["receipt_mode"]:
            raise CommandError(
                f"receipt_mode mismatch for {order.source}: "
                f"expected {expected['receipt_mode']}, got {order.receipt_mode}"
            )
        if not order.provider_payment_url:
            raise CommandError(f"provider payment url is empty for {order.source}")
        if not order.provider_payment_link_id:
            raise CommandError(f"provider payment link id is empty for {order.source}")
        if order.payment.payment_method != Payment.Method.ONLINE:
            raise CommandError(f"payment method mismatch for {order.source}: got {order.payment.payment_method}")
        expected_payment_status = {
            BankPaymentOrder.Status.CANCELLED: Payment.Status.REJECTED,
            BankPaymentOrder.Status.APPROVED: Payment.Status.CONFIRMED,
        }.get(order.status, Payment.Status.PENDING)
        if order.payment.status != expected_payment_status:
            raise CommandError(f"payment status mismatch for {order.source}: got {order.payment.status}")
        if (
            order.status == BankPaymentOrder.Status.APPROVED
            and order.subscription.status != "active"
        ):
            raise CommandError(
                f"approved subscription status mismatch for {order.source}: got {order.subscription.status}"
            )
        if order.payment.tariff_id != tariff_id:
            raise CommandError(f"payment tariff mismatch for {order.source}: got {order.payment.tariff_id}")
        if order.subscription.tariff_id != tariff_id:
            raise CommandError(f"subscription tariff mismatch for {order.source}: got {order.subscription.tariff_id}")

    def _assert_ttl_minutes(self, *, order: BankPaymentOrder, expected_minutes: int, label: str) -> None:
        actual_minutes = round((order.expires_at - order.created_at).total_seconds() / 60)
        if actual_minutes != expected_minutes:
            raise CommandError(
                f"{label} ttl mismatch: expected {expected_minutes} minutes, got {actual_minutes}"
            )

    def _order_evidence(self, order: BankPaymentOrder) -> dict:
        return {
            "id": order.id,
            "payment_id": order.payment_id,
            "subscription_id": order.subscription_id,
            "student_id": order.student_id,
            "source": order.source,
            "status": order.status,
            "provider": order.provider,
            "receipt_mode": order.receipt_mode,
            "ttl_minutes": round((order.expires_at - order.created_at).total_seconds() / 60),
            "target_schedule_id": order.payment.target_schedule_id,
            "target_start_date": (
                order.payment.target_start_date.isoformat()
                if order.payment.target_start_date is not None
                else None
            ),
        }
