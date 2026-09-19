from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.leads.service_modules._shared import (
    LeadClaimConflictError,
    _ensure_active_lead,
    _ensure_expected_trainer_assignment,
    _get_active_trainer,
    _get_lead_for_update,
    _record_lifecycle_event,
)
from apps.students.models import Student

CONTACT_OUTCOMES = {
    "contacted",
    "no_answer",
    "follow_up",
    "book_trial",
    "sell_group",
    "sell_personal",
    "lost",
}

logger = logging.getLogger("apps.leads.services")


def lose_lead(
    *,
    club_id: int,
    student_id: int,
    loss_reason: str,
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
) -> Student:
    valid_reasons = {choice.value for choice in Student.LossReason}
    if loss_reason not in valid_reasons:
        raise BusinessLogicError(
            f"Invalid loss reason: '{loss_reason}'",
            code="invalid_loss_reason",
        )

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        _ensure_expected_trainer_assignment(
            student,
            required_assigned_trainer_id=required_assigned_trainer_id,
        )

        old_status = student.lead_status or ""
        old_student_status = student.status
        student.lead_status = None
        student.status = Student.Status.LOST
        student.loss_reason = loss_reason
        student.save(update_fields=["lead_status", "status", "loss_reason", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
            old_lead_status=old_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            reason=loss_reason,
            metadata={
                "student_status_from": old_student_status,
                "student_status_to": Student.Status.LOST,
            },
        )

    from apps.pipelines.services import cancel_pipeline

    cancel_pipeline(club_id=club_id, student_id=student.id)

    logger.info(
        "lead_lost",
        extra={"student_id": student.id, "club_id": club_id, "loss_reason": loss_reason},
    )
    return student


def record_contact_outcome(
    *,
    club_id: int,
    student_id: int,
    outcome: str,
    due_date: date | None = None,
    loss_reason: str = "",
    notes: str = "",
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
) -> tuple[Student, str | None]:
    """Persist one contact outcome and keep a single NEW_LEAD task actionable."""

    if outcome not in CONTACT_OUTCOMES:
        raise BusinessLogicError("Invalid contact outcome", code="invalid_contact_outcome")
    if outcome == "no_answer" and due_date is None:
        raise BusinessLogicError(
            "Next attempt date is required",
            code="contact_due_date_required",
        )
    clean_notes = notes.strip()[:500]
    club = Club.objects.get(id=club_id)
    today = club_localdate(club)
    next_flow = {
        "book_trial": "book_trial",
        "sell_group": "sell_group",
        "sell_personal": "sell_personal",
    }.get(outcome)

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        _ensure_expected_trainer_assignment(
            student,
            required_assigned_trainer_id=required_assigned_trainer_id,
        )
        if student.assigned_trainer_id is None:
            raise BusinessLogicError(
                "Lead must be assigned before contact",
                code="lead_assignment_required",
            )

        old_status = student.lead_status or ""
        if outcome == "lost":
            if not loss_reason:
                raise BusinessLogicError(
                    "Loss reason is required",
                    code="loss_reason_required",
                )
            student = lose_lead(
                club_id=club_id,
                student_id=student_id,
                loss_reason=loss_reason,
                actor_user_id=actor_user_id,
                required_assigned_trainer_id=required_assigned_trainer_id,
            )
            from apps.retention.models import RetentionTask

            RetentionTask.objects.for_club(club_id).select_for_update().filter(
                student_id=student_id,
                task_type=RetentionTask.TaskType.NEW_LEAD,
                resolved_at__isnull=True,
            ).update(
                resolved_at=timezone.now(),
                resolution=RetentionTask.Resolution.LEAD_CLOSED,
                status=RetentionTask.TaskStatus.CLOSED,
                updated_at=timezone.now(),
            )
            _record_lifecycle_event(
                club_id=club_id,
                student_id=student.id,
                event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
                old_lead_status=old_status,
                new_lead_status="",
                old_trainer_id=student.assigned_trainer_id,
                new_trainer_id=student.assigned_trainer_id,
                actor_user_id=actor_user_id,
                reason=loss_reason,
                metadata={"outcome": outcome},
            )
            return student, None

        if outcome == "contacted" and student.lead_status == Student.LeadStatus.NEW:
            student.lead_status = Student.LeadStatus.CONTACTED
        elif outcome == "follow_up":
            student.lead_status = Student.LeadStatus.THINKING
        if student.lead_status != old_status:
            student.save(update_fields=["lead_status", "updated_at"])

        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
            old_lead_status=old_status,
            new_lead_status=student.lead_status,
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            metadata={"outcome": outcome},
        )

        task_due_date = due_date
        if task_due_date is None:
            task_due_date = today + timedelta(days=1 if outcome in {"no_answer", "follow_up"} else 0)
        if task_due_date < today:
            raise BusinessLogicError(
                "Contact task due date cannot be in the past",
                code="contact_due_date_in_past",
            )
        from apps.retention.models import RetentionTask
        from apps.retention.services import create_or_reuse_retention_task

        create_or_reuse_retention_task(
            student_id=student.id,
            club_id=club_id,
            trainer_id=student.assigned_trainer_id,
            task_type=RetentionTask.TaskType.NEW_LEAD,
            due_date=task_due_date,
            notes=clean_notes,
        )

    logger.info(
        "lead_contact_outcome_recorded",
        extra={"student_id": student.id, "club_id": club_id, "outcome": outcome},
    )
    return student, next_flow


def reopen_lead(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
    claim_trainer_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
) -> Student:
    """Reopen a never-converted archived lead, optionally claiming it atomically."""

    if claim_trainer_id is not None:
        _get_active_trainer(club_id=club_id, trainer_id=claim_trainer_id)
    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        if not (
            student.lead_status is None
            and student.became_student_at is None
            and student.status == Student.Status.LOST
        ):
            raise BusinessLogicError("Lead is not archived", code="lead_not_archived")
        _ensure_expected_trainer_assignment(
            student,
            required_assigned_trainer_id=required_assigned_trainer_id,
        )
        old_trainer_id = student.assigned_trainer_id
        if claim_trainer_id is not None:
            if student.assigned_trainer_id not in {None, claim_trainer_id}:
                raise LeadClaimConflictError("Archived lead is already assigned")
            student.assigned_trainer_id = claim_trainer_id

        student.status = Student.Status.LEAD
        student.lead_status = Student.LeadStatus.NEW
        student.loss_reason = None
        student.save(
            update_fields=[
                "status",
                "lead_status",
                "loss_reason",
                "assigned_trainer",
                "updated_at",
            ]
        )
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_REOPENED,
            old_lead_status="",
            new_lead_status=Student.LeadStatus.NEW,
            old_trainer_id=old_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
        )

        if student.assigned_trainer_id is not None:
            from apps.retention.models import RetentionTask
            from apps.retention.services import create_or_reuse_retention_task

            create_or_reuse_retention_task(
                student_id=student.id,
                club_id=club_id,
                trainer_id=student.assigned_trainer_id,
                task_type=RetentionTask.TaskType.NEW_LEAD,
                due_date=club_localdate(student.club),
            )

    return student
