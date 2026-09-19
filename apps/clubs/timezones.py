from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


def club_zoneinfo(club) -> tzinfo:
    timezone_name = getattr(club, "timezone", "") or timezone.get_default_timezone_name()
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.get_default_timezone()


def club_localtime(club, value: datetime | None = None) -> datetime:
    return timezone.localtime(value or timezone.now(), club_zoneinfo(club))


def club_localdate(club, value: datetime | None = None) -> date:
    return club_localtime(club, value).date()


def club_local_day_start(club, target_date: date) -> datetime:
    """Return the UTC-aware instant at the owning club's local midnight."""
    return timezone.make_aware(
        datetime.combine(target_date, time.min),
        club_zoneinfo(club),
    )


def club_local_date_range_bounds(club, *, date_from: date, date_to: date) -> tuple[datetime, datetime]:
    start = club_local_day_start(club, date_from)
    end = club_local_day_start(club, date_to + timedelta(days=1))
    return start, end


def club_localdate_by_id(club_id: int, value: datetime | None = None) -> date:
    from apps.clubs.models import Club

    club = Club.objects.only("id", "timezone").get(id=club_id)
    return club_localdate(club, value)


def club_local_day_start_by_id(club_id: int, target_date: date) -> datetime:
    from apps.clubs.models import Club

    club = Club.objects.only("id", "timezone").get(id=club_id)
    return club_local_day_start(club, target_date)
