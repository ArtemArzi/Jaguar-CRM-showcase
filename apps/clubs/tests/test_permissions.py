from types import SimpleNamespace

import pytest
from ninja.errors import HttpError

from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    MANAGEMENT_ROLES,
    OWNER,
    PARENT,
    STAFF_ROLES,
    STUDENT,
    TRAINER,
    management_view_required,
    role_required,
    view_role_required,
)


def _make_request(user, club, role):
    """Create a request-like object with _membership set."""
    from apps.clubs.models import ClubMembership

    membership = ClubMembership.objects.filter(user=user, club=club).first()
    if not membership:
        membership = ClubMembershipFactory(user=user, club=club, role=role)
    return SimpleNamespace(user=user, club=club, _membership=membership)


@pytest.mark.django_db
class TestRoleRequired:
    """Test role_required decorator with all 5 roles."""

    def test_owner_can_access_owner_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "owner")

        @role_required("owner")
        def endpoint(request):
            return "ok"

        assert endpoint(request) == "ok"

    def test_admin_can_access_management_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "admin")

        @role_required("owner", "admin")
        def endpoint(request):
            return "ok"

        assert endpoint(request) == "ok"

    def test_trainer_cannot_access_owner_only_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "trainer")

        @role_required("owner")
        def endpoint(request):
            return "ok"

        with pytest.raises(HttpError) as exc_info:
            endpoint(request)
        assert exc_info.value.status_code == 403

    def test_student_can_access_all_roles_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "student")

        @role_required("owner", "admin", "trainer", "student", "parent")
        def endpoint(request):
            return "ok"

        assert endpoint(request) == "ok"

    def test_parent_cannot_access_staff_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "parent")

        @role_required("owner", "admin", "trainer")
        def endpoint(request):
            return "ok"

        with pytest.raises(HttpError) as exc_info:
            endpoint(request)
        assert exc_info.value.status_code == 403

    def test_no_membership_returns_401(self):
        request = SimpleNamespace(user=None, club=None, _membership=None)

        @role_required("owner")
        def endpoint(request):
            return "ok"

        with pytest.raises(HttpError) as exc_info:
            endpoint(request)
        assert exc_info.value.status_code == 401

    def test_all_five_roles_defined(self):
        """Verify all 5 roles are accessible as constants."""
        assert OWNER == "owner"
        assert ADMIN == "admin"
        assert TRAINER == "trainer"
        assert STUDENT == "student"
        assert PARENT == "parent"

    def test_staff_roles_group(self):
        assert STAFF_ROLES == {"owner", "admin", "trainer"}

    def test_management_roles_group(self):
        assert MANAGEMENT_ROLES == {"owner", "admin"}

    def test_all_roles_group(self):
        assert ALL_ROLES == {"owner", "admin", "trainer", "student", "parent"}

    def test_admin_cannot_access_owner_only_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "admin")

        @role_required("owner")
        def endpoint(request):
            return "ok"

        with pytest.raises(HttpError) as exc_info:
            endpoint(request)
        assert exc_info.value.status_code == 403

    def test_trainer_can_access_staff_endpoint(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "trainer")

        @role_required("owner", "admin", "trainer")
        def endpoint(request):
            return "ok"

        assert endpoint(request) == "ok"

    def test_view_role_required_redirects_without_membership(self):
        request = SimpleNamespace(user=None, club=None, _membership=None)

        @view_role_required("owner")
        def view(request):
            return "ok"

        response = view(request)

        assert response.status_code == 302

    def test_view_role_required_allows_matching_role(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "owner")

        @view_role_required("owner")
        def view(request):
            return "ok"

        assert view(request) == "ok"

    def test_view_role_required_forbids_wrong_role(self):
        club = ClubFactory()
        user = UserFactory()
        request = _make_request(user, club, "trainer")

        @view_role_required("owner")
        def view(request):
            return "ok"

        response = view(request)

        assert response.status_code == 403

    def test_management_view_required_allows_owner_admin_only(self):
        club = ClubFactory()
        owner = UserFactory()
        trainer = UserFactory()
        owner_request = _make_request(owner, club, "owner")
        trainer_request = _make_request(trainer, club, "trainer")

        @management_view_required
        def view(request):
            return "ok"

        assert view(owner_request) == "ok"
        assert view(trainer_request).status_code == 403
