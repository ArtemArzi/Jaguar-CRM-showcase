from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import BankPaymentOrder, PaymentReturnState
from apps.common.exceptions import BusinessLogicError

COOKIE_NAME = "jaguar_payment_return"
COOKIE_PATH = "/api/billing/payment-returns/"
SESSION_TTL = timedelta(minutes=30)
TERMINAL_TTL = timedelta(minutes=10)
STATE_GRACE = timedelta(hours=24)
RETENTION_AFTER_EXPIRY = timedelta(days=7)


def create_return_state(*, order: BankPaymentOrder) -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    PaymentReturnState.objects.create(
        club_id=order.club_id,
        order=order,
        state_hash=_hash(raw),
        expires_at=order.expires_at + STATE_GRACE,
    )
    return raw, build_return_url(raw)


def build_return_url(raw_state: str) -> str:
    origin = _return_origin()
    return f"{origin}/payments/return?{urlencode({'state': raw_state})}"


def exchange_return_state(
    *,
    raw_state: str,
    browser_binding: str,
    cookie_handle: str | None,
) -> tuple[str, dict] | None:
    if not _valid_opaque_token(raw_state) or not _valid_opaque_token(browser_binding):
        return None
    if cookie_handle is not None and not _valid_opaque_token(cookie_handle):
        cookie_handle = None
    now = timezone.now()
    with transaction.atomic():
        state = (
            PaymentReturnState.objects.select_for_update()
            .select_related("order")
            .filter(
                state_hash=_hash(raw_state),
                purpose="payment_return",
                version=1,
            )
            .first()
        )
        if state is None or state.expires_at <= now:
            return None
        binding_hash = _hash(browser_binding)
        handle = _session_handle(raw_state=raw_state, browser_binding=browser_binding)
        if state.consumed_at is not None:
            if state.browser_binding_hash != binding_hash:
                return None
            if _hash(handle) != state.session_handle_hash:
                return None
            if state.session_expires_at is None or state.session_expires_at <= now:
                return None
            projection = _generic_projection(state.order)
            _apply_terminal_grace(state=state, projection=projection, now=now)
            return handle, projection
        state.consumed_at = now
        state.browser_binding_hash = binding_hash
        state.session_handle_hash = _hash(handle)
        state.session_expires_at = min(now + SESSION_TTL, state.expires_at)
        projection = _generic_projection(state.order)
        _apply_terminal_grace(state=state, projection=projection, now=now, save=False)
        state.save(
            update_fields=[
                "consumed_at",
                "browser_binding_hash",
                "session_handle_hash",
                "session_expires_at",
                "terminal_grace_expires_at",
                "updated_at",
            ]
        )
        return handle, projection


def projection_for_session(*, cookie_handle: str | None) -> dict | None:
    if not cookie_handle or not _valid_opaque_token(cookie_handle):
        return None
    now = timezone.now()
    state = (
        PaymentReturnState.objects.select_related("order")
        .filter(session_handle_hash=_hash(cookie_handle), session_expires_at__gt=now)
        .order_by("-consumed_at")
        .first()
    )
    if state is None:
        return None
    projection = _generic_projection(state.order)
    _apply_terminal_grace(state=state, projection=projection, now=now)
    return projection


def clear_return_session(*, cookie_handle: str | None) -> None:
    if not cookie_handle or not _valid_opaque_token(cookie_handle):
        return
    PaymentReturnState.objects.filter(session_handle_hash=_hash(cookie_handle)).update(
        session_handle_hash="", session_expires_at=None
    )


def purge_expired_return_states(*, limit: int = 1000) -> int:
    """Delete one bounded batch after the audit/recovery retention window."""

    bounded_limit = max(1, min(int(limit), 5000))
    cutoff = timezone.now() - RETENTION_AFTER_EXPIRY
    ids = list(
        PaymentReturnState.objects.filter(expires_at__lt=cutoff)
        .order_by("expires_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )
    if not ids:
        return 0
    deleted, _ = PaymentReturnState.objects.filter(id__in=ids).delete()
    return deleted


def _generic_projection(order: BankPaymentOrder) -> dict:
    status = order.status
    if status in {"created", "pending", "authorized"}:
        status = "checking"
    if status not in {
        "checking", "approved", "manual_review", "failed", "expired", "cancelled", "refunded", "refunded_partially"
    }:
        status = "unavailable"
    return {"status": status}


def _apply_terminal_grace(*, state: PaymentReturnState, projection: dict, now, save: bool = True) -> None:
    """Start the terminal grace once and never extend it on later polling."""
    if projection["status"] in {"checking", "manual_review"} or state.terminal_grace_expires_at is not None:
        return
    state.terminal_grace_expires_at = min(now + TERMINAL_TTL, state.expires_at)
    state.session_expires_at = state.terminal_grace_expires_at
    if save:
        state.save(update_fields=["terminal_grace_expires_at", "session_expires_at", "updated_at"])


def _return_origin() -> str:
    origin = str(getattr(settings, "JAGUAR_PAYMENT_RETURN_ORIGIN", "") or "").rstrip("/")
    allowed_origin = str(
        getattr(settings, "JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN", "") or ""
    ).rstrip("/")
    parsed = urlsplit(origin)
    is_bare_origin = bool(
        parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )
    is_canonical_https = (
        is_bare_origin
        and origin == allowed_origin
        and parsed.scheme == "https"
        and parsed.port is None
    )
    is_local_mock_origin = (
        is_bare_origin
        and settings.DEBUG
        and settings.PAYMENT_PROVIDER == BankPaymentOrder.Provider.MOCK
        and settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if not is_canonical_https and not is_local_mock_origin:
        raise BusinessLogicError("Настройки возврата оплаты некорректны", code="payment_return_origin_invalid")
    return origin


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_handle(*, raw_state: str, browser_binding: str) -> str:
    material = f"payment-return-v1|{_hash(raw_state)}|{_hash(browser_binding)}".encode("ascii")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _valid_opaque_token(value: object) -> bool:
    if not isinstance(value, str) or not 32 <= len(value) <= 128:
        return False
    return all(character.isalnum() or character in "-_" for character in value)
