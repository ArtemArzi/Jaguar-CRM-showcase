from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.billing.payment_providers.base import SBP_ONLY_PAYMENT_MODES


class Command(BaseCommand):
    help = "Create a small Tochka payment link for local integration smoke testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--amount",
            default="1.00",
            help="Payment amount in RUB. Default: 1.00.",
        )
        parser.add_argument(
            "--ttl",
            type=int,
            default=60,
            help="Payment link TTL in minutes. Default: 60.",
        )
        parser.add_argument(
            "--purpose",
            default="Jaguar CRM Tochka smoke payment link",
            help="Payment purpose text sent to Tochka.",
        )

    def handle(self, *args, **options):
        if not settings.TOCHKA_JWT_TOKEN:
            raise CommandError("TOCHKA_JWT_TOKEN is not configured")
        if not settings.TOCHKA_CUSTOMER_CODE:
            raise CommandError("TOCHKA_CUSTOMER_CODE is not configured")

        amount = self._parse_amount(options["amount"])
        ttl = max(1, min(44640, options["ttl"]))
        payment_link_id = f"jgr-smoke-{uuid4().hex[:8]}"
        data = {
            "amount": str(amount),
            "customerCode": settings.TOCHKA_CUSTOMER_CODE,
            "purpose": options["purpose"],
            "paymentMode": list(SBP_ONLY_PAYMENT_MODES),
            "ttl": ttl,
            "paymentLinkId": payment_link_id,
            "preAuthorization": False,
        }
        if settings.TOCHKA_MERCHANT_ID:
            data["merchantId"] = settings.TOCHKA_MERCHANT_ID

        url = settings.TOCHKA_API_BASE_URL.rstrip("/") + "/acquiring/v1.0/payments"
        self.stdout.write(
            "request "
            + json.dumps(
                {
                    "customer": bool(settings.TOCHKA_CUSTOMER_CODE),
                    "merchant": bool(settings.TOCHKA_MERCHANT_ID),
                    "paymentMode": list(SBP_ONLY_PAYMENT_MODES),
                    "ttl": ttl,
                },
                ensure_ascii=False,
            )
        )

        request = urllib.request.Request(
            url,
            data=json.dumps({"Data": data}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.TOCHKA_JWT_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.TOCHKA_REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise CommandError(f"Tochka HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise CommandError("Tochka request failed") from None

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise CommandError("Tochka returned an invalid response") from None
        data_out = payload.get("Data") if isinstance(payload.get("Data"), dict) else {}
        payment_link = (
            payload.get("paymentLink")
            or payload.get("paymentUrl")
            or payload.get("url")
            or data_out.get("paymentLink")
            or data_out.get("paymentUrl")
            or data_out.get("url")
        )
        operation_id = payload.get("operationId") or data_out.get("operationId")
        provider_status = payload.get("status") or data_out.get("status")
        result = {
            "paymentLinkCreated": bool(payment_link),
            "operationIdReturned": bool(operation_id),
            "providerStatusReturned": bool(provider_status),
        }
        self.stdout.write("result " + json.dumps(result, ensure_ascii=False))

    @staticmethod
    def _parse_amount(value: str) -> Decimal:
        try:
            amount = Decimal(value).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise CommandError("Amount must be a valid decimal value") from exc
        if amount <= 0:
            raise CommandError("Amount must be greater than zero")
        return amount
