from __future__ import annotations

import logging
from collections.abc import Callable

from django.db import transaction

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

VALID_TRANSITIONS: dict[str, list[str]] = {
    "new": ["contacted"],
    "contacted": ["thinking"],
    "trial_booked": ["trial_done"],
    "trial_done": ["thinking"],
    "thinking": [],  # generic status updates stop here; explicit lifecycle actions handle it
}

logger = logging.getLogger("apps.leads.services")


def _clean_reason(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise BusinessLogicError("Reason is required", code="reason_required")
    return clean[:500]


def claim_lead(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    actor_user_id: int | None = None,
) -> Student:
    _get_active_trainer(club_id=club_id, trainer_id=trainer_id)

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        if student.assigned_trainer_id is not None:
            raise LeadClaimConflictError()

        old_status = student.lead_status
        student.assigned_trainer_id = trainer_id
        student.save(update_fields=["assigned_trainer", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CLAIMED,
            old_lead_status=old_status,
            new_lead_status=student.lead_status,
            old_trainer_id=None,
            new_trainer_id=trainer_id,
            actor_user_id=actor_user_id,
        )

    logger.info(
        "lead_claimed",
        extra={"student_id": student.id, "club_id": club_id, "trainer_id": trainer_id},
    )
    return student


def release_lead(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int,
    reason: str,
    actor_user_id: int | None = None,
) -> Student:
    reason = _clean_reason(reason)

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        if student.assigned_trainer_id != trainer_id:
            raise BusinessLogicError("Lead is not assigned to this trainer", code="not_your_lead")

        old_trainer_id = student.assigned_trainer_id
        old_status = student.lead_status
        student.assigned_trainer = None
        student.save(update_fields=["assigned_trainer", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_RELEASED,
            old_lead_status=old_status,
            new_lead_status=student.lead_status,
            old_trainer_id=old_trainer_id,
            new_trainer_id=None,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    logger.info(
        "lead_released",
        extra={"student_id": student.id, "club_id": club_id, "trainer_id": trainer_id},
    )
    return student


def assign_lead(
    *,
    club_id: int,
    student_id: int,
    trainer_id: int | None,
    actor_user_id: int | None = None,
    reason: str = "",
) -> Student:
    if trainer_id is not None:
        _get_active_trainer(club_id=club_id, trainer_id=trainer_id)
    reason = reason.strip()[:500]

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        old_trainer_id = student.assigned_trainer_id
        if old_trainer_id == trainer_id:
            return student

        old_status = student.lead_status
        student.assigned_trainer_id = trainer_id
        student.save(update_fields=["assigned_trainer", "updated_at"])
        if trainer_id is None:
            event_type = LeadLifecycleEvent.EventType.LEAD_RELEASED
        elif old_trainer_id is None:
            event_type = LeadLifecycleEvent.EventType.LEAD_ASSIGNED
        else:
            event_type = LeadLifecycleEvent.EventType.LEAD_REASSIGNED
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=event_type,
            old_lead_status=old_status,
            new_lead_status=student.lead_status,
            old_trainer_id=old_trainer_id,
            new_trainer_id=trainer_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    logger.info(
        "lead_assigned",
        extra={
            "student_id": student.id,
            "club_id": club_id,
            "old_trainer_id": old_trainer_id,
            "new_trainer_id": trainer_id,
        },
    )
    return student


def update_lead_status(
    *,
    club_id: int,
    student_id: int,
    new_status: str,
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
    is_unified_client_journey_enabled: Callable[..., bool],
    trigger_trial_done_side_effects: Callable[..., None],
) -> Student:
    unified_journey_enabled = is_unified_client_journey_enabled(club=club_id)
    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        _ensure_active_lead(student)
        _ensure_expected_trainer_assignment(
            student,
            required_assigned_trainer_id=required_assigned_trainer_id,
        )

        if new_status == Student.LeadStatus.TRIAL_BOOKED:
            raise BusinessLogicError(
                "Trial booking requires book_trial",
                code="trial_booking_requires_book_trial",
            )
        if new_status == Student.LeadStatus.TRIAL_DONE and unified_journey_enabled:
            raise BusinessLogicError(
                "Trial completion requires an exact check-in",
                code="trial_done_requires_checkin",
            )

        allowed = VALID_TRANSITIONS.get(student.lead_status, [])
        if new_status not in allowed:
            raise BusinessLogicError(
                f"Cannot transition from '{student.lead_status}' to '{new_status}'",
                code="invalid_transition",
            )

        old_status = student.lead_status
        student.lead_status = new_status
        student.save(update_fields=["lead_status", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.STATUS_CHANGED,
            old_lead_status=old_status,
            new_lead_status=new_status,
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
        )

    logger.info(
        "lead_status_updated",
        extra={
            "student_id": student.id,
            "club_id": club_id,
            "old_status": old_status,
            "new_status": new_status,
        },
    )
    if new_status == Student.LeadStatus.TRIAL_DONE:
        trigger_trial_done_side_effects(club_id=club_id, student_id=student.id)

    return student
