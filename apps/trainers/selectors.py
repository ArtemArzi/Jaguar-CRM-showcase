from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce

from apps.billing.recognition import payment_recognition_date, payment_recognition_q
from apps.clubs.timezones import club_local_date_range_bounds
from apps.trainers.models import Trainer, TrainerEarning, TrainerEarningAdjustment, TrainerRate

_MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
_ZERO_MONEY = Value(Decimal("0.00"), output_field=_MONEY_FIELD)


@dataclass(frozen=True)
class TrainerSalaryLedgerRow:
    id: int
    row_type: str
    earning_type: str
    amount: Decimal
    rate_percent: Decimal
    subscription_price: Decimal | None
    checkin_date: str
    schedule_name: str
    package_owner_trainer_id: int | None = None
    package_owner_trainer_name: str = ""
    package_transfer_amount_basis: Decimal | None = None
    package_transfer_payable_delta: Decimal | None = None
    package_transfer_affects_payroll: bool | None = None
    package_transfer_reason: str = ""
    adjustment_direction: str = ""
    adjustment_reason: str = ""
    adjustment_effective_date: str = ""
    adjustment_affects_payroll: bool | None = None
    source_checkin_id: int | None = None
    source_checkin_date: str = ""
    source_schedule_name: str = ""
    source_payment_id: int | None = None
    sort_date: date | None = None


def _local_datetime_range(*, club, date_from: date, date_to: date):
    return club_local_date_range_bounds(club, date_from=date_from, date_to=date_to)


def _trainer_adjustment_total_subquery(*, club, date_from: date, date_to: date) -> Subquery:
    return Subquery(
        TrainerEarningAdjustment.objects.for_club(club)
        .filter(
            trainer_id=OuterRef("pk"),
            affects_payroll=True,
            effective_date__gte=date_from,
            effective_date__lte=date_to,
        )
        .values("trainer_id")
        .annotate(total=Sum("payable_amount_delta"))
        .values("total"),
        output_field=_MONEY_FIELD,
    )


def get_trainer_for_user(*, club, user) -> Trainer:
    """Resolve user's Trainer record. Raises Trainer.DoesNotExist if not found."""
    return Trainer.objects.for_club(club).get(user=user, is_active=True)


def resolve_trainer_rate(
    *,
    club_id: int,
    trainer_id: int,
    location_id: int | None,
    training_type_id: int,
    fallback_any_location: bool = False,
) -> Decimal | None:
    """Look up the rate% for (trainer, location, training_type).

    Returns None if no rate is set. Caller decides whether to fail loud
    (calculate_salary) or soft-skip (create_sale_earning).

    fallback_any_location=True: if no rate exists for the exact location,
    return a rate for the same (trainer, training_type) at any location.
    Preserves the current create_sale_earning behaviour where the seller
    may not be tied to the tariff's location.
    """
    qs = TrainerRate.objects.for_club(club_id).filter(
        trainer_id=trainer_id,
        training_type_id=training_type_id,
    )
    if location_id is not None:
        exact = qs.filter(location_id=location_id).first()
        if exact is not None:
            return exact.percent
    if fallback_any_location:
        any_loc = qs.first()
        if any_loc is not None:
            return any_loc.percent
    return None


def get_trainers(*, club) -> QuerySet[Trainer]:
    return Trainer.objects.for_club(club).filter(is_active=True).prefetch_related("trainer_locations__location")


def get_trainers_with_stats(*, club, date_from: date, date_to: date) -> QuerySet[Trainer]:
    payment_verified_start, payment_verified_end = _local_datetime_range(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    # An earning belongs to the period if EITHER its checkin date OR (for
    # T1 sale earnings — checkin is null) its payment recognition date
    # falls in the range.
    period_filter = Q(earnings__in=get_retained_payroll_earnings(club=club).values("pk")) & (
        Q(
            earnings__checkin__date__gte=date_from,
            earnings__checkin__date__lte=date_to,
        )
        | Q(
            payment_recognition_q(
                date_from=date_from,
                date_to=date_to,
                verified_from=payment_verified_start,
                verified_to=payment_verified_end,
                prefix="earnings__payment__",
            ),
            earnings__checkin__isnull=True,
        )
    )
    # Revenue: amount × 100 / rate_percent (inverse of earning formula).
    # Only rows with rate_percent > 0 (division-safe + excludes rate=0 mocks).
    revenue_filter = period_filter & Q(earnings__rate_percent__gt=0)
    revenue_expr = ExpressionWrapper(
        F("earnings__amount") * Decimal("100") / F("earnings__rate_percent"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    adjustment_total = _trainer_adjustment_total_subquery(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    earning_salary = Coalesce(
        Sum("earnings__amount", filter=period_filter),
        _ZERO_MONEY,
        output_field=_MONEY_FIELD,
    )
    return (
        Trainer.objects.for_club(club)
        .prefetch_related("trainer_locations__location")
        .annotate(
            monthly_sessions=Count("earnings", filter=period_filter),
            monthly_salary=ExpressionWrapper(
                earning_salary + Coalesce(adjustment_total, _ZERO_MONEY, output_field=_MONEY_FIELD),
                output_field=_MONEY_FIELD,
            ),
            monthly_revenue=Coalesce(
                Sum(revenue_expr, filter=revenue_filter),
                Decimal("0"),
            ),
        )
        .order_by("-is_active", "first_name")
    )


def get_trainer_by_id(*, club, trainer_id: int) -> Trainer:
    return Trainer.objects.for_club(club).prefetch_related("trainer_locations__location").get(id=trainer_id)


def get_club_sessions(
    *,
    club,
    date_from: date,
    date_to: date,
    trainer_id: int | None = None,
    schedule_id: int | None = None,
) -> list[dict]:
    """Flat list of actual training sessions (schedule × date × trainer),
    with attendee counts. Used by the 'Тренировки' sub-tab on /dashboard/trainers/.

    One row per unique (trainer_id, schedule_id, date). Filters by date
    range and optional trainer/schedule. Excludes soft-deleted checkins.
    """
    from apps.attendance.models import Checkin

    qs = (
        Checkin.objects.for_club(club)
        .filter(
            date__gte=date_from,
            date__lte=date_to,
            deleted_at__isnull=True,
        )
    )
    if trainer_id is not None:
        qs = qs.filter(trainer_id=trainer_id)
    if schedule_id is not None:
        qs = qs.filter(schedule_id=schedule_id)

    rows = (
        qs.values(
            "date",
            "trainer_id",
            "trainer__first_name",
            "trainer__last_name",
            "schedule_id",
            "schedule__group_name",
            "schedule__training_group__name",
            "schedule__start_time",
        )
        .annotate(attendees=Count("id"))
        .order_by("-date", "schedule__start_time", "trainer__first_name")
    )
    return [
        {
            "date": r["date"],
            "trainer_id": r["trainer_id"],
            "trainer_name": f"{r['trainer__first_name']} {r['trainer__last_name']}".strip(),
            "schedule_id": r["schedule_id"],
            "group_name": (
                r["schedule__training_group__name"]
                or r["schedule__group_name"]
            ),
            "start_time": r["schedule__start_time"],
            "attendees": r["attendees"],
        }
        for r in rows
    ]


def get_schedules_for_club(*, club) -> QuerySet:
    """Distinct schedule options for the sessions-tab filter dropdown."""
    from apps.attendance.models import Schedule

    return (
        Schedule.objects.for_club(club)
        .select_related("location", "training_group")
        .order_by("group_name", "start_time")
    )


# ──────────────────────────────────────────────
# Salary selectors
# ──────────────────────────────────────────────


def get_retained_payroll_earnings(*, club) -> QuerySet[TrainerEarning]:
    """Keep closed-period facts when an exact dated debit compensates cancellation."""
    return (
        TrainerEarning.objects.for_club(club).filter(cancelled=False)
        .alias(
            has_cancellation_debit=Exists(
                TrainerEarningAdjustment.objects.for_club(club).filter(
                    trainer_id=OuterRef("trainer_id"),
                    source_earning_id=OuterRef("pk"),
                    kind=TrainerEarningAdjustment.Kind.CHECKIN_CANCELLATION_DEBIT,
                    affects_payroll=True,
                )
            ),
        )
        .filter(
            Q(checkin__isnull=True)
            | Q(checkin__cancelled_at__isnull=True, checkin__deleted_at__isnull=True)
            | Q(has_cancellation_debit=True)
        )
    )


def get_trainer_earnings(
    *,
    club,
    trainer_id: int,
    date_from: date,
    date_to: date,
) -> QuerySet[TrainerEarning]:
    payment_verified_start, payment_verified_end = _local_datetime_range(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    # Include both per-checkin earnings (filter by checkin.date) and T1
    # sale earnings (checkin is null, filter by payment recognition date).
    return (
        get_retained_payroll_earnings(club=club)
        .filter(trainer_id=trainer_id)
        .filter(
            Q(
                checkin__date__gte=date_from,
                checkin__date__lte=date_to,
            )
            | Q(
                payment_recognition_q(
                    date_from=date_from,
                    date_to=date_to,
                    verified_from=payment_verified_start,
                    verified_to=payment_verified_end,
                    prefix="payment__",
                ),
                checkin__isnull=True,
            )
        )
        .select_related("checkin", "checkin__schedule", "payment")
        .prefetch_related(
            Prefetch(
                "checkin__trainer_earning_adjustments",
                queryset=TrainerEarningAdjustment.objects.for_club(club).filter(
                    trainer_id=trainer_id,
                    kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
                    direction=TrainerEarningAdjustment.Direction.INFO,
                ).select_related("counterparty_trainer"),
                to_attr="package_transfer_adjustments",
            ),
            Prefetch(
                "checkin__trainer_earning_adjustments",
                queryset=TrainerEarningAdjustment.objects.for_club(club)
                .select_related("trainer", "counterparty_trainer", "created_by")
                .order_by("id"),
                to_attr="salary_audit_adjustments",
            ),
            Prefetch(
                "payment__trainer_earning_adjustments",
                queryset=TrainerEarningAdjustment.objects.for_club(club)
                .select_related("trainer", "counterparty_trainer", "created_by")
                .order_by("id"),
                to_attr="salary_audit_adjustments",
            ),
        )
    )


def get_trainer_payroll_adjustments(
    *,
    club,
    trainer_id: int,
    date_from: date,
    date_to: date,
) -> QuerySet[TrainerEarningAdjustment]:
    return (
        TrainerEarningAdjustment.objects.for_club(club)
        .filter(
            trainer_id=trainer_id,
            kind__in=[
                TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
                TrainerEarningAdjustment.Kind.REFUND,
            ],
            effective_date__gte=date_from,
            effective_date__lte=date_to,
        )
        .select_related(
            "trainer",
            "counterparty_trainer",
            "created_by",
            "source_checkin",
            "source_checkin__schedule",
            "source_payment",
        )
        .order_by("-effective_date", "-id")
    )


def _package_transfer_adjustment_for_earning(earning: TrainerEarning) -> TrainerEarningAdjustment | None:
    if not earning.checkin_id:
        return None
    adjustments = getattr(earning.checkin, "package_transfer_adjustments", None)
    if adjustments is not None:
        return adjustments[0] if adjustments else None
    return (
        earning.checkin.trainer_earning_adjustments.filter(
            club_id=earning.club_id,
            trainer_id=earning.trainer_id,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
            direction=TrainerEarningAdjustment.Direction.INFO,
        )
        .select_related("counterparty_trainer")
        .first()
    )


def _earning_date(earning: TrainerEarning, *, club) -> date | None:
    if earning.checkin_id and earning.checkin:
        return earning.checkin.date
    if earning.payment_id and earning.payment and earning.payment.verified_at:
        return payment_recognition_date(payment=earning.payment, club=club)
    return None


def _earning_to_salary_ledger_row(earning: TrainerEarning, *, club) -> TrainerSalaryLedgerRow:
    row_date = _earning_date(earning, club=club)
    schedule_name = ""
    if earning.checkin_id and earning.checkin and earning.checkin.schedule:
        schedule_name = earning.checkin.schedule.group_name
    transfer = _package_transfer_adjustment_for_earning(earning)
    return TrainerSalaryLedgerRow(
        id=earning.id,
        row_type="earning",
        earning_type=earning.earning_type,
        amount=earning.amount,
        rate_percent=earning.rate_percent,
        subscription_price=earning.subscription_price,
        checkin_date=row_date.isoformat() if row_date else "",
        schedule_name=schedule_name,
        package_owner_trainer_id=transfer.counterparty_trainer_id if transfer else None,
        package_owner_trainer_name=str(transfer.counterparty_trainer)
        if transfer and transfer.counterparty_trainer
        else "",
        package_transfer_amount_basis=transfer.amount_basis_snapshot if transfer else None,
        package_transfer_payable_delta=transfer.payable_amount_delta if transfer else None,
        package_transfer_affects_payroll=transfer.affects_payroll if transfer else None,
        package_transfer_reason=transfer.reason if transfer else "",
        sort_date=row_date,
    )


def _adjustment_to_salary_ledger_row(adjustment: TrainerEarningAdjustment) -> TrainerSalaryLedgerRow:
    source_checkin_date = ""
    source_schedule_name = ""
    if adjustment.source_checkin_id and adjustment.source_checkin:
        source_checkin_date = adjustment.source_checkin.date.isoformat()
        if adjustment.source_checkin.schedule:
            source_schedule_name = adjustment.source_checkin.schedule.group_name
    return TrainerSalaryLedgerRow(
        id=adjustment.id,
        row_type="adjustment",
        earning_type=adjustment.kind,
        amount=adjustment.payable_amount_delta,
        rate_percent=Decimal("0.00"),
        subscription_price=None,
        checkin_date=adjustment.effective_date.isoformat(),
        schedule_name=source_schedule_name,
        adjustment_direction=adjustment.direction,
        adjustment_reason=adjustment.reason,
        adjustment_effective_date=adjustment.effective_date.isoformat(),
        adjustment_affects_payroll=adjustment.affects_payroll,
        source_checkin_id=adjustment.source_checkin_id,
        source_checkin_date=source_checkin_date,
        source_schedule_name=source_schedule_name,
        source_payment_id=adjustment.source_payment_id,
        sort_date=adjustment.effective_date,
    )


def get_trainer_salary_ledger_rows(
    *,
    club,
    trainer_id: int,
    date_from: date,
    date_to: date,
) -> list[TrainerSalaryLedgerRow]:
    earning_rows = [
        _earning_to_salary_ledger_row(earning, club=club)
        for earning in get_trainer_earnings(
            club=club,
            trainer_id=trainer_id,
            date_from=date_from,
            date_to=date_to,
        )
    ]
    adjustment_rows = [
        _adjustment_to_salary_ledger_row(adjustment)
        for adjustment in (
            TrainerEarningAdjustment.objects.for_club(club)
            .filter(
                trainer_id=trainer_id,
                affects_payroll=True,
                effective_date__gte=date_from,
                effective_date__lte=date_to,
            )
            .select_related(
                "trainer",
                "counterparty_trainer",
                "created_by",
                "source_checkin",
                "source_checkin__schedule",
                "source_payment",
            )
            .order_by("-effective_date", "-id")
        )
    ]
    return sorted(
        [*earning_rows, *adjustment_rows],
        key=lambda row: (
            row.sort_date or date.min,
            1 if row.row_type == "adjustment" else 0,
            row.id,
        ),
        reverse=True,
    )


def get_trainer_earnings_summary(
    *,
    club,
    trainer_id: int,
    date_from: date,
    date_to: date,
) -> dict:
    qs = get_trainer_earnings(club=club, trainer_id=trainer_id, date_from=date_from, date_to=date_to)
    agg = qs.aggregate(total_amount=Sum("amount"), total_sessions=Count("id"))
    by_type = list(qs.values("earning_type").annotate(count=Count("id"), total=Sum("amount")).order_by("earning_type"))
    adjustment_qs = (
        TrainerEarningAdjustment.objects.for_club(club)
        .filter(
            trainer_id=trainer_id,
            affects_payroll=True,
            effective_date__gte=date_from,
            effective_date__lte=date_to,
        )
    )
    adjustment_agg = adjustment_qs.aggregate(total=Sum("payable_amount_delta"), count=Count("id"))
    adjustment_total = adjustment_agg["total"] or Decimal("0")
    for row in adjustment_qs.values("kind").annotate(
        count=Count("id"),
        total=Sum("payable_amount_delta"),
    ):
        by_type.append({
            "earning_type": row["kind"],
            "count": row["count"],
            "total": row["total"],
        })
    return {
        "total_amount": (agg["total_amount"] or Decimal("0")) + adjustment_total,
        "total_sessions": agg["total_sessions"] or 0,
        "by_type": {item["earning_type"]: {"count": item["count"], "total": item["total"]} for item in by_type},
    }


def get_salary_summary(*, club, date_from: date, date_to: date) -> list[dict]:
    payment_verified_start, payment_verified_end = _local_datetime_range(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    rows_by_trainer: dict[int, dict] = {}
    earning_rows = list(
        get_retained_payroll_earnings(club=club)
        .filter(
            Q(
                checkin__date__gte=date_from,
                checkin__date__lte=date_to,
            )
            | Q(
                payment_recognition_q(
                    date_from=date_from,
                    date_to=date_to,
                    verified_from=payment_verified_start,
                    verified_to=payment_verified_end,
                    prefix="payment__",
                ),
                checkin__isnull=True,
            )
        )
        .values("trainer_id", "trainer__first_name", "trainer__last_name")
        .annotate(total=Sum("amount"), sessions=Count("id"))
        .order_by("-total")
    )
    for row in earning_rows:
        rows_by_trainer[row["trainer_id"]] = {
            "trainer_id": row["trainer_id"],
            "trainer__first_name": row["trainer__first_name"],
            "trainer__last_name": row["trainer__last_name"],
            "total": row["total"] or Decimal("0"),
            "sessions": row["sessions"],
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
                "trainer__first_name": row["trainer__first_name"],
                "trainer__last_name": row["trainer__last_name"],
                "total": Decimal("0"),
                "sessions": 0,
            },
        )
        item["total"] = (item["total"] or Decimal("0")) + (row["total"] or Decimal("0"))

    return sorted(
        rows_by_trainer.values(),
        key=lambda item: (item["total"], item["trainer__first_name"], item["trainer__last_name"]),
        reverse=True,
    )


def get_trainer_revenue_summary(
    *,
    club,
    trainer_id: int,
    date_from: date,
    date_to: date,
) -> dict:
    """Gross revenue the trainer generated for the club (any source).

    Formula: revenue_per_earning = amount × 100 / rate_percent.
    This works uniformly for:
      - personal/mini/group checkin earnings (trainer's cut × 100 / %)
      - T1 sale earnings on group sub (paid price recovered the same way)
    So the metric means 'total deal value passed through this trainer'
    across classes he taught AND group subs he sold.
    """
    qs = get_trainer_earnings(
        club=club, trainer_id=trainer_id,
        date_from=date_from, date_to=date_to,
    ).filter(rate_percent__gt=0)

    revenue_expr = ExpressionWrapper(
        F("amount") * Decimal("100") / F("rate_percent"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    agg = qs.annotate(revenue=revenue_expr).aggregate(
        total_revenue=Sum("revenue"),
        deal_count=Count("id"),
    )
    return {
        "total_revenue": agg["total_revenue"] or Decimal("0"),
        "sales_count": agg["deal_count"] or 0,
    }
