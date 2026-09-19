from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment, TrainingGroupMembership
from apps.billing.models import Debt, DebtLifecycleEvent, DebtSettlementEvent, Payment, Subscription
from apps.clubs.models import ClubSettings
from apps.clubs.timezones import club_local_day_start_by_id
from apps.dashboard.selectors import get_dashboard_metrics
from apps.dashboard.services import get_pnl_report
from apps.leads.models import LeadLifecycleEvent
from apps.pipelines.models import PipelineExecution
from apps.retention.models import RetentionTask
from apps.students.models import AccountAccess, Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert owner/admin payment confirmation E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_payment_confirm_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for async sale earning before failing.",
        )
        parser.add_argument(
            "--stage",
            choices=("admission", "confirmed"),
            default="confirmed",
            help="Legacy finance-review stage to assert.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        settings_row = ClubSettings.objects.get(club_id=int(fixture["club_id"]))
        expected_protocol = fixture["expected"]["commercial_journey_protocol_version"]
        if settings_row.unified_client_journey_enabled or (
            settings_row.commercial_journey_protocol_version != expected_protocol
        ):
            raise CommandError("payment confirmation E2E fixture must remain an explicit v1 tenant")
        stage = options["stage"]
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = (
                    self._collect_admission_evidence(fixture)
                    if stage == "admission"
                    else self._collect_evidence(fixture)
                )
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"payment confirmation E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return

    def _collect_admission_evidence(self, fixture: dict) -> dict:
        retained = self._assert_retained_pending(fixture, include_checkin=False)
        return {
            "ok": True,
            "stage": "admission",
            "variants": [],
            "retained": retained,
        }

    def _runtime_variants(self, fixture: dict) -> list[dict]:
        runtime = fixture["runtime"]
        return [
            {
                "label": "adult",
                "club_id": int(fixture["club_id"]),
                "student_id": int(fixture["student_id"]),
                "payment_id": int(runtime["adult_payment_id"]),
                "checkin_id": int(runtime["adult_checkin_id"]) if runtime.get("adult_checkin_id") else None,
                "checkin_ids": [
                    int(checkin_id)
                    for checkin_id in [runtime.get("adult_checkin_id"), runtime.get("adult_second_checkin_id")]
                    if checkin_id
                ],
                "target_schedule_id": int(fixture["target_schedule_id"]),
                "target_training_group_id": int(fixture["target_group"]["training_group_id"]),
                "target_group_schedule_ids": [
                    int(fixture["target_group"]["schedule_id"]),
                    int(fixture["target_group"]["second_schedule_id"]),
                ],
                "target_start_date": date.fromisoformat(fixture["target_start_date"]),
                "trainer_user_id": int(fixture["trainer"]["user_id"]),
                "access_role": AccountAccess.Role.STUDENT,
            },
            {
                "label": "child",
                "club_id": int(fixture["club_id"]),
                "student_id": int(fixture["child"]["id"]),
                "payment_id": int(runtime["child_payment_id"]),
                "checkin_id": int(runtime["child_checkin_id"]) if runtime.get("child_checkin_id") else None,
                "checkin_ids": [
                    int(checkin_id)
                    for checkin_id in [runtime.get("child_checkin_id"), runtime.get("child_second_checkin_id")]
                    if checkin_id
                ],
                "target_schedule_id": int(fixture["target_schedule_id"]),
                "target_training_group_id": int(fixture["target_group"]["training_group_id"]),
                "target_group_schedule_ids": [
                    int(fixture["target_group"]["schedule_id"]),
                    int(fixture["target_group"]["second_schedule_id"]),
                ],
                "target_start_date": date.fromisoformat(fixture["target_start_date"]),
                "trainer_user_id": int(fixture["trainer"]["user_id"]),
                "access_role": AccountAccess.Role.PARENT,
            },
        ]

    def _payment_debt(
        self,
        *,
        club_id: int,
        student_id: int,
        payment: Payment,
        checkin_id: int,
    ) -> tuple[Debt, Checkin]:
        debts = list(
            Debt.objects.for_club(club_id)
            .select_related("checkin")
            .filter(
                student_id=student_id,
                settlement_payment_id=payment.id,
                checkin_id=checkin_id,
            )
            .order_by("id")
        )
        if len(debts) != 1 or debts[0].checkin_id is None:
            raise CommandError("payment must own exactly one pending-window debt/check-in")
        return debts[0], debts[0].checkin

    def _assert_pending_variant(self, *, include_checkin: bool, **variant) -> dict:
        payment = Payment.objects.for_club(variant["club_id"]).select_related(
            "subscription", "conversion_enrollment"
        ).get(id=variant["payment_id"])
        if payment.student_id != variant["student_id"] or payment.status != Payment.Status.PENDING:
            raise CommandError(f"{variant['label']} payment is not the expected pending admission")
        if payment.subscription is None or payment.subscription.status != Subscription.Status.PENDING:
            raise CommandError(f"{variant['label']} subscription is not pending")
        student = Student.objects.for_club(variant["club_id"]).get(id=variant["student_id"])
        enrollment = self._assert_group_enrollment(
            club_id=variant["club_id"],
            student=student,
            target_schedule_id=variant["target_schedule_id"],
            target_start_date=variant["target_start_date"],
            target_training_group_id=variant["target_training_group_id"],
            target_group_schedule_ids=variant["target_group_schedule_ids"],
        )
        if payment.conversion_enrollment_id != enrollment["id"]:
            raise CommandError(f"{variant['label']} payment does not own its enrollment")
        if LeadLifecycleEvent.objects.for_club(variant["club_id"]).filter(
            student=student, event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED
        ).count() != 1:
            raise CommandError(f"{variant['label']} lead conversion is not exactly once")
        checkin_ids: list[int] = []
        debt_ids: list[int] = []
        if include_checkin:
            if len(variant["checkin_ids"]) != 2:
                raise CommandError(f"{variant['label']} must have two pending group-weekday check-ins")
            for checkin_id in variant["checkin_ids"]:
                debt, checkin = self._payment_debt(
                    club_id=variant["club_id"],
                    student_id=variant["student_id"],
                    payment=payment,
                    checkin_id=checkin_id,
                )
                if debt.resolved_at is not None or checkin.subscription_id is not None or not checkin.is_debt:
                    raise CommandError(f"{variant['label']} pending check-in is not debt-backed")
                events = list(
                    DebtSettlementEvent.objects.for_club(variant["club_id"])
                    .filter(payment_id=payment.id, debt_id=debt.id)
                    .values_list("event_type", flat=True)
                )
                if events != [DebtSettlementEvent.EventType.RESERVED]:
                    raise CommandError(f"{variant['label']} pending debt is not exactly reserved")
                checkin_ids.append(checkin.id)
                debt_ids.append(debt.id)
        access = AccountAccess.objects.for_club(variant["club_id"]).get(
            student=student, role=variant["access_role"]
        )
        if access.issued_by_id != variant["trainer_user_id"]:
            raise CommandError(f"{variant['label']} account access provenance is invalid")
        return {
            "label": variant["label"],
            "payment_id": payment.id,
            "checkin_ids": checkin_ids,
            "debt_ids": debt_ids,
        }

    def _assert_retained_pending(self, fixture: dict, *, include_checkin: bool) -> dict:
        retained = fixture["retained"]
        runtime = fixture["runtime"]
        payment = (
            Payment.objects.for_club(fixture["club_id"])
            .select_related("subscription")
            .prefetch_related("applied_discounts")
            .get(id=int(runtime["retained_payment_id"]))
        )
        if payment.student_id != int(retained["id"]) or payment.status != Payment.Status.PENDING:
            raise CommandError("retained discounted payment is not pending")
        if payment.subscription is None or payment.subscription.status != Subscription.Status.PENDING:
            raise CommandError("retained discounted subscription is not pending")
        expected = fixture["expected"]
        student = Student.objects.for_club(fixture["club_id"]).get(id=int(retained["id"]))
        if (
            student.status != expected["retained_student_status_before_confirm"]
            or student.lead_status != expected["retained_lead_status_before_confirm"]
            or student.became_student_at is not None
            or student.user_id is not None
        ):
            raise CommandError("legacy pending payment changed the retained lead before owner review")
        if LeadLifecycleEvent.objects.for_club(fixture["club_id"]).filter(
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        ).exists():
            raise CommandError("legacy pending payment converted the retained lead before owner review")
        pending_events = list(
            LeadLifecycleEvent.objects.for_club(fixture["club_id"])
            .filter(
                student_id=student.id,
                event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
                metadata__payment_id=payment.id,
            )
            .order_by("created_at", "id")
        )
        if (
            len(pending_events) != 1
            or pending_events[0].actor_id != int(fixture["trainer"]["user_id"])
            or pending_events[0].old_lead_status != student.lead_status
            or pending_events[0].new_lead_status != student.lead_status
        ):
            raise CommandError("legacy pending payment lost its v1 snooze evidence")
        if payment.original_amount != Decimal(expected["retained_payment_original_amount"]):
            raise CommandError("retained payment original amount mismatch")
        if payment.amount != Decimal(expected["retained_payment_amount"]):
            raise CommandError("retained payment discounted amount mismatch")
        if list(payment.applied_discounts.values_list("id", flat=True)) != [int(fixture["discount_id"])]:
            raise CommandError("retained payment discount provenance mismatch")
        selected_debt = Debt.objects.for_club(fixture["club_id"]).get(id=int(retained["debt_id"]))
        if selected_debt.settlement_payment_id != payment.id or selected_debt.resolved_at is not None:
            raise CommandError("retained selected debt is not reserved to the payment")
        checkin_id = None
        if include_checkin:
            checkin_id = int(runtime["retained_checkin_id"])
            self._get_pending_window_reservation(
                club_id=int(fixture["club_id"]),
                student_id=int(retained["id"]),
                target_schedule_id=int(fixture["target_schedule_id"]),
                target_start_date=date.fromisoformat(fixture["target_start_date"]),
                payment=payment,
                checkin_id=checkin_id,
            )
        return {
            "payment_id": payment.id,
            "selected_debt_id": selected_debt.id,
            "checkin_id": checkin_id,
        }

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
            "trainer_id",
            "training_type_id",
            "student_id",
            "child",
            "retained",
            "tariff_id",
            "target_schedule_id",
            "target_start_date",
            "kiosk",
            "runtime",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        return self._collect_runtime_evidence(fixture)

    def _collect_runtime_evidence(self, fixture: dict) -> dict:
        retained_fixture = {
            **fixture,
            "student_id": fixture["retained"]["id"],
            "checkin_id": fixture["retained"]["debt_checkin_id"],
            "debt_id": fixture["retained"]["debt_id"],
            "pipeline_execution_id": fixture["retained"]["pipeline_execution_id"],
            "post_trial_task_id": fixture["retained"]["post_trial_task_id"],
            "expected": {
                **fixture["expected"],
                "payment_original_amount": fixture["expected"]["retained_payment_original_amount"],
                "payment_amount": fixture["expected"]["retained_payment_amount"],
                "sale_earning_amount": fixture["expected"]["retained_sale_earning_amount"],
                "student_status_before_confirm": fixture["expected"]["retained_student_status_before_confirm"],
                "lead_status_before_confirm": fixture["expected"]["retained_lead_status_before_confirm"],
                "subscription_trainings_left_after_confirm": fixture["expected"][
                    "retained_subscription_trainings_left_after_confirm"
                ],
                "subscription_trainings_used_after_confirm": fixture["expected"][
                    "retained_subscription_trainings_used_after_confirm"
                ],
            },
        }
        retained = self._collect_legacy_evidence(retained_fixture)
        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "variants": [],
            "retained": retained,
        }

    def _assert_confirmed_variant(self, **variant) -> dict:
        payment = Payment.objects.for_club(variant["club_id"]).select_related(
            "subscription", "conversion_enrollment"
        ).get(id=variant["payment_id"])
        if payment.student_id != variant["student_id"] or payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"{variant['label']} payment is not confirmed")
        if payment.subscription is None or payment.subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"{variant['label']} subscription is not active")
        expected_expiry = club_local_day_start_by_id(
            variant["club_id"],
            variant["target_start_date"] + timedelta(days=payment.subscription.tariff.duration_days),
        )
        if payment.subscription.expires_at != expected_expiry:
            raise CommandError(f"{variant['label']} expiry is not exactly start-anchored")
        if payment.subscription.trainings_left != 6 or payment.subscription.trainings_used != 2:
            raise CommandError(f"{variant['label']} subscription credit decrement mismatch")
        student = Student.objects.for_club(variant["club_id"]).get(id=variant["student_id"])
        enrollment = self._assert_group_enrollment(
            club_id=variant["club_id"],
            student=student,
            target_schedule_id=variant["target_schedule_id"],
            target_start_date=variant["target_start_date"],
            target_training_group_id=variant["target_training_group_id"],
            target_group_schedule_ids=variant["target_group_schedule_ids"],
        )
        if payment.conversion_enrollment_id != enrollment["id"]:
            raise CommandError(f"{variant['label']} confirmation did not reuse its enrollment")
        if LeadLifecycleEvent.objects.for_club(variant["club_id"]).filter(
            student=student, event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED
        ).count() != 1:
            raise CommandError(f"{variant['label']} conversion event is not exactly once")
        if len(variant["checkin_ids"]) != 2:
            raise CommandError(f"{variant['label']} must retain two group-weekday check-ins after confirmation")
        checkins: list[Checkin] = []
        debt_ids: list[int] = []
        for checkin_id in variant["checkin_ids"]:
            debt, checkin = self._payment_debt(
                club_id=variant["club_id"],
                student_id=variant["student_id"],
                payment=payment,
                checkin_id=checkin_id,
            )
            self._assert_debt_and_checkin(
                debt=debt,
                checkin=checkin,
                payment=payment,
                subscription=payment.subscription,
            )
            expected_events = [DebtSettlementEvent.EventType.RESERVED, DebtSettlementEvent.EventType.CONFIRMED]
            events = list(
                DebtSettlementEvent.objects.for_club(variant["club_id"])
                .filter(payment_id=payment.id, debt_id=debt.id)
                .values_list("event_type", flat=True)
            )
            if events != expected_events:
                raise CommandError(f"{variant['label']} debt settlement is not exactly reserve/confirm")
            checkins.append(checkin)
            debt_ids.append(debt.id)
        if TrainerEarning.objects.for_club(variant["club_id"]).filter(payment=payment).count() != 1:
            raise CommandError(f"{variant['label']} sale cascade is not exactly once")
        return {
            "label": variant["label"],
            "payment_id": payment.id,
            "subscription_id": payment.subscription_id,
            "enrollment_id": enrollment["id"],
            "checkin_ids": [checkin.id for checkin in checkins],
            "debt_ids": debt_ids,
        }

    def _required_runtime_checkin_id(self, variant: dict) -> int:
        if variant["checkin_id"] is None:
            raise CommandError(f"{variant['label']} runtime check-in id is missing")
        return int(variant["checkin_id"])

    def _collect_legacy_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        trainer_user_id = int(fixture["trainer"]["user_id"])
        trainer_id = int(fixture["trainer_id"])
        training_type_id = int(fixture["training_type_id"])
        checkin_id = int(fixture["checkin_id"])
        debt_id = int(fixture["debt_id"])
        student_id = int(fixture["student_id"])
        tariff_id = int(fixture["tariff_id"])
        target_schedule_id = int(fixture["target_schedule_id"])
        target_start_date = date.fromisoformat(fixture["target_start_date"])
        discount_id = int(fixture["discount_id"])
        pipeline_execution_id = int(fixture["pipeline_execution_id"])
        post_trial_task_id = int(fixture["post_trial_task_id"])
        expected = fixture["expected"]
        payments = list(
            Payment.objects.for_club(club_id)
            .select_related("subscription__tariff__training_type")
            .prefetch_related("applied_discounts")
            .filter(student_id=student_id)
            .order_by("id")
        )
        if not payments:
            raise CommandError("payment not created")
        if len(payments) != 1:
            raise CommandError(f"payment count mismatch: expected 1, got {len(payments)}")
        payment = payments[0]
        if payment.id != int(fixture["runtime"]["retained_payment_id"]):
            raise CommandError("retained payment does not match the runtime payment id")
        payment_id = payment.id
        subscription_id = payment.subscription_id
        if subscription_id is None:
            raise CommandError("payment subscription is not set")
        if payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"payment not confirmed: got {payment.status}")
        if payment.recorded_by_id != trainer_user_id:
            raise CommandError(
                f"payment recorded_by mismatch: expected {trainer_user_id}, got {payment.recorded_by_id}"
            )
        if payment.verified_by_id != owner_user_id:
            raise CommandError(
                f"payment verified_by mismatch: expected {owner_user_id}, got {payment.verified_by_id}"
            )
        if payment.verified_at is None:
            raise CommandError("payment verified_at is not set")

        expected_amount = Decimal(expected["payment_amount"])
        if payment.amount != expected_amount:
            raise CommandError(f"payment amount mismatch: expected {expected_amount}, got {payment.amount}")
        expected_original_amount = Decimal(expected["payment_original_amount"])
        if payment.original_amount != expected_original_amount:
            raise CommandError(
                "payment original_amount mismatch: "
                f"expected {expected_original_amount}, got {payment.original_amount}"
            )
        if payment.payment_method != expected["payment_method"]:
            raise CommandError(
                "payment method mismatch: "
                f"expected {expected['payment_method']}, got {payment.payment_method}"
            )
        if payment.tariff_id != tariff_id:
            raise CommandError(f"payment tariff mismatch: expected {tariff_id}, got {payment.tariff_id}")
        if payment.target_schedule_id != target_schedule_id:
            raise CommandError(
                "payment target schedule mismatch: "
                f"expected {target_schedule_id}, got {payment.target_schedule_id}"
            )
        if payment.target_start_date != target_start_date:
            raise CommandError(
                "payment target start date mismatch: "
                f"expected {target_start_date}, got {payment.target_start_date}"
            )
        applied_discount_ids = sorted(payment.applied_discounts.values_list("id", flat=True))
        if applied_discount_ids != [discount_id]:
            raise CommandError(
                f"payment discounts mismatch: expected {[discount_id]}, got {applied_discount_ids}"
            )
        self._assert_sale_snapshot(
            payment=payment,
            trainer_id=trainer_id,
            training_type_id=training_type_id,
            expected=expected,
        )

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        self._assert_subscription(
            club_id=club_id,
            target_start_date=target_start_date,
            subscription=subscription,
            payment=payment,
            expected=expected,
        )
        student = Student.objects.for_club(club_id).get(id=student_id)
        lead_conversion_evidence = self._assert_lead_conversion_side_effects(
            club_id=club_id,
            student=student,
            actor_user_id=owner_user_id,
            trainer_id=trainer_id,
            pipeline_execution_id=pipeline_execution_id,
            post_trial_task_id=post_trial_task_id,
            expected=expected,
            source="subscription_payment",
        )
        account_access_evidence = self._assert_explicit_account_access_opened(
            club_id=club_id,
            student=student,
            issued_by_id=trainer_user_id,
        )
        group_enrollment_evidence = self._assert_group_enrollment(
            club_id=club_id,
            student=student,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            target_training_group_id=int(fixture["target_group"]["training_group_id"]),
            target_group_schedule_ids=[
                int(fixture["target_group"]["schedule_id"]),
                int(fixture["target_group"]["second_schedule_id"]),
            ],
        )

        debt = Debt.objects.for_club(club_id).get(id=debt_id)
        checkin = Checkin.objects.for_club(club_id).get(id=checkin_id)
        self._assert_debt_and_checkin(
            debt=debt,
            checkin=checkin,
            payment=payment,
            subscription=subscription,
        )

        settlement_events_by_debt = {}
        lifecycle_events_by_debt = {}
        expected_settlement_events = [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.CONFIRMED,
        ]
        expected_lifecycle_events = [
            DebtLifecycleEvent.EventType.RESERVED,
            DebtLifecycleEvent.EventType.CONFIRMED,
        ]
        for reconciled_debt in (debt,):
            settlement_events = list(
                DebtSettlementEvent.objects.for_club(club_id)
                .filter(payment_id=payment_id, debt_id=reconciled_debt.id)
                .order_by("created_at", "id")
                .values_list("event_type", flat=True)
            )
            if settlement_events != expected_settlement_events:
                raise CommandError(
                    "debt settlement events mismatch: "
                    f"expected {expected_settlement_events}, got {settlement_events}"
                )
            lifecycle_events = list(
                DebtLifecycleEvent.objects.for_club(club_id)
                .filter(payment_id=payment_id, debt_id=reconciled_debt.id)
                .order_by("created_at", "id")
            )
            lifecycle_event_types = [event.event_type for event in lifecycle_events]
            if lifecycle_event_types != expected_lifecycle_events:
                raise CommandError(
                    "debt lifecycle events mismatch: "
                    f"expected {expected_lifecycle_events}, got {lifecycle_event_types}"
                )
            self._assert_lifecycle_snapshots(
                lifecycle_events=lifecycle_events,
                debt=reconciled_debt,
                payment=payment,
                subscription=subscription,
                trainer_user_id=trainer_user_id,
                owner_user_id=owner_user_id,
            )
            settlement_events_by_debt[str(reconciled_debt.id)] = settlement_events
            lifecycle_events_by_debt[str(reconciled_debt.id)] = lifecycle_event_types

        sale_earning = self._get_sale_earning(
            club_id=club_id,
            payment=payment,
            trainer_id=trainer_id,
            expected=expected,
        )
        today = timezone.localdate()
        pnl = get_pnl_report(club=payment.club, date_from=today, date_to=today)
        dashboard = get_dashboard_metrics(club=payment.club, date_from=today, date_to=today)
        self._assert_finance_surfaces(pnl=pnl, dashboard=dashboard, expected=expected)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "payment": {
                "id": payment.id,
                "status": payment.status,
                "amount": str(payment.amount),
                "original_amount": str(payment.original_amount),
                "recorded_by_id": payment.recorded_by_id,
                "verified_by_id": payment.verified_by_id,
                "verified_at": payment.verified_at.isoformat(),
                "discount_ids": applied_discount_ids,
                "sale_snapshot_provenance": payment.sale_snapshot_provenance,
                "target_schedule_id": payment.target_schedule_id,
                "target_start_date": payment.target_start_date.isoformat(),
            },
            "subscription": {
                "id": subscription.id,
                "status": subscription.status,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
                "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
            },
            "lead_conversion": lead_conversion_evidence,
            "account_access": account_access_evidence,
            "group_enrollment": group_enrollment_evidence,
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
            "debt_settlement_events": settlement_events_by_debt,
            "debt_lifecycle_events": lifecycle_events_by_debt,
            "sale_earning": {
                "id": sale_earning.id,
                "amount": str(sale_earning.amount),
                "rate_percent": str(sale_earning.rate_percent),
                "earning_source": sale_earning.earning_source,
            },
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
        }

    def _assert_lead_conversion_side_effects(
        self,
        *,
        club_id: int,
        student: Student,
        actor_user_id: int,
        trainer_id: int,
        pipeline_execution_id: int,
        post_trial_task_id: int,
        expected: dict,
        source: str,
    ) -> dict:
        expected_status = expected["student_status_after_confirm"]
        if student.status != expected_status:
            raise CommandError(f"student status mismatch: expected {expected_status}, got {student.status}")
        expected_lead_status = expected["lead_status_after_confirm"]
        if student.lead_status != expected_lead_status:
            raise CommandError(
                f"student lead_status mismatch: expected {expected_lead_status}, got {student.lead_status}"
            )
        if student.assigned_trainer_id != trainer_id:
            raise CommandError(
                f"student assigned_trainer mismatch: expected {trainer_id}, got {student.assigned_trainer_id}"
            )

        events = list(
            LeadLifecycleEvent.objects.for_club(club_id)
            .filter(student=student, event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED)
            .order_by("created_at", "id")
        )
        if len(events) != 1:
            raise CommandError(f"lead conversion event count mismatch: expected 1, got {len(events)}")
        event = events[0]
        expected_old_status = expected["lead_status_before_confirm"]
        if event.old_lead_status != expected_old_status:
            raise CommandError(
                "lead conversion old status mismatch: "
                f"expected {expected_old_status}, got {event.old_lead_status}"
            )
        if event.new_lead_status != "":
            raise CommandError(f"lead conversion new status mismatch: expected empty, got {event.new_lead_status}")
        if event.old_trainer_id != trainer_id or event.new_trainer_id != trainer_id:
            raise CommandError("lead conversion trainer snapshots do not match the assigned trainer")
        if event.actor_id != actor_user_id:
            raise CommandError(
                f"lead conversion actor mismatch: expected {actor_user_id}, got {event.actor_id}"
            )
        if event.metadata.get("source") != source:
            raise CommandError(f"lead conversion source mismatch: expected {source}")

        pipeline_execution = PipelineExecution.objects.for_club(club_id).get(id=pipeline_execution_id)
        if pipeline_execution.student_id != student.id:
            raise CommandError("pipeline execution student mismatch")
        if pipeline_execution.completed_at is not None:
            raise CommandError("pipeline execution was completed instead of cancelled")
        if pipeline_execution.cancelled_at is None:
            raise CommandError("pipeline execution was not cancelled after lead conversion")

        post_trial_task = RetentionTask.objects.for_club(club_id).get(id=post_trial_task_id)
        if post_trial_task.student_id != student.id:
            raise CommandError("post-trial task student mismatch")
        if post_trial_task.task_type != RetentionTask.TaskType.POST_TRIAL:
            raise CommandError(f"post-trial task type mismatch: got {post_trial_task.task_type}")
        expected_task_status = expected["post_trial_task_status_after_confirm"]
        if post_trial_task.status != expected_task_status:
            raise CommandError(
                f"post-trial task status mismatch: expected {expected_task_status}, got {post_trial_task.status}"
            )
        expected_task_resolution = expected["post_trial_task_resolution_after_confirm"]
        if post_trial_task.resolution != expected_task_resolution:
            raise CommandError(
                "post-trial task resolution mismatch: "
                f"expected {expected_task_resolution}, got {post_trial_task.resolution}"
            )
        if post_trial_task.resolved_at is None:
            raise CommandError("post-trial task resolved_at is not set")

        return {
            "student": {
                "id": student.id,
                "status": student.status,
                "lead_status": student.lead_status,
                "assigned_trainer_id": student.assigned_trainer_id,
            },
            "lifecycle_event": {
                "id": event.id,
                "event_type": event.event_type,
                "old_lead_status": event.old_lead_status,
                "new_lead_status": event.new_lead_status,
                "actor_id": event.actor_id,
                "old_trainer_id": event.old_trainer_id,
                "new_trainer_id": event.new_trainer_id,
            },
            "pipeline_execution": {
                "id": pipeline_execution.id,
                "completed": pipeline_execution.completed_at is not None,
                "cancelled": pipeline_execution.cancelled_at is not None,
            },
            "post_trial_task": {
                "id": post_trial_task.id,
                "task_type": post_trial_task.task_type,
                "status": post_trial_task.status,
                "resolution": post_trial_task.resolution,
                "resolved": post_trial_task.resolved_at is not None,
            },
        }

    def _assert_explicit_account_access_opened(
        self,
        *,
        club_id: int,
        student: Student,
        issued_by_id: int,
    ) -> dict:
        if student.user_id is None:
            raise CommandError("explicit account access did not link the student to a user")
        accesses = list(
            AccountAccess.objects.for_club(club_id)
            .filter(student=student, role=AccountAccess.Role.STUDENT)
            .order_by("id")
        )
        if len(accesses) != 1:
            raise CommandError(f"explicit student account access count mismatch: expected 1, got {len(accesses)}")
        access = accesses[0]
        if access.user_id != student.user_id:
            raise CommandError("explicit account access user does not match the student user")
        if access.issued_by_id != issued_by_id:
            raise CommandError("explicit account access was not issued by the payment-recording trainer")
        if access.status not in {AccountAccess.Status.OPEN, AccountAccess.Status.RESET}:
            raise CommandError(f"explicit account access status mismatch: got {access.status}")
        if not access.must_change_password or access.temporary_credential_revealed_at is None:
            raise CommandError("explicit account access is missing one-time credential evidence")
        return {
            "student_user_id": student.user_id,
            "access_id": access.id,
            "status": access.status,
            "issued_by_id": access.issued_by_id,
            "must_change_password": access.must_change_password,
            "temporary_credential_revealed": access.temporary_credential_revealed_at is not None,
        }

    def _assert_child_parent_access(
        self,
        *,
        club_id: int,
        child: dict,
        issued_by_id: int,
        expected_status: str,
    ) -> dict:
        child_id = int(child["id"])
        payment = Payment.objects.for_club(club_id).select_related("subscription").get(
            id=int(child["payment_id"])
        )
        if payment.student_id != child_id or payment.status != expected_status:
            raise CommandError("child payment cabinet state does not match the expected confirmation stage")
        if payment.subscription_id != int(child["subscription_id"]) or payment.subscription is None:
            raise CommandError("child payment subscription provenance is invalid")
        if expected_status == Payment.Status.PENDING and payment.subscription.status != Subscription.Status.PENDING:
            raise CommandError("child pending payment subscription is not pending")
        if expected_status == Payment.Status.CONFIRMED and payment.subscription.status != Subscription.Status.ACTIVE:
            raise CommandError("child confirmed payment subscription is not active")
        student = Student.objects.for_club(club_id).select_related("parent_user").get(id=child_id)
        if not student.is_child or student.parent_user_id is None:
            raise CommandError("child parent cabinet was not explicitly opened")
        access = AccountAccess.objects.for_club(club_id).get(
            student_id=child_id,
            role=AccountAccess.Role.PARENT,
        )
        if access.user_id != student.parent_user_id or access.issued_by_id != issued_by_id:
            raise CommandError("child parent account access provenance is invalid")
        return {
            "payment_id": payment.id,
            "payment_status": payment.status,
            "access_id": access.id,
            "user_id": access.user_id,
        }

    def _assert_group_enrollment(
        self,
        *,
        club_id: int,
        student: Student,
        target_schedule_id: int,
        target_start_date: date,
        target_training_group_id: int | None = None,
        target_group_schedule_ids: list[int] | None = None,
    ) -> dict:
        if target_training_group_id is not None:
            membership = TrainingGroupMembership.objects.for_club(club_id).get(
                student=student,
                training_group_id=target_training_group_id,
                ends_on__isnull=True,
            )
            if (
                membership.status != TrainingGroupMembership.Status.ACTIVE
                or membership.source != TrainingGroupMembership.Source.PAID_CONVERSION
                or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
                or membership.starts_on != target_start_date
            ):
                raise CommandError("canonical payment-owned membership state mismatch")
            projections = list(
                ScheduleEnrollment.objects.for_club(club_id)
                .filter(training_group_membership_id=membership.id, ends_on__isnull=True)
                .order_by("schedule_id", "id")
            )
            expected_schedule_ids = sorted(target_group_schedule_ids or [])
            if [projection.schedule_id for projection in projections] != expected_schedule_ids:
                raise CommandError("canonical membership projections do not cover every mapped slot")
            enrollment = next(
                (projection for projection in projections if projection.schedule_id == target_schedule_id),
                None,
            )
            if enrollment is None or enrollment.created_from != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION:
                raise CommandError("canonical payment did not own its selected slot projection")
            return {
                "id": enrollment.id,
                "membership_id": membership.id,
                "training_group_id": membership.training_group_id,
                "schedule_id": enrollment.schedule_id,
                "projection_count": len(projections),
                "status": enrollment.status,
                "created_from": enrollment.created_from,
                "starts_on": enrollment.starts_on.isoformat(),
                "ends_on": enrollment.ends_on.isoformat() if enrollment.ends_on else None,
            }
        enrollments = list(
            ScheduleEnrollment.objects.for_club(club_id)
            .filter(
                student=student,
                schedule_id=target_schedule_id,
                ends_on__isnull=True,
            )
            .order_by("id")
        )
        if len(enrollments) != 1:
            raise CommandError(
                "permanent target group enrollment count mismatch: "
                f"expected 1, got {len(enrollments)}"
            )
        enrollment = enrollments[0]
        if enrollment.status != ScheduleEnrollment.Status.ACTIVE:
            raise CommandError(
                "target group enrollment status mismatch: "
                f"expected {ScheduleEnrollment.Status.ACTIVE}, got {enrollment.status}"
            )
        if enrollment.created_from != ScheduleEnrollment.CreatedFrom.PAID_CONVERSION:
            raise CommandError(
                "target group enrollment source mismatch: "
                f"expected {ScheduleEnrollment.CreatedFrom.PAID_CONVERSION}, "
                f"got {enrollment.created_from}"
            )
        if enrollment.starts_on != target_start_date:
            raise CommandError(
                "target group enrollment start mismatch: "
                f"expected {target_start_date}, got {enrollment.starts_on}"
            )
        return {
            "id": enrollment.id,
            "schedule_id": enrollment.schedule_id,
            "status": enrollment.status,
            "created_from": enrollment.created_from,
            "starts_on": enrollment.starts_on.isoformat(),
            "ends_on": enrollment.ends_on.isoformat() if enrollment.ends_on else None,
        }

    def _assert_sale_snapshot(
        self,
        *,
        payment: Payment,
        trainer_id: int,
        training_type_id: int,
        expected: dict,
    ) -> None:
        if not payment.sale_earning_snapshot_recorded:
            raise CommandError("payment sale earning snapshot was not recorded")
        expected_pairs = {
            "sale_trainer_id_snapshot": trainer_id,
            "sale_training_type_id_snapshot": training_type_id,
            "sale_training_type_kind_snapshot": "group",
            "sale_rate_percent_snapshot": Decimal(expected["sale_rate_percent"]),
            "sale_amount_basis_snapshot": Decimal(expected["payment_amount"]),
            "sale_snapshot_provenance": Payment.SaleSnapshotProvenance.CONFIRM_TIME,
        }
        for field, value in expected_pairs.items():
            actual = getattr(payment, field)
            if actual != value:
                raise CommandError(f"payment {field} mismatch: expected {value}, got {actual}")

    def _assert_subscription(
        self,
        *,
        club_id: int,
        target_start_date: date,
        subscription: Subscription,
        payment: Payment,
        expected: dict,
    ) -> None:
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"subscription not active: got {subscription.status}")
        expected_expiry = club_local_day_start_by_id(
            club_id,
            target_start_date + timedelta(days=subscription.tariff.duration_days),
        )
        if subscription.expires_at != expected_expiry:
            raise CommandError(
                f"subscription expiry mismatch: expected {expected_expiry}, got {subscription.expires_at}"
            )
        if subscription.paid_amount != payment.amount:
            raise CommandError(
                f"subscription paid_amount mismatch: expected {payment.amount}, got {subscription.paid_amount}"
            )
        expected_left = int(expected["subscription_trainings_left_after_confirm"])
        expected_used = int(expected["subscription_trainings_used_after_confirm"])
        if subscription.trainings_left != expected_left:
            raise CommandError(
                f"subscription trainings_left mismatch: expected {expected_left}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != expected_used:
            raise CommandError(
                f"subscription trainings_used mismatch: expected {expected_used}, got {subscription.trainings_used}"
            )

    def _assert_debt_and_checkin(
        self,
        *,
        debt: Debt,
        checkin: Checkin,
        payment: Payment,
        subscription: Subscription,
    ) -> None:
        if debt.resolved_at is None:
            raise CommandError("selected debt was not resolved")
        if debt.resolution_type != "payment":
            raise CommandError(f"selected debt resolution_type mismatch: got {debt.resolution_type}")
        if debt.settlement_payment_id != payment.id:
            raise CommandError(
                f"selected debt settlement_payment mismatch: expected {payment.id}, got {debt.settlement_payment_id}"
            )
        if checkin.subscription_id != subscription.id:
            raise CommandError(
                f"debt check-in subscription mismatch: expected {subscription.id}, got {checkin.subscription_id}"
            )
        if checkin.is_debt:
            raise CommandError("debt check-in is still marked as debt")

    def _get_pending_window_reservation(
        self,
        *,
        club_id: int,
        student_id: int,
        target_schedule_id: int,
        target_start_date: date,
        payment: Payment,
        checkin_id: int | None = None,
        expected_reserved: bool = True,
    ) -> tuple[Debt, Checkin]:
        checkin_query = Checkin.objects.for_club(club_id).filter(
            student_id=student_id,
            schedule_id=target_schedule_id,
            date=target_start_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        if checkin_id is not None:
            checkin_query = checkin_query.filter(id=checkin_id)
        checkin = checkin_query.first()
        if checkin is None:
            raise CommandError("payment-owned pending-window check-in is missing")
        debt = Debt.objects.for_club(club_id).filter(checkin=checkin).first()
        if debt is None:
            raise CommandError("payment-owned pending-window debt is missing")
        if debt.required_tariff_id != payment.tariff_id:
            raise CommandError("pending-window debt required tariff does not match payment")
        if debt.tariff_price is not None:
            raise CommandError("pending-window debt tariff price must remain nullable")
        if debt.settlement_payment_id != payment.id:
            raise CommandError("pending-window debt is not reserved to the payment")
        if not expected_reserved:
            return debt, checkin
        if checkin.subscription_id is not None or not checkin.is_debt:
            raise CommandError("pending-window check-in did not remain debt-backed before confirmation")
        settlement_events = list(
            DebtSettlementEvent.objects.for_club(club_id)
            .filter(payment_id=payment.id, debt_id=debt.id)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        lifecycle_events = list(
            DebtLifecycleEvent.objects.for_club(club_id)
            .filter(payment_id=payment.id, debt_id=debt.id)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        if settlement_events != [DebtSettlementEvent.EventType.RESERVED]:
            raise CommandError("pending-window debt settlement events are not exactly reserved")
        if lifecycle_events != [DebtLifecycleEvent.EventType.RESERVED]:
            raise CommandError("pending-window debt lifecycle events are not exactly reserved")
        return debt, checkin

    def _assert_lifecycle_snapshots(
        self,
        *,
        lifecycle_events: list[DebtLifecycleEvent],
        debt: Debt,
        payment: Payment,
        subscription: Subscription,
        trainer_user_id: int,
        owner_user_id: int,
    ) -> None:
        expected_states = [
            (DebtLifecycleEvent.EventType.RESERVED, "open", "reserved", trainer_user_id),
            (
                DebtLifecycleEvent.EventType.CONFIRMED,
                "reserved",
                "resolved:payment",
                owner_user_id,
            ),
        ]
        for event, (event_type, previous_state, new_state, actor_id) in zip(
            lifecycle_events,
            expected_states,
            strict=True,
        ):
            if event.event_type != event_type:
                raise CommandError(f"debt lifecycle event type mismatch: expected {event_type}, got {event.event_type}")
            if event.previous_state != previous_state or event.new_state != new_state:
                raise CommandError(
                    "debt lifecycle state mismatch: "
                    f"expected {previous_state}->{new_state}, got {event.previous_state}->{event.new_state}"
                )
            if event.actor_id != actor_id:
                raise CommandError(
                    f"debt lifecycle actor mismatch: expected {actor_id}, got {event.actor_id}"
                )
            if event.payment_id != payment.id:
                raise CommandError(
                    f"debt lifecycle payment mismatch: expected {payment.id}, got {event.payment_id}"
                )
            if event.subscription_id != subscription.id:
                raise CommandError(
                    f"debt lifecycle subscription mismatch: expected {subscription.id}, got {event.subscription_id}"
                )
            if event.debt_id_snapshot != debt.id or event.checkin_id_snapshot != debt.checkin_id:
                raise CommandError("debt lifecycle immutable snapshots do not match debt/check-in")

    def _get_sale_earning(
        self,
        *,
        club_id: int,
        payment: Payment,
        trainer_id: int,
        expected: dict,
    ) -> TrainerEarning:
        earning = TrainerEarning.objects.for_club(club_id).filter(payment=payment, cancelled=False).first()
        if earning is None:
            raise CommandError("sale earning for confirmed payment not found")
        if earning.earning_source != TrainerEarning.Source.SALE:
            raise CommandError(f"sale earning source mismatch: got {earning.earning_source}")
        if earning.checkin_id is not None:
            raise CommandError(f"sale earning unexpectedly linked to check-in {earning.checkin_id}")
        if earning.trainer_id != trainer_id:
            raise CommandError(f"sale earning trainer mismatch: expected {trainer_id}, got {earning.trainer_id}")
        expected_amount = Decimal(expected["sale_earning_amount"])
        expected_rate = Decimal(expected["sale_rate_percent"])
        expected_basis = Decimal(expected["payment_amount"])
        if earning.amount != expected_amount:
            raise CommandError(f"sale earning amount mismatch: expected {expected_amount}, got {earning.amount}")
        if earning.rate_percent != expected_rate:
            raise CommandError(f"sale earning rate mismatch: expected {expected_rate}, got {earning.rate_percent}")
        if earning.subscription_price != expected_basis:
            raise CommandError(
                f"sale earning subscription_price mismatch: expected {expected_basis}, got {earning.subscription_price}"
            )
        return earning

    def _assert_finance_surfaces(self, *, pnl: dict, dashboard: dict, expected: dict) -> None:
        expected_income = Decimal(expected.get("combined_income", expected["payment_amount"]))
        expected_salary = Decimal(expected.get("combined_salary_expenses", expected["sale_earning_amount"]))
        if pnl["income"] != expected_income:
            raise CommandError(f"P&L income mismatch: expected {expected_income}, got {pnl['income']}")
        if pnl["salary_expenses"] != expected_salary:
            raise CommandError(
                f"P&L salary_expenses mismatch: expected {expected_salary}, got {pnl['salary_expenses']}"
            )
        if pnl["margin"] != expected_income - expected_salary:
            raise CommandError(
                f"P&L margin mismatch: expected {expected_income - expected_salary}, got {pnl['margin']}"
            )
        if dashboard["revenue"] != expected_income:
            raise CommandError(f"dashboard revenue mismatch: expected {expected_income}, got {dashboard['revenue']}")
        if dashboard["debtors"] != 0:
            raise CommandError(f"dashboard debtors mismatch: expected 0, got {dashboard['debtors']}")
