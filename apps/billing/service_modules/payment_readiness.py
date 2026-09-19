from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from apps.billing.models import BankPaymentOrder, PaymentProviderReadinessSnapshot
from apps.billing.payment_providers.base import ProviderRetailerInfo
from apps.common.exceptions import BusinessLogicError


@dataclass(frozen=True)
class OnlinePaymentCapability:
    enabled: bool
    reason_code: str
    payment_modes: tuple[str, ...] = ("sbp",)
    creation_enabled: bool = False
    reconciliation_enabled: bool = False
    reconciliation_available: bool = False


def get_online_payment_capability() -> OnlinePaymentCapability:
    creation_enabled = bool(getattr(settings, "ONLINE_PAYMENT_ORDER_CREATION_ENABLED", False))
    reconciliation_enabled = bool(getattr(settings, "TOCHKA_PAYMENT_RECONCILIATION_ENABLED", False))

    def unavailable(
        reason: str,
        *,
        reconciliation_available: bool = False,
    ) -> OnlinePaymentCapability:
        return OnlinePaymentCapability(
            enabled=False,
            reason_code=reason,
            creation_enabled=creation_enabled,
            reconciliation_enabled=reconciliation_enabled,
            reconciliation_available=reconciliation_available,
        )

    provider = str(getattr(settings, "PAYMENT_PROVIDER", "") or "")
    if provider == BankPaymentOrder.Provider.MOCK:
        # Mock links are an isolated test/development transport, never a
        # production readiness substitute.  Keep the capability fail-closed
        # unless all three mock seams are intentionally enabled alongside
        # Django's existing DEBUG guard used by bank-order creation.
        if not bool(getattr(settings, "DEBUG", False)):
            return unavailable("mock_provider")
        if not creation_enabled:
            return unavailable("creation_disabled")
        if not bool(getattr(settings, "MOCK_PAYMENT_ORDER_CREATION_ENABLED", False)):
            return unavailable("mock_payment_order_creation_disabled")
        if not bool(getattr(settings, "MOCK_PAYMENT_WEBHOOKS_ENABLED", False)):
            return unavailable("mock_payment_webhook_disabled")
        return OnlinePaymentCapability(
            enabled=True,
            reason_code="",
            creation_enabled=True,
            # Reconciliation remains a Tochka-only operational capability;
            # mock tests use their explicit webhook transport instead.
            reconciliation_enabled=False,
            reconciliation_available=False,
        )
    if provider != BankPaymentOrder.Provider.TOCHKA:
        return unavailable("unknown_provider")
    if not _valid_tochka_api_origin():
        return unavailable("api_origin_invalid")
    if not str(getattr(settings, "TOCHKA_JWT_TOKEN", "") or "").strip():
        return unavailable("credentials_missing")
    customer_code = str(getattr(settings, "TOCHKA_CUSTOMER_CODE", "") or "").strip()
    merchant_id = str(getattr(settings, "TOCHKA_MERCHANT_ID", "") or "").strip()
    if not customer_code or not merchant_id:
        return unavailable("merchant_identity_missing")
    if tuple(getattr(settings, "TOCHKA_PAYMENT_MODES", [])) != ("sbp",):
        return unavailable("payment_mode_invalid")
    if not _webhook_verification_ready():
        return unavailable("webhook_verification_not_ready")
    if not _valid_return_origin():
        return unavailable("return_origin_invalid")
    receipt_mode = str(getattr(settings, "TOCHKA_RECEIPT_MODE", "") or "").strip()
    if not _fiscalization_ready(receipt_mode=receipt_mode):
        return unavailable("fiscalization_undecided")

    try:
        snapshot = (
            PaymentProviderReadinessSnapshot.objects.filter(
                provider=BankPaymentOrder.Provider.TOCHKA,
                customer_code_hash=_digest(customer_code),
                merchant_id_hash=_digest(merchant_id),
            )
            .order_by("-checked_at", "-id")
            .first()
        )
    except (OperationalError, ProgrammingError):
        snapshot = None
    if snapshot is None:
        return unavailable("retailer_readback_missing")
    now = timezone.now()
    if snapshot.expires_at <= now:
        return unavailable("retailer_readback_stale")
    retailer_modes = tuple(
        sorted({str(mode).strip().lower() for mode in snapshot.payment_modes if str(mode).strip()})
    )
    if (
        snapshot.retailer_status != "REG"
        or not snapshot.is_active
        or "sbp" not in retailer_modes
        or (receipt_mode == BankPaymentOrder.ReceiptMode.TOCHKA_RECEIPT and not snapshot.cashbox_ready)
    ):
        return unavailable("retailer_not_ready")
    reconciliation_available = reconciliation_enabled
    if not creation_enabled:
        return unavailable(
            "creation_disabled",
            reconciliation_available=reconciliation_available,
        )
    if not reconciliation_enabled:
        return unavailable("reconciliation_disabled")
    return OnlinePaymentCapability(
        enabled=True,
        reason_code="",
        creation_enabled=True,
        reconciliation_enabled=True,
        reconciliation_available=True,
    )


def record_authenticated_retailer_readback(
    *,
    retailer_info: ProviderRetailerInfo,
) -> PaymentProviderReadinessSnapshot:
    """Persist only normalized readiness evidence; identifiers are stored hashed."""
    customer_code = str(getattr(settings, "TOCHKA_CUSTOMER_CODE", "") or "").strip()
    merchant_id = str(getattr(settings, "TOCHKA_MERCHANT_ID", "") or "").strip()
    if not customer_code or not merchant_id or retailer_info.merchant_id != merchant_id:
        raise BusinessLogicError(
            "Данные торговой точки не совпадают с конфигурацией",
            code="tochka_retailer_identity_mismatch",
        )
    modes = retailer_info.payment_modes
    if (
        not isinstance(modes, list)
        or not 1 <= len(modes) <= 8
        or any(not isinstance(mode, str) or not mode or len(mode) > 32 for mode in modes)
    ):
        raise BusinessLogicError(
            "Данные торговой точки некорректны",
            code="tochka_retailer_readback_invalid",
        )
    checked_at = retailer_info.checked_at or timezone.now()
    if timezone.is_naive(checked_at) or checked_at > timezone.now() + timedelta(minutes=5):
        raise BusinessLogicError(
            "Время проверки торговой точки некорректно",
            code="tochka_retailer_readback_invalid",
        )
    max_age_seconds = max(
        60,
        min(int(getattr(settings, "TOCHKA_RETAILER_READBACK_MAX_AGE_SECONDS", 3600)), 86400),
    )
    return PaymentProviderReadinessSnapshot.objects.create(
        provider=BankPaymentOrder.Provider.TOCHKA,
        customer_code_hash=_digest(customer_code),
        merchant_id_hash=_digest(merchant_id),
        retailer_status=str(retailer_info.status)[:40],
        is_active=bool(retailer_info.is_active),
        payment_modes=list(dict.fromkeys(modes)),
        cashbox_ready=bool(retailer_info.cashbox_ready),
        checked_at=checked_at,
        expires_at=checked_at + timedelta(seconds=max_age_seconds),
    )


def refresh_tochka_retailer_readback() -> PaymentProviderReadinessSnapshot:
    """Explicit operator entry point. Calling it performs one authenticated GET."""
    if str(getattr(settings, "PAYMENT_PROVIDER", "") or "") != BankPaymentOrder.Provider.TOCHKA:
        raise BusinessLogicError(
            "Провайдер Точки не выбран",
            code="tochka_provider_not_selected",
        )
    from apps.billing.payment_providers import get_payment_provider

    retailer_info = get_payment_provider(BankPaymentOrder.Provider.TOCHKA).get_retailer_info()
    return record_authenticated_retailer_readback(retailer_info=retailer_info)


def _valid_tochka_api_origin() -> bool:
    parsed = urlsplit(str(getattr(settings, "TOCHKA_API_BASE_URL", "") or "").strip())
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "enter.tochka.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/uapi"
        and not parsed.query
        and not parsed.fragment
    )


def _valid_return_origin() -> bool:
    configured = str(getattr(settings, "JAGUAR_PAYMENT_RETURN_ORIGIN", "") or "").rstrip("/")
    allowed = str(getattr(settings, "JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN", "") or "").rstrip("/")
    parsed = urlsplit(configured)
    return bool(
        configured == allowed
        and parsed.scheme == "https"
        and parsed.hostname
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _webhook_verification_ready() -> bool:
    key_mode = str(getattr(settings, "TOCHKA_WEBHOOK_KEY_MODE", "") or "").strip()
    if key_mode == "official_jwk":
        return True
    return key_mode == "pem" and bool(
        str(getattr(settings, "TOCHKA_WEBHOOK_PUBLIC_KEY", "") or "").strip()
    )


def _fiscalization_ready(*, receipt_mode: str) -> bool:
    if not bool(getattr(settings, "TOCHKA_FISCALIZATION_READY", False)):
        return False
    if not str(getattr(settings, "TOCHKA_FISCALIZATION_DECISION_ID", "") or "").strip():
        return False
    if receipt_mode == BankPaymentOrder.ReceiptMode.NONE:
        return True
    if receipt_mode != BankPaymentOrder.ReceiptMode.TOCHKA_RECEIPT:
        return False
    return all(
        str(getattr(settings, name, "") or "").strip()
        for name in (
            "TOCHKA_RECEIPT_TAX_SYSTEM_CODE",
            "TOCHKA_RECEIPT_VAT_TYPE",
            "TOCHKA_RECEIPT_PAYMENT_METHOD",
        )
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
