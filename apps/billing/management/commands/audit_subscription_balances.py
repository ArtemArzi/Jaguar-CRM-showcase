"""Read-only reconciliation; output contains IDs and counters only."""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.models import Subscription
from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings


class Command(BaseCommand):
    help = "Audit component and aggregate subscription balances without modifying history."

    def add_arguments(self, parser):
        parser.add_argument("--club-id", type=int, required=True)
        parser.add_argument("--subscription-id", type=int)

    def handle(self, *args, **options):
        subscriptions = Subscription.objects.for_club(options["club_id"]).filter(deleted_at__isnull=True)
        if options["subscription_id"]:
            subscriptions = subscriptions.filter(id=options["subscription_id"])
        checked = 0
        mismatched = 0
        for subscription_id in subscriptions.order_by("id").values_list("id", flat=True).iterator():
            with transaction.atomic():
                subscription = Subscription.objects.for_club(options["club_id"]).select_for_update().get(
                    id=subscription_id,
                )
                findings = subscription_balance_findings(subscription=subscription)
            checked += 1
            if findings:
                mismatched += 1
                self.stdout.write(json.dumps({"subscription_id": subscription_id, "findings": findings}))
        self.stdout.write(json.dumps({"club_id": options["club_id"], "checked": checked, "needs_review": mismatched}))
