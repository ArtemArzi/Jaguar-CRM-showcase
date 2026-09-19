from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.common.logging import (
    JsonLogFormatter,
    SafeTextFormatter,
    clear_log_context,
    get_log_context,
    hash_for_log,
    set_log_context,
)
from apps.common.middleware import RequestLogMiddleware, TenantMiddleware


def test_json_formatter_redacts_sensitive_values_and_includes_context():
    token = set_log_context(request_id="req-123", club_id=7)
    try:
        record = logging.LogRecord(
            name="apps.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="auth failed password=%s for %s",
            args=("sample-value", "owner@example.com"),
            exc_info=None,
        )
        record.refresh_token = "sample-value"
        record.safe_counter = 3

        payload = json.loads(JsonLogFormatter().format(record))
    finally:
        clear_log_context(token)

    assert payload["event"] == "auth failed password=[redacted] for [redacted-email]"
    assert payload["refresh_token"] == "[redacted]"
    assert payload["safe_counter"] == 3
    assert payload["request_id"] == "req-123"
    assert payload["club_id"] == 7


def test_log_context_clear_hash_and_decimal_serialization():
    token = set_log_context(request_id="req-456")
    try:
        assert get_log_context() == {"request_id": "req-456"}
        digest = hash_for_log("203.0.113.10", salt="salt-a")
        assert digest == hash_for_log("203.0.113.10", salt="salt-a")
        assert digest != hash_for_log("203.0.113.10", salt="salt-b")

        record = logging.LogRecord(
            name="apps.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="payment recorded",
            args=(),
            exc_info=None,
        )
        record.amount = Decimal("12.30")

        payload = json.loads(JsonLogFormatter().format(record))
    finally:
        clear_log_context(token)

    assert get_log_context() == {}
    assert payload["amount"] == "12.30"


def test_safe_text_formatter_redacts_message_without_mutating_record():
    record = logging.LogRecord(
        name="apps.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="password=%s email=%s",
        args=("sample-value", "owner@example.com"),
        exc_info=None,
    )

    formatted = SafeTextFormatter("%(message)s").format(record)

    assert formatted == "password=[redacted] email=[redacted-email]"
    assert record.msg == "password=%s email=%s"
    assert record.args == ("sample-value", "owner@example.com")


@pytest.mark.django_db
def test_tenant_middleware_sets_and_clears_club_membership_context():
    club = ClubFactory()
    user = UserFactory()
    membership = ClubMembershipFactory(user=user, club=club, role="owner")
    captured = {}

    def app(request):
        captured["club"] = request.club
        captured["membership"] = request._membership
        return HttpResponse("ok")

    request = RequestFactory().get("/dashboard/")
    request.user = user

    response = TenantMiddleware(app)(request)

    assert response.status_code == 200
    assert captured == {"club": club, "membership": membership}

    anonymous_request = RequestFactory().get("/dashboard/")
    anonymous_request.user = AnonymousUser()
    TenantMiddleware(app)(anonymous_request)
    assert anonymous_request.club is None
    assert anonymous_request._membership is None


def test_request_log_middleware_adds_request_id_and_safe_request_summary():
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("apps.common.middleware")
    handler = ListHandler()
    original_level = logger.level
    original_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        request = RequestFactory().get(
            "/api/ping/",
            HTTP_X_REQUEST_ID="req-abc",
            REMOTE_ADDR="203.0.113.10",
        )
        response = RequestLogMiddleware(lambda request: HttpResponse("ok"))(request)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    assert response["X-Request-ID"] == "req-abc"
    assert len(records) == 1
    record = records[0]
    assert record.msg == "http_request_finished"
    assert record.request_id == "req-abc"
    assert record.method == "GET"
    assert record.path == "/api/ping/"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, int)
    assert record.client_ip_hash
    assert record.client_ip_hash != "203.0.113.10"


def test_logging_settings_use_request_middleware_and_safe_json_formatter():
    assert "apps.common.middleware.RequestLogMiddleware" in settings.MIDDLEWARE
    assert settings.LOGGING["formatters"]["json"]["()"] == "apps.common.logging.JsonLogFormatter"
    assert settings.LOGGING["formatters"]["verbose"]["()"] == "apps.common.logging.SafeTextFormatter"
