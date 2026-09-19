"""One date contract for revenue, sale earnings and payroll close guards."""

from datetime import date, datetime

from django.db.models import Q

from apps.billing.models import Payment
from apps.clubs.timezones import club_localdate


def payment_recognition_date(*, payment: Payment, club=None) -> date | None:
    if payment.origin == Payment.Origin.OPENING:
        return payment.opening_effective_on
    if payment.verified_at is None:
        return None
    return club_localdate(club or payment.club, payment.verified_at)


def payment_recognition_q(
    *,
    date_from: date,
    date_to: date,
    verified_from: datetime,
    verified_to: datetime,
    prefix: str = "",
) -> Q:
    """Inclusive local dates; ordinary timestamps use exclusive UTC upper bound.

    Prefix is a static ORM relation path supplied by an owning selector, never
    request input. Keeping the two branches explicit also works on SQLite.
    """
    return Q(**{
        f"{prefix}origin": Payment.Origin.ORDINARY,
        f"{prefix}verified_at__gte": verified_from,
        f"{prefix}verified_at__lt": verified_to,
    }) | Q(**{
        f"{prefix}origin": Payment.Origin.OPENING,
        f"{prefix}opening_effective_on__gte": date_from,
        f"{prefix}opening_effective_on__lte": date_to,
    })
