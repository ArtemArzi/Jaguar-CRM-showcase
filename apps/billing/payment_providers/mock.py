from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.billing.models import BankPaymentOrder
from apps.billing.payment_providers.base import (
    ProviderLinkResult,
    ProviderOperationInfo,
    ProviderRetailerInfo,
    ProviderWebhook,
    validate_sbp_only_payment_modes,
)
from apps.common.exceptions import BusinessLogicError


class MockPaymentProvider:
    provider = BankPaymentOrder.Provider.MOCK

    def create_payment_link(self, *, order: BankPaymentOrder) -> ProviderLinkResult:
        payment_modes = validate_sbp_only_payment_modes(order.provider_payment_modes)
        base_url = settings.MOCK_PAYMENT_BASE_URL.rstrip("/")
        from apps.billing.service_modules.payment_returns import create_return_state

        _raw_state, return_url = create_return_state(order=order)
        payment_url = (
            f"{base_url}/mock-payments/{order.provider_payment_link_id}"
            f"?{urllib.parse.urlencode({'return_url': return_url})}"
        )
        return ProviderLinkResult(
            payment_url=payment_url,
            payment_link_id=order.provider_payment_link_id,
            provider_status="CREATED",
            payment_modes=payment_modes,
        )

    def verify_webhook(self, *, request_body: bytes, headers: Mapping[str, str]) -> ProviderWebhook:
        try:
            payload = json.loads(request_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessLogicError("Некорректный webhook оплаты", code="invalid_payment_webhook") from exc

        paid_at = payload.get("paid_at")
        parsed_paid_at = None
        if paid_at:
            parsed_paid_at = datetime.fromisoformat(str(paid_at).replace("Z", "+00:00"))
            if timezone.is_naive(parsed_paid_at):
                parsed_paid_at = timezone.make_aware(parsed_paid_at, timezone.get_current_timezone())

        amount = payload.get("amount")
        return ProviderWebhook(
            event_type=str(payload.get("webhookType") or payload.get("event_type") or "acquiringInternetPayment"),
            status=str(payload.get("status") or ""),
            payment_link_id=str(payload.get("paymentLinkId") or payload.get("payment_link_id") or ""),
            amount=Decimal(str(amount)) if amount is not None else None,
            operation_id=str(payload.get("operationId") or payload.get("operation_id") or ""),
            event_id=str(payload.get("event_id") or payload.get("eventId") or ""),
            customer_code=str(payload.get("customerCode") or payload.get("customer_code") or ""),
            merchant_id=str(payload.get("merchantId") or payload.get("merchant_id") or ""),
            paid_at=parsed_paid_at,
            metadata={
                "payment_type": payload.get("paymentType") or payload.get("payment_type") or "",
                "has_paid_at": bool(parsed_paid_at),
            },
        )

    def normalize_status(self, *, webhook: ProviderWebhook) -> str:
        return webhook.status.strip().lower()

    def get_payment_operation_info(
        self,
        *,
        order: BankPaymentOrder,
        operation_id: str,
    ) -> ProviderOperationInfo:
        """Deterministic fixture-only operation lookup.

        The mock never makes an outbound request.  Tests can exercise pending
        and failure replies by setting ``MOCK_PAYMENT_OPERATION_STATUS``;
        normal mock reconciliation is an authenticated-equivalent approval
        anchored to the persisted order timestamp, not the wall clock.
        """

        status = str(getattr(settings, "MOCK_PAYMENT_OPERATION_STATUS", "APPROVED") or "APPROVED")
        return ProviderOperationInfo(
            status=status,
            operation_id=operation_id or order.provider_operation_id,
            payment_link_id=order.provider_payment_link_id,
            payment_url=order.provider_payment_url,
            amount=order.amount_snapshot,
            customer_code=order.provider_customer_code,
            merchant_id=order.provider_merchant_id,
            paid_at=order.paid_at or order.created_at,
            payment_modes=validate_sbp_only_payment_modes(order.provider_payment_modes),
        )

    def find_payment_operation_by_link(self, *, order: BankPaymentOrder) -> ProviderOperationInfo | None:
        if getattr(settings, "MOCK_PAYMENT_OPERATION_ABSENT", False):
            return None
        return self.get_payment_operation_info(order=order, operation_id=order.provider_operation_id)

    def get_retailer_info(self) -> ProviderRetailerInfo:
        """Deterministic local fixture; production readiness never accepts mock."""
        return ProviderRetailerInfo(
            status="REG",
            is_active=True,
            merchant_id="mock-merchant",
            payment_modes=["sbp"],
            cashbox_ready=True,
            checked_at=timezone.now(),
        )
