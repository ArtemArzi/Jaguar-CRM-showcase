import pytest
from ninja.testing import TestClient

from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    ClubSettingsFactory,
    LocationFactory,
    UserFactory,
)
from apps.common.tests.helpers import make_auth_params as _auth_params
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestRegisterClubAPI:
    def test_register_club_returns_201(self):
        user = UserFactory()
        response = client.post(
            "/clubs/register/",
            json={"name": "New Club", "city": "Moscow", "disciplines": ["boxing"]},
            user=user,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Club"
        assert data["city"] == "Moscow"
        assert data["is_active"] is True

    def test_register_club_unauthenticated(self):
        response = client.post(
            "/clubs/register/",
            json={"name": "New Club", "city": "Moscow"},
        )
        # Without user, request.user is AnonymousUser -> 403
        assert response.status_code == 403


@pytest.mark.django_db
class TestClubContextAPI:
    def test_member_can_read_own_club_context(self):
        club = ClubFactory(
            name="Context Club",
            city="Ufa",
            disciplines=["muay_thai"],
            timezone="Asia/Yekaterinburg",
        )
        owner = UserFactory()

        response = client.get("/clubs/me/", **_auth_params(owner, club))

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == club.id
        assert data["name"] == "Context Club"
        assert data["city"] == "Ufa"
        assert data["disciplines"] == ["muay_thai"]
        assert data["timezone"] == "Asia/Yekaterinburg"

    def test_owner_can_update_club_settings(self):
        club = ClubFactory(name="Old Club", city="Old City", disciplines=["boxing"])
        owner = UserFactory()

        response = client.put(
            "/clubs/settings/",
            json={"name": "Updated Club", "city": "Kazan", "disciplines": ["mma", "grappling"]},
            **_auth_params(owner, club),
        )

        assert response.status_code == 200
        club.refresh_from_db()
        assert club.name == "Updated Club"
        assert club.city == "Kazan"
        assert club.disciplines == ["mma", "grappling"]

    def test_non_owner_cannot_update_club_settings(self):
        club = ClubFactory()
        trainer = UserFactory()

        response = client.put(
            "/clubs/settings/",
            json={"name": "Trainer Edit"},
            **_auth_params(trainer, club, role=ClubMembership.Role.TRAINER),
        )

        assert response.status_code == 403
        club.refresh_from_db()
        assert club.name != "Trainer Edit"

    def test_owner_reads_fail_closed_commercial_journey_readiness(self, club):
        owner = UserFactory()
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)

        response = client.get("/clubs/commercial-journey-capability/", **_auth_params(owner, club))

        assert response.status_code == 200
        assert response.json() == {
            "commercial_journey_protocol_version": "v1",
            "unified_client_journey_enabled": False,
            "manual_operational_admission_enabled": False,
            "v2_commercial_journey_enabled": False,
            "v2_manual_admission_enabled": False,
            "v1_new_command_status": "commercial_journey_unavailable",
            "v2_personal_manual_command_status": "commercial_journey_unavailable",
            "v2_personal_sbp_command_status": "commercial_journey_unavailable",
            "v2_group_manual_command_status": "commercial_journey_unavailable",
            "v2_group_sbp_command_status": "commercial_journey_unavailable",
            "accepted_replay_status": "replay_or_drain",
        }

    def test_owner_sees_sbp_readiness_separately_from_manual_admission(self, club, settings):
        owner = UserFactory()
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version="v2",
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
        settings.DEBUG = True
        settings.PAYMENT_PROVIDER = "mock"
        settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
        settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True

        response = client.get("/clubs/commercial-journey-capability/", **_auth_params(owner, club))

        assert response.status_code == 200
        assert response.json()["v2_personal_manual_command_status"] == "commercial_journey_unavailable"
        assert response.json()["v2_personal_sbp_command_status"] == "available"
        assert response.json()["v2_group_manual_command_status"] == "commercial_journey_unavailable"
        assert response.json()["v2_group_sbp_command_status"] == "commercial_journey_unavailable"

    def test_trainer_cannot_read_commercial_journey_rollout_readiness(self, club):
        trainer = UserFactory()

        response = client.get(
            "/clubs/commercial-journey-capability/",
            **_auth_params(trainer, club, role=ClubMembership.Role.TRAINER),
        )

        assert response.status_code == 403

    def test_staff_can_list_own_club_locations(self):
        club = ClubFactory()
        LocationFactory(club=club, name="Main Hall", address="Main street")
        LocationFactory(club=club, name="Small Hall")
        LocationFactory(club=ClubFactory(), name="Foreign Hall")
        trainer = UserFactory()

        response = client.get("/clubs/locations/", **_auth_params(trainer, club, role=ClubMembership.Role.TRAINER))

        assert response.status_code == 200
        names = {item["name"] for item in response.json()}
        assert names == {"Main Hall", "Small Hall"}

    def test_owner_lists_only_active_own_club_members(self):
        club = ClubFactory()
        owner = UserFactory()
        admin = UserFactory()
        inactive = UserFactory()
        foreign = UserFactory()
        ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
        ClubMembershipFactory(user=admin, club=club, role=ClubMembership.Role.ADMIN)
        ClubMembershipFactory(user=inactive, club=club, role=ClubMembership.Role.TRAINER, is_active=False)
        ClubMembershipFactory(user=foreign, club=ClubFactory(), role=ClubMembership.Role.ADMIN)

        response = client.get("/clubs/members/", **_auth_params(owner, club))

        assert response.status_code == 200
        rows = response.json()
        emails = {row["user_email"] for row in rows}
        assert emails == {owner.email, admin.email}
        assert {row["role"] for row in rows} == {ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN}

    def test_trainer_cannot_list_club_members(self):
        club = ClubFactory()
        trainer = UserFactory()

        response = client.get("/clubs/members/", **_auth_params(trainer, club, role=ClubMembership.Role.TRAINER))

        assert response.status_code == 403
