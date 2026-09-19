import logging
import re
from datetime import time as dt_time

from django.http import HttpRequest

from apps.clubs.models import ClubSettings

logger = logging.getLogger(__name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_VALID_TIMEZONES = frozenset({
    "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara",
    "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Novosibirsk",
    "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Vladivostok", "Asia/Kamchatka",
})

SETTINGS_TABS = [
    ("general", "Общие", "settings"),
    ("billing", "Биллинг", "receipt-text"),
    ("catalog", "Каталог", "layers"),
    ("documents", "Документы", "file-text"),
    ("notifications", "Уведомления", "bell"),
    ("kiosk", "Киоск", "tablet"),
]


def _parse_time(val: str) -> dt_time | None:
    """Parse HH:MM string to time, return None on failure."""
    try:
        parts = val.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _parse_int(val: str, *, min_val: int = 0, max_val: int = 9999) -> int | None:
    """Parse integer from string, return None if empty/invalid/out of range."""
    val = val.strip()
    if not val:
        return None
    try:
        n = int(val)
        if min_val <= n <= max_val:
            return n
    except ValueError:
        pass
    return None


def _settings_context(active_tab: str, request: HttpRequest) -> dict:
    """Base context for all settings tabs."""
    settings, _ = ClubSettings.objects.get_or_create(club=request.club)
    return {
        "page_title": "Настройки",
        "settings": settings,
        "club": request.club,
        "active_tab": active_tab,
        "settings_tabs": SETTINGS_TABS,
    }
