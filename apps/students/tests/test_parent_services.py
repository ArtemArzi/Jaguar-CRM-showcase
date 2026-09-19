from datetime import timedelta

import pytest
from django.utils import timezone

from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.models import AccountAccess, ParentInvite
from apps.students.tests.factories import ParentInviteFactory, StudentFactory


@pytest.mark.django_db
class TestCreateParentInvite:
    def test_creates_invite_for_child(self, club):
        student = StudentFactory(club=club, is_child=True)
        from apps.students.parent_services import create_parent_invite

        invite = create_parent_invite(club_id=club.id, student_id=student.id)

        assert invite.student_id == student.id
        assert invite.club_id == club.id
        assert invite.token is not None
        assert invite.expires_at > timezone.now()
        assert invite.expires_at <= timezone.now() + timedelta(days=7, seconds=5)
        assert invite.accepted_at is None

    def test_rejects_non_child(self, club):
        student = StudentFactory(club=club, is_child=False)
        from apps.students.parent_services import create_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            create_parent_invite(club_id=club.id, student_id=student.id)
        assert exc_info.value.code == "not_child"

    def test_rejects_child_that_already_has_parent(self, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        from apps.students.parent_services import create_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            create_parent_invite(club_id=club.id, student_id=student.id)
        assert exc_info.value.code == "parent_already_linked"
        assert ParentInvite.objects.for_club(club).filter(student=student).count() == 0

    def test_invalidates_previous_invites(self, club):
        student = StudentFactory(club=club, is_child=True)
        from apps.students.parent_services import create_parent_invite

        invite1 = create_parent_invite(club_id=club.id, student_id=student.id)
        invite2 = create_parent_invite(club_id=club.id, student_id=student.id)

        invite1.refresh_from_db()
        assert invite1.expires_at <= timezone.now()
        assert invite2.expires_at > timezone.now()

    def test_parent_invite_string_does_not_include_token(self, club):
        student = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=student)

        invite_label = str(invite)

        assert str(invite.token) not in invite_label
        assert "token" not in invite_label.lower()
        assert str(student) in invite_label


@pytest.mark.django_db
class TestAcceptParentInvite:
    def test_accept_valid_invite(self, club):
        student = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=student)
        user = UserFactory()
        from apps.students.parent_services import accept_parent_invite

        result = accept_parent_invite(token=invite.token, user_id=user.id)

        assert result.id == student.id
        student.refresh_from_db()
        assert student.parent_user_id == user.id
        invite.refresh_from_db()
        assert invite.accepted_at is not None
        assert invite.accepted_by_id == user.id
        assert ClubMembership.objects.filter(user=user, club=club, role="parent").exists()
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_reject_expired_token(self, club):
        student = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(
            club=club,
            student=student,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        user = UserFactory()
        from apps.students.parent_services import accept_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            accept_parent_invite(token=invite.token, user_id=user.id)
        assert exc_info.value.code == "invite_expired"

    def test_reject_used_token(self, club):
        student = StudentFactory(club=club, is_child=True)
        accepted_user = UserFactory()
        invite = ParentInviteFactory(
            club=club,
            student=student,
            accepted_at=timezone.now(),
            accepted_by=accepted_user,
        )
        new_user = UserFactory()
        from apps.students.parent_services import accept_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            accept_parent_invite(token=invite.token, user_id=new_user.id)
        assert exc_info.value.code == "invite_used"

    def test_reject_already_member(self, club):
        student = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=student)
        user = UserFactory()
        ClubMembership.objects.create(user=user, club=club, role="trainer")
        from apps.students.parent_services import accept_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            accept_parent_invite(token=invite.token, user_id=user.id)
        assert exc_info.value.code == "already_member"

    def test_accept_reuses_existing_parent_membership_for_second_child(self, club):
        first_child = StudentFactory(club=club, is_child=True)
        second_child = StudentFactory(club=club, is_child=True)
        user = UserFactory()
        first_child.parent_user = user
        first_child.save(update_fields=["parent_user"])
        membership = ClubMembership.objects.create(user=user, club=club, role="parent")
        invite = ParentInviteFactory(club=club, student=second_child)
        from apps.students.parent_services import accept_parent_invite

        accepted = accept_parent_invite(token=invite.token, user_id=user.id)

        assert accepted.id == second_child.id
        second_child.refresh_from_db()
        invite.refresh_from_db()
        membership.refresh_from_db()
        assert second_child.parent_user_id == user.id
        assert invite.accepted_by_id == user.id
        assert membership.is_active is True
        assert ClubMembership.objects.filter(user=user, club=club).count() == 1

    def test_accept_reactivates_existing_inactive_parent_membership(self, club):
        child = StudentFactory(club=club, is_child=True)
        user = UserFactory()
        membership = ClubMembership.objects.create(
            user=user,
            club=club,
            role="parent",
            is_active=False,
        )
        invite = ParentInviteFactory(club=club, student=child)
        from apps.students.parent_services import accept_parent_invite

        accept_parent_invite(token=invite.token, user_id=user.id)

        membership.refresh_from_db()
        assert membership.is_active is True

    def test_reject_non_child_invite_without_binding_parent(self, club):
        adult = StudentFactory(club=club, is_child=False)
        invite = ParentInviteFactory(club=club, student=adult)
        user = UserFactory()
        from apps.students.parent_services import accept_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            accept_parent_invite(token=invite.token, user_id=user.id)
        assert exc_info.value.code == "not_child"

        adult.refresh_from_db()
        invite.refresh_from_db()
        assert adult.parent_user_id is None
        assert invite.accepted_at is None
        assert not ClubMembership.objects.filter(user=user, club=club).exists()

    def test_reject_invite_when_child_already_has_parent_without_overwrite(self, club):
        existing_parent = UserFactory()
        child = StudentFactory(club=club, is_child=True, parent_user=existing_parent)
        invite = ParentInviteFactory(club=club, student=child)
        new_parent = UserFactory()
        from apps.students.parent_services import accept_parent_invite

        with pytest.raises(BusinessLogicError) as exc_info:
            accept_parent_invite(token=invite.token, user_id=new_parent.id)
        assert exc_info.value.code == "parent_already_linked"

        child.refresh_from_db()
        invite.refresh_from_db()
        assert child.parent_user_id == existing_parent.id
        assert invite.accepted_at is None
        assert not ClubMembership.objects.filter(user=new_parent, club=club).exists()


@pytest.mark.django_db
class TestParentInviteTenantIsolation:
    def test_invite_not_visible_cross_club(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        student_a = StudentFactory(club=club_a, is_child=True)
        ParentInviteFactory(club=club_a, student=student_a)

        assert ParentInvite.objects.for_club(club_a).count() == 1
        assert ParentInvite.objects.for_club(club_b).count() == 0
