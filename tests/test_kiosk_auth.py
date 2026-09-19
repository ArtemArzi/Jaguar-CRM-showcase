"""Tests for kiosk device authentication: model, services, auth class, and endpoints."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.attendance.models import KioskDevice
from apps.attendance.services import activate_kiosk, deactivate_kiosk, generate_kiosk_pin
from apps.attendance.services.kiosk import (
    KIOSK_ACTIVATION_FAILURE_WINDOW_SECONDS,
    KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES,
    _activation_global_cache_key,
)
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory

# ──────────────────────────────────────────────
# Service tests
# ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_kiosk_activation_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestGenerateKioskPin:
    def test_creates_device_with_pin_and_token(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)

        assert len(pin) == 6
        assert pin.isdigit()

        device = KioskDevice.objects.get(club=club, is_active=True)
        assert device.pin_code == pin
        assert len(device.token) == 64  # hex(32) = 64 chars
        assert device.is_active is True
        assert device.pin_expires_at is not None
        assert timezone.now() + timedelta(minutes=9) <= device.pin_expires_at <= timezone.now() + timedelta(minutes=11)

    def test_deactivates_previous_device(self):
        club = ClubFactory()
        pin1 = generate_kiosk_pin(club_id=club.id)
        device1 = KioskDevice.objects.get(club=club, pin_code=pin1)

        pin2 = generate_kiosk_pin(club_id=club.id)

        device1.refresh_from_db()
        assert device1.is_active is False
        assert device1.deactivated_at is not None

        device2 = KioskDevice.objects.get(club=club, pin_code=pin2)
        assert device2.is_active is True

    def test_active_pin_collision_regenerates_across_clubs(self):
        club_a = ClubFactory()
        club_b = ClubFactory()

        with patch(
            "apps.attendance.services.kiosk.secrets.randbelow",
            side_effect=[123456, 123456, 234567],
        ):
            pin_a = generate_kiosk_pin(club_id=club_a.id)
            pin_b = generate_kiosk_pin(club_id=club_b.id)

        assert pin_a == "123456"
        assert pin_b == "234567"
        assert KioskDevice.objects.filter(pin_code=pin_a, is_active=True).count() == 1


@pytest.mark.django_db
class TestActivateKiosk:
    def test_valid_pin_returns_token_and_club(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)

        result = activate_kiosk(pin=pin)

        assert "token" in result
        assert result["club_id"] == club.id
        assert result["club_name"] == club.name
        assert len(result["token"]) == 64
        device = KioskDevice.objects.get(club=club)
        assert device.pin_expires_at is None

    def test_pin_cleared_after_activation(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        activate_kiosk(pin=pin)

        # PIN should be cleared -- can't reuse
        with pytest.raises(BusinessLogicError, match="PIN"):
            activate_kiosk(pin=pin)

    def test_success_clears_pin_and_cannot_be_reused_after_failures(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        throttle_key = f"test-success-clears:{club.id}"

        for _ in range(4):
            with pytest.raises(BusinessLogicError) as exc_info:
                activate_kiosk(pin="000000", throttle_key=throttle_key)
            assert exc_info.value.code == "invalid_kiosk_pin"

        result = activate_kiosk(pin=pin, throttle_key=throttle_key)
        assert result["club_id"] == club.id

        device = KioskDevice.objects.get(club=club)
        assert device.pin_code == ""
        assert device.pin_expires_at is None
        assert device.activation_failed_attempts == 0
        assert device.activation_locked_at is None

        with pytest.raises(BusinessLogicError) as exc_info:
            activate_kiosk(pin=pin, throttle_key=throttle_key)
        assert exc_info.value.code == "invalid_kiosk_pin"

    def test_invalid_pin_raises_error(self):
        with pytest.raises(BusinessLogicError, match="PIN"):
            activate_kiosk(pin="000000")

    def test_inactive_device_pin_raises_error(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        deactivate_kiosk(club_id=club.id)

        with pytest.raises(BusinessLogicError, match="PIN"):
            activate_kiosk(pin=pin)

    def test_expired_pin_is_rejected_and_cleared(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        KioskDevice.objects.filter(club=club).update(pin_expires_at=timezone.now() - timedelta(seconds=1))

        with pytest.raises(BusinessLogicError) as exc_info:
            activate_kiosk(pin=pin, throttle_key=f"test-expired:{club.id}")

        assert exc_info.value.code == "kiosk_pin_expired"
        device = KioskDevice.objects.get(club=club)
        assert device.pin_code == ""
        assert device.pin_expires_at is None
        assert device.is_active is False

    def test_repeated_failed_attempts_lock_activation_source(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        throttle_key = f"test-lockout:{club.id}"

        for _ in range(5):
            with pytest.raises(BusinessLogicError) as exc_info:
                activate_kiosk(pin="000000", throttle_key=throttle_key)
            assert exc_info.value.code == "invalid_kiosk_pin"

        with pytest.raises(BusinessLogicError) as exc_info:
            activate_kiosk(pin=pin, throttle_key=throttle_key)

        assert exc_info.value.code == "kiosk_activation_locked"
        device = KioskDevice.objects.get(club=club)
        assert device.pin_code == pin

    def test_distributed_failed_attempts_lock_public_activation_budget(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        wrong_pin = "000000" if pin != "000000" else "000001"

        for index in range(KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES):
            with pytest.raises(BusinessLogicError) as exc_info:
                activate_kiosk(pin=wrong_pin, throttle_key=f"source:{index}")
            assert exc_info.value.code == "invalid_kiosk_pin"

        with pytest.raises(BusinessLogicError) as exc_info:
            activate_kiosk(pin=pin, throttle_key="source:fresh")

        assert exc_info.value.code == "kiosk_activation_locked"
        device = KioskDevice.objects.get(club=club)
        assert device.pin_code == pin

    def test_global_public_budget_blocks_before_pin_lookup_after_atomic_limit(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)

        cache.set(
            _activation_global_cache_key("attempts"),
            KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES,
            KIOSK_ACTIVATION_FAILURE_WINDOW_SECONDS,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            activate_kiosk(pin=pin, throttle_key="source:fresh")

        assert exc_info.value.code == "kiosk_activation_locked"
        device = KioskDevice.objects.get(club=club)
        assert device.pin_code == pin

    def test_uses_select_for_update(self):
        """Activation must lock the device row to prevent two kiosks racing on the same PIN."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        if connection.vendor != "postgresql":
            pytest.skip("SELECT FOR UPDATE only meaningful on PostgreSQL")

        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)

        with CaptureQueriesContext(connection) as ctx:
            activate_kiosk(pin=pin)

        assert any("FOR UPDATE" in q["sql"] for q in ctx.captured_queries), \
            "Expected at least one query to use SELECT FOR UPDATE"


@pytest.mark.django_db
class TestDeactivateKiosk:
    def test_deactivates_active_device(self):
        club = ClubFactory()
        generate_kiosk_pin(club_id=club.id)

        deactivate_kiosk(club_id=club.id)

        device = KioskDevice.objects.get(club=club)
        assert device.is_active is False
        assert device.deactivated_at is not None

    def test_noop_when_no_active_device(self):
        club = ClubFactory()
        # Should not raise
        deactivate_kiosk(club_id=club.id)


# ──────────────────────────────────────────────
# Endpoint tests (activate, auth header)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestKioskActivateEndpoint:
    def test_activate_with_valid_pin(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/activate/",
            {"pin": pin},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["club_id"] == club.id
        assert data["club_name"] == club.name
        assert len(data["token"]) == 64

    def test_activate_with_invalid_pin(self):
        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/activate/",
            {"pin": "999999"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_wrong_guesses_with_no_matching_row_are_limited(self):
        client = Client()

        for _ in range(5):
            resp = client.post(
                "/api/checkins/kiosk/activate/",
                {"pin": "999999"},
                content_type="application/json",
                REMOTE_ADDR="198.51.100.10",
            )
            assert resp.status_code == 400
            assert resp.json()["code"] == "invalid_kiosk_pin"

        resp = client.post(
            "/api/checkins/kiosk/activate/",
            {"pin": "999999"},
            content_type="application/json",
            REMOTE_ADDR="198.51.100.10",
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "kiosk_activation_locked"


@pytest.mark.django_db
class TestKioskDeviceAuth:
    def test_valid_token_sets_request_club(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        result = activate_kiosk(pin=pin)

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "0000"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result["token"],
        )
        assert resp.status_code == 200

    def test_invalid_token_returns_401(self):
        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "0000"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN="invalid_token_value",
        )
        assert resp.status_code == 401

    def test_revoked_token_returns_401(self):
        club = ClubFactory()
        pin = generate_kiosk_pin(club_id=club.id)
        result = activate_kiosk(pin=pin)
        deactivate_kiosk(club_id=club.id)

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "0000"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result["token"],
        )
        assert resp.status_code == 401

    def test_tenant_isolation(self):
        """Device token from club A cannot access club B data."""
        club_a = ClubFactory()
        club_b = ClubFactory()

        pin_a = generate_kiosk_pin(club_id=club_a.id)
        result_a = activate_kiosk(pin=pin_a)

        StudentFactory(club=club_b, phone="+79001111111", status="active")

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "1111"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result_a["token"],
        )
        assert resp.status_code == 200
        # Club A's token should not see Club B's students
        assert resp.json() == []

    def test_kiosk_runtime_lookup_rejects_jwt_only(self, monkeypatch):
        """Kiosk runtime endpoints must require device auth, not user JWT auth."""
        club = ClubFactory()
        user = UserFactory()
        membership = ClubMembershipFactory(user=user, club=club, role="owner")
        StudentFactory(club=club, phone="+79001112222", status="active")

        def fake_jwt_auth(self, request):
            request.user = user
            request.club = club
            request._membership = membership
            return {"club_id": club.id, "role": "owner"}

        monkeypatch.setattr("apps.common.auth.TenantJWTAuth.__call__", fake_jwt_auth)

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "2222"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer redacted-test-token",
        )

        assert resp.status_code == 401

    def test_authorization_header_cannot_override_device_club_scope(self, monkeypatch):
        """If both headers exist, X-Kiosk-Token remains the source of tenant scope."""
        device_club = ClubFactory()
        jwt_club = ClubFactory()
        user = UserFactory()
        membership = ClubMembershipFactory(user=user, club=jwt_club, role="owner")

        device_student = StudentFactory(
            club=device_club,
            phone="+79001112222",
            first_name="DeviceClub",
            status="active",
        )
        StudentFactory(
            club=jwt_club,
            phone="+79003332222",
            first_name="JwtClub",
            status="active",
        )

        pin = generate_kiosk_pin(club_id=device_club.id)
        token = activate_kiosk(pin=pin)["token"]

        def fake_jwt_auth(self, request):
            request.user = user
            request.club = jwt_club
            request._membership = membership
            return {"club_id": jwt_club.id, "role": "owner"}

        monkeypatch.setattr("apps.common.auth.TenantJWTAuth.__call__", fake_jwt_auth)

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "2222"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer redacted-test-token",
            HTTP_X_KIOSK_TOKEN=token,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert [item["id"] for item in data] == [device_student.id]


# ──────────────────────────────────────────────
# B-10: Cross-club device auth isolation
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestCrossClubDeviceAuth:
    def test_kiosk_device_token_rejects_other_club(self):
        """Each kiosk device token only sees its own club's students."""
        club_a = ClubFactory()
        club_b = ClubFactory()

        StudentFactory(club=club_a, phone="+79001112222", status="active")
        StudentFactory(club=club_b, phone="+79003332222", status="active")

        pin_a = generate_kiosk_pin(club_id=club_a.id)
        result_a = activate_kiosk(pin=pin_a)

        pin_b = generate_kiosk_pin(club_id=club_b.id)
        result_b = activate_kiosk(pin=pin_b)

        client = Client()

        # Club A token sees only club A student
        resp_a = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "2222"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result_a["token"],
        )
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert len(data_a) == 1
        assert data_a[0]["masked_phone"].endswith("2222")
        assert "phone" not in data_a[0]

        # Club B token sees only club B student
        resp_b = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "2222"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result_b["token"],
        )
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert len(data_b) == 1
        assert data_b[0]["masked_phone"].endswith("2222")
        assert "phone" not in data_b[0]

    def test_kiosk_device_token_rejects_inactive_club(self):
        """Kiosk device for an inactive club returns 401."""
        club = ClubFactory(is_active=False)
        # Temporarily set active to create device
        club.is_active = True
        club.save(update_fields=["is_active"])

        pin = generate_kiosk_pin(club_id=club.id)
        result = activate_kiosk(pin=pin)

        # Now deactivate the club
        club.is_active = False
        club.save(update_fields=["is_active"])

        client = Client()
        resp = client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "0000"},
            content_type="application/json",
            HTTP_X_KIOSK_TOKEN=result["token"],
        )
        assert resp.status_code == 401


# ──────────────────────────────────────────────
# B-9: Concurrent PIN generation
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConcurrentPinGeneration:
    def test_concurrent_pin_generation_single_active_device(self):
        """Sequential PIN generation always leaves exactly one active device."""
        club = ClubFactory()

        pin1 = generate_kiosk_pin(club_id=club.id)
        pin2 = generate_kiosk_pin(club_id=club.id)

        # Only one active device at a time
        assert KioskDevice.objects.filter(club_id=club.id, is_active=True).count() == 1

        # PINs are different
        assert pin1 != pin2

        # The active device has the latest PIN
        active = KioskDevice.objects.get(club_id=club.id, is_active=True)
        assert active.pin_code == pin2
