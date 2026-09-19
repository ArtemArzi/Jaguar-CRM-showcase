import pytest
from django.core.exceptions import ValidationError
from ninja.testing import TestClient

from apps.clubs.tests.factories import ClubSettingsFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestClubSettingsBrandingModel:
    def test_default_branding_values(self):
        settings = ClubSettingsFactory()
        assert settings.primary_color == "#C45A3B"
        assert settings.accent_color == "#C45A3B"
        assert settings.club_name_display == "Jaguar Muay Thai"
        assert settings.logo_url == ""

    def test_logo_src_returns_empty_without_logo(self):
        settings = ClubSettingsFactory(logo_url="")
        assert settings.logo_src == ""

    def test_logo_src_returns_external_logo_url(self):
        settings = ClubSettingsFactory(logo_url="https://example.com/logo.png")
        assert settings.logo_src == "https://example.com/logo.png"

    def test_logo_src_prefers_uploaded_file_over_external_url(self):
        settings = ClubSettingsFactory(logo_url="https://example.com/fallback.png")
        settings.logo_file.name = "club_logos/uploaded.png"

        assert settings.logo_src == settings.logo_file.url
        assert settings.logo_src.endswith("/club_logos/uploaded.png")

    def test_hex_color_validation_rejects_invalid(self):
        settings = ClubSettingsFactory(primary_color="red")
        with pytest.raises(ValidationError):
            settings.full_clean()

    def test_hex_color_validation_accepts_valid(self):
        settings = ClubSettingsFactory(primary_color="#AB12EF", accent_color="#00ff00")
        settings.full_clean()  # should not raise


@pytest.mark.django_db
class TestClubSettingsBrandingAPI:
    def test_get_settings_returns_branding_defaults(self, club, owner_user):
        response = client.get(
            "/billing/settings/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["primary_color"] == "#000000"
        assert data["accent_color"] == "#FF6B00"
        assert data["club_name_display"] == ""
        assert data["logo_url"] == ""

    def test_update_primary_color(self, club, owner_user):
        response = client.put(
            "/billing/settings/",
            json={"primary_color": "#FF0000"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["primary_color"] == "#FF0000"

        # Verify persistence
        response2 = client.get(
            "/billing/settings/",
            **_auth_params(owner_user, club),
        )
        assert response2.json()["primary_color"] == "#FF0000"

    def test_update_logo_url(self, club, owner_user):
        response = client.put(
            "/billing/settings/",
            json={"logo_url": "https://example.com/logo.png"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["logo_url"] == "https://example.com/logo.png"

    def test_invalid_hex_color_rejected(self, club, owner_user):
        response = client.put(
            "/billing/settings/",
            json={"primary_color": "invalid"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 422

    def test_branding_fields_independent(self, club, owner_user):
        """Updating branding does not reset freeze fields."""
        # Set freeze to non-default
        client.put(
            "/billing/settings/",
            json={"freeze_max_days": 15},
            **_auth_params(owner_user, club),
        )
        # Now update branding
        client.put(
            "/billing/settings/",
            json={"primary_color": "#112233"},
            **_auth_params(owner_user, club),
        )
        # Verify freeze not reset
        response = client.get(
            "/billing/settings/",
            **_auth_params(owner_user, club),
        )
        data = response.json()
        assert data["freeze_max_days"] == 15
        assert data["primary_color"] == "#112233"
