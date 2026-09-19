"""Settlement reads reuse earned salary; actual payouts never become expenses."""

import hashlib
import json
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import (
    Trainer,
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerSettlementEntry,
    TrainerSettlementReconciliation,
)
from apps.trainers.selectors import get_trainer_earnings_summary


def get_trainer_settlement_summary(*, club, trainer_id: int, date_from: date, date_to: date) -> dict:
    if date_from > date_to:
        raise BusinessLogicError("Начало периода позже конца.", code="settlement_invalid_period")
    if not Trainer.objects.for_club(club).filter(id=trainer_id).exists():
        raise BusinessLogicError("Тренер недоступен.", code="target_not_available")
    earned = get_trainer_earnings_summary(club=club, trainer_id=trainer_id, date_from=date_from, date_to=date_to)
    entries = TrainerSettlementEntry.objects.for_club(club).filter(trainer_id=trainer_id)
    opening = entries.filter(kind="opening").first()
    period = entries.filter(effective_on__range=(date_from, date_to))
    paid = period.filter(kind="payout").aggregate(total=Sum("amount"))["total"] or Decimal("0")
    reversed_paid = period.filter(kind="payout_reversal").aggregate(total=Sum("amount"))["total"] or Decimal("0")
    unresolved = list(
        TrainerSettlementReconciliation.objects.for_club(club)
        .filter(
            trainer_id=trainer_id,
            resolution__isnull=True,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    result = {
        "earned": earned["total_amount"],
        "paid": paid,
        "reversed": reversed_paid,
        "net_paid": paid - reversed_paid,
        "balance": None,
        "status": "unconfirmed",
        "opening": opening,
        "unresolved_ids": unresolved,
        "date_from": date_from,
        "date_to": date_to,
    }
    if opening is not None and date_to >= opening.effective_on:
        after_opening_earned = get_trainer_earnings_summary(
            club=club,
            trainer_id=trainer_id,
            date_from=opening.effective_on,
            date_to=date_to,
        )["total_amount"]
        changes = entries.filter(effective_on__range=(opening.effective_on, date_to)).aggregate(
            total=Sum("balance_delta"),
        )["total"] or Decimal("0")
        result["status"] = "needs_reconciliation" if unresolved else "known"
        if not unresolved:
            result["balance"] = changes + after_opening_earned
    elif opening is not None:
        result["status"] = "before_opening"
    result["scope"] = get_settlement_financial_scope(club=club, trainer_id=trainer_id)
    result["fingerprint"] = hashlib.sha256(
        json.dumps(
            {
                "scope": result["scope"],
                "as_of": str(date_to),
                "balance": str(result["balance"]),
                "status": result["status"],
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return result


def trainer_period_presets(*, club):
    from datetime import timedelta

    from apps.clubs.timezones import club_localdate

    today = club_localdate(club)
    monday = today - timedelta(days=today.weekday())
    return [
        {"key": "week", "label": "Эта неделя", "start": monday, "end": monday + timedelta(days=6)},
        {
            "key": "last_week",
            "label": "Прошлая",
            "start": monday - timedelta(days=7),
            "end": monday - timedelta(days=1),
        },
        {
            "key": "two_weeks",
            "label": "2 недели",
            "start": monday - timedelta(days=7),
            "end": monday + timedelta(days=6),
        },
        {"key": "month", "label": "Месяц", "start": today.replace(day=1), "end": today},
    ]


def get_settlement_financial_scope(*, club, trainer_id):
    entries = TrainerSettlementEntry.objects.for_club(club).filter(trainer_id=trainer_id)
    data = {
        "opening_id": entries.filter(kind="opening").values_list("id", flat=True).first(),
        "entries": list(entries.order_by("id").values_list("id", flat=True)),
        "earnings": list(
            TrainerEarning.objects.for_club(club)
            .filter(trainer_id=trainer_id)
            .order_by("id")
            .values(
                "id",
                "amount",
                "cancelled",
                "updated_at",
                "checkin__cancelled_at",
                "payment_id",
                "payment__verified_at",
            )
        ),
        "adjustments": list(
            TrainerEarningAdjustment.objects.for_club(club)
            .filter(trainer_id=trainer_id)
            .order_by("id")
            .values(
                "id",
                "affects_payroll",
                "payable_amount_delta",
                "effective_date",
                "updated_at",
                "source_payment_id",
            )
        ),
        "cases": list(
            TrainerSettlementReconciliation.objects.for_club(club)
            .filter(trainer_id=trainer_id)
            .order_by("id")
            .values_list(
                "id",
                "resolution__id",
            )
        ),
    }
    return json.loads(json.dumps(data, default=str))
