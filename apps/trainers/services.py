import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.billing.models import TrainingType
from apps.billing.recognition import payment_recognition_date, payment_recognition_q
from apps.clubs.models import Club, Location
from apps.clubs.timezones import club_local_date_range_bounds, club_localdate_by_id
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import (
    Trainer,
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerLocation,
    TrainerPackageAllocation,
    TrainerPayrollPeriodClose,
    TrainerRate,
)

logger = logging.getLogger(__name__)


_KIND_DEFAULTS: dict[str, Decimal] = {
    TrainingType.Kind.GROUP: Decimal("20.00"),
    TrainingType.Kind.PERSONAL: Decimal("50.00"),
    TrainingType.Kind.MINI_GROUP: Decimal("40.00"),
}
_FALLBACK_DEFAULT = Decimal("25.00")


@dataclass(frozen=True)
class TrainerEarningCorrectionResult:
    debit: TrainerEarningAdjustment
    credit: TrainerEarningAdjustment
    created: bool


def _money(value: Decimal | None) -> Decimal:
    return Decimal(str(value or Decimal("0.00"))).quantize(Decimal("0.01"))


def _local_datetime_range(*, club_id: int, period_start, period_end):
    club = Club.objects.only("id", "timezone").get(id=club_id)
    return club_local_date_range_bounds(club, date_from=period_start, date_to=period_end)


def _payroll_close_message(close: TrainerPayrollPeriodClose) -> str:
    return (
        "Период выплат закрыт: "
        f"{close.period_start.isoformat()} — {close.period_end.isoformat()}. "
        "Новые изменения выплат за этот период недоступны."
    )


def get_trainer_payroll_close_for_date(
    *,
    club_id: int,
    target_date,
) -> TrainerPayrollPeriodClose | None:
    return (
        TrainerPayrollPeriodClose.objects.for_club(club_id)
        .filter(period_start__lte=target_date, period_end__gte=target_date)
        .order_by("-closed_at", "-id")
        .first()
    )


def get_trainer_payroll_closes_for_range(
    *,
    club_id: int,
    period_start,
    period_end,
):
    return (
        TrainerPayrollPeriodClose.objects.for_club(club_id)
        .filter(period_start__lte=period_end, period_end__gte=period_start)
        .select_related("closed_by")
        .order_by("period_start", "period_end", "id")
    )


def assert_trainer_payroll_date_open(
    *,
    club_id: int,
    target_date,
) -> None:
    close = get_trainer_payroll_close_for_date(club_id=club_id, target_date=target_date)
    if close is not None:
        raise BusinessLogicError(_payroll_close_message(close), code="payroll_period_closed")


def lock_trainer_payroll_mutation_scope(*, club_id: int) -> Club:
    """Serialize payroll-changing mutations with period close snapshots."""
    return Club.objects.select_for_update(of=("self",)).only("id").get(id=club_id)


def lock_and_assert_trainer_payroll_date_open(
    *,
    club_id: int,
    target_date,
) -> None:
    lock_trainer_payroll_mutation_scope(club_id=club_id)
    assert_trainer_payroll_date_open(club_id=club_id, target_date=target_date)


def _first_open_payroll_date_at_or_after(*, club_id: int, target_date):
    effective_date = target_date
    while True:
        close = get_trainer_payroll_close_for_date(club_id=club_id, target_date=effective_date)
        if close is None:
            return effective_date
        effective_date = close.period_end + timedelta(days=1)


def create_late_drop_in_settlement_credit(
    *,
    club_id: int,
    checkin_id: int,
    payment_id: int,
    confirmed_at,
) -> TrainerEarningAdjustment:
    """Post a closed-period drop-in earning as an immutable current-period credit."""
    from apps.attendance.models import CheckinCascadeEvent

    checkin = (
        CheckinCascadeEvent.objects.for_club(club_id)
        .select_related("checkin")
        .filter(
            checkin_id=checkin_id,
            effect=CheckinCascadeEvent.Effect.SALARY,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if checkin is None:
        raise BusinessLogicError("Drop-in salary snapshot is missing", code="salary_snapshot_missing")
    payload = checkin.payload or {}
    rate = _money(Decimal(str(payload.get("rate_percent_snapshot") or "0")))
    basis = _money(Decimal(str(payload.get("subscription_price_snapshot") or "0")))
    trainer_id = payload.get("trainer_id_snapshot")
    if not trainer_id or rate <= 0 or basis <= 0:
        raise BusinessLogicError("Drop-in salary snapshot is incomplete", code="salary_snapshot_missing")
    effective_date = _first_open_payroll_date_at_or_after(
        club_id=club_id,
        target_date=club_localdate_by_id(club_id, confirmed_at),
    )
    amount = _money(basis * rate / Decimal("100"))
    adjustment, created = TrainerEarningAdjustment.objects.for_club(club_id).get_or_create(
        source_checkin_id=checkin_id,
        source_payment_id=payment_id,
        trainer_id=trainer_id,
        kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
        direction=TrainerEarningAdjustment.Direction.CREDIT,
        defaults={
            "club_id": club_id,
            "amount_basis_snapshot": basis,
            "payable_amount_delta": amount,
            "affects_payroll": True,
            "effective_date": effective_date,
            "reason": "late_personal_drop_in_settlement",
        },
    )
    if created:
        from apps.trainers.settlement_services import note_adjustment_created

        note_adjustment_created(adjustment=adjustment)
    return adjustment


def create_closed_period_drop_in_cancellation_debits(
    *,
    club_id: int,
    checkin_id: int,
    cancelled_at,
    cancelled_by_id: int | None,
) -> list[TrainerEarningAdjustment]:
    """Compensate closed-period drop-in payroll without mutating its history."""
    effective_date = _first_open_payroll_date_at_or_after(
        club_id=club_id,
        target_date=club_localdate_by_id(club_id, cancelled_at),
    )
    debits: list[TrainerEarningAdjustment] = []
    earnings = list(
        TrainerEarning.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            checkin_id=checkin_id,
            earning_source=TrainerEarning.Source.CHECKIN,
            cancelled=False,
        )
        .order_by("id")
    )
    for earning in earnings:
        debit, created = TrainerEarningAdjustment.objects.for_club(club_id).get_or_create(
            source_checkin_id=checkin_id,
            source_earning_id=earning.id,
            trainer_id=earning.trainer_id,
            kind=TrainerEarningAdjustment.Kind.CHECKIN_CANCELLATION_DEBIT,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            defaults={
                "club_id": club_id,
                "amount_basis_snapshot": earning.subscription_price or earning.amount,
                "payable_amount_delta": -_money(earning.amount),
                "affects_payroll": True,
                "effective_date": effective_date,
                "reason": "closed_period_personal_drop_in_cancellation",
                "created_by_id": cancelled_by_id,
            },
        )
        if created:
            from apps.trainers.settlement_services import note_adjustment_created

            note_adjustment_created(adjustment=debit)
        debits.append(debit)

    late_credits = list(
        TrainerEarningAdjustment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            source_checkin_id=checkin_id,
            kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            affects_payroll=True,
        )
        .order_by("id")
    )
    for credit in late_credits:
        debit, created = TrainerEarningAdjustment.objects.for_club(club_id).get_or_create(
            source_checkin_id=checkin_id,
            reversal_of_id=credit.id,
            trainer_id=credit.trainer_id,
            kind=TrainerEarningAdjustment.Kind.CHECKIN_CANCELLATION_DEBIT,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            defaults={
                "club_id": club_id,
                "source_payment_id": credit.source_payment_id,
                "amount_basis_snapshot": credit.amount_basis_snapshot,
                "payable_amount_delta": -_money(credit.payable_amount_delta),
                "affects_payroll": True,
                "effective_date": effective_date,
                "reason": "closed_period_late_drop_in_cancellation",
                "created_by_id": cancelled_by_id,
            },
        )
        if created:
            from apps.trainers.settlement_services import note_adjustment_created

            note_adjustment_created(adjustment=debit)
        debits.append(debit)
    return debits


def _assert_no_pending_payroll_for_close(
    *,
    club_id: int,
    period_start,
    period_end,
) -> None:
    from apps.attendance.models import CheckinCascadeEvent
    from apps.billing.models import Payment, SubscriptionComponent, Tariff

    period_start_dt, period_end_dt = _local_datetime_range(
        club_id=club_id,
        period_start=period_start,
        period_end=period_end,
    )
    pending_checkin_salary = (
        CheckinCascadeEvent.objects.for_club(club_id)
        .filter(
            effect=CheckinCascadeEvent.Effect.SALARY,
            expected=True,
            checkin__date__gte=period_start,
            checkin__date__lte=period_end,
            checkin__cancelled_at__isnull=True,
            checkin__deleted_at__isnull=True,
            checkin__trainerearning__isnull=True,
        )
        .exclude(
            checkin__trainer_earning_adjustments__kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
            checkin__trainer_earning_adjustments__direction=TrainerEarningAdjustment.Direction.CREDIT,
        )
        .exists()
    )
    if pending_checkin_salary:
        raise BusinessLogicError(
            "Trainer payroll close has pending check-in salary work",
            code="payroll_close_pending_salary",
        )

    pending_sale_salary = (
        Payment.objects.for_club(club_id)
        .filter(
            payment_recognition_q(
                date_from=period_start,
                date_to=period_end,
                verified_from=period_start_dt,
                verified_to=period_end_dt,
                prefix="",
            ),
            status=Payment.Status.CONFIRMED,
            sale_earning_snapshot_recorded=True,
            sale_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_trainer_id_snapshot__isnull=False,
            sale_rate_percent_snapshot__isnull=False,
            sale_amount_basis_snapshot__isnull=False,
            earnings__isnull=True,
        )
        .exists()
    )
    if pending_sale_salary:
        raise BusinessLogicError(
            "Trainer payroll close has pending sale salary work",
            code="payroll_close_pending_salary",
        )

    pending_component_sale_salary = (
        SubscriptionComponent.objects.for_club(club_id)
        .filter(
            payment_recognition_q(
                date_from=period_start,
                date_to=period_end,
                verified_from=period_start_dt,
                verified_to=period_end_dt,
                prefix="subscription__payment__",
            ),
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
            subscription__payment__status=Payment.Status.CONFIRMED,
            sale_trainer_id_snapshot__isnull=False,
            sale_rate_percent_snapshot__isnull=False,
            trainer_earnings__isnull=True,
        )
        .exists()
    )
    if pending_component_sale_salary:
        raise BusinessLogicError(
            "Trainer payroll close has pending sale salary work",
            code="payroll_close_pending_salary",
        )


def _build_payroll_close_snapshot(
    *,
    club_id: int,
    period_start,
    period_end,
) -> tuple[Decimal, dict[str, dict[str, str | int]]]:
    from apps.trainers.selectors import get_retained_payroll_earnings

    period_start_dt, period_end_dt = _local_datetime_range(
        club_id=club_id,
        period_start=period_start,
        period_end=period_end,
    )
    totals_by_trainer: dict[int, dict[str, Decimal | int]] = {}
    earnings = (
        get_retained_payroll_earnings(club=club_id)
        .filter(
            Q(
                checkin__date__gte=period_start,
                checkin__date__lte=period_end,
            )
            | Q(
                payment_recognition_q(
                    date_from=period_start,
                    date_to=period_end,
                    verified_from=period_start_dt,
                    verified_to=period_end_dt,
                    prefix="payment__",
                ),
                checkin__isnull=True,
            )
        )
        .values("trainer_id")
        .annotate(total=Sum("amount"), sessions=Count("id"))
    )
    for row in earnings:
        totals_by_trainer[row["trainer_id"]] = {
            "earning_total": _money(row["total"]),
            "adjustment_total": Decimal("0.00"),
            "sessions": row["sessions"] or 0,
        }

    adjustments = (
        TrainerEarningAdjustment.objects.for_club(club_id)
        .filter(
            affects_payroll=True,
            effective_date__gte=period_start,
            effective_date__lte=period_end,
        )
        .values("trainer_id")
        .annotate(total=Sum("payable_amount_delta"))
    )
    for row in adjustments:
        item = totals_by_trainer.setdefault(
            row["trainer_id"],
            {
                "earning_total": Decimal("0.00"),
                "adjustment_total": Decimal("0.00"),
                "sessions": 0,
            },
        )
        item["adjustment_total"] = _money(row["total"])

    salary_total = Decimal("0.00")
    snapshot: dict[str, dict[str, str | int]] = {}
    for trainer_id, item in totals_by_trainer.items():
        total = _money(item["earning_total"]) + _money(item["adjustment_total"])
        salary_total += total
        snapshot[str(trainer_id)] = {
            "earning_total": str(_money(item["earning_total"])),
            "adjustment_total": str(_money(item["adjustment_total"])),
            "total": str(total),
            "sessions": int(item["sessions"]),
        }
    return _money(salary_total), snapshot


def close_trainer_payroll_period(
    *,
    club_id: int,
    period_start,
    period_end,
    reason: str,
    actor_user_id: int | None,
) -> TrainerPayrollPeriodClose:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise BusinessLogicError("Payroll close reason is required", code="payroll_close_reason_required")
    if actor_user_id is None:
        raise BusinessLogicError("Payroll close actor is required", code="payroll_close_actor_required")
    if period_end < period_start:
        raise BusinessLogicError("Payroll close period is invalid", code="payroll_close_period_invalid")

    with transaction.atomic():
        club = Club.objects.select_for_update().get(id=club_id)
        overlap = (
            TrainerPayrollPeriodClose.objects.for_club(club_id)
            .select_for_update()
            .filter(period_start__lte=period_end, period_end__gte=period_start)
            .first()
        )
        if overlap is not None:
            raise BusinessLogicError(
                "Trainer payroll period overlaps an existing close",
                code="trainer_payroll_period_overlap",
            )
        _assert_no_pending_payroll_for_close(
            club_id=club_id,
            period_start=period_start,
            period_end=period_end,
        )
        salary_total, trainer_totals = _build_payroll_close_snapshot(
            club_id=club_id,
            period_start=period_start,
            period_end=period_end,
        )
        close = TrainerPayrollPeriodClose(
            club=club,
            period_start=period_start,
            period_end=period_end,
            closed_by_id=actor_user_id,
            reason=normalized_reason,
            salary_total_snapshot=salary_total,
            trainer_totals_snapshot=trainer_totals,
        )
        try:
            close.full_clean()
        except ValidationError as exc:
            raise BusinessLogicError(
                "Payroll close is invalid",
                code="payroll_close_invalid",
            ) from exc
        close.save()

    logger.info(
        "trainer_payroll_period_closed",
        extra={
            "club_id": club_id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "closed_by_id": actor_user_id,
        },
    )
    return close


def _refetch_package_transfer_adjustment(
    *,
    club_id: int,
    checkin_id: int,
    trainer_id: int,
) -> TrainerEarningAdjustment | None:
    return (
        TrainerEarningAdjustment.objects.for_club(club_id)
        .filter(
            source_checkin_id=checkin_id,
            trainer_id=trainer_id,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
            direction=TrainerEarningAdjustment.Direction.INFO,
        )
        .first()
    )


def _refetch_package_transfer_reversal(
    *,
    club_id: int,
    transfer: TrainerEarningAdjustment,
) -> TrainerEarningAdjustment | None:
    return (
        TrainerEarningAdjustment.objects.for_club(club_id)
        .filter(
            reversal_of=transfer,
            kind=TrainerEarningAdjustment.Kind.REVERSAL,
        )
        .first()
    )


def _manual_correction_rows_for_key(
    *,
    club_id: int,
    idempotency_key: str,
) -> TrainerEarningCorrectionResult | None:
    if not idempotency_key:
        return None
    rows = list(
        TrainerEarningAdjustment.objects.for_club(club_id)
        .filter(
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            idempotency_key=idempotency_key,
        )
        .select_related("trainer", "counterparty_trainer", "source_checkin", "source_payment", "created_by")
        .order_by("id")
    )
    if not rows:
        return None
    debit = next((row for row in rows if row.direction == TrainerEarningAdjustment.Direction.DEBIT), None)
    credit = next((row for row in rows if row.direction == TrainerEarningAdjustment.Direction.CREDIT), None)
    if debit is None or credit is None:
        raise BusinessLogicError(
            "Manual correction is incomplete",
            code="manual_correction_incomplete",
        )
    return TrainerEarningCorrectionResult(debit=debit, credit=credit, created=False)


def _manual_correction_debit_for_earning(
    *,
    club_id: int,
    earning: TrainerEarning,
) -> TrainerEarningAdjustment | None:
    qs = TrainerEarningAdjustment.objects.for_club(club_id).filter(
        kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
        direction=TrainerEarningAdjustment.Direction.DEBIT,
        trainer_id=earning.trainer_id,
    )
    linked = qs.filter(source_earning_id=earning.id).first()
    if linked is not None:
        return linked
    qs = qs.filter(source_earning__isnull=True)
    if earning.checkin_id:
        qs = qs.filter(source_checkin_id=earning.checkin_id)
    elif earning.payment_id:
        qs = qs.filter(source_payment_id=earning.payment_id)
    else:
        return None
    return qs.select_related("counterparty_trainer", "created_by").first()


def _validate_existing_manual_correction(
    *,
    result: TrainerEarningCorrectionResult,
    earning: TrainerEarning,
    target_trainer_id: int,
    amount: Decimal,
) -> None:
    if result.debit.trainer_id != earning.trainer_id or result.credit.trainer_id != target_trainer_id:
        raise BusinessLogicError(
            "Idempotency key already belongs to another correction",
            code="manual_correction_idempotency_conflict",
        )
    if result.debit.source_checkin_id != earning.checkin_id or result.credit.source_checkin_id != earning.checkin_id:
        raise BusinessLogicError(
            "Idempotency key already belongs to another correction",
            code="manual_correction_idempotency_conflict",
        )
    if result.debit.source_payment_id != earning.payment_id or result.credit.source_payment_id != earning.payment_id:
        raise BusinessLogicError(
            "Idempotency key already belongs to another correction",
            code="manual_correction_idempotency_conflict",
        )
    if result.debit.source_earning_id not in {None, earning.id} or result.credit.source_earning_id not in {
        None,
        earning.id,
    }:
        raise BusinessLogicError(
            "Idempotency key already belongs to another correction",
            code="manual_correction_idempotency_conflict",
        )
    if result.debit.payable_amount_delta != -amount or result.credit.payable_amount_delta != amount:
        raise BusinessLogicError(
            "Idempotency key already belongs to another correction",
            code="manual_correction_idempotency_conflict",
        )


def update_trainer_rates(
    *,
    club_id: int,
    trainer_id: int,
    rates: list[dict],
) -> int:
    """Upsert TrainerRate rows for a trainer.

    rates: list of {"location_id", "training_type_id", "percent"}.
    Validates FKs via .for_club(). Returns number of rows touched.
    Raises BusinessLogicError on cross-tenant FK.
    """
    if not rates:
        return 0

    # Bounds check on percent (0..100). Done before any DB I/O so a bad
    # payload never mutates state. Rejects NaN/Inf implicitly via
    # Decimal comparison.
    normalized: list[dict] = []
    for r in rates:
        try:
            pct = Decimal(str(r["percent"]))
        except Exception:
            raise BusinessLogicError(
                "Некорректная ставка",
                code="rate_invalid",
            )
        if pct.is_nan() or pct < Decimal("0") or pct > Decimal("100"):
            raise BusinessLogicError(
                "Ставка должна быть 0..100%",
                code="rate_out_of_range",
            )
        normalized.append(
            {
                "location_id": r["location_id"],
                "training_type_id": r["training_type_id"],
                "percent": pct,
            }
        )

    location_ids = {r["location_id"] for r in normalized}
    type_ids = {r["training_type_id"] for r in normalized}

    count = 0
    with transaction.atomic():
        # Validate trainer belongs to club (inside atomic to close race
        # windows between check and write).
        if not Trainer.objects.for_club(club_id).filter(id=trainer_id).exists():
            raise BusinessLogicError(
                "Trainer not found in club",
                code="trainer_not_found",
            )
        valid_locs = set(Location.objects.filter(id__in=location_ids, club_id=club_id).values_list("id", flat=True))
        if valid_locs != location_ids:
            raise BusinessLogicError(
                "Location does not belong to this club",
                code="location_club_mismatch",
            )
        valid_types = set(TrainingType.objects.for_club(club_id).filter(id__in=type_ids).values_list("id", flat=True))
        if valid_types != type_ids:
            raise BusinessLogicError(
                "TrainingType does not belong to this club",
                code="training_type_club_mismatch",
            )

        for r in normalized:
            TrainerRate.objects.update_or_create(
                club_id=club_id,
                trainer_id=trainer_id,
                location_id=r["location_id"],
                training_type_id=r["training_type_id"],
                defaults={"percent": r["percent"]},
            )
            count += 1
    logger.info(
        "trainer_rates_updated",
        extra={"trainer_id": trainer_id, "club_id": club_id, "count": count},
    )
    return count


def create_package_allocation_for_subscription(
    *,
    club_id: int,
    subscription_id: int,
    owner_trainer_id: int | None,
    payment_id: int | None = None,
    created_by_id: int | None = None,
    source: str = TrainerPackageAllocation.Source.PAYMENT,
) -> TrainerPackageAllocation | None:
    from apps.billing.models import Payment, Subscription, SubscriptionComponent, Tariff

    if owner_trainer_id is None:
        return None

    subscription = (
        Subscription.objects.for_club(club_id)
        .select_related("student", "tariff", "tariff__training_type")
        .get(id=subscription_id)
    )
    owner_component = (
        SubscriptionComponent.objects.for_club(club_id)
        .filter(
            subscription=subscription,
            training_type__kind__in=[TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP],
            is_active=True,
        )
        .filter(
            Q(trainer_payout_policy_snapshot__in=[Tariff.PayoutPolicy.ON_CHECKIN, Tariff.PayoutPolicy.ON_PAYMENT])
            | Q(
                subscription__payment__origin=Payment.Origin.OPENING,
                sale_snapshot_provenance=Payment.SaleSnapshotProvenance.OPENING_REVIEWED,
            )
        )
        .select_related("training_type")
        .order_by("id")
        .first()
    )
    training_type = owner_component.training_type if owner_component is not None else subscription.tariff.training_type
    if owner_component is None and training_type.kind == TrainingType.Kind.GROUP:
        return None

    owner = Trainer.objects.for_club(club_id).filter(id=owner_trainer_id, is_active=True).first()
    if owner is None:
        raise BusinessLogicError("Тренер-владелец пакета не найден", code="package_owner_trainer_not_found")

    payment = None
    if payment_id is not None:
        payment = Payment.objects.for_club(club_id).filter(id=payment_id, subscription_id=subscription_id).first()
        if payment is None:
            raise BusinessLogicError(
                "Оплата не связана с этим абонементом",
                code="package_payment_subscription_mismatch",
            )

    existing = (
        TrainerPackageAllocation.objects.for_club(club_id).filter(subscription=subscription, is_active=True).first()
    )
    if existing is not None:
        return existing

    allocation = TrainerPackageAllocation(
        club_id=club_id,
        subscription=subscription,
        payment=payment,
        student=subscription.student,
        tariff=subscription.tariff,
        training_type=training_type,
        owner_trainer=owner,
        source=source,
        sessions_total_snapshot=(
            owner_component.credits_total if owner_component is not None else subscription.tariff.trainings_limit
        ),
        sessions_remaining_snapshot=(
            owner_component.credits_left if owner_component is not None else subscription.trainings_left
        ),
        amount_snapshot=(
            owner_component.paid_amount_basis_snapshot
            if owner_component is not None
            else subscription.paid_amount or subscription.tariff.price
        ),
        activated_at=timezone.now(),
        created_by_id=created_by_id,
    )
    allocation.full_clean()
    allocation.save()
    return allocation


def record_checkin_package_transfer(
    *,
    checkin_id: int,
    club_id: int,
    actor_id: int | None = None,
) -> TrainerEarningAdjustment | None:
    from apps.attendance.models import Checkin

    checkin = (
        Checkin.objects.for_club(club_id)
        .select_related("subscription", "trainer", "training_type")
        .filter(id=checkin_id, deleted_at__isnull=True, cancelled_at__isnull=True)
        .first()
    )
    if checkin is None or checkin.subscription_id is None:
        return None
    if checkin.training_type.kind == TrainingType.Kind.GROUP:
        return None

    earning = (
        TrainerEarning.objects.for_club(club_id)
        .filter(
            checkin_id=checkin_id,
            earning_source=TrainerEarning.Source.CHECKIN,
            cancelled=False,
        )
        .first()
    )
    if earning is None:
        return None

    actual_trainer_id = earning.trainer_id
    allocation = (
        TrainerPackageAllocation.objects.for_club(club_id)
        .filter(subscription_id=checkin.subscription_id, is_active=True)
        .select_related("owner_trainer")
        .first()
    )
    if allocation is None or allocation.owner_trainer_id == actual_trainer_id:
        return None

    existing = _refetch_package_transfer_adjustment(
        club_id=club_id,
        checkin_id=checkin_id,
        trainer_id=actual_trainer_id,
    )
    if existing is not None:
        return existing

    payment_id = allocation.payment_id
    adjustment = TrainerEarningAdjustment(
        club_id=club_id,
        trainer_id=actual_trainer_id,
        amount_basis_snapshot=earning.amount,
        payable_amount_delta=Decimal("0.00"),
        affects_payroll=False,
        direction=TrainerEarningAdjustment.Direction.INFO,
        kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
        effective_date=checkin.date,
        source_checkin_id=checkin.id,
        source_subscription_id=checkin.subscription_id,
        source_payment_id=payment_id,
        counterparty_trainer_id=allocation.owner_trainer_id,
        reason="package_owner_differs_from_actual_trainer",
        created_by_id=actor_id,
    )
    adjustment.full_clean()
    try:
        with transaction.atomic():
            adjustment.save()
    except IntegrityError:
        existing = _refetch_package_transfer_adjustment(
            club_id=club_id,
            checkin_id=checkin_id,
            trainer_id=actual_trainer_id,
        )
        if existing is not None:
            return existing
        raise
    return adjustment


def reverse_checkin_package_transfer(
    *,
    checkin_id: int,
    club_id: int,
    actor_id: int | None = None,
) -> TrainerEarningAdjustment | None:
    transfer = (
        TrainerEarningAdjustment.objects.for_club(club_id)
        .filter(
            source_checkin_id=checkin_id,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
            direction=TrainerEarningAdjustment.Direction.INFO,
        )
        .select_related("source_checkin")
        .first()
    )
    if transfer is None:
        return None

    existing = _refetch_package_transfer_reversal(club_id=club_id, transfer=transfer)
    if existing is not None:
        return existing

    adjustment = TrainerEarningAdjustment(
        club_id=club_id,
        trainer_id=transfer.trainer_id,
        amount_basis_snapshot=transfer.amount_basis_snapshot,
        payable_amount_delta=Decimal("0.00"),
        affects_payroll=False,
        direction=TrainerEarningAdjustment.Direction.INFO,
        kind=TrainerEarningAdjustment.Kind.REVERSAL,
        effective_date=transfer.effective_date,
        source_checkin_id=transfer.source_checkin_id,
        source_subscription_id=transfer.source_subscription_id,
        source_payment_id=transfer.source_payment_id,
        counterparty_trainer_id=transfer.counterparty_trainer_id,
        reason="checkin_cancelled",
        created_by_id=actor_id,
        reversal_of=transfer,
    )
    adjustment.full_clean()
    try:
        with transaction.atomic():
            adjustment.save()
    except IntegrityError:
        existing = _refetch_package_transfer_reversal(club_id=club_id, transfer=transfer)
        if existing is not None:
            return existing
        raise
    return adjustment


def reverse_checkin_manual_corrections(
    *,
    checkin_id: int,
    club_id: int,
) -> int:
    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        adjustments = TrainerEarningAdjustment.objects.for_club(club_id).filter(
            source_checkin_id=checkin_id,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            affects_payroll=True,
        )
        first_adjustment = adjustments.select_for_update().select_related("source_checkin").order_by("id").first()
        if first_adjustment is None:
            return 0
        target_date = (
            first_adjustment.source_checkin.date
            if first_adjustment.source_checkin_id
            else first_adjustment.effective_date
        )
        assert_trainer_payroll_date_open(club_id=club_id, target_date=target_date)
        from apps.trainers.settlement_services import record_historical_settlement_change

        for adjustment in adjustments.order_by("trainer_id", "id"):
            record_historical_settlement_change(
                club_id=club_id, trainer_id=adjustment.trainer_id, effective_on=adjustment.effective_date,
                event_key=f"adjustment:{adjustment.id}:cancelled", suggested_delta=-adjustment.payable_amount_delta,
                evidence={"adjustment_id": adjustment.id, "checkin_id": checkin_id},
            )
        updated = adjustments.update(affects_payroll=False)

    if updated:
        logger.info(
            "trainer_manual_corrections_reversed",
            extra={"checkin_id": checkin_id, "club_id": club_id, "adjustment_count": updated},
        )
    return updated


def correct_trainer_earning(
    *,
    club_id: int,
    earning_id: int,
    target_trainer_id: int,
    reason: str,
    actor_user_id: int | None,
    amount: Decimal | None = None,
    idempotency_key: str | None = None,
) -> TrainerEarningCorrectionResult:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise BusinessLogicError(
            "Correction reason is required",
            code="correction_reason_required",
        )
    if actor_user_id is None:
        raise BusinessLogicError(
            "Correction actor is required",
            code="correction_actor_required",
        )

    normalized_key = (idempotency_key or "").strip() or str(uuid.uuid4())
    if len(normalized_key) > TrainerEarningAdjustment._meta.get_field("idempotency_key").max_length:
        raise BusinessLogicError(
            "Manual correction idempotency key is invalid",
            code="manual_correction_idempotency_invalid",
        )

    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        earning = (
            TrainerEarning.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("trainer", "checkin", "checkin__subscription", "payment", "payment__subscription")
            .filter(id=earning_id, cancelled=False)
            .first()
        )
        if earning is None:
            raise BusinessLogicError(
                "Trainer earning not found",
                code="trainer_earning_not_found",
            )

        correction_amount = amount if amount is not None else earning.amount
        correction_amount = Decimal(str(correction_amount)).quantize(Decimal("0.01"))
        if correction_amount <= Decimal("0.00") or correction_amount > earning.amount:
            raise BusinessLogicError(
                "Correction amount is invalid",
                code="correction_amount_invalid",
            )

        existing = _manual_correction_rows_for_key(
            club_id=club_id,
            idempotency_key=normalized_key,
        )
        if existing is not None:
            _validate_existing_manual_correction(
                result=existing,
                earning=earning,
                target_trainer_id=target_trainer_id,
                amount=correction_amount,
            )
            return existing

        if (
            TrainerEarningAdjustment.objects.for_club(club_id)
            .filter(
                source_earning_id=earning.id,
                kind=TrainerEarningAdjustment.Kind.REFUND,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Refund-adjusted earning cannot be reassigned",
                code="trainer_earning_refund_adjusted",
            )

        effective_date = club_localdate_by_id(club_id)
        if earning.checkin_id:
            effective_date = earning.checkin.date
        elif earning.payment_id and earning.payment.verified_at:
            effective_date = payment_recognition_date(payment=earning.payment)
        assert_trainer_payroll_date_open(club_id=club_id, target_date=effective_date)

        already_corrected = _manual_correction_debit_for_earning(
            club_id=club_id,
            earning=earning,
        )
        if already_corrected is not None:
            raise BusinessLogicError(
                "Trainer earning already has a manual correction",
                code="trainer_earning_already_corrected",
            )

        target_trainer = Trainer.objects.for_club(club_id).filter(id=target_trainer_id, is_active=True).first()
        if target_trainer is None:
            raise BusinessLogicError(
                "Target trainer not found",
                code="target_trainer_not_found",
            )
        if target_trainer.id == earning.trainer_id:
            raise BusinessLogicError(
                "Target trainer is the same as source trainer",
                code="target_trainer_same_as_source",
            )

        source_subscription_id = None
        if earning.checkin_id:
            source_subscription_id = earning.checkin.subscription_id
        elif earning.payment_id:
            source_subscription_id = earning.payment.subscription_id

        correction_group_id = uuid.uuid4()
        common = {
            "club_id": club_id,
            "amount_basis_snapshot": earning.amount,
            "affects_payroll": True,
            "kind": TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            "effective_date": effective_date,
            "source_checkin_id": earning.checkin_id,
            "source_subscription_id": source_subscription_id,
            "source_payment_id": earning.payment_id,
            "source_earning_id": earning.id,
            "reason": normalized_reason,
            "created_by_id": actor_user_id,
            "correction_group_id": correction_group_id,
            "idempotency_key": normalized_key,
        }
        debit = TrainerEarningAdjustment(
            **common,
            trainer_id=earning.trainer_id,
            counterparty_trainer_id=target_trainer.id,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            payable_amount_delta=-correction_amount,
        )
        credit = TrainerEarningAdjustment(
            **common,
            trainer_id=target_trainer.id,
            counterparty_trainer_id=earning.trainer_id,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            payable_amount_delta=correction_amount,
        )
        try:
            debit.full_clean()
            credit.full_clean()
        except ValidationError as exc:
            raise BusinessLogicError(
                "Manual correction is invalid",
                code="manual_correction_invalid",
            ) from exc
        try:
            with transaction.atomic():
                debit.save()
                credit.save()
                from apps.trainers.settlement_services import _lock_trainers, note_adjustment_created

                _lock_trainers(club_id=club_id, trainer_ids=[debit.trainer_id, credit.trainer_id])
                note_adjustment_created(adjustment=debit)
                note_adjustment_created(adjustment=credit)
        except IntegrityError:
            existing = _manual_correction_rows_for_key(
                club_id=club_id,
                idempotency_key=normalized_key,
            )
            if existing is not None:
                _validate_existing_manual_correction(
                    result=existing,
                    earning=earning,
                    target_trainer_id=target_trainer_id,
                    amount=correction_amount,
                )
                return existing
            already_corrected = _manual_correction_debit_for_earning(
                club_id=club_id,
                earning=earning,
            )
            if already_corrected is not None:
                raise BusinessLogicError(
                    "Trainer earning already has a manual correction",
                    code="trainer_earning_already_corrected",
                )
            raise

    logger.info(
        "trainer_earning_corrected",
        extra={
            "club_id": club_id,
            "earning_id": earning_id,
            "source_trainer_id": debit.trainer_id,
            "target_trainer_id": credit.trainer_id,
            "correction_group_id": str(correction_group_id),
        },
    )
    return TrainerEarningCorrectionResult(debit=debit, credit=credit, created=True)


def create_trainer(
    *,
    club_id: int,
    first_name: str,
    last_name: str,
    phone: str = "",
    user_id: int | None = None,
    locations: list[dict] | None = None,
) -> Trainer:
    with transaction.atomic():
        trainer = Trainer.objects.create(
            club_id=club_id,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            user_id=user_id,
        )

        if locations:
            _validate_locations_belong_to_club(club_id=club_id, locations=locations)
            _create_trainer_locations(trainer=trainer, club_id=club_id, locations=locations)

    logger.info("trainer_created", extra={"trainer_id": trainer.id, "club_id": club_id})
    return trainer


_UPDATE_TRAINER_FIELDS = frozenset({"first_name", "last_name", "phone", "user_id", "is_active"})


def update_trainer(*, trainer_id: int, club_id: int, **fields) -> Trainer:
    bad = set(fields) - _UPDATE_TRAINER_FIELDS
    if bad:
        raise BusinessLogicError(f"Fields not allowed: {bad}", code="invalid_fields")

    with transaction.atomic():
        if fields.get("is_active") is False:
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            lock_training_group_mutation_scope(club_id=club_id)
        trainer = Trainer.objects.for_club(club_id).select_for_update(of=("self",)).get(id=trainer_id)
        if fields.get("is_active") is False and trainer.is_active:
            from apps.attendance.models import TrainingGroup

            active_group = (
                TrainingGroup.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(
                    responsible_trainer_id=trainer.id,
                    status=TrainingGroup.Status.ACTIVE,
                )
                .order_by("id")
                .first()
            )
            if active_group is not None:
                raise BusinessLogicError(
                    "Reassign or archive every active responsible training group before deactivation.",
                    code="trainer_responsible_training_group_active",
                )
        for field, value in fields.items():
            setattr(trainer, field, value)
        trainer.save(update_fields=[*fields.keys(), "updated_at"])
    return trainer


def update_trainer_locations(*, trainer_id: int, club_id: int, locations: list[dict]) -> list[TrainerLocation]:
    trainer = Trainer.objects.for_club(club_id).get(id=trainer_id)

    _validate_locations_belong_to_club(club_id=club_id, locations=locations)

    with transaction.atomic():
        # Cascade-delete TrainerRate rows for this trainer BEFORE deleting
        # TrainerLocation — otherwise rates orphan and would still be read
        # by calculate_salary via (trainer, training_type) fallback paths.
        TrainerRate.objects.for_club(club_id).filter(trainer=trainer).delete()
        TrainerLocation.objects.for_club(club_id).filter(trainer=trainer).delete()
        result = _create_trainer_locations(trainer=trainer, club_id=club_id, locations=locations)

    logger.info(
        "trainer_locations_updated",
        extra={"trainer_id": trainer_id, "club_id": club_id, "count": len(result)},
    )
    return result


def _validate_locations_belong_to_club(*, club_id: int, locations: list[dict]) -> None:
    location_ids = [loc["location_id"] for loc in locations]
    valid_count = Location.objects.filter(id__in=location_ids, club_id=club_id).count()
    if valid_count != len(location_ids):
        raise BusinessLogicError(
            "Location does not belong to this club",
            code="location_club_mismatch",
        )


def _create_trainer_locations(*, trainer: Trainer, club_id: int, locations: list[dict]) -> list[TrainerLocation]:
    """Create TrainerLocation rows + TrainerRate rows.

    Accepts two shapes per item (backwards compat for API callers):
      - legacy: {"location_id", "rate_group"?, "rate_personal"?, "rate_mini_group"?}
      - new:    {"location_id", "rates": [{"training_type_id", "percent"}, ...]}
    Legacy payloads are expanded to per-type rates on the fly by matching
    training_type.kind. The 3 legacy columns themselves no longer exist
    on TrainerLocation (Wave 4).
    """
    training_types = list(TrainingType.objects.for_club(club_id))

    created_tls: list[TrainerLocation] = []
    for loc in locations:
        # Resolve "new" shape rates list.
        rates_list = loc.get("rates")
        if rates_list is None:
            # Legacy shape → synthesize per-type rates from kind defaults.
            legacy_group = Decimal(str(loc.get("rate_group", 20.00)))
            legacy_personal = Decimal(str(loc.get("rate_personal", 50.00)))
            legacy_mini = Decimal(str(loc.get("rate_mini_group", 40.00)))
            rates_list = []
            for tt in training_types:
                if tt.kind == TrainingType.Kind.GROUP:
                    pct = legacy_group
                elif tt.kind == TrainingType.Kind.PERSONAL:
                    pct = legacy_personal
                elif tt.kind == TrainingType.Kind.MINI_GROUP:
                    pct = legacy_mini
                else:
                    pct = _FALLBACK_DEFAULT
                rates_list.append({"training_type_id": tt.id, "percent": pct})

        tl = TrainerLocation.objects.create(
            club_id=club_id,
            trainer=trainer,
            location_id=loc["location_id"],
        )
        created_tls.append(tl)

        if rates_list:
            normalized = [
                {
                    "location_id": loc["location_id"],
                    "training_type_id": r["training_type_id"],
                    "percent": r["percent"],
                }
                for r in rates_list
            ]
            update_trainer_rates(
                club_id=club_id,
                trainer_id=trainer.id,
                rates=normalized,
            )
    return created_tls
