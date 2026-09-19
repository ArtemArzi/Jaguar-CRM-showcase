from __future__ import annotations

import json
import urllib.error
from io import BytesIO, StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


def _configure_tochka(settings) -> None:
    settings.TOCHKA_API_BASE_URL = "https://tochka.invalid/uapi"
    settings.TOCHKA_JWT_TOKEN = "sensitive-test-token"
    settings.TOCHKA_CUSTOMER_CODE = "sensitive-customer-code"
    settings.TOCHKA_MERCHANT_ID = "sensitive-merchant-id"
    settings.TOCHKA_REQUEST_TIMEOUT_SECONDS = 3


def test_smoke_command_builds_sbp_only_request_and_redacts_provider_result(settings):
    _configure_tochka(settings)
    stdout = StringIO()
    response = _FakeResponse(
        {
            "Data": {
                "paymentLink": "https://sensitive.example.test/payment-link",
                "operationId": "sensitive-operation-id",
                "status": "SENSITIVE_STATUS",
            }
        }
    )

    with patch(
        "apps.common.management.commands.tochka_create_payment_link_smoke.urllib.request.urlopen",
        return_value=response,
    ) as mock_urlopen:
        call_command(
            "tochka_create_payment_link_smoke",
            amount="1.00",
            ttl=60,
            purpose="SBP-only command test",
            stdout=stdout,
        )

    request = mock_urlopen.call_args.args[0]
    request_body = json.loads(request.data.decode())
    assert request_body == {
        "Data": {
            "amount": "1.00",
            "customerCode": "sensitive-customer-code",
            "purpose": "SBP-only command test",
            "paymentMode": ["sbp"],
            "ttl": 60,
            "paymentLinkId": request_body["Data"]["paymentLinkId"],
            "preAuthorization": False,
            "merchantId": "sensitive-merchant-id",
        }
    }
    assert request.get_header("Authorization") == "Bearer sensitive-test-token"
    output = stdout.getvalue()
    assert '"paymentMode": ["sbp"]' in output
    assert '"paymentLinkCreated": true' in output
    assert '"operationIdReturned": true' in output
    assert '"providerStatusReturned": true' in output
    for sensitive_value in (
        "sensitive-test-token",
        "sensitive-customer-code",
        "sensitive-merchant-id",
        "https://sensitive.example.test/payment-link",
        "sensitive-operation-id",
        "SENSITIVE_STATUS",
        request_body["Data"]["paymentLinkId"],
    ):
        assert sensitive_value not in output


def test_smoke_command_redacts_http_error_body(settings):
    _configure_tochka(settings)
    stdout = StringIO()
    error = urllib.error.HTTPError(
        url="https://sensitive.example.test/provider",
        code=403,
        msg="SENSITIVE_HTTP_MESSAGE",
        hdrs=None,
        fp=BytesIO(b"SENSITIVE_PROVIDER_BODY"),
    )

    with (
        patch(
            "apps.common.management.commands.tochka_create_payment_link_smoke.urllib.request.urlopen",
            side_effect=error,
        ) as mock_urlopen,
        pytest.raises(CommandError) as exc_info,
    ):
        call_command("tochka_create_payment_link_smoke", stdout=stdout)

    mock_urlopen.assert_called_once()
    assert str(exc_info.value) == "Tochka HTTP 403"
    combined_output = stdout.getvalue() + str(exc_info.value)
    for sensitive_value in (
        "sensitive-test-token",
        "sensitive-customer-code",
        "sensitive-merchant-id",
        "https://sensitive.example.test/provider",
        "SENSITIVE_HTTP_MESSAGE",
        "SENSITIVE_PROVIDER_BODY",
    ):
        assert sensitive_value not in combined_output


def test_smoke_command_redacts_network_error_details(settings):
    _configure_tochka(settings)
    stdout = StringIO()

    with (
        patch(
            "apps.common.management.commands.tochka_create_payment_link_smoke.urllib.request.urlopen",
            side_effect=urllib.error.URLError("SENSITIVE_NETWORK_DETAIL"),
        ) as mock_urlopen,
        pytest.raises(CommandError) as exc_info,
    ):
        call_command("tochka_create_payment_link_smoke", stdout=stdout)

    mock_urlopen.assert_called_once()
    assert str(exc_info.value) == "Tochka request failed"
    combined_output = stdout.getvalue() + str(exc_info.value)
    assert "SENSITIVE_NETWORK_DETAIL" not in combined_output
    assert "sensitive-test-token" not in combined_output
