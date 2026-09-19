import io
import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student, StudentProvenanceBackfillReceipt
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
def test_audit_student_provenance_is_tenant_scoped_and_pii_free():
    club = ClubFactory()
    other_club = ClubFactory()
    backfilled = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
    Student.objects.for_club(club).filter(id=backfilled.id).update(became_student_at=timezone.now())
    StudentProvenanceBackfillReceipt.objects.create(
        club=club,
        student=backfilled,
        evidence_type=StudentProvenanceBackfillReceipt.EvidenceType.CONFIRMED_PAYMENT,
        evidence_id=123,
        became_student_at=timezone.now(),
    )
    expected_null_lead = StudentFactory(club=club, status=Student.Status.LEAD)
    StudentFactory(club=club, status=Student.Status.TRIAL)
    expected_null_lost = StudentFactory(club=club, status=Student.Status.LOST, lead_status=None)
    LeadLifecycleEvent.objects.create(
        club=club,
        student=expected_null_lost,
        event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
        metadata={"student_status_from": Student.Status.LEAD},
    )
    StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
    StudentFactory(club=other_club, status=Student.Status.LEAD)

    output = io.StringIO()
    call_command("audit_student_provenance", "--club-id", str(club.id), stdout=output)
    report = json.loads(output.getvalue())

    assert report == {
        "audited_club_count": 1,
        "backfilled_count": 1,
        "evidence_counts": {"confirmed_payment": 1},
        "live_student_count": 5,
        "unresolved_legacy_count": 1,
        "ambiguous_legacy_count": 1,
        "expected_null_legacy_count": 3,
    }
    assert expected_null_lead.phone not in output.getvalue()


@pytest.mark.django_db
def test_audit_student_provenance_fails_closed_only_for_ambiguous_legacy_rows(club):
    StudentFactory(club=club, status=Student.Status.LEAD)
    StudentFactory(club=club, status=Student.Status.TRIAL)
    expected_null_lost = StudentFactory(club=club, status=Student.Status.LOST, lead_status=None)
    LeadLifecycleEvent.objects.create(
        club=club,
        student=expected_null_lost,
        event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
        metadata={"student_status_from": Student.Status.TRIAL},
    )

    call_command(
        "audit_student_provenance",
        "--club-id",
        str(club.id),
        "--fail-on-unresolved",
    )

    StudentFactory(club=club, status=Student.Status.LOST, lead_status=None)

    with pytest.raises(CommandError, match="student_provenance_unresolved"):
        call_command(
            "audit_student_provenance",
            "--club-id",
            str(club.id),
            "--fail-on-unresolved",
        )

    StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)

    with pytest.raises(CommandError, match="student_provenance_unresolved"):
        call_command(
            "audit_student_provenance",
            "--club-id",
            str(club.id),
            "--fail-on-unresolved",
        )


@pytest.mark.django_db
def test_audit_student_provenance_all_clubs_aggregates_without_identity_data():
    first = ClubFactory()
    second = ClubFactory()
    StudentFactory(club=first, status=Student.Status.LEAD)
    StudentFactory(club=second, status=Student.Status.ACTIVE, lead_status=None)

    output = io.StringIO()
    call_command("audit_student_provenance", "--all-clubs", stdout=output)
    report = json.loads(output.getvalue())

    assert report["audited_club_count"] == 2
    assert report["unresolved_legacy_count"] == 1
    assert report["ambiguous_legacy_count"] == 1
    assert report["expected_null_legacy_count"] == 1


@pytest.mark.django_db
def test_provenance_receipts_are_append_only_through_unscoped_manager(club):
    student = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
    receipt = StudentProvenanceBackfillReceipt.objects.create(
        club=club,
        student=student,
        evidence_type=StudentProvenanceBackfillReceipt.EvidenceType.CONFIRMED_PAYMENT,
        evidence_id=123,
        became_student_at=timezone.now(),
    )

    with pytest.raises(ValidationError, match="append-only"):
        StudentProvenanceBackfillReceipt.objects.unscoped().filter(id=receipt.id).update(
            evidence_id=456
        )
    with pytest.raises(ValidationError, match="append-only"):
        StudentProvenanceBackfillReceipt.objects.unscoped().filter(id=receipt.id).delete()
