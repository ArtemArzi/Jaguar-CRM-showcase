from __future__ import annotations

import secrets
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership
from apps.common.exceptions import BusinessLogicError
from apps.common.phone import is_valid_normalized_phone, normalize_phone
from apps.students.models import AccountAccess, Student

User = get_user_model()
TEMPORARY_PASSWORD_BYTES = 18


@dataclass(frozen=True)
class AccountAccessResult:
    user: User
    access: AccountAccess
    temporary_password: str | None
    created_user: bool
    created_membership: bool
    created_access: bool


def open_student_access(
    *,
    club_id: int,
    student_id: int,
    issued_by_id: int | None,
) -> AccountAccessResult:
    with transaction.atomic():
        student = _get_locked_student(club_id=club_id, student_id=student_id)
        _validate_accessible_student(student)
        if student.is_child:
            raise BusinessLogicError(
                "Child student access must be opened through parent access",
                code="child_requires_parent_access",
            )
        _validate_paid_access_eligibility(club_id=club_id, student=student)

        existing_access = _existing_access_for_student(
            club_id=club_id,
            student=student,
            role=AccountAccess.Role.STUDENT,
        )
        if existing_access is not None:
            _membership, created_membership = _confirm_membership(
                user=existing_access.user,
                club_id=club_id,
                role=ClubMembership.Role.STUDENT,
            )
            if student.user_id is None:
                student.user_id = existing_access.user_id
                student.save(update_fields=["user", "updated_at"])
            elif student.user_id != existing_access.user_id:
                raise BusinessLogicError(
                    "Student is linked to another account",
                    code="manual_review_required",
                )
            return AccountAccessResult(
                user=existing_access.user,
                access=existing_access,
                temporary_password=None,
                created_user=False,
                created_membership=created_membership,
                created_access=False,
            )

        user, created_user, temporary_password = _resolve_user_for_access(
            club_id=club_id,
            role=ClubMembership.Role.STUDENT,
            phone=student.phone,
            first_name=student.first_name,
            last_name=student.last_name,
            email=student.email,
        )
        _membership, created_membership = _confirm_membership(
            user=user,
            club_id=club_id,
            role=ClubMembership.Role.STUDENT,
        )
        access, created_access = _confirm_access(
            club_id=club_id,
            student=student,
            user=user,
            role=ClubMembership.Role.STUDENT,
            issued_by_id=issued_by_id,
            must_change_password=temporary_password is not None,
        )

        if student.user_id is None:
            student.user = user
            student.save(update_fields=["user", "updated_at"])
        elif student.user_id != user.id:
            raise BusinessLogicError(
                "Student is linked to another account",
                code="manual_review_required",
            )

    return AccountAccessResult(
        user=user,
        access=access,
        temporary_password=temporary_password if created_access else None,
        created_user=created_user,
        created_membership=created_membership,
        created_access=created_access,
    )


def open_parent_access(
    *,
    club_id: int,
    student_id: int,
    parent_phone: str | None,
    issued_by_id: int | None,
) -> AccountAccessResult:
    with transaction.atomic():
        student = _get_locked_student(club_id=club_id, student_id=student_id)
        _validate_accessible_student(student)
        if not student.is_child:
            raise BusinessLogicError("Only child students can have parent access", code="not_child")
        _validate_paid_access_eligibility(club_id=club_id, student=student)

        existing_access = _existing_access_for_student(
            club_id=club_id,
            student=student,
            role=AccountAccess.Role.PARENT,
        )
        if existing_access is not None:
            _membership, created_membership = _confirm_membership(
                user=existing_access.user,
                club_id=club_id,
                role=ClubMembership.Role.PARENT,
            )
            if student.parent_user_id is None:
                student.parent_user_id = existing_access.user_id
                student.save(update_fields=["parent_user", "updated_at"])
            elif student.parent_user_id != existing_access.user_id:
                raise BusinessLogicError(
                    "Child is linked to another parent account",
                    code="manual_review_required",
                )
            return AccountAccessResult(
                user=existing_access.user,
                access=existing_access,
                temporary_password=None,
                created_user=False,
                created_membership=created_membership,
                created_access=False,
            )

        if parent_phone is None and student.parent_user_id is not None:
            user, temporary_password = _resolve_linked_parent_user_for_access(
                club_id=club_id,
                user_id=student.parent_user_id,
            )
            created_user = False
        else:
            if not parent_phone and student.guardian_phone:
                parent_phone = student.guardian_phone
            if not parent_phone:
                raise BusinessLogicError(
                    "Parent phone is required for parent account access",
                    code="parent_phone_required",
                )
            user, created_user, temporary_password = _resolve_user_for_access(
                club_id=club_id,
                role=ClubMembership.Role.PARENT,
                phone=parent_phone,
                first_name="",
                last_name="",
                email="",
            )
        _membership, created_membership = _confirm_membership(
            user=user,
            club_id=club_id,
            role=ClubMembership.Role.PARENT,
        )
        access, created_access = _confirm_access(
            club_id=club_id,
            student=student,
            user=user,
            role=ClubMembership.Role.PARENT,
            issued_by_id=issued_by_id,
            must_change_password=temporary_password is not None,
        )

        if student.parent_user_id is None:
            student.parent_user = user
            student.save(update_fields=["parent_user", "updated_at"])
        elif student.parent_user_id != user.id:
            raise BusinessLogicError(
                "Child is linked to another parent account",
                code="manual_review_required",
            )

    return AccountAccessResult(
        user=user,
        access=access,
        temporary_password=temporary_password if created_access else None,
        created_user=created_user,
        created_membership=created_membership,
        created_access=created_access,
    )


def open_account_access_for_student(
    *,
    club_id: int,
    student_id: int,
    parent_phone: str | None = None,
    issued_by_id: int | None,
) -> AccountAccessResult:
    student = _get_student(club_id=club_id, student_id=student_id)
    if student.is_child and student.parent_user_id is None and not parent_phone and not student.guardian_phone:
        raise BusinessLogicError(
            "Parent phone is required for parent account access",
            code="parent_phone_required",
        )
    if student.is_child:
        return open_parent_access(
            club_id=club_id,
            student_id=student_id,
            parent_phone=parent_phone,
            issued_by_id=issued_by_id,
        )
    return open_student_access(
        club_id=club_id,
        student_id=student_id,
        issued_by_id=issued_by_id,
    )


def reset_account_access_for_student(
    *,
    club_id: int,
    student_id: int,
    reset_by_id: int | None,
) -> AccountAccessResult:
    student = _get_student(club_id=club_id, student_id=student_id)
    role = AccountAccess.Role.PARENT if student.is_child else AccountAccess.Role.STUDENT
    return reset_account_access(
        club_id=club_id,
        student_id=student_id,
        role=role,
        reset_by_id=reset_by_id,
    )


def reset_account_access(
    *,
    club_id: int,
    student_id: int,
    role: str,
    reset_by_id: int | None,
) -> AccountAccessResult:
    valid_roles = {AccountAccess.Role.STUDENT, AccountAccess.Role.PARENT}
    if role not in valid_roles:
        raise BusinessLogicError("Invalid account access role", code="invalid_access_role")

    with transaction.atomic():
        access = (
            AccountAccess.objects.for_club(club_id)
            .select_for_update()
            .select_related("student", "user")
            .filter(student_id=student_id, role=role, student__deleted_at__isnull=True)
            .first()
        )
        if access is None:
            raise BusinessLogicError("Account access is not open", code="account_access_not_open")
        _validate_accessible_student(access.student)
        _validate_paid_access_eligibility(club_id=club_id, student=access.student)

        temporary_password = _generate_temporary_password()
        access.user.set_password(temporary_password)
        access.user.save(update_fields=["password"])

        now = timezone.now()
        access.status = AccountAccess.Status.RESET
        access.reset_by_id = reset_by_id
        access.reset_at = now
        access.must_change_password = True
        access.temporary_credential_revealed_at = now
        access.save(
            update_fields=[
                "status",
                "reset_by",
                "reset_at",
                "must_change_password",
                "temporary_credential_revealed_at",
                "updated_at",
            ]
        )

    return AccountAccessResult(
        user=access.user,
        access=access,
        temporary_password=temporary_password,
        created_user=False,
        created_membership=False,
        created_access=False,
    )


def _get_student(*, club_id: int, student_id: int) -> Student:
    try:
        return (
            Student.objects.for_club(club_id)
            .only("id", "is_child", "status", "parent_user", "guardian_phone")
            .get(
                id=student_id,
                deleted_at__isnull=True,
            )
        )
    except Student.DoesNotExist:
        raise BusinessLogicError("Student not found", code="student_not_found")


def _validate_paid_access_eligibility(*, club_id: int, student: Student) -> None:
    if student.status != Student.Status.ACTIVE:
        raise BusinessLogicError(
            "Account access requires an active student",
            code="account_access_requires_active_student",
        )

    from apps.students.selectors import is_account_access_eligible

    if not is_account_access_eligible(club=student.club, student=student):
        raise BusinessLogicError(
            "Account access requires a paid active subscription",
            code="account_access_requires_paid_subscription",
        )


def _get_locked_student(*, club_id: int, student_id: int) -> Student:
    # Payment review owns Club before the personal Student scope. Account
    # access creates tenant-scoped rows with deferred club FKs, so it must use
    # the same prefix instead of requesting a Club key-share only at commit.
    club = (
        Club.objects.select_for_update(of=("self",))
        .only("id")
        .filter(id=club_id)
        .first()
    )
    if club is None:
        raise BusinessLogicError("Student not found", code="student_not_found")
    try:
        return (
            Student.objects.for_club(club_id)
            .select_for_update()
            .get(id=student_id, deleted_at__isnull=True)
        )
    except Student.DoesNotExist:
        raise BusinessLogicError("Student not found", code="student_not_found")


def _validate_accessible_student(student: Student) -> None:
    if student.status == Student.Status.LOST:
        raise BusinessLogicError("Lost students cannot receive account access", code="student_not_accessible")


def _existing_access_for_student(*, club_id: int, student: Student, role: str) -> AccountAccess | None:
    return (
        AccountAccess.objects.for_club(club_id)
        .select_for_update()
        .select_related("user")
        .filter(student=student, role=role)
        .first()
    )


def _resolve_user_for_access(
    *,
    club_id: int,
    role: ClubMembership.Role,
    phone: str,
    first_name: str,
    last_name: str,
    email: str,
) -> tuple[User, bool, str | None]:
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        raise BusinessLogicError("Phone is required for account access", code="phone_required")
    if not is_valid_normalized_phone(normalized_phone):
        raise BusinessLogicError("Invalid phone for account access", code="invalid_phone")

    user = User.objects.select_for_update().filter(username=normalized_phone).first()
    if user is None:
        temporary_password = _generate_temporary_password()
        user = User(
            username=normalized_phone,
            email=email or "",
            first_name=first_name,
            last_name=last_name,
        )
        user.set_password(temporary_password)
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            user = User.objects.select_for_update().get(username=normalized_phone)
        else:
            return user, True, temporary_password

    active_memberships = ClubMembership.objects.select_for_update().filter(user=user, is_active=True)
    if active_memberships.exclude(club_id=club_id, role=role).exists():
        raise BusinessLogicError(
            "Phone is already attached to another active club or role",
            code="manual_review_required",
        )

    if active_memberships.filter(club_id=club_id, role=role).exists():
        return user, False, None

    temporary_password = _generate_temporary_password() if not user.has_usable_password() else None
    if temporary_password is not None:
        user.set_password(temporary_password)
        user.save(update_fields=["password"])
    return user, False, temporary_password


def _resolve_linked_parent_user_for_access(*, club_id: int, user_id: int) -> tuple[User, str | None]:
    user = User.objects.select_for_update().get(id=user_id)
    active_memberships = ClubMembership.objects.select_for_update().filter(user=user, is_active=True)
    if active_memberships.exclude(club_id=club_id, role=ClubMembership.Role.PARENT).exists():
        raise BusinessLogicError(
            "Parent account is already attached to another active club or role",
            code="manual_review_required",
        )

    temporary_password = _generate_temporary_password() if not user.has_usable_password() else None
    if temporary_password is not None:
        user.set_password(temporary_password)
        user.save(update_fields=["password"])
    return user, temporary_password


def _confirm_membership(
    *,
    user: User,
    club_id: int,
    role: ClubMembership.Role,
) -> tuple[ClubMembership, bool]:
    membership = ClubMembership.objects.select_for_update().filter(user=user, club_id=club_id).first()
    if membership is None:
        return ClubMembership.objects.create(user=user, club_id=club_id, role=role), True

    if membership.role != role:
        raise BusinessLogicError(
            "Phone is already attached to another role in this club",
            code="manual_review_required",
        )

    if not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active", "updated_at"])
    return membership, False


def _confirm_access(
    *,
    club_id: int,
    student: Student,
    user: User,
    role: ClubMembership.Role,
    issued_by_id: int | None,
    must_change_password: bool,
) -> tuple[AccountAccess, bool]:
    access = (
        AccountAccess.objects.for_club(club_id)
        .select_for_update()
        .filter(student=student, role=role)
        .first()
    )
    if access is not None:
        if access.user_id != user.id:
            raise BusinessLogicError(
                "Access is linked to another account",
                code="manual_review_required",
            )
        return access, False

    now = timezone.now()
    return (
        AccountAccess.objects.create(
            club_id=club_id,
            student=student,
            user=user,
            role=role,
            issued_by_id=issued_by_id,
            must_change_password=must_change_password,
            temporary_credential_revealed_at=now if must_change_password else None,
        ),
        True,
    )


def _generate_temporary_password() -> str:
    return secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
