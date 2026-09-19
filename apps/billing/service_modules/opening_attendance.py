"""Admission protection for visits already included in an opening snapshot."""

from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from apps.billing.models import OpeningEntitlementSnapshot, Tariff
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError


def assert_opening_attendance_not_covered(*, club_id, student_id, schedule, checkin_date, subscription=None):
    # Callers hold the shared Club fence. Evidence is immutable, so this read
    # needs no later financial lock and includes exhausted/expired sources.
    snapshots = OpeningEntitlementSnapshot.objects.for_club(club_id).filter(
        subscription__student_id=student_id,
        component__training_type_id=schedule.training_type_id,
    ).filter(
        Q(component__scope=Tariff.Scope.CLUB)
        | Q(component__scope=Tariff.Scope.LOCATION, component__location_id=schedule.location_id)
    )
    if subscription is not None:
        source_filter = Q(subscription_id=subscription.id)
        if subscription.renewal_chain_id is not None:
            source_filter |= Q(subscription__renewal_chain_id=subscription.renewal_chain_id)
        snapshots = snapshots.filter(source_filter)
    else:
        snapshots = snapshots.filter(started_on__lte=checkin_date, expires_on__gte=checkin_date)
    if not snapshots.exists():
        return

    from apps.attendance.services.checkin import _get_schedule_occurrence

    occurrence = _get_schedule_occurrence(schedule=schedule, checkin_date=checkin_date)
    starts_at = timezone.make_aware(
        datetime.combine(occurrence.effective_date, occurrence.effective_start_time),
        club_zoneinfo(schedule.club),
    )
    if snapshots.filter(covered_through__gte=starts_at).exists():
        raise BusinessLogicError(
            "Это занятие уже учтено в перенесённом остатке. Требуется сверка исходных посещений.",
            code="opening_attendance_already_covered",
        )
    if snapshots.filter(operational_cutover__gt=starts_at).exists():
        raise BusinessLogicError(
            "Занятие раньше согласованного начала работы в CRM. Требуется сверка.",
            code="opening_cutover_needs_review",
        )
