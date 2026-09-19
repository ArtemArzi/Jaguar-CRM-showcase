from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import models
from django.db.models import Count

from apps.clubs.models import Club
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student, StudentProvenanceBackfillReceipt


class Command(BaseCommand):
    help = "Report aggregate, PII-free Student provenance backfill evidence."

    def add_arguments(self, parser) -> None:
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--club-id", type=int)
        scope.add_argument("--all-clubs", action="store_true")
        parser.add_argument("--fail-on-unresolved", action="store_true")

    def handle(self, *args, **options) -> None:
        club_id = options.get("club_id")
        if club_id is not None and not Club.objects.filter(id=club_id).exists():
            raise CommandError("club_not_found")

        report = _provenance_report(club_id=club_id)
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_unresolved"] and report["unresolved_legacy_count"]:
            raise CommandError("student_provenance_unresolved")


def _provenance_report(*, club_id: int | None) -> dict:
    students = Student.objects.unscoped()
    receipts = StudentProvenanceBackfillReceipt.objects.unscoped()
    if club_id is not None:
        students = students.filter(club_id=club_id)
        receipts = receipts.filter(club_id=club_id)

    evidence_counts = dict(
        receipts.values("evidence_type")
        .annotate(count=Count("id"))
        .order_by("evidence_type")
        .values_list("evidence_type", "count")
    )
    scoped_students = students.filter(deleted_at__isnull=True)
    legacy_without_provenance = scoped_students.filter(
        crm_entry_kind=Student.CrmEntryKind.LEGACY_UNKNOWN,
        became_student_at__isnull=True,
    )
    expected_lost_ids = LeadLifecycleEvent.objects.unscoped().filter(
        club_id__in=legacy_without_provenance.values("club_id"),
        student_id__in=legacy_without_provenance.filter(status=Student.Status.LOST).values("id"),
        event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
        metadata__student_status_from__in=(Student.Status.LEAD, Student.Status.TRIAL),
    ).exclude(
        student_id__in=LeadLifecycleEvent.objects.unscoped().filter(
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        ).values("student_id")
    ).values("student_id")
    expected_null_legacy_count = legacy_without_provenance.filter(
        models.Q(status__in=(Student.Status.LEAD, Student.Status.TRIAL))
        | models.Q(status=Student.Status.LOST, id__in=expected_lost_ids)
    ).count()
    ambiguous_legacy_count = legacy_without_provenance.count() - expected_null_legacy_count
    return {
        "audited_club_count": 1 if club_id is not None else Club.objects.count(),
        "backfilled_count": receipts.count(),
        "evidence_counts": evidence_counts,
        "live_student_count": scoped_students.count(),
        # Keep this key as the fail-closed compatibility surface: only
        # active-like legacy rows are unresolved.  Lead/trial/never-converted
        # lost records are expected to retain a null provenance fact.
        "unresolved_legacy_count": ambiguous_legacy_count,
        "ambiguous_legacy_count": ambiguous_legacy_count,
        "expected_null_legacy_count": expected_null_legacy_count,
    }
