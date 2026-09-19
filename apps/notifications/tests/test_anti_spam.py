from datetime import UTC, datetime, time, timedelta

import pytest
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory
from apps.notifications.services import (
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    can_send_push,
    is_quiet_hours,
)
from apps.notifications.tests.factories import SentNotificationFactory
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestCanSendPush:
    def test_critical_bypasses_budget(self):
        """Critical notifications always pass regardless of budget."""
        club = ClubFactory()
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=0)

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_CRITICAL
        ) is True

    def test_medium_within_budget(self):
        """Medium priority passes when sent_count < max_push_per_week."""
        club = ClubFactory()
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=3)

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_MEDIUM
        ) is True

    def test_medium_exceeds_budget(self):
        """Medium priority blocked when sent_count >= max_push_per_week."""
        club = ClubFactory()
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=2)

        today = timezone.now().date()
        SentNotificationFactory(club=club, student=student, sent_date=today)
        SentNotificationFactory(
            club=club,
            student=student,
            notification_type="missed_training",
            sent_date=today,
        )

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_MEDIUM
        ) is False

    def test_low_blocked_when_budget_full(self):
        """Low priority blocked same as medium when budget exhausted."""
        club = ClubFactory()
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=1)

        SentNotificationFactory(club=club, student=student)

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_LOW
        ) is False

    def test_no_settings_allows_send(self):
        """When ClubSettings does not exist, allow send."""
        club = ClubFactory()
        student = StudentFactory(club=club)

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_MEDIUM
        ) is True

    def test_old_notifications_not_counted(self):
        """Notifications older than 7 days don't count toward budget."""
        club = ClubFactory()
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=1)

        old_date = timezone.now().date() - timedelta(days=8)
        SentNotificationFactory(club=club, student=student, sent_date=old_date)

        assert can_send_push(
            student_id=student.id, club_id=club.id, priority=PRIORITY_MEDIUM
        ) is True

    def test_budget_window_uses_club_local_date_across_utc_midnight(self):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        student = StudentFactory(club=club)
        ClubSettingsFactory(club=club, max_push_per_week=1)
        now = datetime(2026, 7, 12, 20, 30, tzinfo=UTC)
        SentNotificationFactory(
            club=club,
            student=student,
            sent_date=datetime(2026, 7, 5).date(),
        )

        assert can_send_push(
            student_id=student.id,
            club_id=club.id,
            priority=PRIORITY_MEDIUM,
            now=now,
        ) is True


@pytest.mark.django_db
class TestIsQuietHours:
    def test_quiet_hours_overnight(self):
        """22:00 is within quiet hours (21:00-09:00)."""
        club = ClubFactory(timezone="Europe/Moscow")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(21, 0),
            quiet_hours_end=time(9, 0),
        )

        now = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
        assert is_quiet_hours(club_id=club.id, now=now) is True

    def test_quiet_hours_daytime_ok(self):
        """14:00 is outside quiet hours (21:00-09:00)."""
        club = ClubFactory(timezone="Europe/Moscow")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(21, 0),
            quiet_hours_end=time(9, 0),
        )

        now = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)
        assert is_quiet_hours(club_id=club.id, now=now) is False

    def test_quiet_hours_before_morning(self):
        """08:00 is within quiet hours (21:00-09:00)."""
        club = ClubFactory(timezone="Europe/Moscow")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(21, 0),
            quiet_hours_end=time(9, 0),
        )

        now = datetime(2026, 7, 15, 5, 0, tzinfo=UTC)
        assert is_quiet_hours(club_id=club.id, now=now) is True

    def test_same_utc_moment_is_evaluated_in_each_club_timezone(self):
        now = datetime(2026, 7, 15, 16, 30, tzinfo=UTC)
        yekaterinburg = ClubFactory(timezone="Asia/Yekaterinburg")
        moscow = ClubFactory(timezone="Europe/Moscow")
        for club in (yekaterinburg, moscow):
            ClubSettingsFactory(
                club=club,
                quiet_hours_start=time(21, 0),
                quiet_hours_end=time(9, 0),
            )

        assert is_quiet_hours(club_id=yekaterinburg.id, now=now) is True
        assert is_quiet_hours(club_id=moscow.id, now=now) is False

    def test_no_settings_not_quiet(self):
        """When no ClubSettings exist, not quiet hours."""
        club = ClubFactory()
        assert is_quiet_hours(club_id=club.id) is False
