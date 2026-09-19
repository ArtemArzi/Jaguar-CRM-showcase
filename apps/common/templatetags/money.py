from decimal import Decimal

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def money(value: object) -> str:
    """Format a number as money: 15000.00 → '15 000', 287000 → '287 000'."""
    if value is None:
        return "0"
    try:
        num = Decimal(str(value))
    except Exception:
        return str(value)
    # Remove trailing .00
    if num == num.to_integral_value():
        num = int(num)
    else:
        return f"{num:,.2f}".replace(",", " ").replace(".", ",")
    # Format with space as thousands separator
    return f"{num:,}".replace(",", " ")


@register.filter
def days_ago(dt: object) -> int:
    """Return number of days since datetime."""
    if dt is None:
        return 0
    delta = timezone.now() - dt
    return delta.days
