import pytest

from apps.attendance.models import TrainingGroupRolloutState
from apps.clubs.models import Club, ClubMembership, Location
from apps.clubs.services import register_club
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError


@pytest.mark.django_db
class TestRegisterClub:
    def test_creates_club_with_location_and_membership(self):
        user = UserFactory()
        club = register_club(user=user, name="Test Club", city="Moscow", disciplines=["boxing"])
        assert Club.objects.count() == 1
        assert Location.objects.filter(club=club).count() == 1
        assert ClubMembership.objects.filter(user=user, club=club, role="owner").exists()
        assert TrainingGroupRolloutState.objects.get(club=club).mode == TrainingGroupRolloutState.Mode.OFF

    def test_uses_custom_location_name(self):
        user = UserFactory()
        club = register_club(user=user, name="Test", city="SPb", location_name="Main Hall")
        location = Location.objects.get(club=club)
        assert location.name == "Main Hall"

    def test_rejects_second_club_for_owner(self):
        user = UserFactory()
        register_club(user=user, name="Club 1", city="Moscow")
        with pytest.raises(BusinessLogicError, match="already owns a club"):
            register_club(user=user, name="Club 2", city="SPb")

    def test_stores_disciplines(self):
        user = UserFactory()
        club = register_club(user=user, name="MMA Club", city="Moscow", disciplines=["mma", "bjj"])
        assert club.disciplines == ["mma", "bjj"]

    def test_default_location_name(self):
        user = UserFactory()
        club = register_club(user=user, name="BoxGym", city="Moscow")
        location = Location.objects.get(club=club)
        assert location.name == "BoxGym - Main"

    def test_club_factory_creates_one_off_rollout_state(self):
        from apps.clubs.tests.factories import ClubFactory

        club = ClubFactory()
        assert TrainingGroupRolloutState.objects.filter(club=club).count() == 1
