from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.billing.models import Expense, Payment, PaymentRefund, Subscription
from apps.billing.recognition import payment_recognition_q
from apps.clubs.timezones import club_local_date_range_bounds
from apps.students.models import Student
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment


def _payment_recognition_bounds(*, club, date_from: date, date_to: date):
    """UTC bounds for the ordinary-payment branch of recognition."""
    return club_local_date_range_bounds(club, date_from=date_from, date_to=date_to)


def _get_gross_confirmed_revenue(*, club, date_from: date, date_to: date) -> Decimal:
    dt_from, dt_to = _payment_recognition_bounds(club=club, date_from=date_from, date_to=date_to)
    return Payment.objects.for_club(club).filter(
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


def _get_refunded_revenue(*, club, date_from: date, date_to: date) -> Decimal:
    return PaymentRefund.objects.for_club(club).filter(
        accounting_date__gte=date_from,
        accounting_date__lte=date_to,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")


def _get_confirmed_revenue(*, club, date_from: date, date_to: date) -> Decimal:
    return _get_gross_confirmed_revenue(
        club=club,
        date_from=date_from,
        date_to=date_to,
    ) - _get_refunded_revenue(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )


def _trainer_earning_period_filter(*, club, date_from: date, date_to: date) -> Q:
    dt_from, dt_to = _payment_recognition_bounds(club=club, date_from=date_from, date_to=date_to)
    return Q(
        checkin__date__gte=date_from,
        checkin__date__lte=date_to,
    ) | Q(
        payment_recognition_q(
            date_from=date_from,
            date_to=date_to,
            verified_from=dt_from,
            verified_to=dt_to,
            prefix="payment__",
        ),
        checkin__isnull=True,
    )


def get_salary_total(*, club, date_from: date, date_to: date) -> Decimal:
    earning_salary_total = (
        TrainerEarning.objects.for_club(club)
        .filter(cancelled=False)
        .filter(_trainer_earning_period_filter(club=club, date_from=date_from, date_to=date_to))
        .aggregate(total=Sum("amount"))["total"] or Decimal("0")
    )
    adjustment_salary_total = (
        TrainerEarningAdjustment.objects.for_club(club)
        .filter(
            affects_payroll=True,
            effective_date__gte=date_from,
            effective_date__lte=date_to,
        )
        .aggregate(total=Sum("payable_amount_delta"))["total"] or Decimal("0")
    )
    return earning_salary_total + adjustment_salary_total


def get_pnl_report(*, club, date_from: date, date_to: date) -> dict:
    """P&L report: income - salary - expenses = margin."""
    gross_income = _get_gross_confirmed_revenue(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    refunded_income = _get_refunded_revenue(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    income = gross_income - refunded_income

    salary_total = get_salary_total(club=club, date_from=date_from, date_to=date_to)

    manual_expenses = _get_expenses_for_period(club=club, date_from=date_from, date_to=date_to)

    margin = income - salary_total - manual_expenses
    margin_percent = round(margin / income * 100) if income > 0 else 0

    income_breakdown = _get_income_breakdown(club=club, date_from=date_from, date_to=date_to)
    salary_breakdown = _get_salary_breakdown(club=club, date_from=date_from, date_to=date_to)
    expenses_breakdown = _get_expenses_breakdown(club=club, date_from=date_from, date_to=date_to)

    return {
        "gross_income": gross_income,
        "refunded_income": refunded_income,
        "income": income,
        "income_breakdown": income_breakdown,
        "salary_expenses": salary_total,
        "salary_breakdown": salary_breakdown,
        "manual_expenses": manual_expenses,
        "total_expenses": salary_total + manual_expenses,
        "expenses_breakdown": expenses_breakdown,
        "margin": margin,
        "margin_percent": margin_percent,
    }


def get_business_metrics(*, club, date_from: date, date_to: date) -> dict:
    """ARPM, churn rate, retention rate, LTV."""
    income = _get_confirmed_revenue(club=club, date_from=date_from, date_to=date_to)

    arpm = _calculate_arpm(club=club, income=income)
    churn_rate, churn_base, churn_count = _calculate_churn_rate(
        club=club, date_from=date_from, date_to=date_to,
    )
    retention_rate = Decimal("1") - churn_rate if churn_rate is not None else None
    ltv = (arpm / churn_rate) if (arpm and churn_rate) else None

    churn_pct = round(churn_rate * 100, 1) if churn_rate is not None else None
    retention_pct = round(retention_rate * 100, 1) if retention_rate is not None else None

    return {
        "arpm": arpm,
        "churn_rate": churn_pct,
        "churn_base": churn_base,
        "churn_count": churn_count,
        "retention_rate": retention_pct,
        "retention_base": churn_base,
        "retention_renewed": churn_base - churn_count,
        "ltv": ltv,
    }


def _get_income_breakdown(*, club, date_from: date, date_to: date) -> list[dict]:
    """Aggregate net confirmed revenue by payment method."""
    dt_from, dt_to = _payment_recognition_bounds(club=club, date_from=date_from, date_to=date_to)
    rows = (
        Payment.objects.for_club(club)
        .filter(
            payment_recognition_q(
                date_from=date_from,
                date_to=date_to,
                verified_from=dt_from,
                verified_to=dt_to,
                prefix="",
            ),
            status=Payment.Status.CONFIRMED,
            deleted_at__isnull=True,
        )
        .values("payment_method")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    totals = {row["payment_method"]: row["total"] or Decimal("0") for row in rows}
    refund_rows = (
        PaymentRefund.objects.for_club(club)
        .filter(
            accounting_date__gte=date_from,
            accounting_date__lte=date_to,
        )
        .values("payment__payment_method")
        .annotate(total=Sum("amount"))
    )
    for row in refund_rows:
        method = row["payment__payment_method"]
        totals[method] = totals.get(method, Decimal("0")) - (row["total"] or Decimal("0"))

    method_labels = dict(Payment.Method.choices)
    return [
        {"label": method_labels.get(method, method), "amount": amount}
        for method, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if amount != 0
    ]


def _get_salary_breakdown(*, club, date_from: date, date_to: date) -> list[dict]:
    """Aggregate trainer earnings by trainer, with salary + gross revenue.

    Revenue per earning = amount × 100 / rate_percent (inverse of the earning
    formula). Works uniformly for per-checkin and T1 sale earnings.
    """
    from decimal import Decimal

    from django.db.models import DecimalField, ExpressionWrapper, F

    revenue_expr = ExpressionWrapper(
        F("amount") * Decimal("100") / F("rate_percent"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    rows = list(
        TrainerEarning.objects.for_club(club)
        .filter(cancelled=False, rate_percent__gt=0)
        .filter(_trainer_earning_period_filter(club=club, date_from=date_from, date_to=date_to))
        .values("trainer_id", "trainer__first_name", "trainer__last_name")
        .annotate(
            total=Sum("amount"),
            revenue=Sum(revenue_expr),
            sessions=Count("id"),
        )
        .order_by("-revenue")
    )
    rows_by_trainer = {}
    for row in rows:
        rows_by_trainer[row["trainer_id"]] = {
            "trainer_id": row["trainer_id"],
            "trainer": f"{row['trainer__first_name']} {row['trainer__last_name']}".strip(),
            "amount": row["total"] or Decimal("0"),
            "revenue": row["revenue"] or Decimal("0"),
            "sessions": row["sessions"] or 0,
        }

    adjustment_rows = (
        TrainerEarningAdjustment.objects.for_club(club)
        .filter(
            affects_payroll=True,
            effective_date__gte=date_from,
            effective_date__lte=date_to,
        )
        .values("trainer_id", "trainer__first_name", "trainer__last_name")
        .annotate(total=Sum("payable_amount_delta"))
    )
    for row in adjustment_rows:
        item = rows_by_trainer.setdefault(
            row["trainer_id"],
            {
                "trainer_id": row["trainer_id"],
                "trainer": f"{row['trainer__first_name']} {row['trainer__last_name']}".strip(),
                "amount": Decimal("0"),
                "revenue": Decimal("0"),
                "sessions": 0,
            },
        )
        item["amount"] = item["amount"] + (row["total"] or Decimal("0"))

    return sorted(rows_by_trainer.values(), key=lambda item: item["revenue"], reverse=True)


def _get_expenses_breakdown(*, club, date_from: date, date_to: date) -> list[dict]:
    """List expenses for the period (one-time in range + active recurring)."""
    one_time = Expense.objects.for_club(club).filter(
        is_recurring=False,
        date__gte=date_from,
        date__lte=date_to,
        deleted_at__isnull=True,
    )
    recurring = Expense.objects.for_club(club).filter(
        is_recurring=True,
        date__lte=date_to,
        deleted_at__isnull=True,
    )
    months = _months_in_range(date_from, date_to)
    result: list[dict] = []
    for exp in one_time:
        result.append({"id": exp.id, "name": exp.name, "amount": exp.amount, "is_recurring": False, "date": exp.date})
    for exp in recurring:
        effective = exp.amount * months
        label = f"{exp.name} (×{months} мес.)" if months > 1 else exp.name
        result.append({"id": exp.id, "name": label, "amount": effective, "is_recurring": True, "date": exp.date})
    return result


def _get_expenses_for_period(*, club, date_from: date, date_to: date) -> Decimal:
    """One-time expenses in range + recurring * months spanned."""
    one_time = Expense.objects.for_club(club).filter(
        is_recurring=False,
        date__gte=date_from,
        date__lte=date_to,
        deleted_at__isnull=True,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    recurring_total = Expense.objects.for_club(club).filter(
        is_recurring=True,
        date__lte=date_to,
        deleted_at__isnull=True,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    months = _months_in_range(date_from, date_to)
    return one_time + recurring_total * months


def _months_in_range(date_from: date, date_to: date) -> int:
    months = (date_to.year - date_from.year) * 12 + (date_to.month - date_from.month) + 1
    return max(months, 1)


def _calculate_arpm(*, club, income: Decimal) -> Decimal | None:
    """ARPM = revenue / unique active students with subscriptions."""
    now = timezone.now()
    unique_students = (
        Subscription.objects.for_club(club)
        .filter(
            status=Subscription.Status.ACTIVE,
            expires_at__gt=now,
            deleted_at__isnull=True,
        )
        .values("student_id")
        .distinct()
        .count()
    )
    if unique_students == 0:
        return None
    return income / unique_students


def _calculate_churn_rate(*, club, date_from: date, date_to: date) -> tuple[Decimal | None, int, int]:
    """Churn = expired-not-renewed / active-at-start.

    Returns (rate_decimal_or_None, active_at_start, churned_count).
    """
    period_start, period_end = club_local_date_range_bounds(club, date_from=date_from, date_to=date_to)

    # Subscriptions that were active at the start of the period
    active_at_start = (
        Subscription.objects.for_club(club)
        .filter(
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.EXPIRED],
            created_at__lt=period_start,
            expires_at__gt=period_start,
            deleted_at__isnull=True,
        )
        .count()
    )

    if active_at_start == 0:
        return None, 0, 0

    # Expired during period without renewal
    expired_not_renewed = (
        Subscription.objects.for_club(club)
        .filter(
            status=Subscription.Status.EXPIRED,
            expires_at__gte=period_start,
            expires_at__lt=period_end,
            deleted_at__isnull=True,
        )
        .exclude(
            student_id__in=Subscription.objects.for_club(club)
            .filter(
                status=Subscription.Status.ACTIVE,
                created_at__gte=period_start,
                deleted_at__isnull=True,
            )
            .values("student_id")
        )
        .count()
    )

    rate = Decimal(str(expired_not_renewed)) / Decimal(str(active_at_start))
    return rate, active_at_start, expired_not_renewed


def get_attendance_metrics(*, club, date_from: date, date_to: date) -> dict:
    """Attendance stats for the period."""
    total_checkins = (
        Checkin.objects.for_club(club)
        .filter(date__gte=date_from, date__lte=date_to, deleted_at__isnull=True)
        .count()
    )
    days_in_period = max((date_to - date_from).days + 1, 1)
    avg_per_day = round(total_checkins / days_in_period, 1)
    return {
        "total_checkins": total_checkins,
        "avg_per_day": avg_per_day,
        "days_in_period": days_in_period,
    }


def get_trial_conversion(*, club, date_from: date, date_to: date) -> dict:
    """Trial-to-active conversion for the period."""
    dt_from, dt_to = club_local_date_range_bounds(club, date_from=date_from, date_to=date_to)

    new_trials = (
        Student.objects.for_club(club)
        .filter(
            created_at__gte=dt_from,
            created_at__lt=dt_to,
            deleted_at__isnull=True,
        )
        .count()
    )

    converted = (
        Student.objects.for_club(club)
        .filter(
            created_at__gte=dt_from,
            created_at__lt=dt_to,
            status=Student.Status.ACTIVE,
            deleted_at__isnull=True,
            subscriptions__created_at__gte=dt_from,
            subscriptions__created_at__lt=dt_to,
        )
        .distinct()
        .count()
    )

    conversion_rate = round(converted / new_trials * 100, 1) if new_trials > 0 else None
    return {
        "new_trials": new_trials,
        "converted": converted,
        "conversion_rate": conversion_rate,
    }


def get_dormant_count(*, club) -> dict:
    """Students not seen in 14+ days."""
    fourteen_days_ago = (timezone.now() - timedelta(days=14)).date()

    dormant = (
        Student.objects.for_club(club)
        .filter(
            status__in=[Student.Status.ACTIVE, Student.Status.AT_RISK],
            deleted_at__isnull=True,
        )
        .annotate(last_checkin=Max("checkins__date"))
        .filter(Q(last_checkin__lt=fourteen_days_ago) | Q(last_checkin__isnull=True))
        .count()
    )
    return {"count": dormant}
