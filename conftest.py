import os
from unittest.mock import patch

import pytest

from apps.clubs.models import ClubMembership, ClubSettings
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.leads.tests.postgresql_refactor_guard import (
    UnsafeLeadsRefactorDatabaseError,
    validate_leads_refactor_database_urls,
)


def pytest_configure(config):
    """Fail before pytest-django can initialize an unsafe leads refactor database."""

    refactor_database_url = os.environ.get("LEADS_REFACTOR_POSTGRES_URL", "")
    gate_required = os.environ.get("LEADS_REFACTOR_POSTGRES_GATE_REQUIRED") == "1"
    if not refactor_database_url and not gate_required:
        return

    try:
        validate_leads_refactor_database_urls(
            database_url=os.environ.get("DATABASE_URL", ""),
            refactor_database_url=refactor_database_url,
        )
    except UnsafeLeadsRefactorDatabaseError as error:
        raise pytest.UsageError(f"Unsafe leads refactor PostgreSQL configuration: {error}") from error


@pytest.fixture
def db_access(db):
    """Alias for db fixture -- explicit marker."""
    pass


@pytest.fixture(autouse=True)
def _enable_payment_test_gates(settings):
    """Keep legacy payment-flow tests explicit and isolated from production defaults."""
    settings.DEBUG = True
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
    settings.TOCHKA_FISCALIZATION_READY = True
    settings.TOCHKA_FISCALIZATION_DECISION_ID = "synthetic-test-decision"
    settings.TOCHKA_PAYMENT_MODES = ["sbp"]
    settings.TOCHKA_RETAILER_READBACK_MAX_AGE_SECONDS = 3600
    settings.TOCHKA_WEBHOOK_KEY_MODE = "pem"
    settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
    settings.JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN = "https://app.jaguar.test"


@pytest.fixture
def club(db):
    return ClubFactory()


@pytest.fixture
def other_club(db):
    return ClubFactory(name="Other Club")


@pytest.fixture
def owner_user(club):
    user = UserFactory()
    ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.OWNER)
    return user


@pytest.fixture
def trainer_user(club):
    user = UserFactory()
    ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.TRAINER)
    return user


@pytest.fixture
def admin_user(club):
    user = UserFactory()
    ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.ADMIN)
    return user


@pytest.fixture
def student_user(club):
    user = UserFactory()
    ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.STUDENT)
    return user


@pytest.fixture
def parent_user(club):
    user = UserFactory()
    ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.PARENT)
    return user


def _mock_auth(request):
    if hasattr(request, "auth") and request.auth:
        return request.auth
    return None


@pytest.fixture
def club_settings(club):
    """Create ClubSettings for branding tests."""
    settings, _ = ClubSettings.objects.get_or_create(
        club=club,
        defaults={
            "primary_color": "#1E40AF",
            "accent_color": "#F59E0B",
            "club_name_display": "Test Fight Club",
        },
    )
    return settings


@pytest.fixture
def bypass_jwt_auth():
    """Fixture to bypass JWT auth in API tests."""
    with patch("apps.common.auth.TenantJWTAuth.__call__", side_effect=_mock_auth):
        yield
