from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from django.conf import settings

from apps.billing.models import BankPaymentOrder
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)

SBP_ONLY_PAYMENT_MODES = ("sbp",)


def validate_sbp_only_payment_modes(payment_modes: object) -> list[str]:
    if (
        not isinstance(payment_modes, (list, tuple))
        or tuple(payment_modes) != SBP_ONLY_PAYMENT_MODES
    ):
        raise BusinessLogicError(
            "Разрешена только оплата через СБП",
            code="sbp_only_payment_mode_required",
        )
    return list(SBP_ONLY_PAYMENT_MODES)


def _warn_online_payment_capability_unavailable(*, reason: str) -> None:
    logger.warning(
        "online_payment_capability_unavailable",
        extra={"payment_capability_reason": reason},
    )


def online_payments_enabled() -> bool:
    """Compatibility wrapper around the typed, fail-closed readiness contract."""

    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    capability = get_online_payment_capability()
    if not capability.enabled:
        _warn_online_payment_capability_unavailable(reason=capability.reason_code)
    return capability.enabled


@dataclass(frozen=True)
class ProviderLinkResult:
    payment_url: str
    payment_link_id: str
    provider_status: str = "CREATED"
    operation_id: str = ""
    customer_code: str = ""
    merchant_id: str = ""
    payment_modes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderWebhook:
    event_type: str
    status: str
    payment_link_id: str
    amount: Decimal | None = None
    operation_id: str = ""
    event_id: str = ""
    customer_code: str = ""
    merchant_id: str = ""
    paid_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderOperationInfo:
    """Authenticated, normalized evidence returned by a provider operation lookup.

    A browser redirect is deliberately not represented here.  The reconciler
    accepts this value only after the adapter has authenticated and bounded the
    provider response.
    """

    status: str
    operation_id: str
    payment_link_id: str
    payment_url: str = ""
    amount: Decimal | None = None
    customer_code: str = ""
    merchant_id: str = ""
    paid_at: datetime | None = None
    payment_modes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderRetailerInfo:
    """Normalized result of the authenticated Get Retailers readiness read-back."""

    status: str
    is_active: bool
    merchant_id: str
    payment_modes: list[str] = field(default_factory=list)
    cashbox_ready: bool = False
    checked_at: datetime | None = None


class PaymentProvider(Protocol):
    provider: str

    def create_payment_link(self, *, order: BankPaymentOrder) -> ProviderLinkResult:
        ...

    def verify_webhook(self, *, request_body: bytes, headers: Mapping[str, str]) -> ProviderWebhook:
        ...

    def normalize_status(self, *, webhook: ProviderWebhook) -> str:
        ...

    def get_payment_operation_info(
        self,
        *,
        order: BankPaymentOrder,
        operation_id: str,
    ) -> ProviderOperationInfo:
        ...

    def find_payment_operation_by_link(self, *, order: BankPaymentOrder) -> ProviderOperationInfo | None:
        ...

    def get_retailer_info(self) -> ProviderRetailerInfo:
        ...


def get_payment_provider(provider: str | None = None) -> PaymentProvider:
    provider_name = provider or settings.PAYMENT_PROVIDER
    if provider_name == BankPaymentOrder.Provider.MOCK:
        from apps.billing.payment_providers.mock import MockPaymentProvider

        return MockPaymentProvider()
    if provider_name == BankPaymentOrder.Provider.TOCHKA:
        from apps.billing.payment_providers.tochka import TochkaPaymentProvider

        return TochkaPaymentProvider()
    raise BusinessLogicError("Некорректный провайдер оплаты", code="invalid_payment_provider")
