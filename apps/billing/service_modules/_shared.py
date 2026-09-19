from __future__ import annotations

from decimal import Decimal

from apps.common.exceptions import BusinessLogicError


def _apply_updates(instance, fields: dict, allowed: frozenset[str]) -> list[str]:
    """Validate and apply partial field updates."""
    bad = set(fields) - allowed
    if bad:
        raise BusinessLogicError("Некорректные данные запроса", code="invalid_fields")
    for attr, value in fields.items():
        setattr(instance, attr, value)
    return list(fields.keys())


def _validate_positive_money(value: Decimal) -> None:
    if value <= Decimal("0"):
        raise BusinessLogicError(
            "Сумма должна быть больше нуля",
            code="invalid_money_amount",
        )


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))
