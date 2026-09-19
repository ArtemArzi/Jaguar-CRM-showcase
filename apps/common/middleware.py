from __future__ import annotations

import logging
import time
import uuid

from django.conf import settings

from apps.common.logging import clear_log_context, hash_for_log, set_log_context

logger = logging.getLogger(__name__)


class TenantMiddleware:
    """Set request.club for session-authenticated requests (HTMX admin)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.club = None
        request._membership = None

        if hasattr(request, "user") and request.user.is_authenticated:
            from apps.clubs.models import ClubMembership

            membership = ClubMembership.objects.filter(user=request.user, is_active=True).select_related("club").first()
            if membership:
                request.club = membership.club
                request._membership = membership

        return self.get_response(request)


class RequestLogMiddleware:
    """Attach request_id to logs and emit one safe request summary per request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        request_id = _request_id_from_header(request) or uuid.uuid4().hex
        request.request_id = request_id

        token = set_log_context(
            request_id=request_id,
            method=request.method,
            path=request.path,
            user_id=_request_user_id(request),
            club_id=_request_club_id(request),
            client_ip_hash=client_ip_hash(request),
        )
        try:
            response = self.get_response(request)
        except Exception:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.exception(
                "http_request_exception",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.path,
                    "duration_ms": duration_ms,
                    "user_id": _request_user_id(request),
                    "club_id": _request_club_id(request),
                    "client_ip_hash": client_ip_hash(request),
                },
            )
            raise
        finally:
            clear_log_context(token)

        response["X-Request-ID"] = request_id
        duration_ms = int((time.monotonic() - started) * 1000)
        status_code = getattr(response, "status_code", 0)
        log_method = logger.info
        if status_code >= 500:
            log_method = logger.error
        elif status_code >= 400:
            log_method = logger.warning

        log_method(
            "http_request_finished",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "user_id": _request_user_id(request),
                "club_id": _request_club_id(request),
                "client_ip_hash": client_ip_hash(request),
            },
        )
        return response


def _request_id_from_header(request) -> str:
    request_id = request.META.get("HTTP_X_REQUEST_ID", "").strip()
    if not request_id or len(request_id) > 128:
        return ""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if any(char not in allowed for char in request_id):
        return ""
    return request_id


def _request_user_id(request) -> int | None:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "id", None)


def _request_club_id(request) -> int | None:
    club = getattr(request, "club", None)
    return getattr(club, "id", None)


def client_ip_hash(request) -> str:
    raw_ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",", 1)[0].strip()
    if not raw_ip:
        raw_ip = request.META.get("REMOTE_ADDR", "")
    if not raw_ip:
        return ""
    return hash_for_log(raw_ip, salt=settings.SECRET_KEY)
