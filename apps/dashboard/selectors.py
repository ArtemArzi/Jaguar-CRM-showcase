from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import Debt, Payment, PaymentRefund, Subscription
from apps.billing.recognition import payment_recognition_q
from apps.clubs.timezones import club_local_date_range_bounds
from apps.retention.models import RetentionTask
from apps.students.models import Student


def get_dashboard_metrics(*, club, date_from: date, date_to: date) -> dict:
    """Returns 6 metrics for My Day screen.

    Snapshot metrics (active/expiring/debtors) = always current.
    Period metrics (checkins/revenue/new_students) = filtered by date_from/date_to.
    """
    now = timezone.now()

    # Snapshot metrics (independent of date range)
    active_subs = (
        Subscription.objects.for_club(club)
        .filter(
            status=Subscription.Status.ACTIVE,
            expires_at__gt=now,
            deleted_at__isnull=True,
        )
        .count()
    )

    expiring_subs = (
        Subscription.objects.for_club(club)
        .filter(
            status=Subscription.Status.ACTIVE,
            expires_at__gt=now,
            expires_at__lte=now + timedelta(days=7),
            deleted_at__isnull=True,
        )
        .count()
    )

    debtors_count = (
        Debt.objects.for_club(club)
        .filter(resolved_at__isnull=True, settlement_payment__isnull=True)
        .values("student_id")
        .distinct()
        .count()
    )

    # Period metrics
    from apps.attendance.models import Checkin

    checkins_count = (
        Checkin.objects.for_club(club)
        .filter(
            date__gte=date_from,
            date__lte=date_to,
            deleted_at__isnull=True,
        )
        .count()
    )

    dt_from, dt_to = club_local_date_range_bounds(club, date_from=date_from, date_to=date_to)

    revenue = Payment.objects.for_club(club).filter(
        payment_recognition_q(
            date_from=date_from,
            date_to=date_to,
            verified_from=dt_from,
            verified_to=dt_to,
            prefix="",
        ),
        status=Payment.Status.CONFIRMED,
        deleted_at__isnull=True,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    refunded_revenue = PaymentRefund.objects.for_club(club).filter(
        accounting_date__gte=date_from,
        accounting_date__lte=date_to,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    revenue -= refunded_revenue

    new_students = (
        Student.objects.for_club(club)
        .filter(
            created_at__gte=dt_from,
            created_at__lt=dt_to,
            deleted_at__isnull=True,
        )
        .count()
    )

    return {
        "active_subscriptions": active_subs,
        "expiring_subscriptions": expiring_subs,
        "debtors": debtors_count,
        "checkins": checkins_count,
        "revenue": revenue,
        "new_students": new_students,
    }


def get_attention_alerts(*, club) -> list[dict]:
    """Returns list of alert dicts with type and count."""
    now = timezone.now()
    today = now.date()
    alerts = []

    # 1. Expiring subs (<=3 days)
    expiring = (
        Subscription.objects.for_club(club)
        .filter(
            status=Subscription.Status.ACTIVE,
            expires_at__gt=now,
            expires_at__lte=now + timedelta(days=3),
            deleted_at__isnull=True,
        )
        .count()
    )
    if expiring:
        alerts.append({"type": "expiring_subscriptions", "count": expiring})

    # 2. Unconfirmed payments (>12h)
    unconfirmed = (
        Payment.objects.for_club(club)
        .filter(
            status=Payment.Status.PENDING,
            created_at__lt=now - timedelta(hours=12),
            deleted_at__isnull=True,
        )
        .count()
    )
    if unconfirmed:
        alerts.append({"type": "unconfirmed_payments", "count": unconfirmed})

    # 3. At-risk students
    at_risk = (
        Student.objects.for_club(club)
        .filter(
            status=Student.Status.AT_RISK,
            deleted_at__isnull=True,
        )
        .count()
    )
    if at_risk:
        alerts.append({"type": "at_risk_students", "count": at_risk})

    # 4. Overdue retention tasks
    overdue = (
        RetentionTask.objects.for_club(club)
        .filter(
            resolved_at__isnull=True,
            due_date__lt=today,
        )
        .count()
    )
    if overdue:
        alerts.append({"type": "overdue_retention_tasks", "count": overdue})

    return alerts
