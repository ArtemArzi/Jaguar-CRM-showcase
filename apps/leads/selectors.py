from __future__ import annotations

from datetime import UTC, datetime, time

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    ScheduleEnrollment,
)
from apps.billing.models import BankPaymentOrder, Payment
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.leads.models import LeadLifecycleEvent
from apps.retention.models import RetentionTask
from apps.students.journey_contracts import (
    ACTION_CONTEXT_PRIORITY,
    ActionCandidate,
    ActionContext,
    select_primary_action,
)
from apps.students.models import Student

ACTIVE_LEAD_WORKSPACE = "active"
ARCHIVED_LEAD_WORKSPACE = "archived"
VALID_LEAD_WORKSPACES = {ACTIVE_LEAD_WORKSPACE, ARCHIVED_LEAD_WORKSPACE}


def get_leads(
    *,
    club,
    status: str | None = None,
    assigned_trainer_id: int | None = None,
    scope: str = "all",
    current_trainer_id: int | None = None,
    workspace: str = ACTIVE_LEAD_WORKSPACE,
) -> QuerySet[Student]:
    qs = Student.objects.for_club(club).filter(deleted_at__isnull=True).select_related(
        "assigned_trainer"
    )
    if workspace == ACTIVE_LEAD_WORKSPACE:
        qs = qs.filter(lead_status__isnull=False)
    elif workspace == ARCHIVED_LEAD_WORKSPACE:
        qs = qs.filter(
            lead_status__isnull=True,
            became_student_at__isnull=True,
            status=Student.Status.LOST,
        )
    else:
        return qs.none()

    if scope == "mine":
        if current_trainer_id is None:
            return qs.none()
        qs = qs.filter(assigned_trainer_id=current_trainer_id)
    elif scope == "pool":
        qs = qs.filter(assigned_trainer__isnull=True)
    elif scope != "all":
        return qs.none()

    if status and workspace == ACTIVE_LEAD_WORKSPACE:
        qs = qs.filter(lead_status=status)
    if assigned_trainer_id and scope == "all":
        qs = qs.filter(assigned_trainer_id=assigned_trainer_id)

    return qs.order_by("-created_at", "-id")


def get_lead_funnel_stats(*, club) -> dict:
    rows = (
        Student.objects.for_club(club)
        .filter(lead_status__isnull=False, deleted_at__isnull=True)
        .values("lead_status")
        .annotate(count=Count("id"))
    )
    stats = {
        "new": 0,
        "contacted": 0,
        "trial_booked": 0,
        "trial_done": 0,
        "thinking": 0,
    }
    for row in rows:
        if row["lead_status"] in stats:
            stats[row["lead_status"]] = row["count"]
    return stats


def _action_detail(
    *,
    candidate: ActionCandidate,
    kind: str,
    label: str,
    supporting_text: str,
    resource_type: str,
) -> dict:
    return {
        "kind": kind,
        "label": label,
        "supporting_text": supporting_text,
        "target_resource_type": resource_type,
        "target_resource_id": candidate.resource_id,
        "context": candidate.context.value,
    }


def _candidate_sort_key(candidate: ActionCandidate) -> tuple:
    instant = candidate.tie_break_at
    if instant is None:
        instant = datetime.max.replace(tzinfo=UTC)
    else:
        instant = instant.astimezone(UTC)
    return ACTION_CONTEXT_PRIORITY[candidate.context], instant, candidate.resource_id


def _active_context(context: ActionContext) -> str | None:
    if context in {
        ActionContext.PAYMENT_ACTION_REQUIRED,
        ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT,
    }:
        return "payment"
    if context == ActionContext.UPCOMING_PERSONAL_BOOKING:
        return "personal_booking"
    if context in {ActionContext.UPCOMING_TRIAL, ActionContext.TRIAL_DONE}:
        return "trial"
    return None


def _display_local_datetime(*, value: datetime, zone) -> str:
    return value.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def get_lead_action_contexts(*, club, leads: list[Student]) -> dict[int, dict]:
    """Build D4 read models in bounded batch queries for list and detail views."""

    lead_ids = [lead.id for lead in leads]
    if not lead_ids:
        return {}
    now = timezone.now()
    zone = club_zoneinfo(club)
    today = club_localdate(club)
    entries: dict[int, list[tuple[ActionCandidate, dict]]] = {
        lead_id: [] for lead_id in lead_ids
    }

    def add(student_id: int, candidate: ActionCandidate, detail: dict) -> None:
        entries[student_id].append((candidate, detail))

    order_statuses = {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.AUTHORIZED,
        BankPaymentOrder.Status.FAILED,
        BankPaymentOrder.Status.MANUAL_REVIEW,
    }
    for order in BankPaymentOrder.objects.for_club(club).filter(
        student_id__in=lead_ids,
        status__in=order_statuses,
    ):
        if order.status in {
            BankPaymentOrder.Status.CREATED,
            BankPaymentOrder.Status.PENDING,
            BankPaymentOrder.Status.AUTHORIZED,
        } and order.expires_at <= now:
            continue
        if order.status == BankPaymentOrder.Status.FAILED:
            label = "Повторить оплату"
        elif order.status == BankPaymentOrder.Status.MANUAL_REVIEW:
            label = "Проверить оплату"
        else:
            label = "Продолжить оплату"
        candidate = ActionCandidate(
            context=ActionContext.PAYMENT_ACTION_REQUIRED,
            resource_id=order.id,
            is_live=True,
            tie_break_at=order.expires_at,
        )
        add(
            order.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="open_payment",
                label=label,
                supporting_text=(
                    f"{order.purpose_snapshot}: {order.amount_snapshot} ₽. "
                    f"Действие по оплате до {_display_local_datetime(value=order.expires_at, zone=zone)}."
                ),
                resource_type="bank_payment_order",
            ),
        )

    for payment in Payment.objects.for_club(club).filter(
        student_id__in=lead_ids,
        deleted_at__isnull=True,
        status=Payment.Status.PENDING,
        payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
    ).filter(
        Q(target_schedule__isnull=False)
        | Q(personal_payment_reservation__isnull=False)
    ).select_related("tariff"):
        candidate = ActionCandidate(
            context=ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT,
            resource_id=payment.id,
            is_live=True,
            tie_break_at=payment.created_at,
        )
        add(
            payment.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="open_payment",
                label="Открыть оплату",
                supporting_text=(
                    f"{payment.tariff.name}: {payment.amount} ₽. "
                    "Оплата ожидает проверки руководителем."
                ),
                resource_type="payment",
            ),
        )

    reservation_statuses = {
        PersonalBookingPaymentReservation.Status.BOOKED,
        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
    }
    live_reservation_state = Q(status__in=reservation_statuses) | Q(
        status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
        expires_at__gt=now,
    )
    for reservation in (
        PersonalBookingPaymentReservation.objects.for_club(club)
        .filter(
            student_id__in=lead_ids,
            starts_at__gte=now,
        )
        .filter(live_reservation_state)
    ):
        if reservation.status == PersonalBookingPaymentReservation.Status.BOOKED:
            context = ActionContext.UPCOMING_PERSONAL_BOOKING
            kind = "open_personal_booking"
            label = "Открыть запись"
            supporting_text = (
                "Персональная тренировка "
                f"{_display_local_datetime(value=reservation.starts_at, zone=zone)}."
            )
        else:
            context = (
                ActionContext.PAYMENT_ACTION_REQUIRED
                if reservation.status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
                else ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT
            )
            kind = "open_payment"
            label = (
                "Проверить оплату"
                if reservation.status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
                else "Продолжить оплату"
            )
            supporting_text = (
                "Персональная запись "
                f"{_display_local_datetime(value=reservation.starts_at, zone=zone)} "
                "ожидает оплаты."
            )
        candidate = ActionCandidate(
            context=context,
            resource_id=reservation.id,
            is_live=True,
            tie_break_at=reservation.starts_at,
        )
        add(
            reservation.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind=kind,
                label=label,
                supporting_text=supporting_text,
                resource_type="personal_booking_reservation",
            ),
        )

    for booking in PersonalDropInBooking.objects.for_club(club).filter(
        enrollment__student_id__in=lead_ids,
        state=PersonalDropInBooking.State.SCHEDULED,
        enrollment__status=ScheduleEnrollment.Status.ACTIVE,
        enrollment__starts_on__gte=today,
    ).select_related("enrollment__schedule"):
        schedule = booking.enrollment.schedule
        booking_date = booking.enrollment.starts_on or schedule.one_time_date
        if booking_date is None:
            continue
        starts_at = timezone.make_aware(
            datetime.combine(booking_date, schedule.start_time),
            zone,
        )
        if starts_at < now:
            continue
        candidate = ActionCandidate(
            context=ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT,
            resource_id=booking.id,
            is_live=True,
            tie_break_at=starts_at,
        )
        add(
            booking.enrollment.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="open_personal_booking",
                label="Открыть запись",
                supporting_text=(
                    "Персональная тренировка "
                    f"{_display_local_datetime(value=starts_at, zone=zone)} "
                    "оплачивается при посещении."
                ),
                resource_type="personal_drop_in_booking",
            ),
        )

    personal_sources = {
        ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
    }
    reservation_enrollment_ids = set(
        PersonalBookingPaymentReservation.objects.for_club(club)
        .filter(
            student_id__in=lead_ids,
            enrollment_id__isnull=False,
        )
        .filter(live_reservation_state)
        .values_list("enrollment_id", flat=True)
    )
    for enrollment in ScheduleEnrollment.objects.for_club(club).filter(
        student_id__in=lead_ids,
        status=ScheduleEnrollment.Status.ACTIVE,
        created_from__in=personal_sources,
        starts_on__gte=today,
    ).select_related("schedule"):
        if enrollment.id in reservation_enrollment_ids:
            continue
        starts_at = timezone.make_aware(
            datetime.combine(enrollment.starts_on, enrollment.schedule.start_time),
            zone,
        )
        if starts_at < now:
            continue
        candidate = ActionCandidate(
            context=ActionContext.UPCOMING_PERSONAL_BOOKING,
            resource_id=enrollment.id,
            is_live=True,
            tie_break_at=starts_at,
        )
        add(
            enrollment.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="open_personal_booking",
                label="Открыть запись",
                supporting_text=(
                    "Персональная тренировка "
                    f"{_display_local_datetime(value=starts_at, zone=zone)}."
                ),
                resource_type="schedule_enrollment",
            ),
        )

    for enrollment in ScheduleEnrollment.objects.for_club(club).filter(
        student_id__in=lead_ids,
        status=ScheduleEnrollment.Status.TRIAL,
        trial_at__isnull=False,
        trial_at__gte=now,
    ):
        candidate = ActionCandidate(
            context=ActionContext.UPCOMING_TRIAL,
            resource_id=enrollment.id,
            is_live=True,
            tie_break_at=enrollment.trial_at,
        )
        add(
            enrollment.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="open_trial",
                label="Открыть пробную",
                supporting_text=(
                    "Пробная тренировка "
                    f"{_display_local_datetime(value=enrollment.trial_at, zone=zone)}."
                ),
                resource_type="schedule_enrollment",
            ),
        )

    for task in RetentionTask.objects.for_club(club).filter(
        student_id__in=lead_ids,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        resolved_at__isnull=True,
    ):
        due_at = timezone.make_aware(datetime.combine(task.due_date, time.min), zone)
        candidate = ActionCandidate(
            context=ActionContext.LEAD_TASK_OR_STAGE,
            resource_id=task.id,
            is_live=True,
            tie_break_at=due_at,
        )
        add(
            task.student_id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind="contact_lead",
                label="Связаться",
                supporting_text=f"Следующий контакт до {task.due_date:%d.%m.%Y}.",
                resource_type="retention_task",
            ),
        )

    stage_labels = {
        Student.LeadStatus.NEW: ("Связаться", "Нужно связаться с клиентом."),
        Student.LeadStatus.CONTACTED: (
            "Назначить следующий шаг",
            "Контакт состоялся, зафиксируйте результат.",
        ),
        Student.LeadStatus.TRIAL_BOOKED: (
            "Проверить пробную",
            "Пробная назначена, ожидается точный check-in.",
        ),
        Student.LeadStatus.TRIAL_DONE: (
            "Проверить пробную",
            "Старый статус пробной не подтверждён точным check-in.",
        ),
        Student.LeadStatus.THINKING: (
            "Связаться повторно",
            "Клиент думает, нужен следующий контакт.",
        ),
    }
    latest_trial_done_transition: dict[int, str] = {}
    for student_id, event_type in (
        LeadLifecycleEvent.objects.for_club(club)
        .filter(
            student_id__in=lead_ids,
            new_lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        .exclude(old_lead_status=Student.LeadStatus.TRIAL_DONE)
        .order_by("student_id", "-created_at", "-id")
        .values_list("student_id", "event_type")
    ):
        latest_trial_done_transition.setdefault(student_id, event_type)
    exact_trial_done_ids = {
        student_id
        for student_id, event_type in latest_trial_done_transition.items()
        if event_type == LeadLifecycleEvent.EventType.TRIAL_DONE
    }
    for lead in leads:
        if (
            lead.lead_status == Student.LeadStatus.TRIAL_DONE
            and lead.id in exact_trial_done_ids
        ):
            context = ActionContext.TRIAL_DONE
            kind = "sell_training"
            label = "Оформить обучение"
            supporting_text = "Пробная подтверждена check-in, можно оформить обучение."
        else:
            context = ActionContext.LEAD_TASK_OR_STAGE
            kind = "contact_lead"
            label, supporting_text = stage_labels.get(
                lead.lead_status,
                ("Открыть заявку", "Проверьте текущий этап заявки."),
            )
        candidate = ActionCandidate(
            context=context,
            resource_id=lead.id,
            is_live=True,
            tie_break_at=(lead.created_at if context == ActionContext.TRIAL_DONE else None),
        )
        add(
            lead.id,
            candidate,
            _action_detail(
                candidate=candidate,
                kind=kind,
                label=label,
                supporting_text=supporting_text,
                resource_type="student",
            ),
        )

    result: dict[int, dict] = {}
    for lead_id, candidate_entries in entries.items():
        candidates = tuple(candidate for candidate, _detail in candidate_entries)
        primary = select_primary_action(candidates=candidates)
        detail_by_identity = {
            id(candidate): detail for candidate, detail in candidate_entries
        }
        ordered = sorted(candidates, key=_candidate_sort_key)
        primary_detail = detail_by_identity[id(primary)] if primary is not None else None
        result[lead_id] = {
            "primary_action": primary_detail,
            "active_context": _active_context(primary.context) if primary is not None else None,
            "secondary_capabilities": [
                detail_by_identity[id(candidate)]
                for candidate in ordered
                if candidate is not primary
            ],
        }
    return result


def get_lead_action_context(*, club, lead: Student) -> dict:
    return get_lead_action_contexts(club=club, leads=[lead])[lead.id]
