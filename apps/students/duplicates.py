from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.db.models import Q

from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student


@dataclass(frozen=True)
class DuplicateStudentSnapshot:
    id: int
    first_name: str
    last_name: str
    is_child: bool
    status: str
    lead_status: str | None
    assigned_trainer_id: int | None


class DuplicateStudentError(BusinessLogicError):
    def __init__(self, student: Student, *, message: str = "Person with this phone already exists"):
        super().__init__(message, code="duplicate_phone")
        self.args = (message,)
        self.existing_student = DuplicateStudentSnapshot(
            id=student.id,
            first_name=student.first_name,
            last_name=student.last_name,
            is_child=student.is_child,
            status=student.status,
            lead_status=student.lead_status,
            assigned_trainer_id=student.assigned_trainer_id,
        )


@dataclass(frozen=True)
class StaffIntakeIdentityResolution:
    duplicate: Student | None = None
    soft_deleted: Student | None = None
    confirmation_required: bool = False
    ambiguous: Student | None = None


def _same_name(*, student: Student, first_name: str, last_name: str) -> bool:
    return (
        student.first_name.strip().casefold() == first_name.strip().casefold()
        and student.last_name.strip().casefold() == last_name.strip().casefold()
    )


def _contact_matches(
    *,
    club_id: int,
    contact: str,
    exclude_student_id: int | None = None,
    deleted_at_isnull: bool = True,
):
    qs = (
        Student.objects.for_club(club_id)
        .filter(Q(phone=contact) | Q(guardian_phone=contact), deleted_at__isnull=deleted_at_isnull)
        .order_by("id")
    )
    if exclude_student_id is not None:
        qs = qs.exclude(id=exclude_student_id)
    return qs


def contact_exists_for_club(*, club_id: int, contact: str) -> bool:
    return bool(contact) and _contact_matches(club_id=club_id, contact=contact).exists()


def existing_contacts_for_club(*, club_id: int) -> set[str]:
    contacts: set[str] = set()
    for phone, guardian_phone in (
        Student.objects.for_club(club_id)
        .filter(deleted_at__isnull=True)
        .values_list("phone", "guardian_phone")
    ):
        if phone:
            contacts.add(phone)
        if guardian_phone:
            contacts.add(guardian_phone)
    return contacts


def find_duplicate_for_contact(
    *,
    club_id: int,
    first_name: str,
    last_name: str,
    is_child: bool,
    phone: str,
    guardian_phone: str = "",
    exclude_student_id: int | None = None,
) -> Student | None:
    """Find same-card contact conflicts without making guardian phone globally unique.

    Siblings may share a guardian phone. Personal phone collisions and exact
    child-under-guardian collisions are still duplicates.
    """
    if phone:
        for deleted_at_isnull in (True, False):
            match = _contact_matches(
                club_id=club_id,
                contact=phone,
                exclude_student_id=exclude_student_id,
                deleted_at_isnull=deleted_at_isnull,
            ).first()
            if match is not None:
                return match

    if not is_child or not guardian_phone:
        return None

    for deleted_at_isnull in (True, False):
        for match in _contact_matches(
            club_id=club_id,
            contact=guardian_phone,
            exclude_student_id=exclude_student_id,
            deleted_at_isnull=deleted_at_isnull,
        ):
            if match.phone == guardian_phone:
                return match
            if match.is_child and match.guardian_phone == guardian_phone:
                same_first_name = match.first_name.strip().casefold() == first_name.strip().casefold()
                same_name = _same_name(student=match, first_name=first_name, last_name=last_name)
                public_child_shape = not match.last_name.strip() or not last_name.strip()
                if same_name or (same_first_name and public_child_shape):
                    return match
    return None


def find_existing_for_public_intake(
    *,
    club_id: int,
    first_name: str,
    is_child: bool,
    phone: str,
    guardian_phone: str = "",
) -> Student | None:
    if is_child:
        if guardian_phone:
            child_by_guardian = (
                Student.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    is_child=True,
                    guardian_phone=guardian_phone,
                )
            )
            # Public intake has no last-name field.  Under the same Club lock,
            # a same guardian + first-name child therefore resolves
            # conservatively even if an earlier staff card recorded a surname.
            # Staff can still add a genuine same-name sibling only with a DOB
            # distinction or explicit new-key confirmation.
            same_child = child_by_guardian.filter(first_name__iexact=first_name.strip())
            exact_child = same_child.filter(deleted_at__isnull=True).order_by("id").first()
            if exact_child is None:
                exact_child = same_child.filter(deleted_at__isnull=False).order_by("id").first()
            if exact_child is not None:
                return exact_child
        return None

    adults = (
        Student.objects.for_club(club_id)
        .select_for_update()
        .filter(Q(phone=phone) | Q(guardian_phone=phone))
    )
    return adults.filter(deleted_at__isnull=True).order_by("id").first() or adults.filter(
        deleted_at__isnull=False,
    ).order_by("id").first()


def resolve_staff_intake_identity(
    *,
    club_id: int,
    first_name: str,
    last_name: str,
    date_of_birth: date | None,
    is_child: bool,
    phone: str,
    guardian_phone: str,
    confirm_distinct_child: bool,
) -> StaffIntakeIdentityResolution:
    """Resolve intake conflicts without making a guardian number globally unique.

    Adults retain the established exact-contact behaviour.  For children, an
    exact guardian/name/DOB match is a duplicate; a same-name child lacking a
    DOB comparison is deliberately ambiguous until the staff member confirms a
    distinct sibling under a new idempotency key.
    """

    active_matches = _staff_intake_contact_matches(
        club_id=club_id,
        phone=phone,
        guardian_phone=guardian_phone,
        deleted_at_isnull=True,
    )
    active = _resolve_staff_intake_matches(
        matches=active_matches,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        is_child=is_child,
        phone=phone,
        guardian_phone=guardian_phone,
        confirm_distinct_child=confirm_distinct_child,
    )
    if active.duplicate is not None or active.confirmation_required:
        return active

    soft_deleted_matches = _staff_intake_contact_matches(
        club_id=club_id,
        phone=phone,
        guardian_phone=guardian_phone,
        deleted_at_isnull=False,
    )
    soft_deleted = _resolve_staff_intake_matches(
        matches=soft_deleted_matches,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        is_child=is_child,
        phone=phone,
        guardian_phone=guardian_phone,
        # A confirmation only permits a genuinely distinct active sibling. It
        # must never make a soft-deleted exact/ambiguous identity creatable.
        confirm_distinct_child=False,
    )
    if soft_deleted.duplicate is not None or soft_deleted.ambiguous is not None:
        return StaffIntakeIdentityResolution(
            soft_deleted=soft_deleted.duplicate or soft_deleted.ambiguous,
        )
    return StaffIntakeIdentityResolution()


def _staff_intake_contact_matches(
    *,
    club_id: int,
    phone: str,
    guardian_phone: str,
    deleted_at_isnull: bool,
):
    contacts = [contact for contact in (phone, guardian_phone) if contact]
    if not contacts:
        return Student.objects.none()
    return (
        Student.objects.for_club(club_id)
        .filter(Q(phone__in=contacts) | Q(guardian_phone__in=contacts), deleted_at__isnull=deleted_at_isnull)
        .order_by("id")
    )


def _resolve_staff_intake_matches(
    *,
    matches,
    first_name: str,
    last_name: str,
    date_of_birth: date | None,
    is_child: bool,
    phone: str,
    guardian_phone: str,
    confirm_distinct_child: bool,
) -> StaffIntakeIdentityResolution:
    for match in matches:
        if phone and (match.phone == phone or match.guardian_phone == phone):
            return StaffIntakeIdentityResolution(duplicate=match)
        if not is_child or not guardian_phone:
            continue
        if match.phone == guardian_phone:
            return StaffIntakeIdentityResolution(duplicate=match)
        if not (match.is_child and match.guardian_phone == guardian_phone):
            continue
        same_first_name = match.first_name.strip().casefold() == first_name.strip().casefold()
        same_name = _same_name(student=match, first_name=first_name, last_name=last_name)
        # A public child has no surname, so its pre-existing card must be
        # protected from a parallel staff intake that supplies one.
        public_child_shape = not match.last_name.strip() or not last_name.strip()
        if not same_name and not (same_first_name and public_child_shape):
            continue
        if date_of_birth is not None and match.date_of_birth is not None:
            if match.date_of_birth == date_of_birth:
                return StaffIntakeIdentityResolution(duplicate=match)
            continue
        if not confirm_distinct_child:
            return StaffIntakeIdentityResolution(
                confirmation_required=True,
                ambiguous=match,
            )
    return StaffIntakeIdentityResolution()
