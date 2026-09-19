from __future__ import annotations

from allauth.headless import app_settings
from allauth.headless.tokens.strategies.jwt import internal as jwt_internal
from django.conf import settings
from django.http import HttpResponse

from apps.common.auth import SELECTED_MEMBERSHIP_SESSION_KEY

REFRESH_COOKIE_NAME = "jaguar_refresh"
REFRESH_COOKIE_PATH = "/api/auth/"
REFRESH_COOKIE_SAMESITE = "Lax"


class InvalidRefreshTokenError(Exception):
    pass


def refresh_access_token(*, refresh_token: str) -> tuple[str, str]:
    result = app_settings.TOKEN_STRATEGY.refresh_token(refresh_token)
    if result is None:
        raise InvalidRefreshTokenError
    return result


def revoke_refresh_token(*, refresh_token: str) -> bool:
    user_session_payload = jwt_internal.validate_refresh_token(refresh_token)
    if user_session_payload is None:
        return False
    _user, session, payload = user_session_payload
    jwt_internal.invalidate_refresh_token(session, payload)
    session.save()
    return True


def refresh_token_user_id(*, refresh_token: str) -> int | None:
    user_session_payload = jwt_internal.validate_refresh_token(refresh_token)
    if user_session_payload is None:
        return None
    user, _session, _payload = user_session_payload
    return user.id


def switch_refresh_token_membership(
    *,
    refresh_token: str,
    user_id: int,
    club_id: int,
    role: str,
) -> tuple[str, str]:
    user_session_payload = jwt_internal.validate_refresh_token(refresh_token)
    if user_session_payload is None:
        raise InvalidRefreshTokenError
    user, session, payload = user_session_payload
    if user.id != user_id:
        raise InvalidRefreshTokenError

    from apps.clubs.models import ClubMembership

    if not ClubMembership.objects.filter(
        user=user,
        club_id=club_id,
        role=role,
        is_active=True,
    ).exists():
        raise InvalidRefreshTokenError

    session[SELECTED_MEMBERSHIP_SESSION_KEY] = {"club_id": club_id, "role": role}
    access_token = jwt_internal.create_access_token(
        user,
        session,
        {"club_id": club_id, "role": role},
    )
    if app_settings.JWT_ROTATE_REFRESH_TOKEN:
        jwt_internal.invalidate_refresh_token(session, payload)
        next_refresh_token = jwt_internal.create_refresh_token(user, session)
    else:
        next_refresh_token = refresh_token
    session.save()
    return access_token, next_refresh_token


def set_refresh_cookie(response: HttpResponse, refresh_token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=settings.HEADLESS_JWT_REFRESH_TOKEN_EXPIRES_IN,
        path=REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: HttpResponse) -> None:
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        samesite=REFRESH_COOKIE_SAMESITE,
    )
