from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import (
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
)
from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
)
from apps.clubs.models import Club
from apps.dashboard.services import get_pnl_report
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment


class Command(BaseCommand):
    help = "Assert provider refund resolution accounting, entitlement, debt, and payroll state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_refund_resolution_e2e.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        evidence = self._build_evidence(fixture)
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
            "full",
            "partial",
            "target_group",
            "mixed",
            "mixed_legacy",
            "payroll",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _build_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        full = self._refund_for_case(
            club_id=club_id,
            case_id=int(fixture["full"]["refund_case_id"]),
            label="full",
        )
        partial = self._refund_for_case(
            club_id=club_id,
            case_id=int(fixture["partial"]["refund_case_id"]),
            label="partial",
        )
        mixed = self._refund_for_case(
            club_id=club_id,
            case_id=int(fixture["mixed"]["refund_case_id"]),
            label="mixed",
        )
        self._assert_common_refund_state(club_id=club_id, refund=full)
        self._assert_common_refund_state(club_id=club_id, refund=partial)
        self._assert_common_refund_state(club_id=club_id, refund=mixed)

        full_evidence = self._assert_full_refund(fixture=fixture, refund=full)
        partial_evidence = self._assert_partial_refund(fixture=fixture, refund=partial)
        mixed_evidence = self._assert_mixed_legacy_refund(fixture=fixture, refund=mixed)
        payroll_evidence = self._assert_payroll(fixture=fixture, refund=partial)
        pnl_evidence = self._assert_pnl(fixture=fixture)
        if PaymentRefund.objects.for_club(club_id).count() != 3:
            raise CommandError("refund row count mismatch")
        if PaymentRefundCase.objects.for_club(club_id).exclude(
            status=PaymentRefundCase.Status.RESOLVED,
        ).exists():
            raise CommandError("open refund case remained after owner resolution")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "full": full_evidence,
            "partial": partial_evidence,
            "mixed": mixed_evidence,
            "payroll": payroll_evidence,
            "pnl": pnl_evidence,
        }

    def _refund_for_case(self, *, club_id: int, case_id: int, label: str) -> PaymentRefund:
        refund = (
            PaymentRefund.objects.for_club(club_id)
            .select_related("refund_case", "order", "payment", "subscription")
            .filter(refund_case_id=case_id)
            .first()
        )
        if refund is None:
            raise CommandError(f"{label} refund not posted")
        return refund

    def _assert_common_refund_state(self, *, club_id: int, refund: PaymentRefund) -> None:
        if refund.refund_case.status != PaymentRefundCase.Status.RESOLVED:
            raise CommandError(f"refund case {refund.refund_case_id} is not resolved")
        if refund.payment.status != Payment.Status.CONFIRMED:
            raise CommandError(f"refund payment {refund.payment_id} history was rewritten")
        expected_order_status = (
            BankPaymentOrder.Status.REFUNDED
            if refund.refund_kind == PaymentRefund.Kind.FULL
            else BankPaymentOrder.Status.REFUNDED_PARTIALLY
        )
        if refund.order.status != expected_order_status:
            raise CommandError(
                f"refund order status mismatch: expected {expected_order_status}, got {refund.order.status}"
            )
        if refund.club_id != club_id:
            raise CommandError("refund tenant mismatch")

    def _assert_full_refund(self, *, fixture: dict, refund: PaymentRefund) -> dict:
        payload = fixture["full"]
        refund.subscription.refresh_from_db()
        if refund.amount != Decimal(payload["amount"]):
            raise CommandError("full refund amount mismatch")
        if refund.entitlement_disposition != PaymentRefund.EntitlementDisposition.REVOKE_REMAINING:
            raise CommandError("full refund entitlement was not explicitly revoked")
        if refund.subscription.status != Subscription.Status.CANCELLED:
            raise CommandError("full refund subscription remains usable")
        if SubscriptionComponent.objects.for_club(refund.club_id).filter(
            subscription_id=refund.subscription_id,
            is_active=True,
        ).exists():
            raise CommandError("full refund left an active subscription component")
        enrollment = ScheduleEnrollment.objects.for_club(refund.club_id).get(
            id=int(payload["conversion_enrollment_id"]),
        )
        if enrollment.status != ScheduleEnrollment.Status.CANCELLED:
            raise CommandError("full refund left payment-created enrollment active")
        membership = TrainingGroupMembership.objects.for_club(refund.club_id).get(
            id=int(payload["conversion_group_membership_id"]),
        )
        if (
            membership.status != TrainingGroupMembership.Status.CANCELLED
            or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
        ):
            raise CommandError("full refund did not close only the payment-owned group membership")
        projection_statuses = list(
            ScheduleEnrollment.objects.for_club(refund.club_id)
            .filter(training_group_membership_id=membership.id)
            .order_by("schedule_id")
            .values_list("schedule_id", "status", "created_from")
        )
        expected_schedule_ids = sorted(int(value) for value in fixture["target_group"]["schedule_ids"])
        if (
            [schedule_id for schedule_id, _, _ in projection_statuses] != expected_schedule_ids
            or any(status != ScheduleEnrollment.Status.CANCELLED for _, status, _ in projection_statuses)
            or any(source != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION for _, _, source in projection_statuses)
        ):
            raise CommandError("full refund did not close every payment-owned group projection")
        return {
            "refund_id": refund.id,
            "subscription_status": refund.subscription.status,
            "entitlement_disposition": refund.entitlement_disposition,
            "enrollment_status": enrollment.status,
            "membership_status": membership.status,
            "projection_count": len(projection_statuses),
        }

    def _assert_partial_refund(self, *, fixture: dict, refund: PaymentRefund) -> dict:
        payload = fixture["partial"]
        refund.subscription.refresh_from_db()
        debt = Debt.objects.for_club(refund.club_id).get(id=int(payload["settled_debt_id"]))
        debt_remains_settled = (
            debt.resolved_at is not None
            and debt.settlement_payment_id == refund.payment_id
            and debt.resolution_type == "payment"
        )
        if refund.amount != Decimal(payload["refund_amount"]):
            raise CommandError("partial refund amount mismatch")
        if refund.entitlement_disposition != PaymentRefund.EntitlementDisposition.KEPT_PARTIAL:
            raise CommandError("partial refund entitlement disposition mismatch")
        if refund.subscription.status != Subscription.Status.ACTIVE:
            raise CommandError("partial refund unexpectedly revoked entitlement")
        if not SubscriptionComponent.objects.for_club(refund.club_id).filter(
            subscription_id=refund.subscription_id,
            is_active=True,
        ).exists():
            raise CommandError("partial refund deactivated subscription components")
        if not debt_remains_settled:
            raise CommandError("partial refund reopened or detached a settled debt")
        if refund.settled_debt_disposition != PaymentRefund.SettledDebtDisposition.ABSORBED:
            raise CommandError("partial refund did not record absorbed settled debt")
        membership = TrainingGroupMembership.objects.for_club(refund.club_id).get(
            id=int(payload["conversion_group_membership_id"]),
        )
        if (
            membership.status != TrainingGroupMembership.Status.ACTIVE
            or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
        ):
            raise CommandError("partial refund unexpectedly closed payment-owned group authority")
        projection_count = ScheduleEnrollment.objects.for_club(refund.club_id).filter(
            training_group_membership_id=membership.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        ).count()
        if projection_count != len(fixture["target_group"]["schedule_ids"]):
            raise CommandError("partial refund did not preserve every payment-owned group projection")
        return {
            "refund_id": refund.id,
            "subscription_status": refund.subscription.status,
            "entitlement_disposition": refund.entitlement_disposition,
            "debt_remains_settled": debt_remains_settled,
            "membership_status": membership.status,
            "projection_count": projection_count,
        }

    def _assert_mixed_legacy_refund(self, *, fixture: dict, refund: PaymentRefund) -> dict:
        legacy = fixture["mixed_legacy"]
        membership = TrainingGroupMembership.objects.for_club(fixture["club_id"]).get(
            id=int(legacy["membership_id"]),
            student_id=int(legacy["student_id"]),
            training_group_id=int(fixture["target_group"]["id"]),
        )
        if (
            membership.status != TrainingGroupMembership.Status.ACTIVE
            or membership.authority != TrainingGroupMembership.Authority.INDEPENDENT
        ):
            raise CommandError("legacy mixed payment refund mutated independent group authority")
        manual = ScheduleEnrollment.objects.for_club(fixture["club_id"]).get(
            id=int(legacy["manual_enrollment_id"]),
        )
        if (
            manual.student_id != membership.student_id
            or manual.schedule_id != int(legacy["manual_schedule_id"])
            or manual.status != ScheduleEnrollment.Status.ACTIVE
            or manual.created_from != ScheduleEnrollment.CreatedFrom.MANUAL
            or manual.training_group_membership_id != membership.id
        ):
            raise CommandError("legacy manual source provenance or active slot changed after refund")
        target_schedule_ids = sorted(int(value) for value in fixture["target_group"]["schedule_ids"])
        paid_source = ScheduleEnrollment.objects.for_club(fixture["club_id"]).get(
            id=int(fixture["mixed"]["conversion_enrollment_id"]),
        )
        if (
            paid_source.student_id != membership.student_id
            or paid_source.schedule_id not in target_schedule_ids
            or paid_source.status != ScheduleEnrollment.Status.ACTIVE
            or paid_source.created_from != ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
            or paid_source.training_group_membership_id != membership.id
        ):
            raise CommandError("legacy paid source provenance or active slot changed after refund")
        linked_source_ids = set(
            TrainingGroupMembershipEvent.objects.for_club(fixture["club_id"])
            .filter(
                membership_id=membership.id,
                action__in=["backfilled", "source_linked"],
                source_enrollment_id__in=[manual.id, paid_source.id],
            )
            .values_list("source_enrollment_id", flat=True)
        )
        if linked_source_ids != {manual.id, paid_source.id}:
            raise CommandError("legacy source rows are not both linked to the independent membership")
        linked_source_schedule_ids = sorted({manual.schedule_id, paid_source.schedule_id})
        if linked_source_schedule_ids != target_schedule_ids:
            raise CommandError("legacy source rows do not cover the exact mapped group slots")

        expected_projection_schedule_ids = [
            schedule_id for schedule_id in target_schedule_ids if schedule_id not in linked_source_schedule_ids
        ]
        active_projections = list(
            ScheduleEnrollment.objects.for_club(fixture["club_id"])
            .filter(
                training_group_membership_id=membership.id,
                status=ScheduleEnrollment.Status.ACTIVE,
                created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
            .order_by("schedule_id", "id")
            .values_list("schedule_id", flat=True)
        )
        if active_projections != expected_projection_schedule_ids:
            raise CommandError(
                "legacy mixed refund group projection coverage mismatch: "
                f"expected={expected_projection_schedule_ids}, got={active_projections}"
            )
        covered_schedule_ids = sorted({*linked_source_schedule_ids, *active_projections})
        if covered_schedule_ids != target_schedule_ids:
            raise CommandError("legacy mixed refund did not preserve complete mapped-slot coverage")

        payment = refund.payment
        if (
            payment.target_training_group_id != membership.training_group_id
            or payment.target_group_membership_id != membership.id
            or payment.conversion_group_membership_id is not None
            or payment.group_membership_action_snapshot not in (None, "")
        ):
            raise CommandError("legacy payment refund adopted independent membership ownership")
        if refund.entitlement_disposition != PaymentRefund.EntitlementDisposition.REVOKE_REMAINING:
            raise CommandError("mixed legacy refund did not retain its explicit refund disposition")
        roster = resolve_expected_roster_by_schedule_date(
            club=Club.objects.get(id=fixture["club_id"]),
            schedule_ids=target_schedule_ids,
            target_date=membership.starts_on,
        )
        roster_membership_schedule_ids = []
        for schedule_id in target_schedule_ids:
            entry = roster.get(schedule_id, {}).get(membership.student_id)
            if (
                entry is None
                or entry.source != "training_group_membership"
                or entry.membership_id != membership.id
                or entry.enrollment_status != TrainingGroupMembership.Status.ACTIVE
            ):
                raise CommandError("independent membership no longer resolves eligible on every mapped slot")
            roster_membership_schedule_ids.append(schedule_id)
        return {
            "membership_id": membership.id,
            "status": membership.status,
            "authority": membership.authority,
            "manual_enrollment_id": manual.id,
            "manual_schedule_id": manual.schedule_id,
            "paid_enrollment_id": paid_source.id,
            "paid_schedule_id": paid_source.schedule_id,
            "group_projection_schedule_ids": active_projections,
            "covered_schedule_ids": covered_schedule_ids,
            "roster_membership_schedule_ids": roster_membership_schedule_ids,
            "conversion_group_membership_id": payment.conversion_group_membership_id,
        }

    def _assert_payroll(self, *, fixture: dict, refund: PaymentRefund) -> dict:
        expected_date = date.fromisoformat(fixture["payroll"]["open_date"])
        if refund.status != PaymentRefund.Status.COMPLETED:
            raise CommandError("partial refund payroll action is not completed")
        if refund.payroll_effective_date != expected_date:
            raise CommandError("partial refund payroll effective date mismatch")
        adjustments = list(
            TrainerEarningAdjustment.objects.for_club(refund.club_id).filter(
                source_refund=refund,
                kind=TrainerEarningAdjustment.Kind.REFUND,
            )
        )
        if len(adjustments) != 1:
            raise CommandError(
                f"refund payroll adjustment count mismatch: expected 1, got {len(adjustments)}"
            )
        adjustment = adjustments[0]
        earning = TrainerEarning.objects.for_club(refund.club_id).get(
            id=int(fixture["partial"]["sale_earning_id"]),
        )
        if adjustment.source_earning_id != earning.id:
            raise CommandError("refund payroll adjustment source earning mismatch")
        if adjustment.payable_amount_delta != Decimal(fixture["expected"]["refund_adjustment"]):
            raise CommandError("refund payroll adjustment amount mismatch")
        if adjustment.effective_date != expected_date:
            raise CommandError("refund payroll adjustment date mismatch")
        if earning.amount != Decimal("1000.00") or earning.cancelled:
            raise CommandError("original sale earning was rewritten")
        return {
            "refund_adjustment_count": len(adjustments),
            "adjustment_id": adjustment.id,
            "effective_date": adjustment.effective_date.isoformat(),
            "payable_amount_delta": str(adjustment.payable_amount_delta),
        }

    def _assert_pnl(self, *, fixture: dict) -> dict:
        club = Club.objects.get(id=fixture["club_id"])
        accounting_date = date.fromisoformat(fixture["payroll"]["closed_date"])
        report = get_pnl_report(
            club=club,
            date_from=accounting_date,
            date_to=accounting_date,
        )
        evidence = {
            "gross": format(report["gross_income"], ".2f"),
            "refunded": format(report["refunded_income"], ".2f"),
            "net": format(report["income"], ".2f"),
        }
        expected = {
            "gross": "15000.00",
            "refunded": "11000.00",
            "net": "4000.00",
        }
        if evidence != expected:
            raise CommandError(f"refund P&L mismatch: expected {expected}, got {evidence}")
        return evidence
