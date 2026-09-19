import pytest
from django.core.cache import cache
from django.test import Client

pytestmark = pytest.mark.django_db


class TestCSRFInHTMX:
    def test_csrf_token_in_body_tag(self, client: Client, owner_user, club):
        """CSRF token must be injected via hx-headers on body tag (per FOUND-05)."""
        client.force_login(owner_user)
        response = client.get("/dashboard/")
        content = response.content.decode()
        assert "hx-headers" in content
        assert "X-CSRFToken" in content


class TestRoleProtection:
    def test_trainer_cannot_access_dashboard(self, client: Client, trainer_user, club):
        """Trainer user must get 403 (not 200) on /dashboard/ views."""
        client.force_login(trainer_user)
        response = client.get("/dashboard/")
        assert response.status_code == 403

    def test_owner_can_access_dashboard(self, client: Client, owner_user, club):
        """Owner user must get 200 on /dashboard/."""
        client.force_login(owner_user)
        response = client.get("/dashboard/")
        assert response.status_code == 200


class TestBrandingContext:
    def test_branding_css_vars_in_page(self, client: Client, owner_user, club, club_settings):
        """Branding CSS custom properties injected from ClubSettings (per FOUND-06, D-13)."""
        cache.delete(f"club_branding:{club.id}")
        client.force_login(owner_user)
        response = client.get("/dashboard/")
        content = response.content.decode()
        assert "--branding-primary" in content
        assert club_settings.primary_color in content

    def test_no_branding_for_unauthenticated(self, client: Client):
        """Login page still renders without club context."""
        response = client.get("/dashboard/login/")
        assert response.status_code == 200
