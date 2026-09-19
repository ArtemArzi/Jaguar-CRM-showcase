from __future__ import annotations

from django.db.models import Q

from apps.clubs.models import ClubMembership
from apps.students.journey_contracts import Workspace, WorkspaceFacts, classify_workspace
from apps.students.models import Student
from apps.students.scopes import trainer_student_scope_filter
from apps.students.selectors import (
    filter_students_by_query,
    with_commercial_segment,
)


def _masked_phone(student: Student) -> str | None:
    phone = student.guardian_phone if student.is_child else student.phone
    if not phone:
        return None
    if len(phone) <= 4:
        return "****"
    return f"{phone[:4]}****{phone[-2:]}"


def _display_name(student: Student, *, masked: bool) -> str:
    if masked:
        return student.first_name
    return " ".join(part for part in (student.first_name, student.last_name) if part)


def _workspace(student: Student) -> Workspace:
    return classify_workspace(
        facts=WorkspaceFacts(
            is_deleted=student.deleted_at is not None,
            lead_status=student.lead_status,
            became_student_at=(
                student.became_student_at.isoformat()
                if student.became_student_at is not None
                else None
            ),
            status=student.status,
        )
    ).workspace


def _owner_result(*, student: Student, workspace: Workspace) -> dict:
    allowed_action = "reopen_lead" if workspace == Workspace.ARCHIVED_LEADS else "open"
    return {
        "id": student.id,
        "display_name": _display_name(student, masked=False),
        "masked_phone": _masked_phone(student),
        "target_workspace": workspace.value,
        "route": f"/dashboard/students/{student.id}/card/",
        "identity_visibility": "full",
        "allowed_action": allowed_action,
        "commercial_segment": (
            student._commercial_segment if workspace == Workspace.STUDENTS else None
        ),
    }


def _trainer_result(
    *,
    student: Student,
    workspace: Workspace,
    trainer_id: int,
    scoped_student_ids: set[int],
) -> dict | None:
    if workspace == Workspace.ACTIVE_LEADS:
        if student.assigned_trainer_id == trainer_id:
            return {
                "id": student.id,
                "display_name": _display_name(student, masked=False),
                "masked_phone": _masked_phone(student),
                "target_workspace": workspace.value,
                "route": f"/trainer/leads?lead={student.id}",
                "identity_visibility": "full",
                "allowed_action": "open",
            }
        if student.assigned_trainer_id is None:
            return {
                "id": student.id,
                "display_name": _display_name(student, masked=True),
                "masked_phone": _masked_phone(student),
                "target_workspace": workspace.value,
                "route": "/trainer/leads?scope=pool",
                "identity_visibility": "masked",
                "allowed_action": "can_claim",
            }
        return None

    if workspace == Workspace.ARCHIVED_LEADS:
        if student.assigned_trainer_id == trainer_id:
            return {
                "id": student.id,
                "display_name": _display_name(student, masked=False),
                "masked_phone": _masked_phone(student),
                "target_workspace": workspace.value,
                "route": f"/trainer/leads?workspace=archived&lead={student.id}",
                "identity_visibility": "full",
                "allowed_action": "reopen_lead",
            }
        if student.assigned_trainer_id is None:
            return {
                "id": student.id,
                "display_name": _display_name(student, masked=True),
                "masked_phone": _masked_phone(student),
                "target_workspace": workspace.value,
                "route": "/trainer/leads?workspace=archived",
                "identity_visibility": "masked",
                "allowed_action": "can_reopen_and_claim",
            }
        return None

    if workspace == Workspace.STUDENTS and student.id in scoped_student_ids:
        return {
            "id": student.id,
            "display_name": _display_name(student, masked=False),
            "masked_phone": _masked_phone(student),
            "target_workspace": workspace.value,
            "route": f"/trainer/students/{student.id}",
            "identity_visibility": "full",
            "allowed_action": "open",
            "commercial_segment": student._commercial_segment,
        }
    return None


def search_people(
    *,
    club,
    query: str,
    actor_role: str,
    actor_trainer_id: int | None,
    limit: int = 20,
) -> list[dict]:
    """Search all D3 workspaces and shape every result server-side for privacy."""

    normalized_query = query.strip()
    if len(normalized_query) < 2:
        return []

    candidate_query = filter_students_by_query(
        queryset=with_commercial_segment(
            queryset=Student.objects.for_club(club).filter(deleted_at__isnull=True),
            club=club,
        ),
        query=normalized_query,
    ).select_related("assigned_trainer")
    ordered_candidates = candidate_query.order_by("-created_at", "-id")
    if actor_role != ClubMembership.Role.TRAINER:
        candidates = list(ordered_candidates[:limit])
        return [
            _owner_result(student=student, workspace=workspace)
            for student in candidates
            for workspace in (_workspace(student),)
            if workspace in {
                Workspace.ACTIVE_LEADS,
                Workspace.ARCHIVED_LEADS,
                Workspace.STUDENTS,
            }
        ]

    if actor_trainer_id is None:
        return []
    assigned_or_pool = Q(assigned_trainer_id=actor_trainer_id) | Q(
        assigned_trainer__isnull=True
    )
    active_visible = Q(lead_status__isnull=False) & assigned_or_pool
    archived_visible = (
        Q(
            lead_status__isnull=True,
            became_student_at__isnull=True,
            status=Student.Status.LOST,
        )
        & assigned_or_pool
    )
    student_visible = Q(
        lead_status__isnull=True,
        became_student_at__isnull=False,
    ) & trainer_student_scope_filter(actor_trainer_id)
    visible_query = candidate_query.filter(
        active_visible | archived_visible | student_visible
    ).distinct()
    valid_workspace = Q(lead_status__isnull=False) | Q(
        lead_status__isnull=True,
        became_student_at__isnull=True,
        status=Student.Status.LOST,
    ) | Q(lead_status__isnull=True, became_student_at__isnull=False)
    hidden_match = (
        candidate_query.filter(valid_workspace)
        .exclude(id__in=visible_query.values("id"))
        .exists()
    )
    candidates = list(visible_query.order_by("-created_at", "-id")[:limit])

    candidate_ids = [student.id for student in candidates]
    scoped_student_ids = set(
        Student.objects.for_club(club)
        .filter(id__in=candidate_ids)
        .filter(trainer_student_scope_filter(actor_trainer_id))
        .values_list("id", flat=True)
        .distinct()
    )
    results: list[dict] = []
    for student in candidates:
        workspace = _workspace(student)
        if workspace not in {
            Workspace.ACTIVE_LEADS,
            Workspace.ARCHIVED_LEADS,
            Workspace.STUDENTS,
        }:
            continue
        shaped = _trainer_result(
            student=student,
            workspace=workspace,
            trainer_id=actor_trainer_id,
            scoped_student_ids=scoped_student_ids,
        )
        if shaped is None:
            continue
        results.append(shaped)
    if hidden_match and len(results) < limit:
        results.append(
            {
                "id": None,
                "display_name": None,
                "masked_phone": None,
                "target_workspace": None,
                "route": None,
                "identity_visibility": "none",
                "allowed_action": None,
                "commercial_segment": None,
            }
        )
    return results[:limit]
