from __future__ import annotations

import logging

from apps.billing.service_modules._shared import _apply_updates
from apps.clubs.models import ClubSettings
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)

_UPDATE_CLUB_SETTINGS_FIELDS = frozenset({
    "freeze_enabled", "freeze_max_days", "freeze_max_count",
    "min_trainings_to_freeze",
    "feedback_delay_hours", "max_push_per_week",
    "quiet_hours_start", "quiet_hours_end",
    "primary_color", "accent_color", "club_name_display", "logo_url",
})


def get_or_create_club_settings(club_id: int) -> ClubSettings:
    settings, _ = ClubSettings.objects.get_or_create(club_id=club_id)
    return settings


def update_club_settings(*, club_id: int, **fields) -> ClubSettings:
    # Prevent javascript:/data: URIs in logo
    logo_url = fields.get("logo_url")
    if logo_url:
        from django.core.exceptions import ValidationError
        from django.core.validators import URLValidator
        try:
            URLValidator(schemes=["http", "https"])(logo_url)
        except ValidationError:
            raise BusinessLogicError(
                "Некорректный URL логотипа", code="invalid_logo_url",
            )

    settings = get_or_create_club_settings(club_id)
    changed = _apply_updates(settings, fields, _UPDATE_CLUB_SETTINGS_FIELDS)
    settings.save(update_fields=[*changed, "updated_at"])
    logger.info("club_settings_updated", extra={"club_id": club_id, "fields": list(fields.keys())})
    return settings
