from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.utils import timezone
from ninja.testing import TestClient

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Payment,
    PaymentRefundCase,
    ProviderWebhookDelivery,
    Subscription,
    TrainingType,
)
from apps.billing.payment_providers.tochka import (
    _JWK_CACHE_KEY,
    _JWK_MAX_BYTES,
    _JWK_REFRESH_COOLDOWN_KEY,
    _JWK_REFRESH_LOCK_KEY,
    TochkaPaymentProvider,
)
from apps.billing.services import create_bank_payment_order, process_bank_payment_webhook
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


class _FakeJwkResponse:
    def __init__(self, *, body: bytes, url: str):
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body[:size]


@pytest.fixture(autouse=True)
def _clear_tochka_jwk_cache():
    for key in (_JWK_CACHE_KEY, _JWK_REFRESH_COOLDOWN_KEY, _JWK_REFRESH_LOCK_KEY):
        cache.delete(key)
    yield
    for key in (_JWK_CACHE_KEY, _JWK_REFRESH_COOLDOWN_KEY, _JWK_REFRESH_LOCK_KEY):
        cache.delete(key)


def _private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_pem(private_key) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _public_jwk(private_key) -> dict:
    return json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))


def _signed_token(private_key, *, payload: dict, headers: dict | None = None) -> bytes:
    return jwt.encode(payload, private_key, algorithm="RS256", headers=headers).encode("ascii")


def _full_tochka_payload(**overrides) -> dict:
    payload = {
        "webhookType": "acquiringInternetPayment",
        "status": "APPROVED",
        "paymentType": "sbp",
        "paymentLinkId": "unknown-link",
        "operationId": "unknown-operation",
        "transactionId": "evt-unmatched",
        "customerCode": "test-customer",
        "merchantId": "test-merchant",
        "amount": "5000.00",
    }
    payload.update(overrides)
    return payload


def test_tochka_list_operation_without_explicit_payment_mode_fails_closed():
    provider = TochkaPaymentProvider()

    with pytest.raises(BusinessLogicError) as exc_info:
        provider._operation_info_from_data(
            {
                "status": "APPROVED",
                "operationId": "operation-1",
                "paymentLinkId": "link-1",
                "amount": "1.00",
                "customerCode": "customer-1",
                "merchantId": "merchant-1",
            }
        )

    assert exc_info.value.code == "sbp_only_payment_mode_required"


def test_tochka_get_payment_operation_info_parses_official_operation_envelope(settings):
    synthetic_token = "synthetic" + "-token"
    settings.TOCHKA_JWT_TOKEN = synthetic_token
    provider = TochkaPaymentProvider()
    paid_at = timezone.now() - timedelta(seconds=5)
    response = {
        "Data": {
            "Operation": [
                {
                    "status": "APPROVED",
                    "operationId": "operation-1",
                    "paymentLinkId": "link-1",
                    "paymentLink": "https://pay.tochka.test/link-1",
                    "amount": "1.00",
                    "customerCode": "customer-1",
                    "merchantId": "merchant-1",
                    "paidAt": paid_at.isoformat(),
                    "paymentMode": ["sbp"],
                }
            ]
        }
    }

    with patch.object(provider, "_get_json", return_value=response) as get_json:
        operation = provider.get_payment_operation_info(
            order=SimpleNamespace(),
            operation_id="operation-1",
        )

    assert get_json.call_args.kwargs == {
        "path": "/acquiring/v1.0/payments/operation-1",
        "token": synthetic_token,
    }
    assert operation.status == "APPROVED"
    assert operation.operation_id == "operation-1"
    assert operation.payment_link_id == "link-1"
    assert operation.payment_url == "https://pay.tochka.test/link-1"
    assert operation.amount == Decimal("1.00")
    assert operation.customer_code == "customer-1"
    assert operation.merchant_id == "merchant-1"
    assert operation.paid_at == paid_at
    assert operation.payment_modes == ["sbp"]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"Data": {}},
        {"Data": {"Operation": []}},
        {"Data": {"Operation": [{}, {}]}},
        {"Data": {"Operation": ["not-an-object"]}},
        {"Data": {"Operation": {"status": "APPROVED"}}},
    ],
)
def test_tochka_get_payment_operation_info_rejects_invalid_or_ambiguous_envelope(settings, response):
    settings.TOCHKA_JWT_TOKEN = "synthetic" + "-token"
    provider = TochkaPaymentProvider()

    with (
        patch.object(provider, "_get_json", return_value=response),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider.get_payment_operation_info(
            order=SimpleNamespace(),
            operation_id="operation-1",
        )

    assert exc_info.value.code == "tochka_response_invalid"


def test_tochka_get_retailers_normalizes_only_the_configured_merchant(settings):
    settings.TOCHKA_JWT_TOKEN = "synthetic-token"
    settings.TOCHKA_CUSTOMER_CODE = "customer-1"
    settings.TOCHKA_MERCHANT_ID = "merchant-1"
    provider = TochkaPaymentProvider()
    response = {
        "Data": {
            "Retailer": [
                {
                    "status": "REG",
                    "isActive": True,
                    "merchantId": "merchant-1",
                    "paymentModes": ["sbp"],
                    "cashbox": "ready-cashbox",
                }
            ]
        }
    }

    with patch.object(provider, "_get_json", return_value=response) as get_json:
        retailer = provider.get_retailer_info()

    get_json.assert_called_once_with(
        path="/acquiring/v1.0/retailers?customerCode=customer-1",
        token="synthetic" + "-token",
    )
    assert retailer.status == "REG"
    assert retailer.is_active is True
    assert retailer.merchant_id == "merchant-1"
    assert retailer.payment_modes == ["sbp"]
    assert retailer.cashbox_ready is True
    assert retailer.checked_at is not None


@pytest.mark.parametrize(
    ("retailers", "expected_code"),
    [
        (["not-an-object"], "tochka_retailer_readback_invalid"),
        (
            [
                {
                    "status": "REG",
                    "isActive": True,
                    "merchantId": "merchant-1",
                    "paymentModes": [],
                }
            ],
            "tochka_retailer_readback_invalid",
        ),
        (
            [
                {
                    "status": "REG",
                    "isActive": True,
                    "merchantId": "merchant-1",
                    "paymentModes": ["sbp"],
                },
                {
                    "status": "REG",
                    "isActive": True,
                    "merchantId": "merchant-1",
                    "paymentModes": ["sbp"],
                },
            ],
            "tochka_retailer_readback_ambiguous",
        ),
        (
            [
                {
                    "status": "REG",
                    "isActive": True,
                    "merchantId": "merchant-1",
                    "paymentModes": ["sbp"],
                    "cashbox": "false",
                }
            ],
            "tochka_retailer_readback_invalid",
        ),
    ],
)
def test_tochka_get_retailers_rejects_malformed_or_ambiguous_evidence(
    settings,
    retailers,
    expected_code,
):
    settings.TOCHKA_JWT_TOKEN = "synthetic-token"
    settings.TOCHKA_CUSTOMER_CODE = "customer-1"
    settings.TOCHKA_MERCHANT_ID = "merchant-1"
    provider = TochkaPaymentProvider()

    with (
        patch.object(provider, "_get_json", return_value={"Data": {"Retailer": retailers}}),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider.get_retailer_info()

    assert exc_info.value.code == expected_code


def test_tochka_ttl_rejects_sub_buffer_horizon_and_accepts_two_minute_boundary():
    provider = TochkaPaymentProvider()
    now = timezone.now()
    with patch("apps.billing.payment_providers.tochka.timezone.now", return_value=now):
        with pytest.raises(BusinessLogicError) as exc_info:
            provider._ttl_minutes(order=SimpleNamespace(expires_at=now + timedelta(seconds=119)))
        assert exc_info.value.code == "bank_payment_order_ttl_too_short"
        assert provider._ttl_minutes(order=SimpleNamespace(expires_at=now + timedelta(seconds=120))) == 1


def test_tochka_operation_timestamp_requires_explicit_timezone():
    with pytest.raises(ValueError, match="explicit timezone"):
        TochkaPaymentProvider._parse_datetime("2026-08-05T12:00:00")


def _configure_pem_verification(settings, private_key) -> None:
    settings.TOCHKA_WEBHOOK_KEY_MODE = "pem"
    settings.TOCHKA_WEBHOOK_PUBLIC_KEY = _public_pem(private_key)


def _tariff_for_club(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    return TariffFactory(club=club, training_type=training_type, price=Decimal("5000.00"))


def _tochka_order(*, settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TOCHKA_CUSTOMER_CODE = "test-customer"
    settings.TOCHKA_MERCHANT_ID = "test-merchant"
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=StudentFactory(club=club).id,
        tariff_id=_tariff_for_club(club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    BankPaymentOrder.objects.filter(id=order.id).update(
        provider=BankPaymentOrder.Provider.TOCHKA,
        provider_operation_id=f"tochka-operation-{order.id}",
        provider_customer_code="test-customer",
        provider_merchant_id="test-merchant",
    )
    order.refresh_from_db()
    return order


def _signed_order_webhook(private_key, order, *, status: str, transaction_id: str, **overrides):
    return _signed_token(
        private_key,
        payload=_full_tochka_payload(
            status=status,
            paymentLinkId=order.provider_payment_link_id,
            operationId=order.provider_operation_id,
            transactionId=transaction_id,
            amount=str(order.amount_snapshot),
            **overrides,
        ),
    )


@pytest.mark.django_db
def test_verified_tochka_setup_callback_is_terminal_without_financial_mutation(settings):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    delivery = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=_signed_token(
            private_key,
            payload={"webhookType": "acquiringInternetPayment", "status": "SETUP"},
        ),
        headers={},
        request_id="setup-callback",
    )

    assert isinstance(delivery, ProviderWebhookDelivery)
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE
    assert BankPaymentProviderEvent.objects.count() == 0
    assert BankPaymentOrder.objects.count() == 0
    assert Payment.objects.count() == 0
    assert Subscription.objects.count() == 0


@pytest.mark.django_db
def test_verified_unmatched_tochka_callback_is_terminal_and_idempotent(settings):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    settings.TOCHKA_CUSTOMER_CODE = "test-customer"
    settings.TOCHKA_MERCHANT_ID = "test-merchant"
    request_body = _signed_token(private_key, payload=_full_tochka_payload())

    first = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="unmatched-first\nсекрет" + ("x" * 200),
    )
    duplicate = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="unmatched-duplicate",
    )

    assert isinstance(first, ProviderWebhookDelivery)
    assert duplicate.id == first.id
    assert first.outcome == ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE
    assert first.request_id == ("unmatched-first" + ("x" * 200))[:120]
    assert ProviderWebhookDelivery.objects.count() == 1
    assert BankPaymentProviderEvent.objects.count() == 0
    assert BankPaymentOrder.objects.count() == 0
    assert Payment.objects.count() == 0
    assert Subscription.objects.count() == 0


@pytest.mark.django_db
def test_invalid_tochka_signature_creates_no_global_delivery(settings):
    configured_key = _private_key()
    signing_key = _private_key()
    _configure_pem_verification(settings, configured_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.TOCHKA,
            request_body=_signed_token(signing_key, payload=_full_tochka_payload()),
            headers={},
            request_id="invalid-signature",
        )

    assert exc_info.value.code == "tochka_webhook_invalid"
    assert ProviderWebhookDelivery.objects.count() == 0
    assert BankPaymentProviderEvent.objects.count() == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"jwk": {"kty": "RSA"}},
        {"jku": "https://attacker.example/jwk"},
        {"x5u": "https://attacker.example/certificate"},
        {"x5c": ["attacker-certificate"]},
        {"crit": ["b64"]},
    ],
)
def test_tochka_webhook_rejects_forbidden_jwt_headers(settings, headers):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(private_key, payload=_full_tochka_payload(), headers=headers),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_invalid"


def test_tochka_webhook_rejects_non_rs256_and_oversized_bodies(settings):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    hs256_token = jwt.encode(
        _full_tochka_payload(),
        "synthetic-test-secret-with-32-bytes",
        algorithm="HS256",
    ).encode("ascii")

    with pytest.raises(BusinessLogicError) as wrong_algorithm:
        TochkaPaymentProvider().verify_webhook(request_body=hs256_token, headers={})

    settings.TOCHKA_WEBHOOK_MAX_BODY_BYTES = 1024
    with pytest.raises(BusinessLogicError) as oversized_body:
        TochkaPaymentProvider().verify_webhook(request_body=b"a" * 1025, headers={})

    assert wrong_algorithm.value.code == "tochka_webhook_invalid"
    assert oversized_body.value.code == "tochka_webhook_invalid"


@pytest.mark.parametrize(
    ("field", "max_length"),
    [
        ("webhookType", 80),
        ("status", 60),
        ("paymentType", 30),
        ("paymentLinkId", 45),
        ("operationId", 120),
        ("transactionId", 120),
        ("customerCode", 120),
        ("merchantId", 120),
    ],
)
def test_tochka_webhook_rejects_over_bound_claims(settings, field, max_length):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(
                private_key,
                payload=_full_tochka_payload(**{field: "x" * (max_length + 1)}),
            ),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_invalid"


@pytest.mark.parametrize("header", [{"kid": "x" * 121}, {"typ": "x" * 21}])
def test_tochka_webhook_rejects_over_bound_header_claims(settings, header):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(private_key, payload=_full_tochka_payload(), headers=header),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_invalid"


@pytest.mark.parametrize(
    "amount",
    ["NaN", "Infinity", "0", "-0.01", "100000000.00", "1.001"],
)
def test_tochka_webhook_rejects_unsafe_amounts(settings, amount):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(
                private_key,
                payload=_full_tochka_payload(amount=amount),
            ),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_invalid"


@pytest.mark.parametrize(
    "paid_at",
    [
        lambda now: now - timedelta(days=45, seconds=1),
        lambda now: now + timedelta(minutes=5, seconds=5),
    ],
)
def test_tochka_webhook_rejects_out_of_window_timestamps(settings, paid_at):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)

    with pytest.raises(BusinessLogicError) as exc_info:
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(
                private_key,
                payload=_full_tochka_payload(paidAt=paid_at(timezone.now()).isoformat()),
            ),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_invalid"


def test_tochka_webhook_accepts_bounded_amount_and_timestamp(settings):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    paid_at = timezone.now() - timedelta(days=44, hours=23)

    webhook = TochkaPaymentProvider().verify_webhook(
        request_body=_signed_token(
            private_key,
            payload=_full_tochka_payload(amount="99999999.99", paidAt=paid_at.isoformat()),
        ),
        headers={},
    )

    assert webhook.amount == Decimal("99999999.99")
    assert webhook.paid_at == paid_at


def test_tochka_official_jwk_uses_current_cache_without_second_fetch(settings):
    private_key = _private_key()
    settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"
    provider = TochkaPaymentProvider()
    request_body = _signed_token(private_key, payload=_full_tochka_payload())

    with patch.object(provider, "_fetch_official_jwk", return_value=_public_jwk(private_key)) as fetch:
        first = provider.verify_webhook(request_body=request_body, headers={})
        second = provider.verify_webhook(request_body=request_body, headers={})

    assert first.event_id == second.event_id == "evt-unmatched"
    fetch.assert_called_once_with()


def test_tochka_official_jwk_uses_lkg_when_ordinary_refresh_fails(settings):
    private_key = _private_key()
    settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"
    settings.TOCHKA_WEBHOOK_JWK_CACHE_SECONDS = 60
    settings.TOCHKA_WEBHOOK_JWK_LKG_SECONDS = 3600
    cached_at = timezone.now() - timedelta(seconds=61)
    cache.set(
        _JWK_CACHE_KEY,
        json.dumps({"jwk": json.dumps(_public_jwk(private_key)), "fetched_at": cached_at.timestamp()}),
        timeout=3600,
    )
    provider = TochkaPaymentProvider()

    with patch.object(
        provider,
        "_fetch_official_jwk",
        side_effect=BusinessLogicError("Unavailable", code="tochka_webhook_key_unavailable"),
    ) as fetch:
        webhook = provider.verify_webhook(
            request_body=_signed_token(private_key, payload=_full_tochka_payload()),
            headers={},
        )

    assert webhook.event_id == "evt-unmatched"
    fetch.assert_called_once_with()


def test_tochka_official_jwk_forced_refresh_is_singleton_during_cooldown(settings):
    cached_key = _private_key()
    refreshed_key = _private_key()
    rejected_key = _private_key()
    settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"
    cache.set(
        _JWK_CACHE_KEY,
        json.dumps({
            "jwk": json.dumps(_public_jwk(cached_key)),
            "fetched_at": timezone.now().timestamp(),
        }),
        timeout=3600,
    )
    provider = TochkaPaymentProvider()

    with patch.object(provider, "_fetch_official_jwk", return_value=_public_jwk(refreshed_key)) as fetch:
        webhook = provider.verify_webhook(
            request_body=_signed_token(refreshed_key, payload=_full_tochka_payload()),
            headers={},
        )
        with pytest.raises(BusinessLogicError) as cooldown_error:
            provider.verify_webhook(
                request_body=_signed_token(rejected_key, payload=_full_tochka_payload()),
                headers={},
            )

    assert webhook.event_id == "evt-unmatched"
    assert cooldown_error.value.code == "tochka_webhook_key_unavailable"
    fetch.assert_called_once_with()


def test_tochka_official_jwk_rejects_private_and_small_keys(settings):
    private_key = _private_key()
    private_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key))
    small_public_jwk = _public_jwk(
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
    )

    for unsafe_jwk in (private_jwk, small_public_jwk):
        with pytest.raises(ValueError):
            TochkaPaymentProvider._validated_jwk(unsafe_jwk)


def test_tochka_official_jwk_cache_failure_is_safe_non_verification(settings):
    private_key = _private_key()
    settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"

    with (
        patch(
            "apps.billing.payment_providers.tochka.cache.get",
            side_effect=RuntimeError("sensitive-cache-detail"),
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        TochkaPaymentProvider().verify_webhook(
            request_body=_signed_token(private_key, payload=_full_tochka_payload()),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_key_unavailable"


@pytest.mark.parametrize(
    "response",
    [
        _FakeJwkResponse(body=b"{}", url="https://redirected.example.test/key"),
        _FakeJwkResponse(
            body=b"x" * (_JWK_MAX_BYTES + 1),
            url="https://enter.tochka.com/doc/openapi/static/keys/public",
        ),
    ],
)
def test_tochka_official_jwk_rejects_redirects_and_oversized_responses(response):
    opener = Mock()
    opener.open.return_value = response

    with (
        patch(
            "apps.billing.payment_providers.tochka.urllib.request.build_opener",
            return_value=opener,
        ),
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        TochkaPaymentProvider()._fetch_official_jwk()

    assert exc_info.value.code == "tochka_webhook_key_unavailable"


def test_tochka_invalid_signature_never_falls_back_after_failed_forced_refresh(settings):
    cached_key = _private_key()
    signing_key = _private_key()
    settings.TOCHKA_WEBHOOK_KEY_MODE = "official_jwk"
    cache.set(
        _JWK_CACHE_KEY,
        json.dumps({
            "jwk": json.dumps(_public_jwk(cached_key)),
            "fetched_at": timezone.now().timestamp(),
        }),
        timeout=3600,
    )
    provider = TochkaPaymentProvider()

    with (
        patch.object(
            provider,
            "_fetch_official_jwk",
            side_effect=BusinessLogicError("Unavailable", code="tochka_webhook_key_unavailable"),
        ) as fetch,
        pytest.raises(BusinessLogicError) as exc_info,
    ):
        provider.verify_webhook(
            request_body=_signed_token(signing_key, payload=_full_tochka_payload()),
            headers={},
        )

    assert exc_info.value.code == "tochka_webhook_key_unavailable"
    fetch.assert_called_once_with()


@pytest.mark.django_db
def test_mock_webhook_is_disabled_before_any_delivery_is_written(settings):
    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = False

    with pytest.raises(BusinessLogicError) as exc_info:
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=b'{"event_id":"mock-disabled"}',
            headers={},
            request_id="mock-disabled",
        )

    assert exc_info.value.code == "mock_payment_webhook_disabled"
    assert ProviderWebhookDelivery.objects.count() == 0


@pytest.mark.django_db
def test_tochka_api_returns_global_delivery_for_unmatched_callback(settings):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    settings.TOCHKA_CUSTOMER_CODE = "test-customer"
    settings.TOCHKA_MERCHANT_ID = "test-merchant"
    request_body = _signed_token(private_key, payload=_full_tochka_payload())

    response = client.post(
        "/billing/payment-provider-webhooks/tochka/",
        data=request_body,
        headers={"Content-Length": str(len(request_body)), "X-Request-ID": "api-unmatched"},
        read=Mock(return_value=request_body),
    )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": None,
        "delivery_id": ProviderWebhookDelivery.objects.get().id,
        "order_id": None,
        "processing_status": ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE,
        "provider_status": "APPROVED",
    }


def test_tochka_api_rejects_declared_oversized_body_before_reading(settings):
    settings.TOCHKA_WEBHOOK_MAX_BODY_BYTES = 1024
    read = Mock(return_value=b"not-used")

    response = client.post(
        "/billing/payment-provider-webhooks/tochka/",
        data=b"x" * 1025,
        headers={"Content-Length": "1025"},
        read=read,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_payment_webhook"
    read.assert_not_called()


@pytest.mark.django_db
def test_tochka_conflicting_identifiers_create_terminal_delivery_without_mutation(settings, club, owner_user):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TOCHKA_CUSTOMER_CODE = "test-customer"
    settings.TOCHKA_MERCHANT_ID = "test-merchant"
    first = create_bank_payment_order(
        club_id=club.id,
        student_id=StudentFactory(club=club).id,
        tariff_id=_tariff_for_club(club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    second_club = ClubFactory()
    second_owner = UserFactory()
    second = create_bank_payment_order(
        club_id=second_club.id,
        student_id=StudentFactory(club=second_club).id,
        tariff_id=_tariff_for_club(second_club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=second_owner.id,
    )
    BankPaymentOrder.objects.filter(id=first.id).update(
        provider=BankPaymentOrder.Provider.TOCHKA,
        provider_payment_link_id="link-first",
        provider_operation_id="operation-first",
    )
    BankPaymentOrder.objects.filter(id=second.id).update(
        provider=BankPaymentOrder.Provider.TOCHKA,
        provider_payment_link_id="link-second",
        provider_operation_id="operation-second",
    )
    first.refresh_from_db()
    second.refresh_from_db()

    delivery = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=_signed_token(
            private_key,
            payload=_full_tochka_payload(
                paymentLinkId=first.provider_payment_link_id,
                operationId=second.provider_operation_id,
                transactionId="evt-identifier-conflict",
            ),
        ),
        headers={},
        request_id="identifier-conflict",
    )

    first.refresh_from_db()
    second.refresh_from_db()
    assert isinstance(delivery, ProviderWebhookDelivery)
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.VERIFIED_IDENTIFIER_CONFLICT
    assert BankPaymentProviderEvent.objects.count() == 0
    assert first.status == second.status == BankPaymentOrder.Status.PENDING
    assert first.payment.status == second.payment.status == Payment.Status.PENDING


@pytest.mark.django_db
def test_terminal_non_actionable_transaction_never_mutates_on_corrected_replay(settings, club, owner_user):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TOCHKA_CUSTOMER_CODE = "test-customer"
    settings.TOCHKA_MERCHANT_ID = "test-merchant"
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=StudentFactory(club=club).id,
        tariff_id=_tariff_for_club(club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    BankPaymentOrder.objects.filter(id=order.id).update(
        provider=BankPaymentOrder.Provider.TOCHKA,
        provider_operation_id="corrected-operation",
        provider_customer_code="test-customer",
        provider_merchant_id="test-merchant",
    )
    order.refresh_from_db()
    first = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=_signed_token(
            private_key,
            payload={
                "webhookType": "acquiringInternetPayment",
                "status": "SETUP",
                "transactionId": "terminal-transaction",
            },
        ),
        headers={},
        request_id="terminal-first",
    )

    replay = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=_signed_token(
            private_key,
            payload=_full_tochka_payload(
                paymentLinkId=order.provider_payment_link_id,
                operationId=order.provider_operation_id,
                transactionId="terminal-transaction",
            ),
        ),
        headers={},
        request_id="terminal-replay",
    )

    order.refresh_from_db()
    assert replay.id == first.id
    assert first.outcome == ProviderWebhookDelivery.Outcome.VERIFIED_NON_ACTIONABLE
    assert ProviderWebhookDelivery.objects.count() == 1
    assert BankPaymentProviderEvent.objects.count() == 0
    assert order.status == BankPaymentOrder.Status.PENDING


@pytest.mark.django_db
@patch("django_q.tasks.async_task")
def test_signed_matched_tochka_approved_is_atomic_and_idempotent(
    mock_async,
    settings,
    club,
    owner_user,
):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    order = _tochka_order(settings=settings, club=club, owner_user=owner_user)
    request_body = _signed_order_webhook(
        private_key,
        order,
        status="APPROVED",
        transaction_id="signed-approved",
        paidAt=timezone.now().isoformat(),
    )

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="signed-approved",
    )
    duplicate = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="signed-approved-duplicate",
    )

    order.refresh_from_db()
    order.payment.refresh_from_db()
    delivery = ProviderWebhookDelivery.objects.get()
    assert duplicate.id == event.id
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.MATCHED
    assert delivery.provider_event_id == event.id
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert order.payment.status == Payment.Status.CONFIRMED
    assert ProviderWebhookDelivery.objects.count() == 1
    assert BankPaymentProviderEvent.objects.count() == 1
    assert mock_async.call_count == 0


@pytest.mark.django_db
def test_signed_matched_tochka_failed_updates_only_its_tenant_and_is_idempotent(
    settings,
    club,
    owner_user,
):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    order = _tochka_order(settings=settings, club=club, owner_user=owner_user)
    other_club = ClubFactory()
    other_order = _tochka_order(
        settings=settings,
        club=other_club,
        owner_user=UserFactory(),
    )
    request_body = _signed_order_webhook(
        private_key,
        order,
        status="FAILED",
        transaction_id="signed-failed",
    )

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="signed-failed",
    )
    duplicate = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=request_body,
        headers={},
        request_id="signed-failed-duplicate",
    )

    order.refresh_from_db()
    order.payment.refresh_from_db()
    other_order.refresh_from_db()
    other_order.payment.refresh_from_db()
    delivery = ProviderWebhookDelivery.objects.get()
    assert duplicate.id == event.id
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
    assert delivery.provider_event_id == event.id
    assert order.status == BankPaymentOrder.Status.FAILED
    assert order.payment.status == Payment.Status.REJECTED
    assert other_order.status == BankPaymentOrder.Status.PENDING
    assert other_order.payment.status == Payment.Status.PENDING
    assert ProviderWebhookDelivery.objects.count() == 1
    assert BankPaymentProviderEvent.objects.count() == 1


@pytest.mark.django_db
@patch("django_q.tasks.async_task")
def test_signed_matched_tochka_refund_opens_one_accounting_case(
    mock_async,
    settings,
    club,
    owner_user,
):
    private_key = _private_key()
    _configure_pem_verification(settings, private_key)
    order = _tochka_order(settings=settings, club=club, owner_user=owner_user)
    process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=_signed_order_webhook(
            private_key,
            order,
            status="APPROVED",
            transaction_id="signed-before-refund",
            paidAt=timezone.now().isoformat(),
        ),
        headers={},
        request_id="signed-before-refund",
    )
    refund_body = _signed_order_webhook(
        private_key,
        order,
        status="REFUNDED",
        transaction_id="signed-refund",
    )

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=refund_body,
        headers={},
        request_id="signed-refund",
    )
    duplicate = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.TOCHKA,
        request_body=refund_body,
        headers={},
        request_id="signed-refund-duplicate",
    )

    event.refresh_from_db()
    order.refresh_from_db()
    order.payment.refresh_from_db()
    refund_case = PaymentRefundCase.objects.for_club(club).get(provider_event=event)
    delivery = ProviderWebhookDelivery.objects.get(provider_event=event)
    assert duplicate.id == event.id
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
    assert event.failure_code == "bank_payment_refunded_requires_review"
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.MATCHED
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert order.payment.status == Payment.Status.CONFIRMED
    assert refund_case.refund_kind == PaymentRefundCase.Kind.FULL
    assert ProviderWebhookDelivery.objects.count() == 2
    assert BankPaymentProviderEvent.objects.count() == 2
    assert PaymentRefundCase.objects.for_club(club).count() == 1
    assert mock_async.call_count == 0


@pytest.mark.django_db
def test_matched_callback_links_global_delivery_to_tenant_event(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=StudentFactory(club=club).id,
        tariff_id=_tariff_for_club(club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    request_body = json.dumps({
        "webhookType": "acquiringInternetPayment",
        "event_id": "mock-matched-event",
        "status": "AUTHORIZED",
        "paymentLinkId": order.provider_payment_link_id,
        "operationId": "mock-matched-operation",
        "amount": str(order.amount_snapshot),
    }).encode()

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=request_body,
        headers={},
        request_id="matched-delivery",
    )

    delivery = ProviderWebhookDelivery.objects.get()
    assert isinstance(event, BankPaymentProviderEvent)
    assert delivery.outcome == ProviderWebhookDelivery.Outcome.MATCHED
    assert delivery.provider_event_id == event.id
    assert event.global_delivery.id == delivery.id


@pytest.mark.django_db
def test_matched_processing_failure_rolls_back_delivery_event_and_order_update(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=StudentFactory(club=club).id,
        tariff_id=_tariff_for_club(club).id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
    )
    original_provider_status = order.provider_status
    request_body = json.dumps({
        "webhookType": "acquiringInternetPayment",
        "event_id": "mock-rollback-event",
        "status": "AUTHORIZED",
        "paymentLinkId": order.provider_payment_link_id,
        "operationId": "mock-rollback-operation",
        "amount": str(order.amount_snapshot),
    }).encode()

    with (
        patch(
            "apps.billing.service_modules.provider_events._webhook_matches_order",
            side_effect=RuntimeError("synthetic processing failure"),
        ),
        pytest.raises(RuntimeError, match="synthetic processing failure"),
    ):
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=request_body,
            headers={},
            request_id="matched-rollback",
        )

    order.refresh_from_db()
    assert ProviderWebhookDelivery.objects.count() == 0
    assert BankPaymentProviderEvent.objects.count() == 0
    assert order.provider_status == original_provider_status
