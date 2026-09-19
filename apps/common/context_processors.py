import logging
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest

logger = logging.getLogger(__name__)

_NOT_FOUND = object()


def club_branding(request: HttpRequest) -> dict[str, Any]:
    """Inject club branding into all Django template contexts.

    Uses request.club set by TenantMiddleware for authenticated users.
    Returns empty context for unauthenticated or club-less requests.
    Caches ClubSettings per club for 5 minutes to avoid per-request DB hit.
    """
    if not hasattr(request, "club") or not request.club:
        return {"club_settings": None, "club": None}

    cache_key = f"club_branding:{request.club.id}"
    settings = cache.get(cache_key, _NOT_FOUND)

    if settings is _NOT_FOUND:
        from apps.clubs.models import ClubSettings

        try:
            settings = ClubSettings.objects.get(club=request.club)
        except ClubSettings.DoesNotExist:
            settings = None

        cache.set(cache_key, settings, timeout=300)

    return {
        "club_settings": settings,
        "club": request.club,
    }
