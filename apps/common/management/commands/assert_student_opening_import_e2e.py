"""Read-back acceptance of a synthetic mixed book; emits booleans only."""

import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import Checkin, TrainingGroupMembership
from apps.billing.models import (
    BankPaymentOrder,
    Expense,
    OpeningEntitlementSnapshot,
    Payment,
    PaymentRefund,
    SubscriptionComponent,
)
from apps.clubs.models import Club
from apps.clubs.timezones import club_localdate
from apps.common.management.commands.prepare_student_opening_import_e2e import load_import_fixture
from apps.dashboard.selectors import get_dashboard_metrics
from apps.students.models import OpeningImportBatch, OpeningImportItemReceipt, Student
from apps.trainers.models import (
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerPackageAllocation,
    TrainerSettlementEntry,
)
from apps.trainers.settlement_selectors import get_trainer_settlement_summary


class Command(BaseCommand):
    help = "Assert mixed import, original bases, settlement and post-import corrections."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--phase", choices=["preview", "partial", "complete", "adjusted", "replay"], required=True)

    def handle(self, *args, **options):
        data = load_import_fixture(options["fixture"])
        club = Club.objects.get(id=data["club_id"])
        phase = options["phase"]
        batches = OpeningImportBatch.objects.for_club(club).filter(source_namespace=data["namespace"])
        batch = batches.order_by("-id").first()
        payments = Payment.objects.for_club(club)
        if batch is None:
            raise CommandError("No synthetic batch")
        if phase == "preview":
            evidence = {
                "draft_only": not payments.exists() and not Student.objects.for_club(club).exists(),
                "ready": batch.items.filter(status="ready").count() == 4,
                "review": batch.items.filter(status="needs_review").count() == 1,
            }
        else:
            partial = phase == "partial"
            adjusted = phase in {"adjusted", "replay"}
            expected_students = 2 if partial else 3
            personal = OpeningEntitlementSnapshot.objects.for_club(club).get(
                source_namespace=data["namespace"], entitlement_source_key="personal-package"
            )
            group = OpeningEntitlementSnapshot.objects.for_club(club).get(
                source_namespace=data["namespace"], entitlement_source_key="group-package"
            )
            component = SubscriptionComponent.objects.for_club(club).get(subscription=personal.subscription)
            allocation = TrainerPackageAllocation.objects.for_club(club).get(subscription=personal.subscription)
            earning = TrainerEarning.objects.for_club(club).get(payment=group.payment)
            today = club_localdate(club)
            summary = get_trainer_settlement_summary(
                club=club, trainer_id=data["seller_id"], date_from=today, date_to=today
            )
            evidence = {
                "batch_state": batch.status == ("partial" if partial else "completed"),
                "domain_counts": payments.count() == Student.objects.for_club(club).count() == expected_students,
                "receipts": OpeningImportItemReceipt.objects.for_club(club).count() == (4 if partial else 5),
                "replay": phase != "replay" or batch.items.filter(status="replayed").count() == 5,
                "source_money": component.paid_amount_basis_snapshot == Decimal("12000")
                and component.unit_amount_basis_snapshot == Decimal("1000"),
                "source_counters": component.credits_used == 5
                and component.credits_total == 12
                and component.credits_left == (9 if adjusted else 7),
                "owners": personal.subscription.student.assigned_trainer_id == data["assigned_id"]
                and allocation.owner_trainer_id == data["package_owner_id"],
                "group_commission": earning.amount == Decimal("1300") and earning.trainer_id == data["seller_id"],
                "only_sale_salary": TrainerEarning.objects.for_club(club).count() == 1,
                "no_past_visits": not Checkin.objects.for_club(club).exists(),
                "membership": TrainingGroupMembership.objects.for_club(club)
                .filter(training_group_id=data["group_id"], authority="payment_owned", status="active")
                .count()
                == 1,
                "settlement": TrainerSettlementEntry.objects.for_club(club).count() == 2
                and summary["balance"] == Decimal("150" if adjusted else "800"),
                "no_implicit_credentials": not Student.objects.for_club(club)
                .exclude(user__isnull=True, parent_user__isnull=True)
                .exists(),
                "current_net_revenue": get_dashboard_metrics(club=club, date_from=today, date_to=today)["revenue"]
                == Decimal("-3250" if adjusted else "0"),
                "no_provider_or_expense": not BankPaymentOrder.objects.for_club(club).exists()
                and not Expense.objects.for_club(club).exists(),
                "refund_count": PaymentRefund.objects.for_club(club).count() == (1 if adjusted else 0),
            }
            if adjusted:
                refund = PaymentRefund.objects.for_club(club).get()
                debit = TrainerEarningAdjustment.objects.for_club(club).get(source_refund=refund)
                evidence["refund_effect"] = (
                    refund.payment_id == group.payment_id
                    and refund.amount == Decimal("3250")
                    and debit.payable_amount_delta == Decimal("-650")
                )
        if not all(evidence.values()):
            raise CommandError(json.dumps({"ok": False, "checks": evidence}))
        self.stdout.write(json.dumps({"ok": True, "checks": evidence}))
