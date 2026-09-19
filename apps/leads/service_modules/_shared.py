from __future__ import annotations

from typing import Any

from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student
from apps.trainers.models import Trainer


class LeadClaimConflictError(Exception):
    def __init__(self, message: str = "Lead is already assigned"):
        self.message = message


def _record_lifecycle_event(
    *,
    club_id: int,
    student_id: int,
    event_type: str,
    old_lead_status: str | None = "",
    new_lead_status: str | None = "",
    old_trainer_id: int | None = None,
    new_trainer_id: int | None = None,
    actor_user_id: int | None = None,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> LeadLifecycleEvent:
    return LeadLifecycleEvent.objects.create(
        club_id=club_id,
        student_id=student_id,
        actor_id=actor_user_id,
        event_type=event_type,
        old_lead_status=old_lead_status or "",
        new_lead_status=new_lead_status or "",
        old_trainer_id=old_trainer_id,
        new_trainer_id=new_trainer_id,
        reason=reason,
        metadata=metadata or {},
    )


def _get_lead_for_update(*, club_id: int, student_id: int) -> Student:
    return (
        Student.objects.for_club(club_id)
        .select_for_update()
        .get(id=student_id, deleted_at__isnull=True)
    )


def _ensure_active_lead(student: Student) -> None:
    if student.lead_status is None:
        raise BusinessLogicError("Student is not a lead", code="not_a_lead")


def _ensure_expected_trainer_assignment(
    student: Student,
    *,
    required_assigned_trainer_id: int | None,
) -> None:
    """Revalidate trainer mutation scope after the lead row is locked."""

    if (
        required_assigned_trainer_id is not None
        and student.assigned_trainer_id != required_assigned_trainer_id
    ):
        raise BusinessLogicError(
            "Lead is not assigned to this trainer",
            code="not_your_lead",
        )


def _get_active_trainer(
    *,
    club_id: int,
    trainer_id: int,
    lock_for_update: bool = False,
) -> Trainer:
    qs = Trainer.objects.for_club(club_id)
    if lock_for_update:
        qs = qs.select_for_update(of=("self",))
    trainer = qs.filter(id=trainer_id, is_active=True).first()
    if trainer is None:
        raise BusinessLogicError(
            "Trainer does not belong to this club",
            code="trainer_club_mismatch",
        )
    return trainer
