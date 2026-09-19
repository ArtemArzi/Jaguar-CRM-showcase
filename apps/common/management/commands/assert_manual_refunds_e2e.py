import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.billing.models import BankPaymentOrder, Payment, PaymentRefund, SubscriptionComponent
from apps.common.management.commands.prepare_student_operations_e2e import load_owned_fixture
from apps.trainers.models import TrainerEarning, TrainerPackageAllocation


class Command(BaseCommand):
    help = "Read back exact manual refunds, entitlement and immutable money evidence."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--phase", choices=["partial", "full"], required=True)

    def handle(self, *args, **options):
        fixture = load_owned_fixture(options["fixture"])
        payment = Payment.objects.for_club(fixture["club_id"]).get(id=fixture["payment_id"])
        refunds = PaymentRefund.objects.for_club(fixture["club_id"]).filter(payment=payment)
        full = options["phase"] == "full"
        evidence = {
            "refund_count": refunds.count() == (2 if full else 1),
            "refund_sum": refunds.aggregate(total=Sum("amount"))["total"] == Decimal("8000" if full else "2000"),
            "source_links": not refunds.exclude(source="manual", order__isnull=True, refund_case__isnull=True).exists(),
            "money_unchanged": payment.amount == Decimal("8000") and payment.status == "confirmed",
            "entitlement": payment.subscription.status == ("cancelled" if full else "active"),
            "component": SubscriptionComponent.objects.for_club(fixture["club_id"])
            .get(id=fixture["component_id"])
            .is_active
            != full,
            "allocation": TrainerPackageAllocation.objects.for_club(fixture["club_id"])
            .get(subscription_id=fixture["subscription_id"])
            .is_active
            != full,
            "no_provider": not BankPaymentOrder.objects.for_club(fixture["club_id"]).exists(),
            "no_salary": not TrainerEarning.objects.for_club(fixture["club_id"]).exists(),
        }
        if not all(evidence.values()):
            raise CommandError(json.dumps({"ok": False, "checks": evidence}))
        self.stdout.write(json.dumps({"ok": True, "checks": evidence}))
