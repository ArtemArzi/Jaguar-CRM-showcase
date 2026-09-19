from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import pytest
from django.db import connection


def _has_disposable_purpose_token(database_name: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[_-])(?:test|e2e|refactor)(?:[_-]|$)",
            database_name.lower(),
        )
    )


@pytest.mark.parametrize(
    ("database_name", "expected"),
    [
        ("billing_refactor_test", True),
        ("real-stack-e2e", True),
        ("test", True),
        ("contest", False),
        ("latest", False),
        ("protest", False),
        ("production", False),
    ],
)
def test_disposable_database_name_requires_a_token_boundary(database_name, expected):
    assert _has_disposable_purpose_token(database_name) is expected


@pytest.mark.django_db
def test_billing_refactor_postgresql_gate_uses_isolated_postgresql():
    database_url = os.environ.get("BILLING_REFACTOR_POSTGRES_URL", "")
    gate_required = os.environ.get("BILLING_REFACTOR_POSTGRES_GATE_REQUIRED") == "1"
    if not database_url and not gate_required:
        pytest.skip("billing refactor PostgreSQL gate is opt-in")
    assert database_url, "BILLING_REFACTOR_POSTGRES_URL is required"

    parsed = urlparse(database_url)
    assert parsed.scheme in {"postgres", "postgresql"}
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    assert not parsed.query, "PostgreSQL gate URL must not contain query parameters"
    database_name = parsed.path.lstrip("/")
    assert database_name
    assert _has_disposable_purpose_token(
        database_name,
    ), "PostgreSQL gate database name must contain a disposable-purpose token"

    assert connection.vendor == "postgresql"
    active_database_name = str(connection.settings_dict["NAME"])
    assert active_database_name in {database_name, f"test_{database_name}"}
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone() == (active_database_name,)
