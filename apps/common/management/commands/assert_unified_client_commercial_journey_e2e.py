from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment, TrainingGroupMembership
from apps.attendance.services.checkin import create_checkin
from apps.billing.models import BankPaymentOrder, Debt, Payment, Subscription, SubscriptionRenewalEvent
from apps.billing.services import verify_payment
from apps.clubs.models import Club, ClubSettings
from apps.leads.models import LeadLifecycleEvent
from apps.retention.models import RetentionTask
from apps.students.models import AccountAccess, Student


class Command(BaseCommand):
    help = "Assert unified commercial journey E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--timeout-seconds", type=float, default=30)

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        deadline = time.monotonic() + max(float(options["timeout_seconds"]), 0)
        while True:
            try:
                with (
                    patch("apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"),
                    patch("apps.attendance.services.async_task"),
                    patch("django_q.tasks.async_task"),
                    transaction.atomic(),
                ):
                    evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(
                        f"unified commercial journey E2E assertion failed: {exc}"
                    ) from exc
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
            "trainer",
            "owner",
            "trial_lead",
            "contact_lead",
            "archived_lead",
            "student",
            "cash_group_lead",
            "sbp_group_lead",
            "checkin_group_lead",
            "reject_group_lead",
            "commercial",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        required_commercial = {
            "tariff_id",
            "tariff_name",
            "tariff_price",
            "tariff_trainings_limit",
            "tariff_duration_days",
            "training_type_id",
            "training_group_id",
            "schedule_id",
            "start_date",
            "renewal_student_id",
            "renewal_source_subscription_id",
            "renewal_membership_id",
        }
        missing_commercial = sorted(required_commercial - set(fixture["commercial"]))
        if missing_commercial:
            raise CommandError(
                "fixture commercial context is missing required fields: "
                f"{', '.join(missing_commercial)}"
            )
        if fixture["commercial"].get("protocol_version") != "v2":
            raise CommandError("fixture commercial protocol must be v2")
        self._require_positive_int(fixture["club_id"], "club_id")
        self._require_positive_int(fixture["trainer"].get("user_id"), "trainer.user_id")
        self._require_positive_int(fixture["owner"].get("user_id"), "owner.user_id")
        for field in (
            "trial_lead",
            "contact_lead",
            "archived_lead",
            "student",
            "cash_group_lead",
            "sbp_group_lead",
            "checkin_group_lead",
            "reject_group_lead",
        ):
            person = fixture[field]
            self._require_positive_int(person.get("id"), f"{field}.id")
            self._require_nonempty_string(person.get("first_name"), f"{field}.first_name")
        for field in (
            "tariff_id",
            "training_type_id",
            "training_group_id",
            "schedule_id",
            "renewal_student_id",
            "renewal_source_subscription_id",
            "renewal_membership_id",
        ):
            self._require_positive_int(fixture["commercial"].get(field), f"commercial.{field}")
        self._require_nonempty_string(fixture["commercial"].get("tariff_name"), "commercial.tariff_name")
        try:
            if Decimal(str(fixture["commercial"]["tariff_price"])) <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError) as exc:
            raise CommandError("fixture commercial.tariff_price must be a positive decimal") from exc
        for field in ("tariff_trainings_limit", "tariff_duration_days"):
            self._require_positive_int(fixture["commercial"].get(field), f"commercial.{field}")
        try:
            date.fromisoformat(str(fixture["commercial"]["start_date"]))
        except ValueError as exc:
            raise CommandError("fixture commercial.start_date must be an ISO date") from exc
        return fixture

    @staticmethod
    def _require_positive_int(value, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CommandError(f"fixture {field} must be a positive integer")

    @staticmethod
    def _require_nonempty_string(value, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise CommandError(f"fixture {field} must be a non-empty string")

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        settings_row = ClubSettings.objects.get(club=club)
        if not settings_row.unified_client_journey_enabled:
            raise CommandError("club unified journey capability is not enabled")
        if settings_row.commercial_journey_protocol_version != ClubSettings.CommercialJourneyProtocol.V2:
            raise CommandError("club commercial journey protocol is not v2")

        contact_id = int(fixture["contact_lead"]["id"])
        archived_id = int(fixture["archived_lead"]["id"])
        contact = Student.objects.for_club(club).get(id=contact_id)
        reopened = Student.objects.for_club(club).get(id=archived_id)
        if contact.lead_status != Student.LeadStatus.CONTACTED:
            raise CommandError("contact outcome did not move the lead to contacted")
        if reopened.id != archived_id:
            raise CommandError("archived lead identity changed during reopen")
        if reopened.status != Student.Status.LEAD or reopened.lead_status != Student.LeadStatus.NEW:
            raise CommandError("archived lead was not reopened into the active workspace")
        if reopened.became_student_at is not None:
            raise CommandError("reopened never-converted lead gained student provenance")

        contact_events = LeadLifecycleEvent.objects.for_club(club).filter(
            student_id=contact_id,
            event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
        ).count()
        reopen_events = LeadLifecycleEvent.objects.for_club(club).filter(
            student_id=archived_id,
            event_type=LeadLifecycleEvent.EventType.LEAD_REOPENED,
        ).count()
        open_tasks = RetentionTask.objects.for_club(club).filter(
            student_id__in=[contact_id, archived_id],
            task_type=RetentionTask.TaskType.NEW_LEAD,
            resolved_at__isnull=True,
        ).count()
        if contact_events != 1 or reopen_events != 1 or open_tasks != 2:
            raise CommandError(
                "lifecycle evidence mismatch: "
                f"contact_events={contact_events}, reopen_events={reopen_events}, "
                f"open_tasks={open_tasks}"
            )

        commercial = fixture["commercial"]
        cash_lead = Student.objects.for_club(club).get(id=int(fixture["cash_group_lead"]["id"]))
        cash_payment = self._pending_group_payment(
            club=club,
            student_id=cash_lead.id,
            commercial=commercial,
        )
        if (
            cash_lead.status != Student.Status.ACTIVE
            or cash_lead.lead_status is not None
            or cash_lead.became_student_at is None
        ):
            raise CommandError("v2 manual group admission did not admit the existing lead safely")
        if cash_payment.subscription.status != Subscription.Status.PENDING:
            raise CommandError("manual group receipt did not retain a pending subscription")
        if cash_payment.target_training_group_id != int(commercial["training_group_id"]):
            raise CommandError("manual group receipt lost canonical group identity")
        if cash_payment.target_schedule_id != int(commercial["schedule_id"]):
            raise CommandError("manual group receipt lost exact schedule identity")
        if cash_payment.target_start_date.isoformat() != commercial["start_date"]:
            raise CommandError("manual group receipt lost exact start date")
        if cash_payment.conversion_group_membership_id is None:
            raise CommandError("manual group admission has no payment-owned membership")
        cash_task = RetentionTask.objects.for_club(club).get(
            student_id=cash_lead.id,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        )
        if cash_task.status != RetentionTask.TaskStatus.CLOSED or cash_task.resolved_at is None:
            raise CommandError("v2 manual group admission did not close the captured task")
        cash_conversion_event = self._assert_v2_manual_conversion_event(
            club=club,
            student=cash_lead,
            payment=cash_payment,
            actor_user_id=int(fixture["trainer"]["user_id"]),
        )
        cash_access = self._assert_account_access(
            club=club,
            student=cash_lead,
            issued_by_id=int(fixture["trainer"]["user_id"]),
        )

        checkin_lead = Student.objects.for_club(club).get(
            id=int(fixture["checkin_group_lead"]["id"])
        )
        reject_lead = Student.objects.for_club(club).get(
            id=int(fixture["reject_group_lead"]["id"])
        )
        with (
            patch("apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"),
            patch("django_q.tasks.async_task"),
        ):
            checkin_payment = self._create_pending_group_payment(
                club=club,
                student_id=checkin_lead.id,
                commercial=commercial,
                key=f"{fixture['fixture_id']}-checkin-admission",
                recorded_by_id=int(fixture["trainer"]["user_id"]),
            )
            reject_payment = self._create_pending_group_payment(
                club=club,
                student_id=reject_lead.id,
                commercial=commercial,
                key=f"{fixture['fixture_id']}-reject-admission",
                recorded_by_id=int(fixture["trainer"]["user_id"]),
            )
            self._assert_v2_manual_group_admissions(checkin_lead, reject_lead)
            if checkin_payment.status != Payment.Status.PENDING:
                raise CommandError("check-in evidence requires a pending manual payment before owner review")
            cash_checkin_result = create_checkin(
                club_id=club.id,
                student_id=cash_lead.id,
                schedule_id=int(commercial["schedule_id"]),
                training_type_id=int(commercial["training_type_id"]),
                source=Checkin.Source.KIOSK,
                checkin_date=cash_payment.target_start_date,
            )
            cash_checkin = Checkin.objects.for_club(club).get(id=cash_checkin_result["checkin_id"])
            cash_debt = Debt.objects.for_club(club).get(checkin_id=cash_checkin.id)
            if (
                not cash_checkin.is_debt
                or cash_checkin.subscription_id is not None
                or cash_debt.settlement_payment_id != cash_payment.id
                or cash_debt.resolved_at is not None
            ):
                raise CommandError(
                    "pending manual cash admission did not retain its check-in as a reserved debt"
                )
            checkin_result = create_checkin(
                club_id=club.id,
                student_id=checkin_lead.id,
                schedule_id=int(commercial["schedule_id"]),
                training_type_id=int(commercial["training_type_id"]),
                source=Checkin.Source.KIOSK,
                checkin_date=cash_payment.target_start_date,
            )
            checkin_debt = Debt.objects.for_club(club).get(
                checkin_id=checkin_result["checkin_id"]
            )
            if checkin_debt.settlement_payment_id != checkin_payment.id:
                raise CommandError("attended rejection fixture did not reserve its exact debt")
            checkin_membership_id = checkin_payment.conversion_group_membership_id
            reject_membership_id = reject_payment.conversion_group_membership_id
            if checkin_membership_id is None or reject_membership_id is None:
                raise CommandError("rejection fixtures lost their payment-owned memberships")
            verify_payment(
                payment_id=cash_payment.id,
                club_id=club.id,
                verified_by_id=int(fixture["owner"]["user_id"]),
                action="confirm",
            )
            verify_payment(
                payment_id=checkin_payment.id,
                club_id=club.id,
                verified_by_id=int(fixture["owner"]["user_id"]),
                action="reject",
                rejection_reason="Slice 6 attended rejection evidence",
            )
            verify_payment(
                payment_id=reject_payment.id,
                club_id=club.id,
                verified_by_id=int(fixture["owner"]["user_id"]),
                action="reject",
                rejection_reason="Slice 6 unattended rejection evidence",
            )

        cash_lead.refresh_from_db()
        checkin_lead.refresh_from_db()
        reject_lead.refresh_from_db()
        cash_payment.refresh_from_db()
        cash_subscription = Subscription.objects.for_club(club).get(id=cash_payment.subscription_id)
        cash_checkin.refresh_from_db()
        cash_debt.refresh_from_db()
        checkin_payment.refresh_from_db()
        reject_payment.refresh_from_db()
        checkin_debt.refresh_from_db()
        rejected_checkin_subscription = Subscription.objects.for_club(club).get(
            id=checkin_payment.subscription_id
        )
        rejected_unattended_subscription = Subscription.objects.for_club(club).get(
            id=reject_payment.subscription_id
        )
        cash_conversion_event_after = self._assert_v2_manual_conversion_event(
            club=club,
            student=cash_lead,
            payment=cash_payment,
            actor_user_id=int(fixture["trainer"]["user_id"]),
        )
        if (
            cash_lead.status != Student.Status.ACTIVE
            or cash_lead.user_id != cash_access.user_id
            or cash_payment.status != Payment.Status.CONFIRMED
            or cash_subscription.status != Subscription.Status.ACTIVE
        ):
            raise CommandError("manual confirmation did not retain the admitted student and account access")
        if cash_conversion_event_after.id != cash_conversion_event.id:
            raise CommandError("manual confirmation replaced the original v2 conversion evidence")
        if (
            cash_checkin.subscription_id != cash_subscription.id
            or cash_checkin.is_debt
            or cash_debt.settlement_payment_id != cash_payment.id
            or cash_debt.resolved_at is None
            or cash_debt.resolution_type != "payment"
        ):
            raise CommandError("manual confirmation did not reconcile the pending-window cash check-in")
        expected_trainings_left = int(commercial["tariff_trainings_limit"]) - 1
        if (
            cash_subscription.trainings_left != expected_trainings_left
            or cash_subscription.trainings_used != 1
        ):
            raise CommandError("manual confirmation did not decrement subscription credit for the cash check-in")
        if checkin_lead.status != Student.Status.ACTIVE or checkin_lead.user_id is not None:
            raise CommandError("exact pending-admission check-in did not retain the admitted student safely")
        if reject_lead.status != Student.Status.ACTIVE or reject_lead.lead_status is not None:
            raise CommandError("rejected v2 group admission demoted the admitted student")
        self._assert_rejected_group_artifacts(
            club=club,
            payment=checkin_payment,
            subscription=rejected_checkin_subscription,
            membership_id=checkin_membership_id,
        )
        self._assert_rejected_group_artifacts(
            club=club,
            payment=reject_payment,
            subscription=rejected_unattended_subscription,
            membership_id=reject_membership_id,
        )
        rejected_checkin = Checkin.objects.for_club(club).get(id=checkin_result["checkin_id"])
        if (
            not rejected_checkin.is_debt
            or rejected_checkin.subscription_id is not None
            or checkin_debt.resolved_at is not None
            or checkin_debt.settlement_payment_id is not None
        ):
            raise CommandError("attended rejection did not reopen the preserved check-in debt")
        for rejected_student, rejected_payment in (
            (checkin_lead, checkin_payment),
            (reject_lead, reject_payment),
        ):
            if LeadLifecycleEvent.objects.for_club(club).filter(
                student_id=rejected_student.id,
                event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
                metadata__payment_id=rejected_payment.id,
            ).exists():
                raise CommandError("rejected v2 group admission unexpectedly restored the lead")
        reject_task = RetentionTask.objects.for_club(club).get(
            student_id=reject_lead.id,
            task_type=RetentionTask.TaskType.NEW_LEAD,
        )
        if reject_task.status != RetentionTask.TaskStatus.CLOSED or reject_task.resolved_at is None:
            raise CommandError("rejected v2 group admission did not retain the closed conversion task")

        sbp_lead = Student.objects.for_club(club).get(id=int(fixture["sbp_group_lead"]["id"]))
        sbp_order = self._pending_v2_group_order(
            club=club,
            student_id=sbp_lead.id,
            commercial=commercial,
        )
        if sbp_lead.status != Student.Status.LEAD or sbp_lead.lead_status is None:
            raise CommandError("v2 group SBP order admitted the lead before provider confirmation")
        if sbp_order.payment.status != Payment.Status.PENDING:
            raise CommandError("v2 group SBP receipt did not retain a pending payment")
        with (
            patch("apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"),
            patch("django_q.tasks.async_task"),
        ):
            self._approve_mock_order(order=sbp_order, fixture_id=fixture["fixture_id"], suffix="group-sbp")
        sbp_order.refresh_from_db()
        sbp_order.payment.refresh_from_db()
        sbp_lead.refresh_from_db()
        if (
            sbp_order.status != BankPaymentOrder.Status.APPROVED
            or sbp_order.payment.status != Payment.Status.CONFIRMED
            or sbp_lead.status != Student.Status.ACTIVE
        ):
            raise CommandError("mock provider confirmation did not finalize the v2 group SBP receipt")

        renewal_order = (
            BankPaymentOrder.objects.for_club(club)
            .select_related("payment", "subscription__renewed_from", "renewed_from_subscription")
            .filter(
                student_id=int(commercial["renewal_student_id"]),
                source=BankPaymentOrder.Source.TRAINER,
                renewed_from_subscription_id=int(commercial["renewal_source_subscription_id"]),
            )
            .order_by("id")
            .first()
        )
        if renewal_order is None:
            raise CommandError("browser SBP exact-source renewal order is missing")
        if (
            renewal_order.status != BankPaymentOrder.Status.PENDING
            or renewal_order.payment.status != Payment.Status.PENDING
        ):
            raise CommandError("payment return/provider state was not authoritative before provider confirmation")
        if not renewal_order.provider_payment_url or not renewal_order.provider_payment_link_id:
            raise CommandError("SBP renewal receipt did not persist the provider link")
        if renewal_order.payment.target_training_group_id != int(commercial["training_group_id"]):
            raise CommandError("SBP renewal lost its exact canonical group target")
        if renewal_order.payment.target_schedule_id != int(commercial["schedule_id"]):
            raise CommandError("SBP renewal lost its exact schedule target")
        if renewal_order.payment.target_group_membership_id != int(commercial["renewal_membership_id"]):
            raise CommandError("SBP renewal lost its exact existing membership target")
        with (
            patch("apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"),
            patch("django_q.tasks.async_task"),
        ):
            self._approve_mock_order(order=renewal_order, fixture_id=fixture["fixture_id"], suffix="renewal")
        renewal_order.refresh_from_db()
        renewal_order.payment.refresh_from_db()
        renewal_order.subscription.refresh_from_db()
        renewal_source = Subscription.objects.for_club(club).get(
            id=int(commercial["renewal_source_subscription_id"])
        )
        renewal_event = SubscriptionRenewalEvent.objects.for_club(club).get(
            payment_id=renewal_order.payment_id
        )
        if (
            renewal_order.status != BankPaymentOrder.Status.APPROVED
            or renewal_order.payment.status != Payment.Status.CONFIRMED
        ):
            raise CommandError("mock provider approval did not authoritatively finalize the renewal")
        if renewal_source.status != Subscription.Status.EXPIRED:
            raise CommandError("exhausted renewal source did not expire on finalization")
        if renewal_event.carry_snapshot.get("outcome") != "exhausted":
            raise CommandError("exhausted renewal unexpectedly carried remaining entitlement")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "contact_lead_id": contact.id,
            "reopened_lead_id": reopened.id,
            "contact_events": contact_events,
            "reopen_events": reopen_events,
            "open_tasks": open_tasks,
            "manual_group_payment_id": cash_payment.id,
            "manual_group_account_access_id": cash_access.id,
            "manual_group_pending_checkin_id": cash_checkin.id,
            "pending_checkin_id": checkin_result["checkin_id"],
            "group_sbp_order_id": sbp_order.id,
            "renewal_order_id": renewal_order.id,
            "renewal_event_id": renewal_event.id,
        }

    def _pending_group_payment(self, *, club, student_id: int, commercial: dict) -> Payment:
        payments = list(
            Payment.objects.for_club(club)
            .select_related("subscription")
            .filter(
                student_id=student_id,
                target_training_group_id=int(commercial["training_group_id"]),
                target_schedule_id=int(commercial["schedule_id"]),
            )
            .order_by("id")
        )
        if len(payments) != 1:
            raise CommandError(f"expected one browser manual group payment, got {len(payments)}")
        payment = payments[0]
        if payment.payment_method != Payment.Method.CASH or payment.status != Payment.Status.PENDING:
            raise CommandError("browser group payment must remain a pending cash receipt")
        return payment

    def _assert_account_access(self, *, club, student: Student, issued_by_id: int) -> AccountAccess:
        accesses = list(
            AccountAccess.objects.for_club(club)
            .filter(student_id=student.id, role=AccountAccess.Role.STUDENT)
            .order_by("id")
        )
        if len(accesses) != 1:
            raise CommandError(
                f"cash lead account access count mismatch: expected 1, got {len(accesses)}"
            )
        access = accesses[0]
        student.refresh_from_db()
        if (
            access.status != AccountAccess.Status.OPEN
            or access.issued_by_id != issued_by_id
            or access.user_id != student.user_id
        ):
            raise CommandError("cash lead account access lost its explicit trainer provenance")
        return access

    def _assert_v2_manual_conversion_event(
        self,
        *,
        club: Club,
        student: Student,
        payment: Payment,
        actor_user_id: int,
    ) -> LeadLifecycleEvent:
        events = list(
            LeadLifecycleEvent.objects.for_club(club)
            .filter(
                student_id=student.id,
                event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            )
            .order_by("created_at", "id")
        )
        if len(events) != 1:
            raise CommandError(
                f"v2 manual conversion event count mismatch: expected 1, got {len(events)}"
            )
        event = events[0]
        metadata = event.metadata
        resource_ids = metadata.get("resource_ids") or {}
        if (
            event.actor_id != actor_user_id
            or metadata.get("source") != "manual_operational_admission_v2"
            or metadata.get("payment_id") != payment.id
            or metadata.get("origin") != "group"
            or resource_ids.get("training_group_id") != payment.target_training_group_id
            or resource_ids.get("membership_id") != payment.conversion_group_membership_id
            or resource_ids.get("enrollment_id") != payment.conversion_enrollment_id
        ):
            raise CommandError("v2 manual conversion event lost exact actor or payment evidence")
        return event

    def _assert_rejected_group_artifacts(
        self,
        *,
        club: Club,
        payment: Payment,
        subscription: Subscription,
        membership_id: int,
    ) -> None:
        membership = TrainingGroupMembership.objects.for_club(club).get(id=membership_id)
        projections = list(
            ScheduleEnrollment.objects.for_club(club)
            .filter(training_group_membership_id=membership.id)
            .order_by("schedule_id", "id")
        )
        if (
            payment.status != Payment.Status.REJECTED
            or subscription.deleted_at is None
            or membership.status != TrainingGroupMembership.Status.CANCELLED
            or membership.ends_on is None
            or not projections
            or any(
                projection.status != ScheduleEnrollment.Status.CANCELLED
                or projection.ends_on is None
                for projection in projections
            )
        ):
            raise CommandError("rejected v2 group admission left live payment-owned artifacts")

    @staticmethod
    def _assert_v2_manual_group_admissions(*students: Student) -> None:
        for student in students:
            student.refresh_from_db()
        if any(student.status != Student.Status.ACTIVE for student in students):
            raise CommandError("v2 manual group admission must admit the lead before owner review")

    def _create_pending_group_payment(
        self,
        *,
        club,
        student_id: int,
        commercial: dict,
        key: str,
        recorded_by_id: int,
    ) -> Payment:
        from apps.billing.services import create_v2_group_sale_manual, resolve_v2_group_sale_offer

        offer = resolve_v2_group_sale_offer(
            club_id=club.id,
            student_id=student_id,
            tariff_id=int(commercial["tariff_id"]),
            target_training_group_id=int(commercial["training_group_id"]),
            target_schedule_id=int(commercial["schedule_id"]),
            target_start_date=date.fromisoformat(commercial["start_date"]),
            lock=False,
        )
        return create_v2_group_sale_manual(
            club_id=club.id,
            student_id=student_id,
            tariff_id=int(commercial["tariff_id"]),
            payment_method=Payment.Method.CASH,
            target_training_group_id=int(commercial["training_group_id"]),
            target_schedule_id=int(commercial["schedule_id"]),
            target_start_date=date.fromisoformat(commercial["start_date"]),
            expected_offer_digest=offer.payload["offer_digest"],
            command_idempotency_key=key,
            recorded_by_id=recorded_by_id,
            enforce_trainer_group_contract=True,
        )

    def _pending_v2_group_order(self, *, club, student_id: int, commercial: dict) -> BankPaymentOrder:
        orders = list(
            BankPaymentOrder.objects.for_club(club)
            .select_related("payment", "payment__subscription")
            .filter(
                student_id=student_id,
                payment__target_training_group_id=int(commercial["training_group_id"]),
                payment__target_schedule_id=int(commercial["schedule_id"]),
            )
            .order_by("id")
        )
        if len(orders) != 1:
            raise CommandError(f"expected one browser v2 group SBP order, got {len(orders)}")
        order = orders[0]
        if (
            order.status != BankPaymentOrder.Status.PENDING
            or order.payment.payment_method != Payment.Method.ONLINE
        ):
            raise CommandError("browser v2 group SBP receipt must remain provider-pending")
        return order

    def _approve_mock_order(self, *, order: BankPaymentOrder, fixture_id: str, suffix: str) -> None:
        from apps.billing.services import process_bank_payment_webhook

        payload = {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"{fixture_id}-{suffix}-approved",
            "status": "APPROVED",
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": f"{fixture_id}-{suffix}-approved-operation",
            "amount": str(order.amount_snapshot),
            "paid_at": timezone.now().isoformat(),
        }
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(payload).encode(),
            headers={},
            request_id=f"e2e-{fixture_id}-{suffix}-approved",
        )
