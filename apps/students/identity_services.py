"""Shared person identity normalization and club-scoped duplicate arbitration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError
from apps.common.phone import is_valid_normalized_phone, normalize_phone
from apps.students.duplicates import (
    StaffIntakeIdentityResolution,
    find_duplicate_for_contact,
    find_existing_for_public_intake,
    resolve_staff_intake_identity,
)
from apps.students.models import Student

IdentityPurpose = Literal["staff_create", "public_intake"]


@dataclass(frozen=True)
class NormalizedPersonIdentity:
    first_name: str
    last_name: str
    is_child: bool
    phone: str
    guardian_phone: str
    date_of_birth: date | None = None


def normalize_person_identity(
    *,
    first_name: str,
    last_name: str = "",
    is_child: bool,
    phone: str,
    guardian_phone: str = "",
    date_of_birth: date | None = None,
    invalid_phone_message: str = "Введите корректный телефон",
) -> NormalizedPersonIdentity:
    """Normalize and validate the person contact rules shared by every intake path."""

    normalized_phone = _normalize_optional_phone(phone)
    normalized_guardian_phone = _normalize_optional_phone(guardian_phone)
    if is_child:
        if not normalized_guardian_phone:
            normalized_guardian_phone = normalized_phone
            normalized_phone = ""
        _require_valid_phone(
            normalized_guardian_phone,
            code="guardian_phone_required",
            message=invalid_phone_message,
        )
        if normalized_phone:
            _require_valid_phone(normalized_phone, message=invalid_phone_message)
    else:
        _require_valid_phone(normalized_phone, message=invalid_phone_message)
        normalized_guardian_phone = ""

    return NormalizedPersonIdentity(
        first_name=first_name,
        last_name=last_name,
        is_child=is_child,
        phone=normalized_phone,
        guardian_phone=normalized_guardian_phone,
        date_of_birth=date_of_birth,
    )


def arbitrate_person_identity(
    *,
    club_id: int,
    identity: NormalizedPersonIdentity,
    purpose: IdentityPurpose,
) -> Student | None:
    """Lock one club row, then resolve an existing person under that arbitration lock.

    Callers must hold ``transaction.atomic()``. The Club lock serializes all current
    staff and public person-create paths within a tenant without cross-tenant impact.
    """

    lock_club_person_identity_arbitration(club_id=club_id)
    return resolve_person_identity(identity=identity, club_id=club_id, purpose=purpose)


def lock_club_person_identity_arbitration(*, club_id: int) -> Club:
    """Acquire the one short-lived club row lock used by every person create path."""

    return Club.objects.select_for_update(of=("self",)).get(id=club_id)


def resolve_person_identity(
    *,
    club_id: int,
    identity: NormalizedPersonIdentity,
    purpose: IdentityPurpose,
) -> Student | None:
    """Resolve a duplicate after ``lock_club_person_identity_arbitration``."""

    if purpose == "public_intake":
        return find_existing_for_public_intake(
            club_id=club_id,
            first_name=identity.first_name,
            is_child=identity.is_child,
            phone=identity.phone,
            guardian_phone=identity.guardian_phone,
        )
    return find_duplicate_for_contact(
        club_id=club_id,
        first_name=identity.first_name,
        last_name=identity.last_name,
        is_child=identity.is_child,
        phone=identity.phone,
        guardian_phone=identity.guardian_phone,
    )


def resolve_staff_intake_person_identity(
    *,
    club_id: int,
    identity: NormalizedPersonIdentity,
    confirm_distinct_child: bool,
) -> StaffIntakeIdentityResolution:
    """Resolve new staff-intake identity after the caller has locked the club."""

    return resolve_staff_intake_identity(
        club_id=club_id,
        first_name=identity.first_name,
        last_name=identity.last_name,
        date_of_birth=identity.date_of_birth,
        is_child=identity.is_child,
        phone=identity.phone,
        guardian_phone=identity.guardian_phone,
        confirm_distinct_child=confirm_distinct_child,
    )


def _normalize_optional_phone(phone: str | None) -> str:
    return normalize_phone(phone or "") if str(phone or "").strip() else ""


def _require_valid_phone(
    phone: str,
    *,
    code: str = "invalid_phone",
    message: str,
) -> None:
    if not phone or not is_valid_normalized_phone(phone):
        raise BusinessLogicError(message, code=code)
