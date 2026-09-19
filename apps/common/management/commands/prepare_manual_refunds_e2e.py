"""Reuse the guarded synthetic student fixture with an exact refund target."""

from apps.billing.models import Payment
from apps.common.management.commands.prepare_student_operations_e2e import Command as StudentCommand


class Command(StudentCommand):
    help = "Prepare an isolated manual-refund browser fixture."

    def create_fixture(self):
        fixture = super().create_fixture()
        payment = Payment.objects.for_club(fixture["club_id"]).get(subscription_id=fixture["subscription_id"])
        fixture["payment_id"] = payment.id
        return fixture
