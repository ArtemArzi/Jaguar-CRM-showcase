from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.billing.service_modules.payment_readiness import refresh_tochka_retailer_readback
from apps.common.exceptions import BusinessLogicError


class Command(BaseCommand):
    help = (
        "Perform one authenticated Tochka Get Retailers read-back and persist "
        "only redacted readiness evidence."
    )

    def handle(self, *args, **options):
        try:
            snapshot = refresh_tochka_retailer_readback()
        except BusinessLogicError as exc:
            raise CommandError(exc.message) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "Tochka retailer readiness stored "
                f"(status={snapshot.retailer_status}, active={str(snapshot.is_active).lower()}, "
                f"checked_at={snapshot.checked_at.isoformat()})"
            )
        )
