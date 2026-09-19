from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    ScheduleEnrollment,
)
from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent, Debt, Payment, Subscription
from apps.billing.services import process_bank_payment_webhook
from apps.retention.models import RetentionTask

LIVE_ORDER_STATUSES = {
    BankPaymentOrder.Status.CREATED,
    BankPaymentOrder.Status.PENDING,
    BankPaymentOrder.Status.AUTHORIZED,
}


class Command(BaseCommand):
    help = "Assert PWA UX ledger real-stack E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")
        parser.add_argument(
            "--stage",
            default="all",
            choices=[
                "renewal_reused",
                "renewal_cancelled",
                "renewal_late_approval_manual_review",
                "trainer_direct_order_pending",
                "trainer_direct_order_cancelled",
                "personal_pending",
                "personal_cancelled",
                "personal_booked",
                "existing_package_booked",
                "trainer_personal_pending",
                "trainer_roster_readonly",
                "empty_states",
                "all",
            ],
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        stage = options["stage"]
        control_evidence = self._assert_control_decoys(fixture)
        if stage == "all":
            evidence = {
                "ok": True,
                "fixture_id": fixture["fixture_id"],
                "stage": stage,
                "control": control_evidence,
                "checks": [
                    self._assert_renewal_cancelled(fixture),
                    self._assert_trainer_direct_order_cancelled(fixture),
                    self._assert_existing_package_booked(fixture),
                    self._assert_trainer_roster_readonly(fixture),
                    self._assert_empty_states(fixture),
                ],
            }
        else:
            method = getattr(self, f"_assert_{stage}")
            evidence = method(fixture)
            evidence["control"] = control_evidence

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
            "control_club_id",
            "student",
            "package_student",
            "parent",
            "renewal",
            "trainer_recovery",
            "tariffs",
            "trainer_session",
            "slots",
            "baseline",
            "control",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _assert_control_decoys(self, fixture: dict) -> dict:
        control_club_id = int(fixture["control_club_id"])
        control = fixture["control"]
        order = (
            BankPaymentOrder.objects.for_club(control_club_id)
            .select_related("payment", "subscription")
            .filter(id=int(control["order_id"]))
            .first()
        )
        if order is None:
            raise CommandError("control bank payment order decoy is missing")
        if order.status != control.get("order_status"):
            raise CommandError(f"control bank payment order changed: got {order.status}")
        if order.payment_id != int(control["order_payment_id"]):
            raise CommandError("control bank payment order payment link changed")
        if order.subscription_id != int(control["order_subscription_id"]):
            raise CommandError("control bank payment order subscription link changed")
        if order.payment.status != control.get("payment_status"):
            raise CommandError(f"control payment changed: got {order.payment.status}")
        if order.subscription.status != control.get("subscription_status"):
            raise CommandError(f"control subscription changed: got {order.subscription.status}")

        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(control_club_id)
            .select_related("availability_slot")
            .filter(id=int(control["reservation_id"]))
            .first()
        )
        if reservation is None:
            raise CommandError("control personal reservation decoy is missing")
        if reservation.status != control.get("reservation_status"):
            raise CommandError(f"control personal reservation changed: got {reservation.status}")
        if reservation.bank_payment_order_id != int(control["reservation_order_id"]):
            raise CommandError("control personal reservation bank order link changed")
        if reservation.payment_id != int(control["reservation_payment_id"]):
            raise CommandError("control personal reservation payment link changed")
        if reservation.subscription_id != int(control["reservation_subscription_id"]):
            raise CommandError("control personal reservation subscription link changed")
        if reservation.availability_slot_id != int(control["slot_id"]):
            raise CommandError("control personal reservation slot link changed")
        if reservation.availability_slot.status != control.get("slot_status"):
            raise CommandError(f"control slot changed: got {reservation.availability_slot.status}")

        checkin = Checkin.objects.for_club(control_club_id).filter(id=int(control["checkin_id"])).first()
        if checkin is None:
            raise CommandError("control checkin decoy is missing")
        if (checkin.cancelled_at is not None) != bool(control["checkin_cancelled"]):
            raise CommandError("control checkin cancellation changed")
        debt = Debt.objects.for_club(control_club_id).filter(id=int(control["debt_id"])).first()
        if debt is None:
            raise CommandError("control debt decoy is missing")
        if str(debt.tariff_price) != control.get("debt_tariff_price"):
            raise CommandError(f"control debt amount changed: got {debt.tariff_price}")
        if debt.reason != control.get("debt_reason"):
            raise CommandError(f"control debt reason changed: got {debt.reason}")
        if debt.settlement_payment_id != control.get("debt_settlement_payment_id"):
            raise CommandError("control debt settlement payment changed")
        if (debt.resolved_at is not None) != bool(control["debt_resolved"]):
            raise CommandError("control debt resolution changed")
        if debt.resolution_type != control.get("debt_resolution_type"):
            raise CommandError(f"control debt resolution type changed: got {debt.resolution_type}")
        task = RetentionTask.objects.for_club(control_club_id).filter(id=int(control["task_id"])).first()
        if task is None:
            raise CommandError("control task decoy is missing")
        if task.status != control.get("task_status"):
            raise CommandError(f"control task changed: got {task.status}")
        if (task.resolved_at is not None) != bool(control["task_resolved"]):
            raise CommandError("control task resolution changed")

        return {
            "club_id": control_club_id,
            "order_status": order.status,
            "reservation_status": reservation.status,
            "checkin_cancelled": checkin.cancelled_at is not None,
            "task_status": task.status,
        }

    def _student_renewal_orders(self, fixture: dict) -> list[BankPaymentOrder]:
        return list(
            BankPaymentOrder.objects.for_club(int(fixture["club_id"]))
            .filter(
                source=BankPaymentOrder.Source.STUDENT,
                student_id=int(fixture["student"]["student_id"]),
                payment__tariff_id=int(fixture["renewal"]["tariff_id"]),
            )
            .select_related("payment", "subscription", "renewed_from_subscription")
            .order_by("created_at", "id")
        )

    def _trainer_recovery_order(self, fixture: dict) -> BankPaymentOrder:
        order = (
            BankPaymentOrder.objects.for_club(int(fixture["club_id"]))
            .select_related("payment", "subscription")
            .filter(
                id=int(fixture["trainer_recovery"]["order_id"]),
                source=BankPaymentOrder.Source.TRAINER,
                student_id=int(fixture["student"]["student_id"]),
            )
            .first()
        )
        if order is None:
            raise CommandError("trainer recovery order is missing")
        if order.subscription_id != int(fixture["trainer_recovery"]["subscription_id"]):
            raise CommandError("trainer recovery order subscription link changed")
        return order

    def _assert_trainer_direct_order_pending(self, fixture: dict) -> dict:
        order = self._trainer_recovery_order(fixture)
        if order.status != BankPaymentOrder.Status.PENDING:
            raise CommandError(f"trainer recovery order should be pending, got {order.status}")
        if order.payment.status != Payment.Status.PENDING:
            raise CommandError("trainer recovery payment should be pending")
        if order.subscription.status != Subscription.Status.PENDING or order.subscription.deleted_at is not None:
            raise CommandError("trainer recovery subscription should be live pending")
        return {
            "ok": True,
            "stage": "trainer_direct_order_pending",
            "order_id": order.id,
            "subscription_id": order.subscription_id,
        }

    def _assert_trainer_direct_order_cancelled(self, fixture: dict) -> dict:
        order = self._trainer_recovery_order(fixture)
        if order.status != BankPaymentOrder.Status.CANCELLED:
            raise CommandError(f"trainer recovery order should be cancelled, got {order.status}")
        if order.payment.status != Payment.Status.REJECTED:
            raise CommandError("trainer recovery payment should be rejected")
        if order.subscription.deleted_at is None:
            raise CommandError("trainer recovery subscription was not soft-deleted")
        return {
            "ok": True,
            "stage": "trainer_direct_order_cancelled",
            "order_id": order.id,
        }

    def _assert_renewal_reused(self, fixture: dict) -> dict:
        orders = self._student_renewal_orders(fixture)
        if len(orders) != 1:
            raise CommandError(f"expected one renewal order, got {len(orders)}")
        order = orders[0]
        if order.status != BankPaymentOrder.Status.PENDING:
            raise CommandError(f"expected pending renewal order, got {order.status}")
        if order.payment.status != Payment.Status.PENDING:
            raise CommandError(f"expected pending renewal payment, got {order.payment.status}")
        if order.subscription.status != Subscription.Status.PENDING or order.subscription.deleted_at is not None:
            raise CommandError("renewal pending subscription is not live pending")
        baseline = Subscription.objects.for_club(int(fixture["club_id"])).get(
            id=int(fixture["renewal"]["subscription_id"])
        )
        if baseline.status != Subscription.Status.EXPIRED or baseline.deleted_at is not None:
            raise CommandError("renewal source subscription changed unexpectedly")
        renewal_pending_count = (
            Subscription.objects.for_club(int(fixture["club_id"]))
            .filter(
                id=order.subscription_id,
                status=Subscription.Status.PENDING,
                deleted_at__isnull=True,
            )
            .count()
        )
        if renewal_pending_count != 1:
            raise CommandError(
                f"expected one live pending renewal subscription, got {renewal_pending_count}"
            )
        return {
            "ok": True,
            "stage": "renewal_reused",
            "order_id": order.id,
            "payment_id": order.payment_id,
            "subscription_id": order.subscription_id,
        }

    def _assert_renewal_cancelled(self, fixture: dict) -> dict:
        orders = self._student_renewal_orders(fixture)
        if len(orders) != 1:
            raise CommandError(f"expected one renewal order, got {len(orders)}")
        order = orders[0]
        if order.status != BankPaymentOrder.Status.CANCELLED:
            raise CommandError(f"expected cancelled renewal order, got {order.status}")
        if order.payment.status != Payment.Status.REJECTED:
            raise CommandError(f"expected rejected renewal payment, got {order.payment.status}")
        if order.subscription.deleted_at is None:
            raise CommandError("cancelled renewal subscription was not soft-deleted")
        active_count = (
            Subscription.objects.for_club(int(fixture["club_id"]))
            .filter(
                student_id=int(fixture["student"]["student_id"]),
                tariff_id=int(fixture["renewal"]["tariff_id"]),
                status=Subscription.Status.ACTIVE,
                deleted_at__isnull=True,
            )
            .count()
        )
        if active_count:
            raise CommandError(f"cancelled renewal created active subscriptions: {active_count}")
        return {"ok": True, "stage": "renewal_cancelled", "order_id": order.id}

    def _assert_renewal_late_approval_manual_review(self, fixture: dict) -> dict:
        orders = self._student_renewal_orders(fixture)
        if len(orders) != 1:
            raise CommandError(f"expected one renewal order, got {len(orders)}")
        order = orders[0]
        if order.status == BankPaymentOrder.Status.CANCELLED:
            self._process_mock_approved(order=order, request_id=f"pwa-ux-renewal-late-{order.id}")
            order.refresh_from_db()
            order.payment.refresh_from_db()
            order.subscription.refresh_from_db()
        if order.status != BankPaymentOrder.Status.MANUAL_REVIEW:
            raise CommandError(f"late approved renewal should be manual_review, got {order.status}")
        if order.payment.status != Payment.Status.REJECTED:
            raise CommandError(f"late approved renewal payment changed to {order.payment.status}")
        if order.subscription.deleted_at is None:
            raise CommandError("late approved renewal resurrected the pending subscription")
        return {"ok": True, "stage": "renewal_late_approval_manual_review", "order_id": order.id}

    def _student_personal_reservations(self, fixture: dict) -> list[PersonalBookingPaymentReservation]:
        return list(
            PersonalBookingPaymentReservation.objects.for_club(int(fixture["club_id"]))
            .filter(
                student_id=int(fixture["student"]["student_id"]),
                tariff_id=int(fixture["tariffs"]["personal_id"]),
            )
            .select_related("bank_payment_order", "payment", "subscription", "availability_slot")
            .order_by("created_at", "id")
        )

    def _assert_personal_pending(self, fixture: dict) -> dict:
        reservations = self._student_personal_reservations(fixture)
        live = [
            reservation
            for reservation in reservations
            if reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
            and reservation.expires_at > timezone.now()
            and reservation.availability_slot_id == int(fixture["slots"]["student_primary"]["id"])
        ]
        if len(live) != 1:
            raise CommandError(f"expected one live student personal reservation, got {len(live)}")
        reservation = live[0]
        if reservation.availability_slot_id != int(fixture["slots"]["student_primary"]["id"]):
            raise CommandError("student personal reservation is not tied to primary slot")
        if not reservation.bank_payment_order_id or not reservation.bank_payment_order.provider_payment_url:
            raise CommandError("student personal reservation has no payable bank order")
        if PersonalAvailabilitySlot.objects.for_club(int(fixture["club_id"])).get(
            id=reservation.availability_slot_id
        ).status != PersonalAvailabilitySlot.Status.HELD:
            raise CommandError("student personal slot was not held")
        return {
            "ok": True,
            "stage": "personal_pending",
            "reservation_id": reservation.id,
            "order_id": reservation.bank_payment_order_id,
        }

    def _assert_personal_cancelled(self, fixture: dict) -> dict:
        reservations = [
            reservation
            for reservation in self._student_personal_reservations(fixture)
            if reservation.availability_slot_id == int(fixture["slots"]["student_primary"]["id"])
        ]
        if len(reservations) != 1:
            raise CommandError(
                f"expected one student primary personal reservation after cancel, got {len(reservations)}"
            )
        reservation = reservations[0]
        if reservation.status != PersonalBookingPaymentReservation.Status.CANCELLED:
            raise CommandError(f"expected cancelled student personal reservation, got {reservation.status}")
        if reservation.bank_payment_order.status != BankPaymentOrder.Status.CANCELLED:
            raise CommandError(f"expected cancelled personal bank order, got {reservation.bank_payment_order.status}")
        if reservation.payment.status != Payment.Status.REJECTED:
            raise CommandError(f"expected rejected personal payment, got {reservation.payment.status}")
        slot_status = PersonalAvailabilitySlot.objects.for_club(int(fixture["club_id"])).get(
            id=int(fixture["slots"]["student_primary"]["id"])
        ).status
        if slot_status != PersonalAvailabilitySlot.Status.PUBLISHED:
            raise CommandError(f"cancelled personal slot was not released, got {slot_status}")
        return {"ok": True, "stage": "personal_cancelled", "reservation_id": reservation.id}

    def _assert_personal_booked(self, fixture: dict) -> dict:
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(int(fixture["club_id"]))
            .filter(
                student_id=int(fixture["parent"]["child_id"]),
                availability_slot_id=int(fixture["slots"]["parent_primary"]["id"]),
            )
            .select_related("bank_payment_order", "payment", "subscription", "schedule", "enrollment")
            .first()
        )
        if reservation is None:
            raise CommandError("parent personal payment reservation was not created")
        if reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT:
            self._process_mock_approved(
                order=reservation.bank_payment_order,
                request_id=f"pwa-ux-parent-personal-approved-{reservation.id}",
            )
            reservation.refresh_from_db()
            reservation.bank_payment_order.refresh_from_db()
            reservation.payment.refresh_from_db()
            reservation.subscription.refresh_from_db()
        if reservation.status != PersonalBookingPaymentReservation.Status.BOOKED:
            raise CommandError(f"expected booked parent personal reservation, got {reservation.status}")
        if reservation.bank_payment_order.status != BankPaymentOrder.Status.APPROVED:
            raise CommandError(f"expected approved parent personal order, got {reservation.bank_payment_order.status}")
        if reservation.payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"expected confirmed parent personal payment, got {reservation.payment.status}")
        if not reservation.schedule_id or not reservation.enrollment_id:
            raise CommandError("booked parent personal reservation lacks schedule/enrollment")
        if reservation.enrollment.status != ScheduleEnrollment.Status.ACTIVE:
            raise CommandError(f"parent personal enrollment is not active: {reservation.enrollment.status}")
        return {
            "ok": True,
            "stage": "personal_booked",
            "reservation_id": reservation.id,
            "schedule_id": reservation.schedule_id,
            "enrollment_id": reservation.enrollment_id,
        }

    def _assert_existing_package_booked(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        student_id = int(fixture["package_student"]["student_id"])
        enrollments = list(
            ScheduleEnrollment.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
                status=ScheduleEnrollment.Status.ACTIVE,
            )
            .order_by("id")
        )
        if len(enrollments) != 1:
            raise CommandError(f"expected one package personal enrollment, got {len(enrollments)}")
        reservation_count = PersonalBookingPaymentReservation.objects.for_club(club_id).filter(
            student_id=student_id
        ).count()
        if reservation_count:
            raise CommandError(f"package flow created personal payment reservations: {reservation_count}")
        order_count = BankPaymentOrder.objects.for_club(club_id).filter(student_id=student_id).count()
        if order_count:
            raise CommandError(f"package flow created bank payment orders: {order_count}")
        slot = PersonalAvailabilitySlot.objects.for_club(club_id).get(id=int(fixture["slots"]["package_primary"]["id"]))
        if slot.status != PersonalAvailabilitySlot.Status.BOOKED:
            raise CommandError(f"package personal slot not booked: {slot.status}")
        return {
            "ok": True,
            "stage": "existing_package_booked",
            "enrollment_id": enrollments[0].id,
            "slot_id": slot.id,
        }

    def _assert_trainer_personal_pending(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(
                student_id=int(fixture["student"]["student_id"]),
                tariff_id=int(fixture["tariffs"]["personal_id"]),
                availability_slot_id=int(fixture["slots"]["trainer_payment"]["id"]),
            )
            .select_related("bank_payment_order", "payment", "subscription", "availability_slot", "enrollment")
            .first()
        )
        if reservation is None:
            raise CommandError("trainer personal payment reservation was not created")
        if reservation.status != PersonalBookingPaymentReservation.Status.PENDING_PAYMENT:
            raise CommandError(f"expected pending trainer personal reservation, got {reservation.status}")
        if reservation.expires_at <= timezone.now():
            raise CommandError("trainer personal payment reservation is expired")
        if reservation.bank_payment_order_id is None:
            raise CommandError("trainer personal payment reservation has no bank order")
        if reservation.bank_payment_order.source != BankPaymentOrder.Source.TRAINER:
            raise CommandError(f"expected trainer source, got {reservation.bank_payment_order.source}")
        if reservation.bank_payment_order.status not in LIVE_ORDER_STATUSES:
            raise CommandError(f"trainer personal order is not payable: {reservation.bank_payment_order.status}")
        if reservation.payment.status != Payment.Status.PENDING:
            raise CommandError(f"trainer personal payment is not pending: {reservation.payment.status}")
        if reservation.subscription.status != Subscription.Status.PENDING:
            raise CommandError(f"trainer personal subscription is not pending: {reservation.subscription.status}")
        if reservation.availability_slot.status != PersonalAvailabilitySlot.Status.HELD:
            raise CommandError(f"trainer personal slot was not held: {reservation.availability_slot.status}")
        if reservation.schedule_id is not None or reservation.enrollment_id is not None:
            raise CommandError("trainer personal pending payment already created an active booking")
        return {
            "ok": True,
            "stage": "trainer_personal_pending",
            "reservation_id": reservation.id,
            "order_id": reservation.bank_payment_order_id,
            "slot_id": reservation.availability_slot_id,
        }

    def _assert_trainer_roster_readonly(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        expected = fixture["baseline"]["roster_checkins"]
        actual = list(
            Checkin.objects.for_club(club_id)
            .filter(
                schedule_id=int(fixture["trainer_session"]["schedule_id"]),
                date=fixture["trainer_session"]["date"],
            )
            .order_by("id")
            .values("id", "student_id", "source", "cancelled_at")
        )
        if actual != expected:
            raise CommandError(f"trainer roster checkins changed: expected {expected}, got {actual}")
        debt_count = Debt.objects.for_club(club_id).filter(
            student_id=int(fixture["trainer_session"]["roster_student_id"])
        ).count()
        if debt_count != int(fixture["baseline"]["roster_debt_count"]):
            raise CommandError(f"trainer roster debt count changed: got {debt_count}")
        return {"ok": True, "stage": "trainer_roster_readonly", "checkin_count": len(actual)}

    def _assert_empty_states(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        attendance_count = Checkin.objects.for_club(club_id).filter(
            student_id=int(fixture["student"]["student_id"])
        ).count()
        if attendance_count != int(fixture["baseline"]["attendance_empty_student_checkins"]):
            raise CommandError(f"attendance-empty checkins changed: got {attendance_count}")
        task_count = (
            RetentionTask.objects.for_club(club_id)
            .filter(
                trainer_id=int(fixture["trainer"]["trainer_id"]),
                resolved_at__isnull=True,
            )
            .count()
        )
        if task_count != int(fixture["baseline"]["trainer_open_task_count"]):
            raise CommandError(f"trainer open task count changed: got {task_count}")
        return {
            "ok": True,
            "stage": "empty_states",
            "attendance_count": attendance_count,
            "trainer_open_task_count": task_count,
        }

    def _process_mock_approved(self, *, order: BankPaymentOrder, request_id: str) -> BankPaymentProviderEvent:
        body = json.dumps(
            {
                "webhookType": "acquiringInternetPayment",
                "event_id": request_id,
                "status": "APPROVED",
                "paymentLinkId": order.provider_payment_link_id,
                "operationId": order.provider_operation_id or f"op-{order.id}",
                "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            }
        ).encode("utf-8")
        return process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id=request_id,
        )
