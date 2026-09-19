from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment, TrainingGroupMembership
from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date
from apps.billing.models import Debt, DebtLifecycleEvent, DebtSettlementEvent, Payment
from apps.clubs.capabilities import get_commercial_journey_capability
from apps.clubs.models import Club, ClubSettings
from apps.dashboard.selectors import get_dashboard_metrics
from apps.dashboard.services import get_pnl_report
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import AccountAccess, Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert owner/admin payment rejection E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_payment_reject_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for rejection side effects before failing.",
        )
        parser.add_argument(
            "--stage",
            choices=("admission", "checked_in", "rejected"),
            default="rejected",
            help="Assert the existing canonical rejection pack at each durable lifecycle boundary.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        stage = options["stage"]
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_runtime_evidence(fixture, stage=stage)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"payment rejection E2E assertion failed: {exc}") from exc
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
            "trainer",
            "student_id",
            "adult",
            "schedule",
            "runtime",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        return self._collect_runtime_evidence(fixture, stage="rejected")

    def _runtime_variants(self, fixture: dict) -> list[dict]:
        runtime = fixture["runtime"]
        return [
            {
                "label": "child",
                "student_id": int(fixture["child"]["id"]),
                "payment_id": int(runtime["child_payment_id"]),
                "checkin_id": int(runtime["child_checkin_id"]) if runtime.get("child_checkin_id") else None,
                "role": AccountAccess.Role.PARENT,
                "schedule_id": int(fixture["schedule_id"]),
            },
            {
                "label": "adult",
                "student_id": int(fixture["adult"]["id"]),
                "payment_id": int(runtime["adult_payment_id"]),
                "checkin_id": int(runtime["adult_checkin_id"]) if runtime.get("adult_checkin_id") else None,
                "role": AccountAccess.Role.STUDENT,
                "schedule_id": int(fixture["schedule_id"]),
            },
        ]

    def _collect_runtime_evidence(self, fixture: dict, *, stage: str) -> dict:
        club_id = int(fixture["club_id"])
        expected_protocol = fixture["expected"].get("commercial_journey_protocol_version")
        if expected_protocol != ClubSettings.CommercialJourneyProtocol.V2:
            raise CommandError("payment rejection fixture is not pinned to commercial journey v2")
        capability = get_commercial_journey_capability(club=club_id)
        if not capability.v2_manual_admission_enabled:
            raise CommandError("payment rejection fixture does not have v2 manual admission enabled")
        trainer_user_id = int(fixture["trainer"]["user_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        results = []
        for variant in self._runtime_variants(fixture):
            payment = Payment.objects.for_club(club_id).select_related("subscription", "conversion_enrollment").get(
                id=variant["payment_id"]
            )
            if payment.student_id != variant["student_id"]:
                raise CommandError(f"{variant['label']} runtime payment belongs to another student")
            student = Student.objects.for_club(club_id).get(id=variant["student_id"])
            conversion_events = list(
                LeadLifecycleEvent.objects.for_club(club_id)
                .filter(student=student, event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED)
                .order_by("created_at", "id")
            )
            if len(conversion_events) != 1:
                raise CommandError(f"{variant['label']} lead conversion is not exactly once")
            conversion_event = conversion_events[0]
            conversion_metadata = conversion_event.metadata
            conversion_resource_ids = conversion_metadata.get("resource_ids") or {}
            if (
                conversion_event.actor_id != trainer_user_id
                or conversion_metadata.get("source") != "manual_operational_admission_v2"
                or conversion_metadata.get("payment_id") != payment.id
                or conversion_metadata.get("origin") != "group"
                or conversion_resource_ids.get("training_group_id") != payment.target_training_group_id
                or conversion_resource_ids.get("membership_id") != payment.conversion_group_membership_id
                or conversion_resource_ids.get("enrollment_id") != payment.conversion_enrollment_id
            ):
                raise CommandError(f"{variant['label']} lead conversion lost exact v2 payment evidence")
            if payment.subscription is None or payment.conversion_enrollment is None:
                raise CommandError(f"{variant['label']} admission has no owned subscription/enrollment")
            if payment.target_training_group_id != int(fixture["schedule"]["training_group_id"]):
                raise CommandError(f"{variant['label']} payment did not persist canonical group identity")
            membership = TrainingGroupMembership.objects.for_club(club_id).get(
                id=payment.conversion_group_membership_id,
                training_group_id=payment.target_training_group_id,
            )
            access = AccountAccess.objects.for_club(club_id).get(student=student, role=variant["role"])
            if access.issued_by_id != trainer_user_id:
                raise CommandError(f"{variant['label']} account access provenance is invalid")
            if stage == "admission":
                if payment.status != Payment.Status.PENDING or payment.subscription.status != "pending":
                    raise CommandError(f"{variant['label']} payment is not pending after UI admission")
                if payment.conversion_enrollment.schedule_id != variant["schedule_id"]:
                    raise CommandError(f"{variant['label']} admission enrollment targets the wrong group")
                if membership.status != TrainingGroupMembership.Status.ACTIVE:
                    raise CommandError(f"{variant['label']} payment-owned canonical membership is not active")
                results.append({"label": variant["label"], "payment_id": payment.id, "status": payment.status})
                continue
            if variant["checkin_id"] is None:
                raise CommandError(f"{variant['label']} runtime check-in id is missing")
            debt_filters = {
                "student": student,
                "checkin__schedule_id": variant["schedule_id"],
                "checkin_id": variant["checkin_id"],
            }
            if stage == "checked_in":
                debt_filters["settlement_payment_id"] = payment.id
            debts = list(Debt.objects.for_club(club_id).select_related("checkin").filter(**debt_filters).order_by("id"))
            if len(debts) != 1 or debts[0].checkin_id is None:
                raise CommandError(f"{variant['label']} must have exactly one payment-reserved check-in debt")
            debt = debts[0]
            if stage == "checked_in":
                if payment.status != Payment.Status.PENDING or not debt.checkin.is_debt or debt.resolved_at is not None:
                    raise CommandError(f"{variant['label']} pending check-in is not preserved as reserved debt")
                results.append({"label": variant["label"], "payment_id": payment.id, "debt_id": debt.id})
                continue
            if payment.status != Payment.Status.REJECTED or payment.verified_by_id is not None:
                raise CommandError(f"{variant['label']} payment is not rejected")
            if payment.rejection_reason != fixture["expected"]["rejection_reason"]:
                raise CommandError(f"{variant['label']} rejection reason is not preserved")
            if (
                payment.subscription.deleted_at is None
                or payment.conversion_enrollment.status != ScheduleEnrollment.Status.CANCELLED
                or membership.status != TrainingGroupMembership.Status.CANCELLED
            ):
                raise CommandError(f"{variant['label']} terminal payment-owned artifacts are not closed")
            group_schedule_ids = sorted(
                [
                    int(fixture["schedule"]["id"]),
                    int(fixture["schedule"]["second_schedule_id"]),
                ]
            )
            projection_rows = list(
                ScheduleEnrollment.objects.for_club(club_id)
                .filter(training_group_membership_id=membership.id)
                .order_by("schedule_id", "id")
                .values_list("schedule_id", "status", "created_from")
            )
            if (
                [schedule_id for schedule_id, _, _ in projection_rows] != group_schedule_ids
                or any(status != ScheduleEnrollment.Status.CANCELLED for _, status, _ in projection_rows)
                or any(source != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION for _, _, source in projection_rows)
            ):
                raise CommandError(
                    f"{variant['label']} rejection did not close both canonical group projections"
                )
            second_schedule_id = int(fixture["schedule"]["second_schedule_id"])
            roster = resolve_expected_roster_by_schedule_date(
                club=Club.objects.get(id=club_id),
                schedule_ids=[second_schedule_id],
                target_date=date.fromisoformat(fixture["schedule"]["second_start_date"]),
            )
            if student.id in roster.get(second_schedule_id, {}):
                raise CommandError(f"{variant['label']} remains eligible on the second group slot after rejection")
            if debt.resolved_at is not None or debt.settlement_payment_id is not None or not debt.checkin.is_debt:
                raise CommandError(f"{variant['label']} attended debt is not reopened after rejection")
            if debt.tariff_price is None or debt.tariff_price <= 0:
                raise CommandError(f"{variant['label']} attended debt is not payable after rejection")
            events = list(
                DebtSettlementEvent.objects.for_club(club_id)
                .filter(payment=payment, debt=debt)
                .values_list("event_type", flat=True)
            )
            if events != [DebtSettlementEvent.EventType.RESERVED, DebtSettlementEvent.EventType.REJECTED]:
                raise CommandError(f"{variant['label']} debt events are not reserve/reject")
            lifecycle_events = list(
                DebtLifecycleEvent.objects.for_club(club_id)
                .filter(payment=payment, debt=debt)
                .order_by("created_at", "id")
                .values_list("event_type", flat=True)
            )
            if lifecycle_events != [DebtLifecycleEvent.EventType.RESERVED, DebtLifecycleEvent.EventType.REJECTED]:
                raise CommandError(f"{variant['label']} debt lifecycle is not reserve/reject")
            if TrainerEarning.objects.for_club(club_id).filter(payment=payment).exists():
                raise CommandError(f"{variant['label']} rejected payment has a sale earning")
            results.append(
                {
                    "label": variant["label"],
                    "payment_id": payment.id,
                    "debt_id": debt.id,
                    "owner_id": owner_user_id,
                    "projection_schedule_ids": [schedule_id for schedule_id, _, _ in projection_rows],
                    "projection_statuses": [status for _, status, _ in projection_rows],
                    "second_slot_membership_eligible": False,
                }
            )
        return {"ok": True, "stage": stage, "variants": results}

    def _collect_legacy_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        trainer_user_id = int(fixture["trainer"]["user_id"])
        student_id = int(fixture["student_id"])
        adult = fixture["adult"]
        checkin_id = int(fixture["checkin_id"])
        debt_id = int(fixture["debt_id"])
        payment_id = int(fixture["payment_id"])
        subscription_id = int(fixture["subscription_id"])
        conversion_enrollment_id = int(fixture["conversion_enrollment_id"])
        expected = fixture["expected"]

        payment = (
            Payment.objects.for_club(club_id)
            .select_related("subscription__tariff__training_type")
            .get(id=payment_id)
        )
        if payment.status != Payment.Status.REJECTED:
            raise CommandError(f"payment not rejected: got {payment.status}")
        if payment.verified_by_id is not None:
            raise CommandError(f"rejected payment unexpectedly has verified_by={payment.verified_by_id}")
        if payment.verified_at is not None:
            raise CommandError("rejected payment unexpectedly has verified_at")

        expected_reason = expected["rejection_reason"]
        if payment.rejection_reason != expected_reason:
            raise CommandError(
                f"payment rejection_reason mismatch: expected {expected_reason!r}, got {payment.rejection_reason!r}"
            )

        expected_amount = Decimal(expected["payment_amount"])
        if payment.amount != expected_amount:
            raise CommandError(f"payment amount mismatch: expected {expected_amount}, got {payment.amount}")
        if payment.sale_earning_snapshot_recorded:
            raise CommandError("rejected payment unexpectedly has sale earning snapshot")

        subscription = payment.subscription
        if subscription is None or subscription.id != subscription_id:
            raise CommandError(f"payment subscription mismatch: expected {subscription_id}, got {subscription}")
        if subscription.deleted_at is None:
            raise CommandError("rejected payment subscription was not soft-deleted")
        if subscription.status != "pending":
            raise CommandError(f"rejected payment subscription status changed unexpectedly: got {subscription.status}")
        if subscription.expires_at is not None:
            raise CommandError("rejected payment subscription unexpectedly has expires_at")

        enrollment = ScheduleEnrollment.objects.for_club(club_id).get(id=conversion_enrollment_id)
        if enrollment.status != ScheduleEnrollment.Status.CANCELLED:
            raise CommandError(f"payment-owned enrollment not cancelled: got {enrollment.status}")
        if enrollment.starts_on is None or enrollment.ends_on is None or enrollment.ends_on < enrollment.starts_on:
            raise CommandError("payment-owned enrollment has invalid terminal interval")

        debt = Debt.objects.for_club(club_id).get(id=debt_id)
        checkin = Checkin.objects.for_club(club_id).get(id=checkin_id)
        self._assert_debt_and_checkin(debt=debt, checkin=checkin)

        settlement_events = list(
            DebtSettlementEvent.objects.for_club(club_id)
            .filter(payment_id=payment_id, debt_id=debt_id)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        expected_settlement_events = [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.REJECTED,
        ]
        if settlement_events != expected_settlement_events:
            raise CommandError(
                "debt settlement events mismatch: "
                f"expected {expected_settlement_events}, got {settlement_events}"
            )

        lifecycle_events = list(
            DebtLifecycleEvent.objects.for_club(club_id)
            .filter(payment_id=payment_id, debt_id=debt_id)
            .order_by("created_at", "id")
        )
        lifecycle_event_types = [event.event_type for event in lifecycle_events]
        expected_lifecycle_events = [
            DebtLifecycleEvent.EventType.RESERVED,
            DebtLifecycleEvent.EventType.REJECTED,
        ]
        if lifecycle_event_types != expected_lifecycle_events:
            raise CommandError(
                "debt lifecycle events mismatch: "
                f"expected {expected_lifecycle_events}, got {lifecycle_event_types}"
            )
        self._assert_lifecycle_snapshots(
            lifecycle_events=lifecycle_events,
            debt=debt,
            payment=payment,
            subscription_id=subscription_id,
            recorded_by_id=trainer_user_id,
            owner_user_id=owner_user_id,
            expected_reason=expected_reason,
        )

        sale_earning_count = TrainerEarning.objects.for_club(club_id).filter(payment=payment).count()
        if sale_earning_count:
            raise CommandError(f"rejected payment unexpectedly has {sale_earning_count} sale earning row(s)")

        today = timezone.localdate()
        pnl = get_pnl_report(club=payment.club, date_from=today, date_to=today)
        dashboard = get_dashboard_metrics(club=payment.club, date_from=today, date_to=today)
        adult_evidence = self._assert_adult_rejection_state(club_id=club_id, adult=adult)
        self._assert_no_finance_surfaces(pnl=pnl, dashboard=dashboard, expected_debtors=2)
        parent_access = self._assert_parent_account_access(
            club_id=club_id,
            student_id=student_id,
            issued_by_id=trainer_user_id,
        )
        adult_access = self._assert_student_account_access(
            club_id=club_id,
            adult_id=int(adult["id"]),
            issued_by_id=trainer_user_id,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "payment": {
                "id": payment.id,
                "status": payment.status,
                "amount": str(payment.amount),
                "verified_by_id": payment.verified_by_id,
                "verified_at": payment.verified_at.isoformat() if payment.verified_at else None,
                "rejection_reason": payment.rejection_reason,
                "sale_snapshot_recorded": payment.sale_earning_snapshot_recorded,
            },
            "subscription": {
                "id": subscription.id,
                "status": subscription.status,
                "deleted": subscription.deleted_at is not None,
                "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
            },
            "enrollment": {
                "id": enrollment.id,
                "status": enrollment.status,
                "starts_on": enrollment.starts_on.isoformat(),
                "ends_on": enrollment.ends_on.isoformat(),
            },
            "debt": {
                "id": debt.id,
                "resolved": debt.resolved_at is not None,
                "resolution_type": debt.resolution_type,
                "settlement_payment_id": debt.settlement_payment_id,
            },
            "checkin": {
                "id": checkin.id,
                "subscription_id": checkin.subscription_id,
                "is_debt": checkin.is_debt,
            },
            "debt_settlement_events": settlement_events,
            "debt_lifecycle_events": lifecycle_event_types,
            "sale_earning_count": sale_earning_count,
            "pnl": {
                "income": str(pnl["income"]),
                "salary_expenses": str(pnl["salary_expenses"]),
                "margin": str(pnl["margin"]),
            },
            "dashboard": {
                "revenue": str(dashboard["revenue"]),
                "active_subscriptions": dashboard["active_subscriptions"],
                "debtors": dashboard["debtors"],
            },
            "parent_account_access": parent_access,
            "adult": adult_evidence,
            "adult_account_access": adult_access,
        }

    def _assert_adult_rejection_state(self, *, club_id: int, adult: dict) -> dict:
        payment = Payment.objects.for_club(club_id).select_related("subscription").get(
            id=int(adult["payment_id"])
        )
        if payment.student_id != int(adult["id"]) or payment.status != Payment.Status.REJECTED:
            raise CommandError("adult payment is not rejected")
        if payment.subscription_id != int(adult["subscription_id"]) or payment.subscription is None:
            raise CommandError("adult rejected payment subscription provenance is invalid")
        if payment.subscription.deleted_at is None:
            raise CommandError("adult rejected payment subscription was not deleted")
        enrollment = ScheduleEnrollment.objects.for_club(club_id).get(id=int(adult["conversion_enrollment_id"]))
        if enrollment.status != ScheduleEnrollment.Status.CANCELLED:
            raise CommandError("adult rejected payment enrollment was not cancelled")
        debt = Debt.objects.for_club(club_id).get(id=int(adult["debt_id"]))
        checkin = Checkin.objects.for_club(club_id).get(id=int(adult["checkin_id"]))
        self._assert_debt_and_checkin(debt=debt, checkin=checkin)
        return {
            "payment_id": payment.id,
            "payment_status": payment.status,
            "debt_id": debt.id,
            "debt_open": debt.resolved_at is None and debt.settlement_payment_id is None,
        }

    def _assert_student_account_access(self, *, club_id: int, adult_id: int, issued_by_id: int) -> dict:
        student = Student.objects.for_club(club_id).get(id=adult_id)
        if student.user_id is None:
            raise CommandError("rejected adult payment does not retain explicit student cabinet access")
        access = AccountAccess.objects.for_club(club_id).get(
            student_id=adult_id,
            role=AccountAccess.Role.STUDENT,
        )
        if access.user_id != student.user_id or access.issued_by_id != issued_by_id:
            raise CommandError("student account access provenance is invalid")
        return {
            "id": access.id,
            "user_id": access.user_id,
            "status": access.status,
            "issued_by_id": access.issued_by_id,
        }

    def _assert_parent_account_access(self, *, club_id: int, student_id: int, issued_by_id: int) -> dict:
        student = Student.objects.for_club(club_id).select_related("parent_user").get(id=student_id)
        if not student.is_child or student.parent_user_id is None:
            raise CommandError("rejected child payment does not retain explicit parent cabinet access")
        access = AccountAccess.objects.for_club(club_id).get(
            student_id=student_id,
            role=AccountAccess.Role.PARENT,
        )
        if access.user_id != student.parent_user_id:
            raise CommandError("parent account access user does not match the child parent user")
        if access.issued_by_id != issued_by_id:
            raise CommandError("parent account access was not issued by the payment-recording trainer")
        if access.status not in {AccountAccess.Status.OPEN, AccountAccess.Status.RESET}:
            raise CommandError(f"parent account access status mismatch: got {access.status}")
        return {
            "id": access.id,
            "user_id": access.user_id,
            "status": access.status,
            "issued_by_id": access.issued_by_id,
        }

    def _assert_debt_and_checkin(self, *, debt: Debt, checkin: Checkin) -> None:
        if debt.resolved_at is not None:
            raise CommandError("selected debt was unexpectedly resolved")
        if debt.resolution_type:
            raise CommandError(f"selected debt resolution_type mismatch: expected blank, got {debt.resolution_type}")
        if debt.settlement_payment_id is not None:
            raise CommandError(
                f"selected debt settlement_payment should be released, got {debt.settlement_payment_id}"
            )
        if checkin.subscription_id is not None:
            raise CommandError(f"debt check-in unexpectedly linked to subscription {checkin.subscription_id}")
        if not checkin.is_debt:
            raise CommandError("debt check-in is no longer marked as debt")

    def _assert_lifecycle_snapshots(
        self,
        *,
        lifecycle_events: list[DebtLifecycleEvent],
        debt: Debt,
        payment: Payment,
        subscription_id: int,
        recorded_by_id: int,
        owner_user_id: int,
        expected_reason: str,
    ) -> None:
        expected_states = [
            (DebtLifecycleEvent.EventType.RESERVED, "open", "reserved"),
            (DebtLifecycleEvent.EventType.REJECTED, "reserved", "open"),
        ]
        for index, (event, (event_type, previous_state, new_state)) in enumerate(
            zip(lifecycle_events, expected_states, strict=True)
        ):
            if event.event_type != event_type:
                raise CommandError(f"debt lifecycle event type mismatch: expected {event_type}, got {event.event_type}")
            if event.previous_state != previous_state or event.new_state != new_state:
                raise CommandError(
                    "debt lifecycle state mismatch: "
                    f"expected {previous_state}->{new_state}, got {event.previous_state}->{event.new_state}"
                )
            expected_actor_id = recorded_by_id if index == 0 else owner_user_id
            if event.actor_id != expected_actor_id:
                raise CommandError(
                    f"debt lifecycle actor mismatch: expected {expected_actor_id}, got {event.actor_id}"
                )
            if event.payment_id != payment.id:
                raise CommandError(
                    f"debt lifecycle payment mismatch: expected {payment.id}, got {event.payment_id}"
                )
            if event.subscription_id != subscription_id:
                raise CommandError(
                    f"debt lifecycle subscription mismatch: expected {subscription_id}, got {event.subscription_id}"
                )
            if event.debt_id_snapshot != debt.id or event.checkin_id_snapshot != debt.checkin_id:
                raise CommandError("debt lifecycle immutable snapshots do not match debt/check-in")

        rejected_event = lifecycle_events[-1]
        if rejected_event.reason != expected_reason:
            raise CommandError(
                f"debt lifecycle rejection reason mismatch: expected {expected_reason!r}, got {rejected_event.reason!r}"
            )

    def _assert_no_finance_surfaces(self, *, pnl: dict, dashboard: dict, expected_debtors: int) -> None:
        if pnl["income"] != Decimal("0"):
            raise CommandError(f"P&L income mismatch: expected 0, got {pnl['income']}")
        if pnl["salary_expenses"] != Decimal("0"):
            raise CommandError(f"P&L salary_expenses mismatch: expected 0, got {pnl['salary_expenses']}")
        if pnl["margin"] != Decimal("0"):
            raise CommandError(f"P&L margin mismatch: expected 0, got {pnl['margin']}")
        if dashboard["revenue"] != Decimal("0"):
            raise CommandError(f"dashboard revenue mismatch: expected 0, got {dashboard['revenue']}")
        if dashboard["active_subscriptions"] != 0:
            raise CommandError(
                f"dashboard active_subscriptions mismatch: expected 0, got {dashboard['active_subscriptions']}"
            )
        if dashboard["debtors"] != expected_debtors:
            raise CommandError(
                f"dashboard debtors mismatch: expected {expected_debtors}, got {dashboard['debtors']}"
            )
