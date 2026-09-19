"""Typed, privacy-safe staff intake command service."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.clubs.models import ClubMembership
from apps.common.exceptions import BusinessLogicError
from apps.leads.services import create_lead
from apps.students.identity_services import (
    NormalizedPersonIdentity,
    lock_club_person_identity_arbitration,
    normalize_person_identity,
    resolve_staff_intake_person_identity,
)
from apps.students.models import Student, StudentIntakeCommand
from apps.students.scopes import trainer_student_scope_filter
from apps.trainers.models import Trainer

MAX_PERSON_NAME_LENGTH = 100


@dataclass(frozen=True)
class StudentIntakeResult:
    result_kind: str
    target_workspace: str
    route: str | None
    identity_visibility: str
    allowed_action: str | None
    student_id: int | None = None
    detail: str | None = None
    code: str | None = None
    commercial_segment: Literal["no_crm_entitlement"] | None = None
    replayed: bool = False

    def as_receipt(self) -> dict:
        receipt = {
            "result_kind": self.result_kind,
            "target_workspace": self.target_workspace,
            "route": self.route,
            "identity_visibility": self.identity_visibility,
            "allowed_action": self.allowed_action,
        }
        if self.student_id is not None:
            receipt["student_id"] = self.student_id
        if self.detail is not None:
            receipt["detail"] = self.detail
        if self.code is not None:
            receipt["code"] = self.code
        if self.commercial_segment is not None:
            receipt["commercial_segment"] = self.commercial_segment
        return receipt

    @classmethod
    def from_receipt(cls, receipt: dict, *, replayed: bool = False) -> StudentIntakeResult:
        return cls(
            result_kind=receipt["result_kind"],
            target_workspace=receipt["target_workspace"],
            route=receipt.get("route"),
            identity_visibility=receipt["identity_visibility"],
            allowed_action=receipt.get("allowed_action"),
            student_id=receipt.get("student_id"),
            detail=receipt.get("detail"),
            code=receipt.get("code"),
            commercial_segment=receipt.get("commercial_segment"),
            replayed=replayed,
        )

    @property
    def is_conflict(self) -> bool:
        return self.result_kind in {
            "duplicate",
            "identity_requires_owner_review",
            "child_confirmation_required",
            "idempotency_conflict",
            "readiness_anomaly",
        }


def submit_student_intake(
    *,
    club_id: int,
    actor_user_id: int,
    actor_role: str,
    actor_trainer_id: int | None,
    idempotency_key: UUID,
    intake_kind: str,
    first_name: str,
    last_name: str,
    phone: str,
    guardian_phone: str,
    date_of_birth: date | None,
    is_child: bool,
    source: str,
    assigned_trainer_id: int | None,
    confirm_distinct_child: bool,
) -> StudentIntakeResult:
    """Persist and execute one staff intake with club-scoped serialization.

    The caller supplies only server-derived actor and trainer context.  A Club
    row lock protects both person identity arbitration and durable command-key
    replay against the public and legacy create paths sharing that same lock.
    """

    if intake_kind not in StudentIntakeCommand.IntakeKind.values:
        raise BusinessLogicError("Invalid intake kind", code="invalid_intake_kind")
    if source not in Student.Source.values:
        raise BusinessLogicError("Invalid student source", code="invalid_student_source")
    if actor_role not in {
        ClubMembership.Role.OWNER,
        ClubMembership.Role.ADMIN,
        ClubMembership.Role.TRAINER,
    }:
        raise BusinessLogicError("Staff role required", code="staff_role_required")

    first_name = _normalize_intake_name(
        value=first_name,
        field="first_name",
        required=True,
    )
    last_name = _normalize_intake_name(
        value=last_name,
        field="last_name",
        required=False,
    )

    identity = normalize_person_identity(
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        is_child=is_child,
        phone=phone,
        guardian_phone=guardian_phone,
    )
    effective_trainer_id = _effective_assigned_trainer_id(
        actor_role=actor_role,
        actor_trainer_id=actor_trainer_id,
        requested_trainer_id=assigned_trainer_id,
    )
    fingerprint = _request_fingerprint(
        actor_user_id=actor_user_id,
        intake_kind=intake_kind,
        identity=identity,
        source=source,
        assigned_trainer_id=effective_trainer_id,
        confirm_distinct_child=confirm_distinct_child,
    )

    with transaction.atomic():
        lock_club_person_identity_arbitration(club_id=club_id)
        existing_command = (
            StudentIntakeCommand.objects.for_club(club_id)
            .select_for_update()
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_command is not None:
            if not hmac.compare_digest(existing_command.request_fingerprint, fingerprint):
                return _idempotency_conflict_result()
            return StudentIntakeResult.from_receipt(existing_command.result_receipt, replayed=True)

        _require_active_trainer(club_id=club_id, trainer_id=effective_trainer_id)
        identity_resolution = resolve_staff_intake_person_identity(
            club_id=club_id,
            identity=identity,
            confirm_distinct_child=confirm_distinct_child,
        )
        receipt_student_id = None
        if identity_resolution.soft_deleted is not None:
            receipt_student_id = identity_resolution.soft_deleted.id
            result = _soft_deleted_result(
                actor_role=actor_role,
                student=identity_resolution.soft_deleted,
            )
        elif identity_resolution.confirmation_required:
            result = StudentIntakeResult(
                result_kind="child_confirmation_required",
                target_workspace="none",
                route=None,
                identity_visibility="none",
                allowed_action="confirm_distinct_child",
                detail="Подтвердите, что это другой ребёнок, и отправьте новую попытку.",
                code="possible_child_duplicate_confirmation_required",
            )
        elif identity_resolution.duplicate is not None:
            receipt_student_id = identity_resolution.duplicate.id
            result = _duplicate_result(
                club_id=club_id,
                actor_role=actor_role,
                actor_trainer_id=actor_trainer_id,
                student=identity_resolution.duplicate,
            )
        elif intake_kind == StudentIntakeCommand.IntakeKind.NEW_CONTACT:
            student = create_lead(
                club_id=club_id,
                first_name=identity.first_name,
                last_name=identity.last_name,
                phone=identity.phone,
                guardian_phone=identity.guardian_phone,
                date_of_birth=identity.date_of_birth,
                is_child=identity.is_child,
                source=source,
                assigned_trainer_id=effective_trainer_id,
                crm_entry_kind=Student.CrmEntryKind.LEAD_INTAKE,
                crm_entered_by_id=actor_user_id,
                confirm_distinct_child=confirm_distinct_child,
            )
            result = _created_result(
                actor_role=actor_role,
                student=student,
                result_kind="created_new_contact",
            )
            receipt_student_id = student.id
        else:
            student = Student(
                club_id=club_id,
                first_name=identity.first_name,
                last_name=identity.last_name,
                phone=identity.phone,
                guardian_phone=identity.guardian_phone,
                date_of_birth=identity.date_of_birth,
                is_child=identity.is_child,
                source=source,
                status=Student.Status.ACTIVE,
                lead_status=None,
                assigned_trainer_id=effective_trainer_id,
                crm_entry_kind=Student.CrmEntryKind.EXISTING_STUDENT,
                crm_entered_by_id=actor_user_id,
            )
            _save_new_existing_student(student=student)
            result = _created_result(
                actor_role=actor_role,
                student=student,
                result_kind="created_existing_student",
            )
            receipt_student_id = student.id

        StudentIntakeCommand.objects.create(
            club_id=club_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            student_id=receipt_student_id,
            intake_kind=intake_kind,
            result_kind=result.result_kind,
            result_receipt=result.as_receipt(),
            created_by_id=actor_user_id,
        )
        return result


def _effective_assigned_trainer_id(
    *,
    actor_role: str,
    actor_trainer_id: int | None,
    requested_trainer_id: int | None,
) -> int | None:
    if actor_role == ClubMembership.Role.TRAINER:
        if actor_trainer_id is None:
            raise BusinessLogicError("Trainer profile is required", code="trainer_profile_required")
        return actor_trainer_id
    return requested_trainer_id


def _require_active_trainer(*, club_id: int, trainer_id: int | None) -> None:
    if trainer_id is None:
        return
    if not Trainer.objects.for_club(club_id).filter(id=trainer_id, is_active=True).exists():
        raise BusinessLogicError("Trainer does not belong to this club", code="trainer_club_mismatch")


def _request_fingerprint(
    *,
    actor_user_id: int,
    intake_kind: str,
    identity: NormalizedPersonIdentity,
    source: str,
    assigned_trainer_id: int | None,
    confirm_distinct_child: bool,
) -> str:
    """Return a keyed, non-reversible representation; never persist raw contact."""

    payload = json.dumps(
        {
            "actor_user_id": actor_user_id,
            "assigned_trainer_id": assigned_trainer_id,
            "confirm_distinct_child": confirm_distinct_child,
            "date_of_birth": identity.date_of_birth.isoformat() if identity.date_of_birth else None,
            "first_name": identity.first_name.casefold(),
            "guardian_phone": identity.guardian_phone,
            "intake_kind": intake_kind,
            "is_child": identity.is_child,
            "last_name": identity.last_name.casefold(),
            "phone": identity.phone,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def _save_new_existing_student(*, student: Student) -> None:
    try:
        student.full_clean()
    except ValidationError as exc:
        raise BusinessLogicError("Invalid existing student intake", code="invalid_existing_student_intake") from exc
    student.save()
    # ``created_at`` is assigned by the insert.  The controlled follow-up
    # update keeps the first student-state timestamp exact rather than merely
    # close to the wall-clock time of intake processing.
    student.became_student_at = student.created_at
    Student.objects.for_club(student.club_id).filter(id=student.id).update(
        became_student_at=student.created_at,
    )


def _created_result(*, actor_role: str, student: Student, result_kind: str) -> StudentIntakeResult:
    return StudentIntakeResult(
        result_kind=result_kind,
        target_workspace="leads_active" if student.lead_status is not None else "students",
        route=_authorized_route(actor_role=actor_role, student=student),
        identity_visibility="full",
        allowed_action="open",
        student_id=student.id,
        code=("no_crm_entitlement" if result_kind == "created_existing_student" else None),
        commercial_segment=(
            "no_crm_entitlement" if result_kind == "created_existing_student" else None
        ),
    )


def _duplicate_result(
    *,
    club_id: int,
    actor_role: str,
    actor_trainer_id: int | None,
    student: Student,
) -> StudentIntakeResult:
    if _workspace_for(student=student) == "none":
        return _workspace_truth_table_anomaly_result()

    if actor_role != ClubMembership.Role.TRAINER:
        is_archived = student.status == Student.Status.LOST
        has_never_been_student = student.became_student_at is None
        return StudentIntakeResult(
            result_kind="duplicate",
            target_workspace=_workspace_for(student=student),
            route=_authorized_route(actor_role=actor_role, student=student),
            identity_visibility="full",
            allowed_action=(
                "reopen_lead"
                if is_archived and has_never_been_student
                else ("open" if _authorized_route(actor_role=actor_role, student=student) else None)
            ),
            student_id=student.id,
            detail="Этот контакт уже есть в CRM.",
            code="duplicate_phone",
        )

    if actor_trainer_id is None:
        raise BusinessLogicError("Trainer profile is required", code="trainer_profile_required")
    archived_action = _archived_trainer_action(
        student=student,
        actor_trainer_id=actor_trainer_id,
    )
    if archived_action is not None:
        identity_visibility, allowed_action = archived_action
        return StudentIntakeResult(
            result_kind="duplicate",
            target_workspace="leads_archived",
            route=None,
            identity_visibility=identity_visibility,
            allowed_action=allowed_action,
            student_id=(student.id if identity_visibility == "full" else None),
            detail="Этот контакт уже есть в CRM.",
            code="duplicate_phone",
        )
    is_in_trainer_scope = (
        Student.objects.for_club(club_id)
        .filter(id=student.id, deleted_at__isnull=True)
        .filter(trainer_student_scope_filter(actor_trainer_id))
        .exists()
    )
    if is_in_trainer_scope:
        return StudentIntakeResult(
            result_kind="duplicate",
            target_workspace=_workspace_for(student=student),
            route=_authorized_route(actor_role=actor_role, student=student),
            identity_visibility="full",
            allowed_action="open",
            student_id=student.id,
            detail="Этот контакт уже есть в CRM.",
            code="duplicate_phone",
        )
    if student.lead_status is not None and student.assigned_trainer_id is None:
        return StudentIntakeResult(
            result_kind="duplicate",
            target_workspace="leads_active",
            route=None,
            identity_visibility="masked",
            allowed_action="can_claim",
            detail="Такая заявка уже есть в свободных.",
            code="duplicate_phone",
        )
    return StudentIntakeResult(
        result_kind="duplicate",
        target_workspace=_workspace_for(student=student),
        route=None,
        identity_visibility="none",
        allowed_action=None,
        detail="Такой контакт уже есть в CRM.",
        code="duplicate_phone",
    )


def _soft_deleted_result(*, actor_role: str, student: Student) -> StudentIntakeResult:
    owner_can_review = actor_role != ClubMembership.Role.TRAINER
    return StudentIntakeResult(
        result_kind="identity_requires_owner_review",
        target_workspace="none",
        route=None,
        identity_visibility="none",
        allowed_action=("owner_review" if owner_can_review else None),
        student_id=(student.id if owner_can_review else None),
        detail="Контакт требует проверки руководителем.",
        code="identity_requires_owner_review",
    )


def _idempotency_conflict_result() -> StudentIntakeResult:
    return StudentIntakeResult(
        result_kind="idempotency_conflict",
        target_workspace="none",
        route=None,
        identity_visibility="none",
        allowed_action=None,
        detail="Этот ключ уже использован с другими данными.",
        code="idempotency_conflict",
    )


def _workspace_truth_table_anomaly_result() -> StudentIntakeResult:
    return StudentIntakeResult(
        result_kind="readiness_anomaly",
        target_workspace="none",
        route=None,
        identity_visibility="none",
        allowed_action=None,
        detail="Контакт требует проверки руководителем.",
        code="workspace_truth_table_anomaly",
    )


def _workspace_for(*, student: Student) -> str:
    if student.lead_status is not None:
        return "leads_active"
    if student.became_student_at is not None:
        return "students"
    if student.status == Student.Status.LOST:
        return "leads_archived"
    return "none"


def _normalize_intake_name(*, value: str, field: str, required: bool) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise BusinessLogicError("First name is required", code="first_name_required")
    if len(normalized) > MAX_PERSON_NAME_LENGTH:
        raise BusinessLogicError(
            f"{field} is too long",
            code=f"{field}_too_long",
        )
    return normalized


def _authorized_route(*, actor_role: str, student: Student) -> str | None:
    workspace = _workspace_for(student=student)
    if workspace == "leads_archived":
        return None
    if actor_role == ClubMembership.Role.TRAINER:
        if workspace == "leads_active":
            return f"/trainer/leads?lead={student.id}"
        if workspace == "students":
            return f"/trainer/students/{student.id}"
        return None
    if workspace in {"leads_active", "students"}:
        return f"/dashboard/students/{student.id}/card/"
    return None


def _archived_trainer_action(*, student: Student, actor_trainer_id: int) -> tuple[str, str] | None:
    if _workspace_for(student=student) != "leads_archived":
        return None
    if student.assigned_trainer_id == actor_trainer_id:
        return "full", "reopen_lead"
    if student.assigned_trainer_id is None:
        return "masked", "can_reopen_and_claim"
    return None


def restore_soft_deleted_person(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int,
) -> StudentIntakeResult:
    """Restore one exact soft-deleted person under the shared identity lock."""

    from apps.leads.models import LeadLifecycleEvent

    with transaction.atomic():
        lock_club_person_identity_arbitration(club_id=club_id)
        student = (
            Student.objects.for_club(club_id)
            .select_for_update()
            .get(id=student_id, deleted_at__isnull=False)
        )
        identity = normalize_person_identity(
            first_name=student.first_name,
            last_name=student.last_name,
            is_child=student.is_child,
            phone=student.phone,
            guardian_phone=student.guardian_phone,
            date_of_birth=student.date_of_birth,
        )
        resolution = resolve_staff_intake_person_identity(
            club_id=club_id,
            identity=identity,
            confirm_distinct_child=False,
        )
        if (
            resolution.duplicate is not None
            or resolution.confirmation_required
            or resolution.ambiguous is not None
            or (
                resolution.soft_deleted is not None
                and resolution.soft_deleted.id != student.id
            )
        ):
            raise BusinessLogicError(
                "A live or earlier identity conflicts with this restore",
                code="restore_identity_conflict",
            )
        workspace = _workspace_for(student=student)
        if workspace == "none":
            raise BusinessLogicError(
                "Person is outside the workspace truth table",
                code="workspace_truth_table_anomaly",
            )
        student.deleted_at = None
        student.save(update_fields=["deleted_at", "updated_at"])
        LeadLifecycleEvent.objects.create(
            club_id=club_id,
            student_id=student.id,
            actor_id=actor_user_id,
            event_type=LeadLifecycleEvent.EventType.LEAD_RESTORED,
            old_lead_status=student.lead_status or "",
            new_lead_status=student.lead_status or "",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            metadata={"workspace": workspace},
        )

    return StudentIntakeResult(
        result_kind="restored",
        target_workspace=workspace,
        route=_authorized_route(actor_role=ClubMembership.Role.OWNER, student=student),
        identity_visibility="full",
        allowed_action=("reopen_lead" if workspace == "leads_archived" else "open"),
        student_id=student.id,
    )
