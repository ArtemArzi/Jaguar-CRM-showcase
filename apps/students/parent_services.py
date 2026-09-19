from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.clubs.models import ClubMembership
from apps.common.exceptions import BusinessLogicError
from apps.students.models import ParentInvite, Student

logger = logging.getLogger(__name__)

INVITE_EXPIRY_DAYS = 7


def create_parent_invite(*, club_id: int, student_id: int) -> ParentInvite:
    student = Student.objects.for_club(club_id).get(id=student_id, deleted_at__isnull=True)

    if not student.is_child:
        raise BusinessLogicError("Only child students can have parent invites", code="not_child")
    if student.parent_user_id is not None:
        raise BusinessLogicError("Child already has a linked parent", code="parent_already_linked")

    now = timezone.now()

    # Invalidate previous active invites for this student
    ParentInvite.objects.for_club(club_id).filter(
        student_id=student_id,
        accepted_at__isnull=True,
    ).update(expires_at=now)

    invite = ParentInvite.objects.create(
        club_id=club_id,
        student_id=student_id,
        expires_at=now + timedelta(days=INVITE_EXPIRY_DAYS),
    )

    logger.info(
        "parent_invite_created",
        extra={"invite_id": invite.id, "student_id": student_id, "club_id": club_id},
    )
    return invite


def accept_parent_invite(*, token: uuid.UUID, user_id: int) -> Student:
    with transaction.atomic():
        try:
            invite = (
                ParentInvite.objects.select_for_update()
                .select_related("student", "club")
                .get(token=token)
            )
        except ParentInvite.DoesNotExist:
            raise BusinessLogicError("Invite not found", code="invite_not_found")

        if invite.accepted_at is not None:
            raise BusinessLogicError("Invite already used", code="invite_used")

        if invite.expires_at <= timezone.now():
            raise BusinessLogicError("Invite expired", code="invite_expired")

        if invite.student.deleted_at is not None:
            raise BusinessLogicError("Student not found", code="student_deleted")

        if not invite.student.is_child:
            raise BusinessLogicError("Only child students can have parent invites", code="not_child")

        if invite.student.parent_user_id is not None:
            raise BusinessLogicError("Child already has a linked parent", code="parent_already_linked")

        membership = ClubMembership.objects.filter(user_id=user_id, club=invite.club).first()
        if membership is not None and membership.role != ClubMembership.Role.PARENT:
            raise BusinessLogicError("Already a member of this club", code="already_member")

        invite.accepted_at = timezone.now()
        invite.accepted_by_id = user_id
        invite.save(update_fields=["accepted_at", "accepted_by_id", "updated_at"])

        invite.student.parent_user_id = user_id
        invite.student.save(update_fields=["parent_user_id", "updated_at"])

        if membership is None:
            ClubMembership.objects.create(
                user_id=user_id,
                club=invite.club,
                role=ClubMembership.Role.PARENT,
            )
        elif not membership.is_active:
            membership.is_active = True
            membership.save(update_fields=["is_active", "updated_at"])

    logger.info(
        "parent_invite_accepted",
        extra={
            "invite_id": invite.id,
            "student_id": invite.student_id,
            "user_id": user_id,
            "club_id": invite.club_id,
        },
    )
    return invite.student
