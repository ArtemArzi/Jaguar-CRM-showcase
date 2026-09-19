import pytest
from django.test import RequestFactory

from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory


@pytest.mark.django_db
class TestTenantJWTTokenStrategy:
    def test_custom_strategy_is_configured(self):
        from django.conf import settings

        assert "TenantJWTTokenStrategy" in settings.HEADLESS_TOKEN_STRATEGY

    def test_get_claims_with_membership(self):
        from apps.common.auth import TenantJWTTokenStrategy

        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(user=user, club=club, role="owner")

        strategy = TenantJWTTokenStrategy()
        claims = strategy.get_claims(user)
        assert claims["club_id"] == club.id
        assert claims["role"] == "owner"

    def test_get_claims_without_membership(self):
        from apps.common.auth import TenantJWTTokenStrategy

        user = UserFactory()
        strategy = TenantJWTTokenStrategy()
        claims = strategy.get_claims(user)
        assert "club_id" not in claims
        assert "role" not in claims


@pytest.mark.django_db
class TestTenantJWTAuth:
    def test_auth_class_exists(self):
        from apps.common.auth import TenantJWTAuth

        auth = TenantJWTAuth()
        assert callable(auth)

    def test_jwt_auth_sets_active_club_membership(self, monkeypatch):
        from apps.common.auth import JWTTokenAuth, TenantJWTAuth

        user = UserFactory()
        club = ClubFactory()
        membership = ClubMembershipFactory(user=user, club=club, role="trainer")
        request = RequestFactory().get("/api/trainers/me/")
        request.user = user
        monkeypatch.setattr(
            JWTTokenAuth,
            "__call__",
            lambda self, request: {"club_id": club.id, "role": "trainer"},
        )

        payload = TenantJWTAuth()(request)

        assert payload == {"club_id": club.id, "role": "trainer"}
        assert request.club == club
        assert request._membership == membership

    def test_session_auth_fallback_returns_membership_payload(self):
        from apps.common.auth import TenantJWTOrSessionAuth

        user = UserFactory()
        club = ClubFactory()
        membership = ClubMembershipFactory(user=user, club=club, role="owner")
        request = RequestFactory().get("/api/dashboard/")
        request.user = user
        request.club = club
        request._membership = membership

        payload = TenantJWTOrSessionAuth()(request)

        assert payload == {
            "user_id": user.id,
            "club_id": club.id,
            "role": "owner",
            "auth_type": "session",
        }
