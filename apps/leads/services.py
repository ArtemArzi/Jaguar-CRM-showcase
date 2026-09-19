from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.utils import timezone

import apps.leads.service_modules.conversions as conversions_lifecycle
import apps.leads.service_modules.intake as intake_lifecycle
import apps.leads.service_modules.ownership as ownership_lifecycle
import apps.leads.service_modules.trials as trials_lifecycle
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.leads.models import LeadIntakeEvent
from apps.leads.service_modules._shared import LeadClaimConflictError
from apps.leads.service_modules.funnel import CONTACT_OUTCOMES as _CONTACT_OUTCOMES
from apps.leads.service_modules.funnel import lose_lead, record_contact_outcome, reopen_lead
from apps.leads.service_modules.ownership import assign_lead, claim_lead, release_lead
from apps.leads.service_modules.trials import is_exact_booked_trial_checkin
from apps.students.identity_services import resolve_staff_intake_person_identity
from apps.students.models import Student

VALID_TRANSITIONS = ownership_lifecycle.VALID_TRANSITIONS
CONTACT_OUTCOMES = _CONTACT_OUTCOMES
restore_lead_after_terminal_group_payment = conversions_lifecycle.restore_lead_after_terminal_group_payment
restore_lead_after_terminal_personal_payment = conversions_lifecycle.restore_lead_after_terminal_personal_payment
snooze_lead_for_pending_group_payment = conversions_lifecycle.snooze_lead_for_pending_group_payment
snooze_lead_for_pending_personal_payment = conversions_lifecycle.snooze_lead_for_pending_personal_payment
__all__ = [
    "LeadClaimConflictError", "create_lead", "create_landing_lead_intake",
    "claim_lead", "release_lead", "assign_lead", "update_lead_status",
    "is_exact_booked_trial_checkin", "complete_booked_trial_after_checkin", "book_trial",
    "convert_lead", "convert_lead_for_manual_operational_admission",
    "admit_lead_for_manual_operational_admission",
    "convert_lead_after_subscription_payment", "snooze_lead_for_pending_personal_payment",
    "restore_lead_after_terminal_personal_payment", "snooze_lead_for_pending_group_payment",
    "restore_lead_after_terminal_group_payment",
    "convert_lead_after_group_admission_checkin", "convert_lead_after_personal_attendance",
    "convert_lead_after_personal_payment_confirmation",
    "lose_lead",
    "record_contact_outcome",
    "reopen_lead",
]


def create_lead(
    *,
    club_id: int,
    first_name: str,
    last_name: str = "",
    phone: str,
    is_child: bool = False,
    guardian_phone: str = "",
    date_of_birth: date | None = None,
    source: str = "other",
    assigned_trainer_id: int | None = None,
    crm_entry_kind: str = Student.CrmEntryKind.LEGACY_UNKNOWN,
    crm_entered_by_id: int | None = None,
    confirm_distinct_child: bool = False,
) -> Student:
    return intake_lifecycle.create_lead(
        club_id=club_id,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        is_child=is_child,
        guardian_phone=guardian_phone,
        date_of_birth=date_of_birth,
        source=source,
        assigned_trainer_id=assigned_trainer_id,
        crm_entry_kind=crm_entry_kind,
        crm_entered_by_id=crm_entered_by_id,
        confirm_distinct_child=confirm_distinct_child,
        resolve_staff_intake_person_identity=resolve_staff_intake_person_identity,
    )


def create_landing_lead_intake(
    *,
    club_id: int,
    name: str,
    phone: str,
    goal: str,
    preferred_format: str,
    is_child: bool,
    consent: dict[str, Any],
    source: dict[str, Any],
    request_id: str,
    client_ip_hash: str,
    user_agent: str,
    idempotency_key: UUID | None,
) -> LeadIntakeEvent:
    return intake_lifecycle.create_landing_lead_intake(
        club_id=club_id,
        name=name,
        phone=phone,
        goal=goal,
        preferred_format=preferred_format,
        is_child=is_child,
        consent=consent,
        source=source,
        request_id=request_id,
        client_ip_hash=client_ip_hash,
        user_agent=user_agent,
        idempotency_key=idempotency_key,
        queue_lead_intake_telegram=_queue_lead_intake_telegram,
        now=timezone.now,
    )


def update_lead_status(
    *,
    club_id: int,
    student_id: int,
    new_status: str,
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
) -> Student:
    return ownership_lifecycle.update_lead_status(
        club_id=club_id,
        student_id=student_id,
        new_status=new_status,
        actor_user_id=actor_user_id,
        required_assigned_trainer_id=required_assigned_trainer_id,
        is_unified_client_journey_enabled=is_unified_client_journey_enabled,
        trigger_trial_done_side_effects=_trigger_trial_done_side_effects,
    )


def _queue_lead_intake_telegram(*, event_id: int, club_id: int) -> None:
    intake_lifecycle._queue_lead_intake_telegram(event_id=event_id, club_id=club_id)


def _trigger_trial_done_side_effects(*, club_id: int, student_id: int) -> None:
    trials_lifecycle._trigger_trial_done_side_effects(club_id=club_id, student_id=student_id)


def complete_booked_trial_after_checkin(*, club_id: int, student_id: int, checkin=None) -> bool:
    return trials_lifecycle.complete_booked_trial_after_checkin(
        club_id=club_id,
        student_id=student_id,
        checkin=checkin,
        trigger_trial_done_side_effects=_trigger_trial_done_side_effects,
    )


def book_trial(
    *,
    club_id: int,
    student_id: int,
    trial_date=None,
    schedule_id: int | None = None,
    occurrence_date=None,
    required_trainer_id: int | None = None,
    mode: str = "group",
    starts_at=None,
    ends_at=None,
    trainer_id: int | None = None,
    location_id: int | None = None,
    training_type_id: int | None = None,
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
) -> Student:
    return trials_lifecycle.book_trial(
        club_id=club_id,
        student_id=student_id,
        trial_date=trial_date,
        schedule_id=schedule_id,
        occurrence_date=occurrence_date,
        required_trainer_id=required_trainer_id,
        mode=mode,
        starts_at=starts_at,
        ends_at=ends_at,
        trainer_id=trainer_id,
        location_id=location_id,
        training_type_id=training_type_id,
        actor_user_id=actor_user_id,
        required_assigned_trainer_id=required_assigned_trainer_id,
        now=timezone.now,
    )


def convert_lead(*, club_id: int, student_id: int, actor_user_id: int | None = None) -> Student:
    return conversions_lifecycle.convert_lead(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects,
        now=timezone.now,
    )


def convert_lead_for_manual_operational_admission(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
) -> bool:
    return conversions_lifecycle.convert_lead_for_manual_operational_admission(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects,
        now=timezone.now,
    )


def admit_lead_for_manual_operational_admission(
    *,
    evidence,
    occurred_at: datetime | None = None,
) -> bool:
    """Use the evidence-bound owner for new manual operational admissions."""

    return conversions_lifecycle.admit_lead_for_manual_operational_admission(
        evidence=evidence,
        finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects,
        now=timezone.now,
        occurred_at=occurred_at,
    )


def convert_lead_after_subscription_payment(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
) -> bool:
    return conversions_lifecycle.convert_lead_after_subscription_payment(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects,
        now=timezone.now,
    )


def convert_lead_after_group_admission_checkin(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    checkin_id: int,
    actor_user_id: int | None,
) -> bool:
    return conversions_lifecycle.convert_lead_after_group_admission_checkin(
        club_id=club_id,
        student_id=student_id,
        schedule_id=schedule_id,
        checkin_id=checkin_id,
        actor_user_id=actor_user_id,
        finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects,
        now=timezone.now,
    )


def convert_lead_after_personal_attendance(
    *,
    club_id: int,
    student_id: int,
    booking_id: int,
    checkin_id: int,
    payment_id: int | None,
    actor_user_id: int | None,
) -> bool:
    return conversions_lifecycle.convert_lead_after_personal_attendance(
        club_id=club_id,
        student_id=student_id,
        booking_id=booking_id,
        checkin_id=checkin_id,
        payment_id=payment_id,
        actor_user_id=actor_user_id,
        is_exact_booked_trial_checkin=is_exact_booked_trial_checkin,
        now=timezone.now,
    )


def convert_lead_after_personal_payment_confirmation(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
) -> bool:
    return conversions_lifecycle.convert_lead_after_personal_payment_confirmation(
        club_id=club_id,
        student_id=student_id,
        payment_id=payment_id,
        actor_user_id=actor_user_id,
        now=timezone.now,
    )


def _finalize_lead_conversion_side_effects(*, club_id: int, student_id: int) -> None:
    conversions_lifecycle._finalize_lead_conversion_side_effects(club_id=club_id, student_id=student_id)
