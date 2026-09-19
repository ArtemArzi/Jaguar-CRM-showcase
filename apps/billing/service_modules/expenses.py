from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from apps.billing.models import Expense
from apps.billing.service_modules._shared import _apply_updates, _validate_positive_money

logger = logging.getLogger(__name__)

_UPDATE_EXPENSE_FIELDS = frozenset({"name", "amount", "date", "category", "is_recurring"})


def create_expense(
    *,
    club_id: int,
    name: str,
    amount: Decimal,
    date: date,
    category: str = "",
    is_recurring: bool = False,
) -> Expense:
    _validate_positive_money(amount)

    expense = Expense(
        club_id=club_id,
        name=name,
        amount=amount,
        date=date,
        category=category,
        is_recurring=is_recurring,
    )
    expense.full_clean()
    expense.save()
    logger.info("expense_created", extra={"id": expense.id, "club_id": club_id})
    return expense


def update_expense(*, expense_id: int, club_id: int, **fields) -> Expense:
    expense = Expense.objects.for_club(club_id).get(id=expense_id, deleted_at__isnull=True)
    if "amount" in fields:
        _validate_positive_money(fields["amount"])
    changed = _apply_updates(expense, fields, _UPDATE_EXPENSE_FIELDS)
    expense.full_clean()
    expense.save(update_fields=[*changed, "updated_at"])
    logger.info("expense_updated", extra={"id": expense_id, "club_id": club_id})
    return expense


def delete_expense(*, expense_id: int, club_id: int) -> None:
    expense = Expense.objects.for_club(club_id).get(id=expense_id, deleted_at__isnull=True)
    expense.soft_delete()
    logger.info("expense_deleted", extra={"id": expense_id, "club_id": club_id})
