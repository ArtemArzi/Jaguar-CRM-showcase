import pytest
from django.db import DatabaseError

from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.tests.factories import ClubSettingsFactory


@pytest.mark.django_db
class TestUnifiedClientJourneyCapability:
    def test_fails_closed_when_global_setting_is_disabled(self, club, settings):
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False

        assert is_unified_client_journey_enabled(club=club) is False

    def test_requires_same_club_setting_when_global_setting_is_enabled(self, club, other_club, settings):
        ClubSettingsFactory(club=other_club, unified_client_journey_enabled=True)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True

        assert is_unified_client_journey_enabled(club=club) is False

    def test_is_enabled_only_when_global_and_same_club_flags_are_true(self, club, settings):
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True

        assert is_unified_client_journey_enabled(club=club.id) is True

    def test_fails_closed_when_the_capability_query_raises_database_error(
        self,
        club,
        settings,
        monkeypatch,
    ):
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True

        def raise_database_error(*args, **kwargs):
            raise DatabaseError("schema unavailable")

        monkeypatch.setattr(
            "apps.clubs.capabilities.ClubSettings.objects.filter",
            raise_database_error,
        )

        assert is_unified_client_journey_enabled(club=club) is False
