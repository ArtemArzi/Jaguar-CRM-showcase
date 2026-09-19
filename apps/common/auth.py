from __future__ import annotations

import logging
from typing import Any

from allauth.headless import app_settings
from allauth.headless.contrib.ninja.security import JWTTokenAuth
from allauth.headless.tokens.strategies.jwt import JWTTokenStrategy
from allauth.headless.tokens.strategies.jwt import internal as jwt_internal
from django.http import HttpRequest
from ninja.security import APIKeyHeader

logger = logging.getLogger(__name__)

SELECTED_MEMBERSHIP_SESSION_KEY = "jaguar_selected_membership"


def _membership_claims_for_user(user, *, club_id: int | None = None, role: str | None = None) -> dict[str, Any]:
    if not user or not user.is_authenticated:
        return {}

    from apps.clubs.models import ClubMembership

    memberships = ClubMembership.objects.filter(user=user, is_active=True).select_related("club").order_by("id")
    if club_id is not None:
        memberships = memberships.filter(club_id=club_id)
    if role is not None:
        memberships = memberships.filter(role=role)
    membership = memberships.first()
    if not membership:
        return {}
    return {"club_id": membership.club_id, "role": membership.role}


class TenantJWTTokenStrategy(JWTTokenStrategy):
    """Add club_id and role to JWT access token claims."""

    def get_claims(self, user) -> dict[str, Any]:
        claims = super().get_claims(user)
        claims.update(_membership_claims_for_user(user))
        return claims

    def _get_session_claims(self, user, session) -> dict[str, Any]:
        claims = super().get_claims(user)
        selected = session.get(SELECTED_MEMBERSHIP_SESSION_KEY) or {}
        selected_claims = _membership_claims_for_user(
            user,
            club_id=selected.get("club_id"),
            role=selected.get("role"),
        )
        claims.update(selected_claims or _membership_claims_for_user(user))
        return claims

    def refresh_token(self, refresh_token: str) -> tuple[str, str] | None:
        user_session_payload = jwt_internal.validate_refresh_token(refresh_token)
        if user_session_payload is None:
            return None
        user, session, payload = user_session_payload
        access_token = jwt_internal.create_access_token(
            user,
            session,
            self._get_session_claims(user, session),
        )
        if app_settings.JWT_ROTATE_REFRESH_TOKEN:
            jwt_internal.invalidate_refresh_token(session, payload)
            next_refresh_token = jwt_internal.create_refresh_token(user, session)
        else:
            next_refresh_token = refresh_token
        session.save()
        return access_token, next_refresh_token


class TenantJWTAuth(JWTTokenAuth):
    """Ninja auth: validate JWT via allauth, then set request.club and request._membership."""

    openapi_type = "http"

    def __init__(self) -> None:
        super().__init__()
        # Override OpenAPI schema so Orval can generate correct security types
        from ninja.security.base import SecuritySchema

        self.openapi_security_schema = SecuritySchema(
            type="http", scheme="bearer", bearerFormat="JWT"
        )

    def __call__(self, request: HttpRequest):
        payload = super().__call__(request)
        if payload is None:
            return None

        club_id = payload.get("club_id")
        if not club_id:
            # User authenticated but has no club (e.g. just registered, no club yet)
            request.club = None
            request._membership = None
            return payload

        from apps.clubs.models import Club, ClubMembership

        try:
            club = Club.objects.get(id=club_id, is_active=True)
        except Club.DoesNotExist:
            logger.debug("JWT club_id=%s not found or inactive", club_id)
            return None

        # Verify membership still active
        membership = ClubMembership.objects.filter(user=request.user, club=club, is_active=True).first()
        if not membership:
            logger.debug("User %s has no active membership in club %s", request.user, club_id)
            return None

        request.club = club
        request._membership = membership
        return payload


class TenantJWTOrSessionAuth(TenantJWTAuth):
    """Ninja auth for endpoints shared by PWA JWT clients and Dashboard session fetches."""

    def __call__(self, request: HttpRequest):
        authorization = request.headers.get("Authorization")
        payload = super().__call__(request)
        if payload is not None:
            return payload
        if authorization:
            return None

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None

        club = getattr(request, "club", None)
        membership = getattr(request, "_membership", None)
        if club is None or membership is None:
            from apps.clubs.models import ClubMembership

            membership = ClubMembership.objects.filter(user=user, is_active=True).select_related("club").first()
            club = membership.club if membership else None

        if club is None or membership is None or not club.is_active:
            return None

        request.club = club
        request._membership = membership
        return {
            "user_id": user.id,
            "club_id": club.id,
            "role": membership.role,
            "auth_type": "session",
        }


class KioskDeviceAuth(APIKeyHeader):
    """Authenticates kiosk device via X-Kiosk-Token header."""

    param_name = "X-Kiosk-Token"

    def authenticate(self, request: HttpRequest, key: str | None):
        if not key:
            return None

        from apps.attendance.models import KioskDevice

        try:
            device = KioskDevice.objects.select_related("club").get(token=key, is_active=True)
        except KioskDevice.DoesNotExist:
            return None
        if not device.club.is_active:
            return None
        request.club = device.club
        request._membership = None  # No user membership for kiosk
        return device
