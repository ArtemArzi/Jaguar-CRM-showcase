from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.leads.service_modules._shared import (
    _ensure_active_lead,
    _get_lead_for_update,
    _record_lifecycle_event,
)
from apps.leads.service_modules.trials import (
    is_exact_booked_trial_checkin as _is_exact_booked_trial_checkin,
)
from apps.students.models import Student

logger = logging.getLogger("apps.leads.services")


def _has_paid_active_subscription(*, club_id: int, student_id: int) -> bool:
    from apps.billing.models import Subscription
    from apps.billing.selectors import current_active_subscription_q

    return (
        Subscription.objects.for_club(club_id)
        .filter(
            current_active_subscription_q(),
            student_id=student_id,
            paid_amount__gt=0,
        )
        .exists()
    )


def _ensure_paid_conversion_eligible(*, club_id: int, student: Student) -> None:
    if _has_paid_active_subscription(club_id=club_id, student_id=student.id):
        return

    raise BusinessLogicError(
        "Lead conversion requires a paid active subscription",
        code="lead_conversion_requires_paid_subscription",
    )


def convert_lead(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
) -> Student:
    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)

        _ensure_active_lead(student)
        _ensure_paid_conversion_eligible(club_id=club_id, student=student)

        old_status = student.lead_status
        student.lead_status = None
        student.status = Student.Status.ACTIVE
        update_fields = ["lead_status", "status", "updated_at"]
        if student.became_student_at is None:
            student.became_student_at = now()
            update_fields.append("became_student_at")
        student.save(update_fields=update_fields)
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            old_lead_status=old_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
        )

    finalize_lead_conversion_side_effects(club_id=club_id, student_id=student.id)

    logger.info("lead_converted", extra={"student_id": student.id, "club_id": club_id})
    return student


def _convert_lead_after_payment(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
    source: str,
    establishes_student_provenance: bool = True,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
) -> bool:
    """Convert a lead once for a staff payment lifecycle transition.

    A confirmed/direct-sale caller may also establish the monotonic fact for
    an already operationally admitted active student. It still returns False
    in that case because there is no second lead conversion to finalize.
    """
    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        # Paid confirmation is commercial evidence even when a lead is in the
        # durable TRIAL/TRIAL_DONE lifecycle.  Only the exact free-trial
        # attendance path is non-converting; it is handled from its enrollment
        # provenance below, not from the mutable Student status alone.
        if student.lead_status is None:
            if establishes_student_provenance and student.became_student_at is None:
                student.became_student_at = now()
                student.save(update_fields=["became_student_at", "updated_at"])
            return False

        old_lead_status = student.lead_status or ""
        old_student_status = student.status
        student.lead_status = None
        student.status = Student.Status.ACTIVE
        update_fields = ["lead_status", "status", "updated_at"]
        if establishes_student_provenance and student.became_student_at is None:
            student.became_student_at = now()
            update_fields.append("became_student_at")
        student.save(update_fields=update_fields)
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            old_lead_status=old_lead_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            metadata={
                "student_status_from": old_student_status,
                "student_status_to": Student.Status.ACTIVE,
                "source": source,
            },
        )

    finalize_lead_conversion_side_effects(club_id=club_id, student_id=student.id)
    logger.info(
        "lead_converted_after_payment",
        extra={"student_id": student.id, "club_id": club_id},
    )
    return True


def convert_lead_for_manual_operational_admission(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
) -> bool:
    return _convert_lead_after_payment(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        source="manual_operational_admission",
        establishes_student_provenance=False,
        finalize_lead_conversion_side_effects=finalize_lead_conversion_side_effects,
        now=now,
    )


def _manual_operational_admission_evidence_error() -> None:
    """Keep every malformed manual-admission family fail-closed.

    The caller intentionally gets one safe error code.  More precise details
    would turn a staff mutation endpoint into a cross-tenant resource probe.
    """

    raise BusinessLogicError(
        "Manual operational admission evidence is incomplete or no longer valid.",
        code="manual_operational_admission_evidence_invalid",
    )


def _assert_pending_manual_payment_evidence(*, payment, student: Student) -> None:
    """Validate the payment/subscription portion shared by both origins."""

    subscription = payment.subscription
    if (
        payment.club_id != student.club_id
        or payment.student_id != student.id
        or payment.payment_method not in {"cash", "transfer"}
        or payment.status != "pending"
        or subscription is None
        or subscription.club_id != payment.club_id
        or subscription.student_id != student.id
        or subscription.tariff_id != payment.tariff_id
        or subscription.status != "pending"
        or subscription.deleted_at is not None
    ):
        _manual_operational_admission_evidence_error()


def _assert_personal_manual_operational_admission_evidence(*, payment, student: Student) -> dict[str, int]:
    """Return exact personal resource ids only when the immutable family is whole."""

    from apps.attendance.models import (
        PersonalDropInBooking,
        PersonalDropInPaymentLink,
        PersonalServiceTermsSnapshot,
        complete_personal_terms_queryset,
    )

    link = (
        PersonalDropInPaymentLink.objects.for_club(payment.club_id)
        .select_for_update(of=("self",))
        .select_related("booking__enrollment__schedule")
        .filter(payment_id=payment.id)
        .first()
    )
    if link is None:
        _manual_operational_admission_evidence_error()
    booking = link.booking
    enrollment = booking.enrollment
    terms = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(payment.club_id)
        .select_for_update(of=("self",))
        .filter(booking_id=booking.id)
    ).first()
    if (
        terms is None
        or booking.club_id != payment.club_id
        or enrollment.club_id != payment.club_id
        or enrollment.student_id != student.id
        or booking.state
        not in {
            PersonalDropInBooking.State.SCHEDULED,
            PersonalDropInBooking.State.ATTENDED,
        }
        or terms.tariff_id_snapshot != payment.tariff_id
        or terms.payable_amount != payment.amount
    ):
        _manual_operational_admission_evidence_error()
    return {"payment_link_id": link.id, "booking_id": booking.id, "terms_id": terms.id}


def _assert_group_manual_operational_admission_evidence(*, payment, student: Student) -> dict[str, int]:
    """Return canonical payment-owned group ids, never a legacy projection."""

    from apps.attendance.models import ScheduleEnrollment, TrainingGroupMembership

    membership = payment.conversion_group_membership
    enrollment = payment.conversion_enrollment
    if (
        membership is None
        or enrollment is None
        or payment.target_training_group_id is None
        or payment.target_schedule_id is None
        or payment.target_start_date is None
        or payment.group_membership_action_snapshot != "new_admission"
        or payment.target_group_membership_id != membership.id
        or membership.club_id != payment.club_id
        or membership.student_id != student.id
        or membership.training_group_id != payment.target_training_group_id
        or membership.starts_on != payment.target_start_date
        or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
        or enrollment.club_id != payment.club_id
        or enrollment.student_id != student.id
        or enrollment.schedule_id != payment.target_schedule_id
        or enrollment.training_group_membership_id != membership.id
        or enrollment.created_from != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        or enrollment.status not in {
            ScheduleEnrollment.Status.ACTIVE,
            ScheduleEnrollment.Status.FROZEN,
        }
    ):
        _manual_operational_admission_evidence_error()
    return {
        "training_group_id": membership.training_group_id,
        "membership_id": membership.id,
        "enrollment_id": enrollment.id,
    }


def admit_lead_for_manual_operational_admission(
    *,
    evidence,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
    occurred_at: datetime | None = None,
) -> bool:
    """Admit once from exact pending-manual evidence inside the caller transaction.

    This is deliberately separate from the legacy ``student_id`` helper above.
    It validates the complete immutable payment family before it touches a
    person, then records only non-PII resource identifiers in lifecycle
    provenance.  A replay sees the already-admitted person and creates no
    second conversion event or side effect.
    """

    from apps.billing.models import Payment

    if evidence.actor_user_id is None:
        _manual_operational_admission_evidence_error()
    with transaction.atomic():
        student = _get_lead_for_update(
            club_id=evidence.club_id,
            student_id=evidence.student_id,
        )
        payment = (
            Payment.objects.for_club(evidence.club_id)
            .select_for_update(of=("self",))
            .select_related(
                "subscription",
                "conversion_group_membership",
                "conversion_enrollment",
            )
            .filter(id=evidence.payment_id, student_id=student.id)
            .first()
        )
        if payment is None:
            _manual_operational_admission_evidence_error()
        # Request authorization is enforced by the command boundary.  The
        # durable owner additionally binds provenance to the immutable payment
        # recorder so a different valid staff actor cannot claim this
        # conversion or append a misleading lifecycle event.
        if evidence.actor_user_id != payment.recorded_by_id:
            _manual_operational_admission_evidence_error()
        from apps.clubs.models import ClubMembership

        if not ClubMembership.objects.filter(
            club_id=payment.club_id,
            user_id=evidence.actor_user_id,
            is_active=True,
            role__in={
                ClubMembership.Role.OWNER,
                ClubMembership.Role.ADMIN,
                ClubMembership.Role.TRAINER,
            },
        ).exists():
            _manual_operational_admission_evidence_error()
        _assert_pending_manual_payment_evidence(payment=payment, student=student)
        if evidence.origin == "personal":
            resource_ids = _assert_personal_manual_operational_admission_evidence(
                payment=payment,
                student=student,
            )
        elif evidence.origin == "group":
            resource_ids = _assert_group_manual_operational_admission_evidence(
                payment=payment,
                student=student,
            )
        else:
            _manual_operational_admission_evidence_error()

        if student.lead_status is None:
            # A prior exact admission or confirmed flow may already have
            # established the monotonic person fact. Never synthesize another
            # conversion event from a payment replay.
            if student.status != Student.Status.ACTIVE:
                _manual_operational_admission_evidence_error()
            if student.became_student_at is not None:
                return False

        old_lead_status = student.lead_status
        old_student_status = student.status
        student.lead_status = None
        student.status = Student.Status.ACTIVE
        update_fields = ["lead_status", "status", "updated_at"]
        if student.became_student_at is None:
            # Reconciliation supplies a timestamp from immutable exact
            # payment evidence. New commands use their authoritative command
            # time; neither path invents a mutable client timestamp.
            student.became_student_at = occurred_at or now()
            update_fields.append("became_student_at")
        student.save(update_fields=update_fields)
        _record_lifecycle_event(
            club_id=evidence.club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            old_lead_status=old_lead_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=evidence.actor_user_id,
            metadata={
                "source": "manual_operational_admission_v2",
                "payment_id": payment.id,
                "origin": evidence.origin,
                "resource_ids": resource_ids,
                "student_status_from": old_student_status,
                "student_status_to": Student.Status.ACTIVE,
            },
        )

    finalize_lead_conversion_side_effects(club_id=evidence.club_id, student_id=evidence.student_id)
    return True


def convert_lead_after_subscription_payment(
    *,
    club_id: int,
    student_id: int,
    actor_user_id: int | None = None,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
) -> bool:
    """Convert a lead when a staff-recorded subscription payment is confirmed."""
    return _convert_lead_after_payment(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        source="subscription_payment",
        finalize_lead_conversion_side_effects=finalize_lead_conversion_side_effects,
        now=now,
    )


def snooze_lead_for_pending_personal_payment(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
) -> None:
    """Capture the actionable lead context while a manual personal payment waits.

    The event is deliberately append-only compensation evidence.  The pending
    payment does not convert the person or cancel their pipeline.
    """

    from apps.retention.models import RetentionTask

    student = _get_lead_for_update(club_id=club_id, student_id=student_id)
    # Trial and trial_done are still active funnel provenance.  Person status
    # is a workspace projection, while lead_status is the lifecycle authority
    # for capturing/restoring a pending personal-admission task.
    if student.lead_status is None:
        return
    if LeadLifecycleEvent.objects.for_club(club_id).filter(
        student_id=student_id,
        event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_PENDING,
        metadata__payment_id=payment_id,
    ).exists():
        return

    task_type = (
        RetentionTask.TaskType.POST_TRIAL
        if student.lead_status == Student.LeadStatus.TRIAL_DONE
        else RetentionTask.TaskType.NEW_LEAD
    )
    task = (
        RetentionTask.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student_id=student_id,
            task_type=task_type,
            resolved_at__isnull=True,
        )
        .order_by("id")
        .first()
    )
    task_snapshot: dict[str, object] = {}
    if task is not None:
        task_snapshot = {
            "id": task.id,
            "task_type": task.task_type,
            "status": task.status,
            "due_date": task.due_date.isoformat(),
        }
        if task.status != RetentionTask.TaskStatus.SNOOZED:
            task.status = RetentionTask.TaskStatus.SNOOZED
            task.save(update_fields=["status", "updated_at"])

    _record_lifecycle_event(
        club_id=club_id,
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_PENDING,
        old_lead_status=student.lead_status,
        new_lead_status=student.lead_status,
        old_trainer_id=student.assigned_trainer_id,
        new_trainer_id=student.assigned_trainer_id,
        actor_user_id=actor_user_id,
        metadata={"payment_id": payment_id, "task": task_snapshot},
    )


def restore_lead_after_terminal_personal_payment(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
    outcome: str,
) -> None:
    """Restore only the lead context captured before an unattended rejection."""

    from apps.retention.models import RetentionTask

    student = _get_lead_for_update(club_id=club_id, student_id=student_id)
    if student.lead_status is None:
        return
    captured = (
        LeadLifecycleEvent.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_PENDING,
            metadata__payment_id=payment_id,
        )
        .order_by("-id")
        .first()
    )
    if captured is None:
        return
    if LeadLifecycleEvent.objects.for_club(club_id).filter(
        student_id=student_id,
        event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_RESTORED,
        metadata__payment_id=payment_id,
    ).exists():
        return

    task_snapshot = (captured.metadata or {}).get("task") or {}
    task_id = task_snapshot.get("id")
    if task_id:
        task = (
            RetentionTask.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=task_id, student_id=student_id, resolved_at__isnull=True)
            .first()
        )
        if task is not None:
            task.status = task_snapshot.get("status") or RetentionTask.TaskStatus.OPEN
            due_date = task_snapshot.get("due_date")
            if due_date:
                task.due_date = date.fromisoformat(due_date)
            task.save(update_fields=["status", "due_date", "updated_at"])

    _record_lifecycle_event(
        club_id=club_id,
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_RESTORED,
        old_lead_status=student.lead_status,
        new_lead_status=student.lead_status,
        old_trainer_id=student.assigned_trainer_id,
        new_trainer_id=student.assigned_trainer_id,
        actor_user_id=actor_user_id,
        metadata={"payment_id": payment_id, "outcome": outcome},
    )


def snooze_lead_for_pending_group_payment(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
) -> None:
    """Capture group-admission lead context without converting the person."""

    from apps.retention.models import RetentionTask

    student = _get_lead_for_update(club_id=club_id, student_id=student_id)
    if student.lead_status is None:
        return
    if LeadLifecycleEvent.objects.for_club(club_id).filter(
        student_id=student_id,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
        metadata__payment_id=payment_id,
    ).exists():
        return
    task_type = (
        RetentionTask.TaskType.POST_TRIAL
        if student.lead_status == Student.LeadStatus.TRIAL_DONE
        else RetentionTask.TaskType.NEW_LEAD
    )
    task = (
        RetentionTask.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student_id=student_id,
            task_type=task_type,
            resolved_at__isnull=True,
        )
        .order_by("id")
        .first()
    )
    task_snapshot: dict[str, object] = {}
    if task is not None:
        task_snapshot = {
            "id": task.id,
            "task_type": task.task_type,
            "status": task.status,
            "due_date": task.due_date.isoformat(),
        }
        if task.status != RetentionTask.TaskStatus.SNOOZED:
            task.status = RetentionTask.TaskStatus.SNOOZED
            task.save(update_fields=["status", "updated_at"])
    _record_lifecycle_event(
        club_id=club_id,
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
        old_lead_status=student.lead_status,
        new_lead_status=student.lead_status,
        old_trainer_id=student.assigned_trainer_id,
        new_trainer_id=student.assigned_trainer_id,
        actor_user_id=actor_user_id,
        metadata={"payment_id": payment_id, "task": task_snapshot},
    )


def restore_lead_after_terminal_group_payment(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
    outcome: str,
) -> None:
    """Undo only an unattended pending group-admission task snooze."""

    from apps.billing.models import Debt, Payment
    from apps.retention.models import RetentionTask

    student = _get_lead_for_update(club_id=club_id, student_id=student_id)
    if student.lead_status is None:
        return
    payment = Payment.objects.for_club(club_id).filter(id=payment_id, student_id=student_id).first()
    if payment is None:
        return
    # Only the debt reservation written by the exact pending-admission
    # check-in proves attended admission.  A historical visit in the same
    # group must not suppress terminal restoration of this new payment.
    attended = Debt.objects.for_club(club_id).filter(
        student_id=student_id,
        settlement_payment_id=payment.id,
        reason="pending_manual_admission",
        checkin__isnull=False,
    ).exists()
    if attended:
        return
    captured = (
        LeadLifecycleEvent.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
            metadata__payment_id=payment_id,
        )
        .order_by("-id")
        .first()
    )
    if captured is None or LeadLifecycleEvent.objects.for_club(club_id).filter(
        student_id=student_id,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
        metadata__payment_id=payment_id,
    ).exists():
        return
    task_snapshot = (captured.metadata or {}).get("task") or {}
    task_id = task_snapshot.get("id")
    if task_id:
        task = (
            RetentionTask.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=task_id, student_id=student_id, resolved_at__isnull=True)
            .first()
        )
        if task is not None:
            task.status = task_snapshot.get("status") or RetentionTask.TaskStatus.OPEN
            due_date = task_snapshot.get("due_date")
            if due_date:
                task.due_date = date.fromisoformat(due_date)
            task.save(update_fields=["status", "due_date", "updated_at"])
    _record_lifecycle_event(
        club_id=club_id,
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
        old_lead_status=student.lead_status,
        new_lead_status=student.lead_status,
        old_trainer_id=student.assigned_trainer_id,
        new_trainer_id=student.assigned_trainer_id,
        actor_user_id=actor_user_id,
        metadata={"payment_id": payment_id, "outcome": outcome},
    )


def convert_lead_after_group_admission_checkin(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    checkin_id: int,
    actor_user_id: int | None,
    finalize_lead_conversion_side_effects: Callable[..., None],
    now: Callable[[], datetime],
) -> bool:
    """Exact group admission attendance is one-way conversion evidence."""

    from apps.billing.models import Debt, Payment

    payment = (
        Payment.objects.for_club(club_id)
        .filter(
            id__in=Debt.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                checkin_id=checkin_id,
                reason="pending_manual_admission",
                settlement_payment__isnull=False,
            )
            .values("settlement_payment_id"),
            student_id=student_id,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
            conversion_group_membership__isnull=False,
        )
        .order_by("-id")
        .first()
    )
    if payment is None:
        return False
    return _convert_lead_after_payment(
        club_id=club_id,
        student_id=student_id,
        actor_user_id=actor_user_id,
        source="group_admission_checkin",
        establishes_student_provenance=True,
        finalize_lead_conversion_side_effects=finalize_lead_conversion_side_effects,
        now=now,
    )


def convert_lead_after_personal_attendance(
    *,
    club_id: int,
    student_id: int,
    booking_id: int,
    checkin_id: int,
    payment_id: int | None,
    actor_user_id: int | None,
    is_exact_booked_trial_checkin: Callable[..., bool] = _is_exact_booked_trial_checkin,
    now: Callable[[], datetime] = timezone.now,
) -> bool:
    """Make exact attended personal intent the one-way conversion evidence."""

    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        from apps.attendance.models import Checkin

        checkin = Checkin.objects.for_club(club_id).filter(id=checkin_id).first()
        if student.lead_status is None or is_exact_booked_trial_checkin(
            club_id=club_id,
            student=student,
            checkin=checkin,
            lock=True,
        ):
            return False
        old_lead_status = student.lead_status or ""
        old_student_status = student.status
        student.lead_status = None
        student.status = Student.Status.ACTIVE
        update_fields = ["lead_status", "status", "updated_at"]
        if student.became_student_at is None:
            student.became_student_at = now()
            update_fields.append("became_student_at")
        student.save(update_fields=update_fields)
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            old_lead_status=old_lead_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            metadata={
                "source": "personal_attendance",
                "booking_id": booking_id,
                "checkin_id": checkin_id,
                "payment_id": payment_id,
                "student_status_from": old_student_status,
                "student_status_to": Student.Status.ACTIVE,
            },
        )
    from apps.pipelines.services import cancel_pipeline
    from apps.retention.services import auto_close_tasks_on_personal_admission

    cancel_pipeline(club_id=club_id, student_id=student.id)
    auto_close_tasks_on_personal_admission(club_id=club_id, student_id=student.id)
    return True


def convert_lead_after_personal_payment_confirmation(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    actor_user_id: int | None,
    now: Callable[[], datetime],
) -> bool:
    """Convert a lead from a confirmed complete personal payment family."""
    with transaction.atomic():
        student = _get_lead_for_update(club_id=club_id, student_id=student_id)
        if student.lead_status is None and student.status != Student.Status.TRIAL:
            return False
        old_lead_status = student.lead_status or ""
        old_student_status = student.status
        student.lead_status = None
        student.status = Student.Status.ACTIVE
        update_fields = ["lead_status", "status", "updated_at"]
        if student.became_student_at is None:
            student.became_student_at = now()
            update_fields.append("became_student_at")
        student.save(update_fields=update_fields)
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            old_lead_status=old_lead_status,
            new_lead_status="",
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            metadata={
                "source": "personal_payment_confirmation",
                "payment_id": payment_id,
                "student_status_from": old_student_status,
                "student_status_to": Student.Status.ACTIVE,
            },
        )
    from apps.pipelines.services import cancel_pipeline
    from apps.retention.services import auto_close_tasks_on_personal_admission

    cancel_pipeline(club_id=club_id, student_id=student.id)
    auto_close_tasks_on_personal_admission(club_id=club_id, student_id=student.id)
    return True


def _finalize_lead_conversion_side_effects(*, club_id: int, student_id: int) -> None:
    from apps.pipelines.services import cancel_pipeline

    cancel_pipeline(club_id=club_id, student_id=student_id)

    from apps.retention.services import auto_close_tasks_on_subscription

    auto_close_tasks_on_subscription(student_id=student_id, club_id=club_id)
