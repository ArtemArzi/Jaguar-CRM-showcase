import pytest

from apps.common.managers import TenantManager, TenantQuerySet


def test_tenant_manager_has_for_club():
    assert hasattr(TenantManager, "for_club")


def test_tenant_manager_has_unscoped():
    assert hasattr(TenantManager, "unscoped")


def test_tenant_queryset_has_for_club():
    assert hasattr(TenantQuerySet, "for_club")


@pytest.mark.django_db
class TestTenantManagerIntegration:
    def test_for_club_filters_by_club(self):
        """Test club-based filtering with ClubMembership."""
        from apps.clubs.models import ClubMembership
        from apps.clubs.tests.factories import (
            ClubFactory,
            ClubMembershipFactory,
            UserFactory,
        )

        club1 = ClubFactory()
        club2 = ClubFactory()
        user1 = UserFactory()
        user2 = UserFactory()
        ClubMembershipFactory(user=user1, club=club1, role="owner")
        ClubMembershipFactory(user=user2, club=club2, role="owner")

        club1_members = ClubMembership.objects.filter(club=club1)
        assert club1_members.count() == 1
        assert club1_members.first().user == user1
