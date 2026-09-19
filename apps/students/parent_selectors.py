from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment, ScheduleException
from apps.attendance.selectors import (
    get_student_attendance,
    get_student_schedule,
    get_student_schedule_occurrences_for_range,
)
from apps.billing.models import Subscription, SubscriptionFreeze
from apps.billing.selectors import current_active_subscription_q, get_student_open_debts
from apps.billing.service_modules.renewals import get_renewal_offer
from apps.clubs.timezones import club_localdate
from apps.documents.selectors import get_document_checklist
from apps.grades.models import StudentGrade
from apps.grades.selectors import get_student_all_grades
from apps.students.models import Student
from apps.students.selectors import get_cabinet_financial_read_model

PARENT_RECENT_EXPIRED_SUBSCRIPTION_DAYS = 30
PARENT_SUBSCRIPTION_STATUS_PRIORITY = {
    Subscription.Status.ACTIVE: 0,
    Subscription.Status.FROZEN: 1,
    Subscription.Status.PENDING: 2,
    Subscription.Status.EXPIRED: 3,
    Subscription.Status.CANCELLED: 4,
}
PARENT_SUBSCRIPTION_ATTENTION_PRIORITY = {
    "pending_freeze": 0,
    Subscription.Status.FROZEN: 1,
    Subscription.Status.PENDING: 2,
    Subscription.Status.EXPIRED: 3,
    Subscription.Status.CANCELLED: 10,
    "last_training": 4,
    "low_trainings": 5,
    "ok": 9,
}


def _parent_child_queryset(*, user_id: int, club):
    return (
        Student.objects.for_club(club)
        .filter(
            parent_user_id=user_id,
            is_child=True,
            deleted_at__isnull=True,
        )
        .order_by("last_name", "first_name", "id")
    )


def get_parent_child(*, user_id: int, club, student_id: int) -> Student:
    return _parent_child_queryset(user_id=user_id, club=club).get(id=student_id)


def _next_training_preview(*, club, student_id: int, today=None) -> dict:
    today = today or club_localdate(club)
    occurrences = get_student_schedule_occurrences_for_range(
        club=club,
        student_id=student_id,
        date_from=today,
        date_to=today + timedelta(days=14),
    )
    if not occurrences:
        return {}

    attended_occurrences = set(
        Checkin.objects.for_club(club)
        .filter(
            student_id=student_id,
            schedule_id__in={occurrence.schedule_id for occurrence in occurrences},
            date__gte=today,
            date__lte=today + timedelta(days=14),
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .values_list("schedule_id", "date")
    )
    occurrence = next(
        (
            occurrence
            for occurrence in occurrences
            if (occurrence.schedule_id, occurrence.effective_date) not in attended_occurrences
        ),
        None,
    )
    if occurrence is None:
        return {}

    return {
        "next_training_day_of_week": occurrence.effective_date.weekday(),
        "next_training_start_time": occurrence.effective_start_time.strftime("%H:%M"),
        "next_training_group_name": occurrence.group_name,
        "next_training_trainer_name": occurrence.trainer_name,
        "next_training_is_rescheduled": occurrence.is_rescheduled,
        "next_training_is_substitute": occurrence.is_substitute,
    }


def _occurrence_summary(occurrence) -> dict:
    return {
        "schedule_id": occurrence.schedule_id,
        "effective_date": occurrence.effective_date,
        "effective_start_time": occurrence.effective_start_time,
        "effective_end_time": occurrence.effective_end_time,
        "trainer_id": occurrence.trainer_id,
        "trainer_name": occurrence.trainer_name,
        "location_id": occurrence.location_id,
        "location_name": occurrence.location_name,
        "is_rescheduled": occurrence.is_rescheduled,
        "is_substitute": occurrence.is_substitute,
        "training_type_id": occurrence.training_type_id,
        "training_type_name": occurrence.training_type_name,
        "training_type_kind": occurrence.training_type_kind,
        "one_time_date": occurrence.one_time_date,
        "enrollment_id": occurrence.enrollment_id,
        "created_from": occurrence.created_from,
        "can_cancel": occurrence.can_cancel,
    }


def _exception_summary(exception: ScheduleException) -> dict:
    substitute_name = ""
    if exception.substitute_trainer:
        substitute_name = f"{exception.substitute_trainer.first_name} {exception.substitute_trainer.last_name}"
    return {
        "id": exception.id,
        "schedule_id": exception.schedule_id,
        "date": exception.date,
        "exception_type": exception.exception_type,
        "reason": exception.reason,
        "new_date": exception.new_date,
        "new_start_time": exception.new_start_time,
        "new_end_time": exception.new_end_time,
        "substitute_trainer_id": exception.substitute_trainer_id,
        "substitute_trainer_name": substitute_name,
    }


def _parent_subscription_attention_key(
    subscription: Subscription,
    *,
    freeze_status: str | None = None,
) -> tuple[int, int, float, int]:
    if freeze_status == SubscriptionFreeze.FreezeStatus.PENDING:
        attention = PARENT_SUBSCRIPTION_ATTENTION_PRIORITY["pending_freeze"]
    elif subscription.status in (
        Subscription.Status.FROZEN,
        Subscription.Status.PENDING,
        Subscription.Status.EXPIRED,
        Subscription.Status.CANCELLED,
    ):
        attention = PARENT_SUBSCRIPTION_ATTENTION_PRIORITY[subscription.status]
    elif subscription.trainings_left == 1:
        attention = PARENT_SUBSCRIPTION_ATTENTION_PRIORITY["last_training"]
    elif subscription.trainings_left is not None and subscription.trainings_left <= 3:
        attention = PARENT_SUBSCRIPTION_ATTENTION_PRIORITY["low_trainings"]
    else:
        attention = PARENT_SUBSCRIPTION_ATTENTION_PRIORITY["ok"]

    return (attention, *_subscription_sort_key(subscription))


def _upcoming_schedule_state(
    *,
    club,
    student_id: int,
    schedule_ids: list[int],
    today=None,
) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    if not schedule_ids:
        return {}, {}

    today = today or club_localdate(club)
    date_to = today + timedelta(days=14)
    all_enrollments = ScheduleEnrollment.objects.for_club(club).filter(
        student_id=student_id,
        schedule_id__in=schedule_ids,
    )
    enrollment_schedule_ids = set(
        all_enrollments.values_list("schedule_id", flat=True).distinct()
    )
    enrollments = list(
        all_enrollments
        .filter(
            Q(
                status__in=[
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.TRIAL,
                    ScheduleEnrollment.Status.FROZEN,
                ]
            )
            | Q(
                status__in=[
                    ScheduleEnrollment.Status.TRANSFERRED,
                    ScheduleEnrollment.Status.CANCELLED,
                ],
                ends_on__isnull=False,
                ends_on__gte=today,
            )
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=today))
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=date_to))
        .only("schedule_id", "status", "starts_on", "ends_on")
    )
    legacy_attended_schedule_ids = set(
        Checkin.objects.for_club(club)
        .filter(
            student_id=student_id,
            schedule_id__in=schedule_ids,
            deleted_at__isnull=True,
        )
        .exclude(schedule_id__in=enrollment_schedule_ids)
        .values_list("schedule_id", flat=True)
        .distinct()
    )

    def is_enrolled_for_exception(exception: ScheduleException) -> bool:
        if exception.schedule_id in legacy_attended_schedule_ids:
            return True

        candidate_dates = [exception.date]
        if exception.new_date:
            candidate_dates.append(exception.new_date)

        for enrollment in enrollments:
            if enrollment.schedule_id != exception.schedule_id:
                continue
            for candidate_date in candidate_dates:
                if enrollment.starts_on and candidate_date < enrollment.starts_on:
                    continue
                if enrollment.ends_on and candidate_date > enrollment.ends_on:
                    continue
                return True
        return False

    occurrences_by_schedule: dict[int, list[dict]] = {}
    for occurrence in get_student_schedule_occurrences_for_range(
        club=club,
        student_id=student_id,
        date_from=today,
        date_to=date_to,
    ):
        if occurrence.schedule_id in schedule_ids:
            occurrences_by_schedule.setdefault(occurrence.schedule_id, []).append(
                _occurrence_summary(occurrence)
            )

    exceptions_by_schedule: dict[int, list[dict]] = {}
    exceptions = (
        ScheduleException.objects.for_club(club)
        .filter(schedule_id__in=schedule_ids)
        .filter(
            Q(date__gte=today, date__lte=date_to) |
            Q(new_date__gte=today, new_date__lte=date_to)
        )
        .select_related("substitute_trainer")
        .order_by("date", "id")
    )
    for exception in exceptions:
        if not is_enrolled_for_exception(exception):
            continue
        exceptions_by_schedule.setdefault(exception.schedule_id, []).append(
            _exception_summary(exception)
        )

    return occurrences_by_schedule, exceptions_by_schedule


def get_parent_children(*, user_id: int, club) -> list[dict]:
    """Return enriched child summaries with grade, subscription, last visit."""
    children = list(_parent_child_queryset(user_id=user_id, club=club).select_related("club"))

    if not children:
        return []

    child_ids = [c.id for c in children]

    # Batch fetch parent-visible subscriptions for all children.
    subscriptions_by_student: dict[int, list[Subscription]] = {}
    now = timezone.now()
    recent_expired_cutoff = now - timedelta(
        days=PARENT_RECENT_EXPIRED_SUBSCRIPTION_DAYS
    )
    subscriptions = list(
        Subscription.objects.for_club(club)
        .filter(student_id__in=child_ids, deleted_at__isnull=True)
        .filter(
            current_active_subscription_q(now)
            | Q(
                status__in=[
                    Subscription.Status.FROZEN,
                    Subscription.Status.PENDING,
                    Subscription.Status.CANCELLED,
                ]
            )
            | Q(
                status=Subscription.Status.EXPIRED,
                expires_at__isnull=False,
                expires_at__gte=recent_expired_cutoff,
            )
        )
        .select_related("tariff")
    )
    pending_freeze_statuses = _pending_freeze_statuses(
        club=club,
        subscription_ids=[sub.id for sub in subscriptions],
    )
    for sub in sorted(
        subscriptions,
        key=lambda item: (
            item.student_id,
            *_parent_subscription_attention_key(
                item,
                freeze_status=pending_freeze_statuses.get(item.id),
            ),
        ),
    ):
        subscriptions_by_student.setdefault(sub.student_id, []).append(sub)

    # Batch fetch current grades (first per student, by grade system order)
    grade_names = {}
    for sg in (
        StudentGrade.objects.for_club(club.id)
        .filter(student_id__in=child_ids)
        .select_related("current_grade")
        .order_by("student_id", "grade_system_id")
    ):
        if sg.student_id not in grade_names and sg.current_grade:
            grade_names[sg.student_id] = sg.current_grade.name

    today = club_localdate(club)
    result = []
    for child in children:
        sub = (subscriptions_by_student.get(child.id) or [None])[0]
        preview = _next_training_preview(club=club, student_id=child.id, today=today)
        result.append({
            "id": child.id,
            "first_name": child.first_name,
            "last_name": child.last_name,
            "status": child.status,
            "is_child": child.is_child,
            "grade_name": grade_names.get(child.id),
            "subscription_remaining": sub.trainings_left if sub else None,
            "subscription_total": sub.tariff.trainings_limit if sub else None,
            "subscription_status": sub.status if sub else None,
            "subscription_freeze_status": pending_freeze_statuses.get(sub.id) if sub else None,
            "last_visit_date": child.last_visit_date.isoformat() if child.last_visit_date else None,
            "next_training_day_of_week": preview.get("next_training_day_of_week"),
            "next_training_start_time": preview.get("next_training_start_time"),
            "next_training_group_name": preview.get("next_training_group_name"),
            "next_training_trainer_name": preview.get("next_training_trainer_name"),
            "next_training_is_rescheduled": preview.get("next_training_is_rescheduled"),
            "next_training_is_substitute": preview.get("next_training_is_substitute"),
        })

    return result


def get_child_attendance(
    *,
    user_id: int,
    club,
    student_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """Return recent attendance for parent's child. Reuses get_student_attendance."""
    # Verify parent owns this child
    get_parent_child(user_id=user_id, club=club, student_id=student_id)
    checkins = get_student_attendance(club=club, student_id=student_id)[
        offset:offset + limit
    ]
    return [
        {
            "date": c.date.isoformat(),
            "group_name": c.schedule.group_name,
            "trainer_name": f"{c.trainer.first_name} {c.trainer.last_name}",
            "training_type_name": c.training_type.name,
            "start_time": c.schedule.start_time.strftime("%H:%M"),
        }
        for c in checkins
    ]


def _pending_freeze_statuses(*, club, subscription_ids: list[int]) -> dict[int, str]:
    if not subscription_ids:
        return {}

    pending_by_subscription: dict[int, str] = {}
    for freeze in (
        SubscriptionFreeze.objects.for_club(club)
        .filter(
            subscription_id__in=subscription_ids,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )
        .only("subscription_id", "status")
        .order_by("subscription_id", "-created_at", "-id")
    ):
        if freeze.subscription_id not in pending_by_subscription:
            pending_by_subscription[freeze.subscription_id] = freeze.status
    return pending_by_subscription


def _subscription_summary(
    subscription: Subscription,
    *,
    freeze_status: str | None = None,
) -> dict:
    renewal_offer = get_renewal_offer(
        club_id=subscription.club_id,
        source_tariff=subscription.tariff,
    )
    renewal_target_tariff_id = renewal_offer.target_tariff_id if renewal_offer.is_available else None
    renewal_target_tariff_name = renewal_offer.target_tariff_name if renewal_offer.is_available else ""
    renewal_target_price = renewal_offer.target_price if renewal_offer.is_available else None
    return {
        "id": subscription.id,
        "tariff_id": subscription.tariff_id,
        "tariff_name": subscription.tariff.name,
        "trainings_used": subscription.trainings_used,
        "trainings_total": subscription.tariff.trainings_limit,
        "trainings_left": subscription.trainings_left,
        "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
        "status": subscription.status,
        "freeze_status": freeze_status,
        "renewal_target_tariff_id": renewal_target_tariff_id,
        "renewal_target_tariff_name": renewal_target_tariff_name,
        "renewal_target_price": renewal_target_price,
    }


def _subscription_sort_key(subscription: Subscription) -> tuple[int, float, int]:
    expires_sort = float("inf")
    if subscription.expires_at:
        expires_sort = subscription.expires_at.timestamp()
    return (
        PARENT_SUBSCRIPTION_STATUS_PRIORITY.get(subscription.status, 9),
        -expires_sort,
        -subscription.id,
    )


def _parent_visible_subscriptions(*, club, student_id: int) -> list[Subscription]:
    now = timezone.now()
    recent_expired_cutoff = now - timedelta(
        days=PARENT_RECENT_EXPIRED_SUBSCRIPTION_DAYS
    )
    base_qs = (
        Subscription.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .select_related("tariff")
    )
    visible = list(
        base_qs.filter(
            current_active_subscription_q(now)
            | Q(
                status__in=[
                    Subscription.Status.FROZEN,
                    Subscription.Status.PENDING,
                    Subscription.Status.CANCELLED,
                ]
            )
            | Q(
                status=Subscription.Status.EXPIRED,
                expires_at__isnull=False,
                expires_at__gte=recent_expired_cutoff,
            )
        )
    )
    return sorted(visible, key=_subscription_sort_key)


def _debt_summary(debt) -> dict:
    return {
        "id": debt.id,
        "checkin_id": debt.checkin_id,
        "tariff_price": debt.tariff_price,
        "reason": debt.reason,
        "training_type_name": debt.checkin.training_type.name,
        "checkin_date": debt.checkin.date,
        "created_at": debt.created_at,
    }


def get_child_profile(*, user_id: int, club, student_id: int) -> dict:
    student = get_parent_child(user_id=user_id, club=club, student_id=student_id)

    grade_progress = get_student_all_grades(club_id=club.id, student_id=student_id)
    attendance_count = Checkin.objects.for_club(club).filter(
        student_id=student_id,
        deleted_at__isnull=True,
    ).count()

    parent_visible_subscriptions = _parent_visible_subscriptions(
        club=club,
        student_id=student_id,
    )
    pending_freeze_statuses = _pending_freeze_statuses(
        club=club,
        subscription_ids=[sub.id for sub in parent_visible_subscriptions],
    )
    active_subscriptions = [
        _subscription_summary(
            sub,
            freeze_status=pending_freeze_statuses.get(sub.id),
        )
        for sub in parent_visible_subscriptions
    ]
    active_subscription = active_subscriptions[0] if active_subscriptions else None
    open_debts = [
        _debt_summary(debt)
        for debt in get_student_open_debts(club=club, student_id=student_id)
    ]

    schedules = list(
        get_student_schedule(club=club, student_id=student_id).select_related(
            "training_type"
        )
    )
    schedule_ids = [s.id for s in schedules]
    today = club_localdate(club)
    occurrences_by_schedule, exceptions_by_schedule = _upcoming_schedule_state(
        club=club,
        student_id=student_id,
        schedule_ids=schedule_ids,
        today=today,
    )
    fallback_training_types_by_schedule = {}
    if schedule_ids:
        for checkin in (
            Checkin.objects.for_club(club)
            .filter(
                student_id=student_id,
                schedule_id__in=schedule_ids,
                deleted_at__isnull=True,
            )
            .select_related("training_type")
            .order_by("schedule_id", "-date", "-id")
        ):
            fallback_training_types_by_schedule.setdefault(
                checkin.schedule_id,
                checkin.training_type,
            )
    schedule_list = [
        {
            "id": s.id,
            "day_of_week": s.day_of_week,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "group_name": s.training_group.name if s.training_group_id else s.group_name,
            "training_type_id": (
                s.training_type_id
                or getattr(fallback_training_types_by_schedule.get(s.id), "id", None)
            ),
            "training_type_name": (
                s.training_type.name
                if s.training_type
                else getattr(fallback_training_types_by_schedule.get(s.id), "name", "")
            ),
            "training_type_kind": (
                s.training_type.kind
                if s.training_type
                else getattr(fallback_training_types_by_schedule.get(s.id), "kind", "")
            ),
            "one_time_date": s.one_time_date,
            "trainer_name": f"{s.trainer.first_name} {s.trainer.last_name}",
            "location_name": s.location.name,
            "upcoming_occurrences": occurrences_by_schedule.get(s.id, []),
            "upcoming_exceptions": exceptions_by_schedule.get(s.id, []),
        }
        for s in schedules
    ]

    return {
        "id": student.id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "status": student.status,
        "grade_progress": grade_progress,
        "attendance_count": attendance_count,
        "active_subscription": active_subscription,
        "active_subscriptions": active_subscriptions,
        "open_debts": open_debts,
        "financial_state": get_cabinet_financial_read_model(club=club, student=student),
        "document_checklist": get_document_checklist(club=club, student_id=student_id),
        "schedule": schedule_list,
    }
