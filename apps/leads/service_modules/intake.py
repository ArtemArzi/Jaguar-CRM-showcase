from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.common.exceptions import BusinessLogicError
from apps.common.logging import hash_for_log
from apps.leads.models import LeadIntakeEvent, LeadLifecycleEvent
from apps.leads.service_modules._shared import _record_lifecycle_event
from apps.students.duplicates import DuplicateStudentError, StaffIntakeIdentityResolution
from apps.students.identity_services import (
    lock_club_person_identity_arbitration,
    normalize_person_identity,
    resolve_person_identity,
)
from apps.students.models import Student
from apps.trainers.models import Trainer

logger = logging.getLogger("apps.leads.services")

MAX_NAME_LENGTH = 100
MAX_GOAL_LENGTH = 500
MAX_PAGE_LENGTH = 200
MAX_UTM_LENGTH = 120
MAX_CONSENT_VERSION_LENGTH = 50
MAX_CONSENT_HASH_LENGTH = 128
MAX_REQUEST_ID_LENGTH = 128
MAX_HASH_LENGTH = 64


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
    resolve_staff_intake_person_identity: Callable[..., StaffIntakeIdentityResolution],
) -> Student:
    identity = normalize_person_identity(
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        is_child=is_child,
        phone=phone,
        guardian_phone=guardian_phone,
        invalid_phone_message="Invalid phone",
    )

    with transaction.atomic():
        lock_club_person_identity_arbitration(club_id=club_id)
        resolution = resolve_staff_intake_person_identity(
            club_id=club_id,
            identity=identity,
            confirm_distinct_child=confirm_distinct_child,
        )
        duplicate = resolution.duplicate or resolution.soft_deleted
        if duplicate is not None:
            raise DuplicateStudentError(duplicate, message="Lead with this phone already exists")
        if resolution.confirmation_required:
            # Legacy lead creation has no confirmation protocol. Preserve its
            # established duplicate/409 surface; the typed confirmation path
            # belongs solely to the unified staff intake service.
            raise DuplicateStudentError(
                resolution.ambiguous,
                message="Lead with this phone already exists",
            )
        if assigned_trainer_id is not None and not Trainer.objects.for_club(club_id).filter(
            id=assigned_trainer_id,
        ).exists():
            raise BusinessLogicError(
                "Trainer does not belong to this club",
                code="trainer_club_mismatch",
            )
        student = Student.objects.create(
            club_id=club_id,
            first_name=first_name,
            last_name=last_name,
            phone=identity.phone,
            guardian_phone=identity.guardian_phone,
            date_of_birth=date_of_birth,
            is_child=is_child,
            source=source,
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer_id=assigned_trainer_id,
            crm_entry_kind=crm_entry_kind,
            crm_entered_by_id=crm_entered_by_id,
        )
        if assigned_trainer_id is not None:
            _record_lifecycle_event(
                club_id=club_id,
                student_id=student.id,
                event_type=LeadLifecycleEvent.EventType.LEAD_ASSIGNED,
                old_lead_status="",
                new_lead_status=student.lead_status,
                old_trainer_id=None,
                new_trainer_id=assigned_trainer_id,
            )
    logger.info("lead_created", extra={"student_id": student.id, "club_id": club_id})
    return student


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
    queue_lead_intake_telegram: Callable[..., None],
    now: Callable[[], datetime],
) -> LeadIntakeEvent:
    if idempotency_key is not None:
        existing = LeadIntakeEvent.objects.for_club(club_id).filter(
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            return existing

    first_name = _clean_required_text(name, field="name", max_length=MAX_NAME_LENGTH)
    identity = normalize_person_identity(
        first_name=first_name,
        last_name="",
        is_child=is_child,
        phone=phone,
        guardian_phone="",
        invalid_phone_message="Invalid phone",
    )
    clean_goal = _clean_required_text(goal, field="goal", max_length=MAX_GOAL_LENGTH)
    clean_format = _validate_preferred_format(preferred_format)
    _validate_consent(consent)

    with transaction.atomic():
        lock_club_person_identity_arbitration(club_id=club_id)
        if idempotency_key is not None:
            existing = (
                LeadIntakeEvent.objects.for_club(club_id)
                .select_for_update()
                .filter(idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                return existing

        student = resolve_person_identity(
            club_id=club_id,
            identity=identity,
            purpose="public_intake",
        )
        is_repeat_submission = student is not None
        requires_owner_review = student is not None and student.deleted_at is not None
        if student is None:
            student = Student(
                club_id=club_id,
                first_name=first_name,
                last_name="",
                phone=identity.phone,
                guardian_phone=identity.guardian_phone,
                is_child=is_child,
                source=Student.Source.WEBSITE,
                status=Student.Status.LEAD,
                lead_status=Student.LeadStatus.NEW,
                crm_entry_kind=Student.CrmEntryKind.LEAD_INTAKE,
            )
            student.save()
        elif student.deleted_at is None and student.status == Student.Status.LOST:
            old_lead_status = student.lead_status or ""
            old_trainer_id = student.assigned_trainer_id
            old_loss_reason = student.loss_reason or ""
            student.status = Student.Status.LEAD
            student.lead_status = Student.LeadStatus.NEW
            student.assigned_trainer_id = None
            student.loss_reason = None
            student.save(
                update_fields=[
                    "status",
                    "lead_status",
                    "assigned_trainer",
                    "loss_reason",
                    "updated_at",
                ]
            )
            _record_lifecycle_event(
                club_id=club_id,
                student_id=student.id,
                event_type=LeadLifecycleEvent.EventType.STATUS_CHANGED,
                old_lead_status=old_lead_status,
                new_lead_status=Student.LeadStatus.NEW,
                old_trainer_id=old_trainer_id,
                new_trainer_id=None,
                reason=old_loss_reason,
            )

        event = LeadIntakeEvent(
            club_id=club_id,
            student=student,
            goal=clean_goal,
            preferred_format=clean_format,
            source_page=_clean_optional_text(source.get("page"), max_length=MAX_PAGE_LENGTH),
            utm_source=_clean_optional_text(source.get("utm_source"), max_length=MAX_UTM_LENGTH),
            utm_medium=_clean_optional_text(source.get("utm_medium"), max_length=MAX_UTM_LENGTH),
            utm_campaign=_clean_optional_text(source.get("utm_campaign"), max_length=MAX_UTM_LENGTH),
            utm_content=_clean_optional_text(source.get("utm_content"), max_length=MAX_UTM_LENGTH),
            utm_term=_clean_optional_text(source.get("utm_term"), max_length=MAX_UTM_LENGTH),
            privacy_policy_version=_clean_required_text(
                consent.get("privacy_policy_version"),
                field="privacy_policy_version",
                max_length=MAX_CONSENT_VERSION_LENGTH,
            ),
            consent_text_hash=_clean_required_text(
                consent.get("consent_text_hash"),
                field="consent_text_hash",
                max_length=MAX_CONSENT_HASH_LENGTH,
            ),
            consent_accepted_at=now(),
            request_id=_clean_optional_text(request_id, max_length=MAX_REQUEST_ID_LENGTH),
            client_ip_hash=_clean_optional_text(client_ip_hash, max_length=MAX_HASH_LENGTH),
            user_agent_hash=_hash_user_agent(user_agent),
            idempotency_key=idempotency_key,
            is_repeat_submission=is_repeat_submission,
            requires_owner_review=requires_owner_review,
        )
        event.full_clean()
        event.save()
        transaction.on_commit(
            lambda: queue_lead_intake_telegram(event_id=event.id, club_id=club_id)
        )

    logger.info(
        "landing_lead_intake_created",
        extra={
            "event_id": event.id,
            "student_id": student.id,
            "club_id": club_id,
            "is_repeat_submission": is_repeat_submission,
            "requires_owner_review": requires_owner_review,
        },
    )
    return event


def _validate_phone(phone: str) -> None:
    try:
        Student._meta.get_field("phone").run_validators(phone)
    except ValidationError:
        raise BusinessLogicError("Invalid phone", code="invalid_phone")


def _validate_preferred_format(value: str) -> str:
    allowed = {choice.value for choice in LeadIntakeEvent.PreferredFormat}
    if value not in allowed:
        raise BusinessLogicError("Invalid preferred format", code="invalid_preferred_format")
    return value


def _validate_consent(consent: dict[str, Any]) -> None:
    if not consent.get("personal_data"):
        raise BusinessLogicError("Personal data consent is required", code="consent_required")
    _clean_required_text(
        consent.get("privacy_policy_version"),
        field="privacy_policy_version",
        max_length=MAX_CONSENT_VERSION_LENGTH,
    )
    _clean_required_text(
        consent.get("consent_text_hash"),
        field="consent_text_hash",
        max_length=MAX_CONSENT_HASH_LENGTH,
    )


def _clean_required_text(value: Any, *, field: str, max_length: int) -> str:
    clean = _clean_optional_text(value, max_length=max_length)
    if not clean:
        raise BusinessLogicError(f"{field} is required", code=f"{field}_required")
    return clean


def _clean_optional_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    clean = str(value).strip()
    if len(clean) > max_length:
        clean = clean[:max_length]
    return clean


def _hash_user_agent(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hash_for_log(user_agent, salt=settings.SECRET_KEY)


def _queue_lead_intake_telegram(*, event_id: int, club_id: int) -> None:
    from django_q.tasks import async_task

    async_task(
        "apps.leads.tasks.send_lead_intake_telegram_task",
        event_id,
        club_id,
    )
