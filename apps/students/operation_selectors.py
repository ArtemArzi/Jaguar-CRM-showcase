"""Read models for the owner/admin student-card operations."""

from collections import defaultdict
from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import Checkin, StudentAttendanceCorrection, TrainingGroupMembership
from apps.billing.models import OpeningEntitlementSnapshot, Payment, SubscriptionComponent, SubscriptionCorrection
from apps.billing.recognition import payment_recognition_date
from apps.clubs.timezones import club_localdate


def subscription_expires_on(*, subscription, club):
    if subscription.expires_at is None:
        return None
    return club_localdate(club, subscription.expires_at - timedelta(microseconds=1))


def get_student_operation_sections(*, club, student, subscriptions):
    components = defaultdict(list)
    for component in (
        SubscriptionComponent.objects.for_club(club)
        .filter(
            subscription_id__in=[s.id for s in subscriptions],
        )
        .select_related("training_type", "location")
        .order_by("id")
    ):
        components[component.subscription_id].append(component)
    openings = {
        s.subscription_id: s
        for s in OpeningEntitlementSnapshot.objects.for_club(club).filter(
            subscription__student=student,
        )
    }
    cards, archive = [], []
    for subscription in subscriptions:
        rows = components[subscription.id]
        card = {
            "subscription": subscription,
            "components": rows,
            "expires_on": subscription_expires_on(subscription=subscription, club=club),
            "opening": openings.get(subscription.id),
            "can_correct": subscription.status in {"active", "expired"},
        }
        expired = subscription.expires_at and subscription.expires_at <= timezone.now()
        (archive if subscription.status in {"expired", "cancelled"} or expired else cards).append(card)
    payments = list(Payment.objects.for_club(club).filter(student=student).select_related("tariff").order_by("-id")[:5])
    for payment in payments:
        payment.recognition_on = payment_recognition_date(payment=payment, club=club)
    memberships = (
        TrainingGroupMembership.objects.for_club(club)
        .filter(
            student=student,
            status__in=["active", "frozen"],
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=club_localdate(club)))
        .select_related(
            "training_group",
            "training_group__responsible_trainer",
        )
        .order_by("id")
    )
    return {
        "operation_cards": cards,
        "archived_operation_cards": archive,
        "recent_checkins": Checkin.objects.for_club(club)
        .filter(
            student=student,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .select_related("schedule", "trainer", "training_type")
        .order_by("-date", "-id")[:5],
        "recent_payments": payments,
        "operation_memberships": memberships,
    }


def get_student_operation_history(*, club, student_id, kind, page=1):
    if kind == "attendance":
        rows = (
            Checkin.objects.for_club(club)
            .filter(student_id=student_id)
            .select_related(
                "schedule",
                "training_type",
                "trainer",
                "cancelled_by",
            )
            .order_by("-date", "-id")
        )
    elif kind == "payments":
        rows = Payment.objects.for_club(club).filter(student_id=student_id).select_related("tariff").order_by("-id")
    else:
        rows = list(
            SubscriptionCorrection.objects.for_club(club)
            .filter(
                subscription__student_id=student_id,
            )
            .select_related("actor", "subscription__tariff", "component__training_type")
        )
        rows += list(
            StudentAttendanceCorrection.objects.for_club(club)
            .filter(
                checkin__student_id=student_id,
            )
            .select_related("actor", "checkin__schedule")
        )
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    result = Paginator(rows, 25).get_page(page)
    if kind == "payments":
        for payment in result:
            payment.recognition_on = payment_recognition_date(payment=payment, club=club)
    return result


def get_student_attendance_options(*, club, student, day):
    from apps.attendance.models import Schedule, ScheduleEnrollment
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.attendance.services.student_corrections import _roster_evidence
    from apps.common.exceptions import BusinessLogicError

    group_ids = TrainingGroupMembership.objects.for_club(club).filter(student=student).values("training_group_id")
    ids = ScheduleEnrollment.objects.for_club(club).filter(student=student).values("schedule_id")
    schedules = {
        row.id: row
        for row in Schedule.objects.for_club(club)
        .filter(
            Q(id__in=ids) | Q(training_group_id__in=group_ids),
        )
        .select_related("club")
    }
    result = []
    for occurrence in get_schedule_occurrences_for_date(club=club, target_date=day):
        schedule = schedules.get(occurrence.schedule_id)
        if schedule is None:
            continue
        try:
            _roster_evidence(schedule=schedule, student=student, day=day)
        except BusinessLogicError:
            continue
        result.append(occurrence)
    components = (
        SubscriptionComponent.objects.for_club(club)
        .filter(
            subscription__student=student,
            subscription__deleted_at__isnull=True,
            subscription__status__in=["active", "expired"],
            is_active=True,
        )
        .select_related("subscription__tariff", "training_type")
        .order_by("subscription_id", "id")
    )
    return result, components
