"""Reuse an owned synthetic purchase for the tariff price revision browser pack."""

from django.db import transaction

from apps.billing.models import Payment, Subscription
from apps.common.management.commands.prepare_student_operations_e2e import Command as StudentOperationsCommand


class Command(StudentOperationsCommand):
    help = "Prepare disposable tariff price revision and renewal browser evidence."

    @transaction.atomic
    def create_fixture(self):
        fixture = super().create_fixture()
        source = (
            Subscription.objects.for_club(fixture["club_id"])
            .select_related("tariff")
            .get(id=fixture["subscription_id"])
        )
        payment = Payment.objects.for_club(fixture["club_id"]).get(subscription=source)
        fixture.update(
            tariff_id=source.tariff_id,
            tariff_name=source.tariff.name,
            original_payment_id=payment.id,
            original_expires_at=source.expires_at.isoformat(),
        )
        return fixture
