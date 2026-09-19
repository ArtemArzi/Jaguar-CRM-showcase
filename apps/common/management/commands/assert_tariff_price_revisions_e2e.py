"""Check persisted financial and entitlement results without printing identities."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.billing.models import (
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionRenewalEvent,
    Tariff,
    TariffPriceRevision,
)
from apps.common.management.commands.prepare_student_operations_e2e import load_owned_fixture


class Command(BaseCommand):
    help = "Assert an owned tariff revision browser fixture with aggregate evidence only."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--phase", choices=["revised", "pending", "confirmed"], required=True)

    def handle(self, *args, **options):
        fixture = load_owned_fixture(options["fixture"])
        club_id = fixture["club_id"]
        phase = options["phase"]
        source = Subscription.objects.for_club(club_id).get(id=fixture["subscription_id"])
        original_component = SubscriptionComponent.objects.for_club(club_id).get(id=fixture["component_id"])
        original_payment = Payment.objects.for_club(club_id).get(id=fixture["original_payment_id"])
        original_tariff = Tariff.objects.for_club(club_id).get(id=fixture["tariff_id"])
        first_edge = TariffPriceRevision.objects.for_club(club_id).filter(source_tariff=original_tariff).first()
        checks = {
            "original_payment_preserved": original_payment.amount == Decimal("8000")
            and original_payment.original_amount == Decimal("8000")
            and original_payment.status == "confirmed"
            and original_payment.tariff_id == original_tariff.id
            and original_payment.subscription_id == source.id,
            "original_purchase_preserved": original_component.credits_total == 8
            and original_component.paid_amount_basis_snapshot == Decimal("8000")
            and original_component.unit_amount_basis_snapshot == Decimal("1000")
            and (original_component.credits_left, original_component.credits_used) == (7, 1)
            and (source.trainings_left, source.trainings_used) == (7, 1)
            and source.expires_at.isoformat() == fixture["original_expires_at"],
            "original_tariff_archived": original_tariff.price == Decimal("8000")
            and original_tariff.name == fixture["tariff_name"]
            and not original_tariff.is_active,
            "first_revision_exists": first_edge is not None,
        }
        ids = {}
        if first_edge is not None:
            accepted_tariff = Tariff.objects.for_club(club_id).get(id=first_edge.target_tariff_id)
            ids["target_tariff_id"] = accepted_tariff.id
            checks["compatible_price_revision"] = (
                accepted_tariff.price == Decimal("8500")
                and accepted_tariff.trainings_limit == original_tariff.trainings_limit == 8
                and accepted_tariff.duration_days == original_tariff.duration_days == 30
                and accepted_tariff.trainer_payout_policy == original_tariff.trainer_payout_policy == "on_checkin"
            )
            if phase == "revised":
                checks["one_current_version"] = accepted_tariff.is_active and (
                    TariffPriceRevision.objects.for_club(club_id).count() == 1
                )
            else:
                payments = Payment.objects.for_club(club_id).filter(subscription__renewed_from=source)
                payment = payments.select_related("subscription").first()
                checks["one_exact_renewal"] = payments.count() == 1 and payment is not None
                if payment is not None:
                    ids["payment_id"] = payment.id
                    child = payment.subscription
                    checks["accepted_price_preserved"] = (
                        payment.amount == Decimal("8500")
                        and payment.original_amount == Decimal("8500")
                        and payment.tariff_id == accepted_tariff.id
                        and child.tariff_id == accepted_tariff.id
                    )
                    if phase == "pending":
                        checks["awaiting_confirmation"] = payment.status == child.status == "pending"
                        checks["no_premature_carry"] = not SubscriptionRenewalEvent.objects.for_club(club_id).exists()
                    else:
                        current_edge = TariffPriceRevision.objects.for_club(club_id).filter(
                            source_tariff=accepted_tariff,
                        ).first()
                        checks["later_price_is_separate"] = current_edge is not None and (
                            current_edge.target_tariff.price == Decimal("9000")
                            and current_edge.target_tariff.is_active
                            and not accepted_tariff.is_active
                        )
                        checks["confirmed_exact_child"] = payment.status == "confirmed" and child.status == "active"
                        checks["source_closed"] = source.status == "expired"
                        checks["expiry_carried_exactly"] = child.expires_at == (
                            datetime.fromisoformat(fixture["original_expires_at"]) + timedelta(days=30)
                        )
                        components = SubscriptionComponent.objects.for_club(club_id).filter(subscription=child)
                        component = components.first()
                        checks["credits_and_basis_carried_exactly"] = (
                            components.count() == 1
                            and component is not None
                            and component.credits_total == 8
                            and component.credits_left == child.trainings_left == 15
                            and component.credits_used == child.trainings_used == 0
                            and component.paid_amount_basis_snapshot == Decimal("8500")
                            and component.unit_amount_basis_snapshot == Decimal("1062.50")
                        )
                        checks["one_finalization_receipt"] = (
                            SubscriptionRenewalEvent.objects.for_club(club_id).filter(
                                renewed_from=source, renewed_to=child, payment=payment,
                            ).count() == 1
                        )
        if not all(checks.values()):
            raise CommandError(json.dumps({"ok": False, "checks": checks}))
        self.stdout.write(json.dumps({"ok": True, "checks": checks, **ids}))
