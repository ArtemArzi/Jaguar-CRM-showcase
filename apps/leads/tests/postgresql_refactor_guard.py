from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_POSTGRES_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UnsafeLeadsRefactorDatabaseError(ValueError):
    pass


@dataclass(frozen=True)
class PostgresTarget:
    scheme: str
    host: str | None
    port: int | None
    username: str | None
    database_name: str


def _has_isolated_database_token(database_name: str) -> bool:
    return bool(re.search(r"(?:^|[_-])(?:test|e2e|refactor)(?:[_-]|$)", database_name.lower()))


def _parse_postgres_target(database_url: str) -> PostgresTarget:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise UnsafeLeadsRefactorDatabaseError("PostgreSQL URL must use postgres scheme")
    if parsed.query:
        raise UnsafeLeadsRefactorDatabaseError("PostgreSQL URL must not contain query parameters")
    try:
        port = parsed.port
    except ValueError as error:
        raise UnsafeLeadsRefactorDatabaseError("PostgreSQL URL has an invalid port") from error

    database_name = parsed.path.lstrip("/")
    if not database_name:
        raise UnsafeLeadsRefactorDatabaseError("PostgreSQL URL must name a database")
    return PostgresTarget(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=port,
        username=parsed.username,
        database_name=database_name,
    )


def _validate_isolated_local_target(target: PostgresTarget) -> None:
    if target.host not in LOCAL_POSTGRES_HOSTS:
        raise UnsafeLeadsRefactorDatabaseError("PostgreSQL gate must use a loopback host")
    if not _has_isolated_database_token(target.database_name):
        raise UnsafeLeadsRefactorDatabaseError(
            "PostgreSQL gate database must include a bounded test, e2e, or refactor token"
        )


def _targets_match(left: PostgresTarget, right: PostgresTarget) -> bool:
    return (
        left.scheme,
        left.host,
        left.port,
        left.username,
        left.database_name,
    ) == (
        right.scheme,
        right.host,
        right.port,
        right.username,
        right.database_name,
    )


def validate_leads_refactor_database_urls(
    *,
    database_url: str,
    refactor_database_url: str,
) -> PostgresTarget:
    """Validate sanitized connection targets before pytest can initialize a database."""

    if not database_url:
        raise UnsafeLeadsRefactorDatabaseError("DATABASE_URL is required for the PostgreSQL gate")
    if not refactor_database_url:
        raise UnsafeLeadsRefactorDatabaseError("LEADS_REFACTOR_POSTGRES_URL is required")
    database_target = _parse_postgres_target(database_url)
    refactor_target = _parse_postgres_target(refactor_database_url)
    _validate_isolated_local_target(refactor_target)
    if not _targets_match(database_target, refactor_target):
        raise UnsafeLeadsRefactorDatabaseError(
            "DATABASE_URL and LEADS_REFACTOR_POSTGRES_URL must identify the same target"
        )
    return refactor_target


def derived_test_database_name(target: PostgresTarget) -> str:
    return f"test_{target.database_name}"
