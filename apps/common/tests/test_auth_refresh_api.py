from types import SimpleNamespace

import pytest
from django.test import override_settings
from ninja.testing import TestClient

from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from config.api import api

api_client = TestClient(api)


class TestRefreshTokenHelpers:
    def test_refresh_access_token_returns_rotated_pair(self, monkeypatch):
        class FakeTokenStrategy:
            def refresh_token(self, refresh_token):
                return ("access-token", "next-refresh-token")

        monkeypatch.setattr(
            "apps.common.auth_tokens.app_settings",
            SimpleNamespace(TOKEN_STRATEGY=FakeTokenStrategy()),
        )

        from apps.common.auth_tokens import refresh_access_token

        assert refresh_access_token(refresh_token="refresh-token") == (
            "access-token",
            "next-refresh-token",
        )

    def test_refresh_access_token_raises_for_invalid_token(self, monkeypatch):
        class FakeTokenStrategy:
            def refresh_token(self, refresh_token):
                return None

        monkeypatch.setattr(
            "apps.common.auth_tokens.app_settings",
            SimpleNamespace(TOKEN_STRATEGY=FakeTokenStrategy()),
        )

        from apps.common.auth_tokens import InvalidRefreshTokenError, refresh_access_token

        with pytest.raises(InvalidRefreshTokenError):
            refresh_access_token(refresh_token="refresh-token")

    def test_refresh_token_user_id_and_revoke_handle_invalid_token(self, monkeypatch):
        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.validate_refresh_token",
            lambda refresh_token: None,
        )

        from apps.common.auth_tokens import refresh_token_user_id, revoke_refresh_token

        assert refresh_token_user_id(refresh_token="refresh-token") is None
        assert revoke_refresh_token(refresh_token="refresh-token") is False

    def test_refresh_token_user_id_and_revoke_handle_valid_token(self, monkeypatch):
        class FakeSession:
            saved = False

            def save(self):
                self.saved = True

        user = SimpleNamespace(id=123)
        session = FakeSession()
        payload = {"session_id": "session-id"}
        captured = {}

        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.validate_refresh_token",
            lambda refresh_token: (user, session, payload),
        )
        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.invalidate_refresh_token",
            lambda actual_session, actual_payload: captured.update(
                {"session": actual_session, "payload": actual_payload}
            ),
        )

        from apps.common.auth_tokens import refresh_token_user_id, revoke_refresh_token

        assert refresh_token_user_id(refresh_token="refresh-token") == user.id
        assert revoke_refresh_token(refresh_token="refresh-token") is True
        assert captured == {"session": session, "payload": payload}
        assert session.saved is True

    @pytest.mark.django_db
    def test_switch_refresh_token_membership_sets_selected_membership_and_rotates(self, monkeypatch):
        from apps.clubs.models import ClubMembership
        from apps.common.auth import SELECTED_MEMBERSHIP_SESSION_KEY
        from apps.common.auth_tokens import switch_refresh_token_membership

        class FakeSession(dict):
            saved = False

            def save(self):
                self.saved = True

        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.PARENT)
        session = FakeSession()
        payload = {"session_id": "session-id"}
        captured = {}

        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.validate_refresh_token",
            lambda refresh_token: (user, session, payload),
        )
        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.create_access_token",
            lambda actual_user, actual_session, claims: captured.update(
                {"user": actual_user, "session": actual_session, "claims": claims}
            )
            or "parent-access-token",
        )
        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.invalidate_refresh_token",
            lambda actual_session, actual_payload: captured.update(
                {"invalidated_session": actual_session, "invalidated_payload": actual_payload}
            ),
        )
        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.create_refresh_token",
            lambda actual_user, actual_session: "next-refresh-token",
        )
        monkeypatch.setattr(
            "apps.common.auth_tokens.app_settings",
            SimpleNamespace(JWT_ROTATE_REFRESH_TOKEN=True),
        )

        access_token, next_refresh_token = switch_refresh_token_membership(
            refresh_token="current-refresh-token",
            user_id=user.id,
            club_id=club.id,
            role=ClubMembership.Role.PARENT,
        )

        assert access_token == "parent-access-token"
        assert next_refresh_token == "next-refresh-token"
        assert session[SELECTED_MEMBERSHIP_SESSION_KEY] == {
            "club_id": club.id,
            "role": ClubMembership.Role.PARENT,
        }
        assert captured["user"] == user
        assert captured["session"] == session
        assert captured["claims"] == {"club_id": club.id, "role": ClubMembership.Role.PARENT}
        assert captured["invalidated_session"] == session
        assert captured["invalidated_payload"] == payload
        assert session.saved is True

    @pytest.mark.django_db
    def test_switch_refresh_token_membership_rejects_membership_mismatch(self, monkeypatch):
        from apps.clubs.models import ClubMembership
        from apps.common.auth import SELECTED_MEMBERSHIP_SESSION_KEY
        from apps.common.auth_tokens import InvalidRefreshTokenError, switch_refresh_token_membership

        user = UserFactory()
        other_club = ClubFactory()
        session = {}

        monkeypatch.setattr(
            "apps.common.auth_tokens.jwt_internal.validate_refresh_token",
            lambda refresh_token: (user, session, {"session_id": "session-id"}),
        )

        with pytest.raises(InvalidRefreshTokenError):
            switch_refresh_token_membership(
                refresh_token="current-refresh-token",
                user_id=user.id,
                club_id=other_club.id,
                role=ClubMembership.Role.PARENT,
            )

        assert SELECTED_MEMBERSHIP_SESSION_KEY not in session


@pytest.mark.django_db
class TestRefreshCookieAPI:
    @pytest.fixture(autouse=True)
    def _bypass_jwt_auth(self, bypass_jwt_auth):
        pass

    def test_store_refresh_cookie_requires_authenticated_access_token(self, client):
        response = client.post(
            "/api/auth/refresh-cookie/",
            data={"refresh_token": "raw-refresh-token"},
            content_type="application/json",
        )

        assert response.status_code == 401
        assert "jaguar_refresh" not in response.cookies

    def test_store_refresh_cookie_rejects_refresh_token_for_different_user(self, club, owner_user, monkeypatch):
        other_user = UserFactory()

        monkeypatch.setattr(
            "apps.common.auth_tokens.refresh_token_user_id",
            lambda *, refresh_token: other_user.id,
        )

        response = api_client.post(
            "/auth/refresh-cookie/",
            json={"refresh_token": "raw-refresh-token"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 401
        assert "jaguar_refresh" not in response.cookies

    def test_store_refresh_cookie_sets_httponly_cookie_without_echoing_token(self, club, owner_user, monkeypatch):
        monkeypatch.setattr(
            "apps.common.auth_tokens.refresh_token_user_id",
            lambda *, refresh_token: owner_user.id,
        )

        response = api_client.post(
            "/auth/refresh-cookie/",
            json={"refresh_token": "raw-refresh-token"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 204
        cookie = response.cookies["jaguar_refresh"]
        assert cookie.value == "raw-refresh-token"
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"
        assert cookie["path"] == "/api/auth/"
        assert b"raw-refresh-token" not in response.content

    @override_settings(DEBUG=True, REFRESH_COOKIE_SECURE=True)
    def test_store_refresh_cookie_can_force_secure_flag_in_public_debug_mode(self, club, owner_user, monkeypatch):
        monkeypatch.setattr(
            "apps.common.auth_tokens.refresh_token_user_id",
            lambda *, refresh_token: owner_user.id,
        )

        response = api_client.post(
            "/auth/refresh-cookie/",
            json={"refresh_token": "raw-refresh-token"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 204
        assert response.cookies["jaguar_refresh"]["secure"] is True

    def test_refresh_uses_cookie_not_request_body_and_rotates_cookie(self, client, monkeypatch):
        captured = {}

        def fake_refresh(*, refresh_token):
            captured["refresh_token"] = refresh_token
            return "access-token", "next-refresh-token"

        monkeypatch.setattr(
            "apps.common.auth_tokens.refresh_access_token",
            fake_refresh,
        )
        client.cookies["jaguar_refresh"] = "cookie-refresh-token"

        response = client.post(
            "/api/auth/refresh/",
            data={"refresh_token": "body-refresh-token"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert captured["refresh_token"] == "cookie-refresh-token"
        assert response.json() == {"access_token": "access-token"}
        assert response.cookies["jaguar_refresh"].value == "next-refresh-token"

    def test_logout_deletes_cookie_and_revokes_current_refresh(self, client, monkeypatch):
        captured = {}

        def fake_revoke(*, refresh_token):
            captured["refresh_token"] = refresh_token
            return True

        monkeypatch.setattr(
            "apps.common.auth_tokens.revoke_refresh_token",
            fake_revoke,
        )
        client.cookies["jaguar_refresh"] = "cookie-refresh-token"

        response = client.post("/api/auth/logout/")

        assert response.status_code == 204
        assert captured["refresh_token"] == "cookie-refresh-token"
        assert response.cookies["jaguar_refresh"].value == ""
        assert response.cookies["jaguar_refresh"]["max-age"] == 0
