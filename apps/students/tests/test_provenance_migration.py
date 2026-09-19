import importlib
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
)
from apps.billing.models import Payment, Subscription
from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory
from apps.clubs.tests.factories import ClubFactory
from apps.students.models import Student, StudentProvenanceBackfillReceipt
from apps.students.tests.factories import StudentFactory


@pytest.fixture
def migration_executor_with_restore():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    yield executor
    MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_0010_migration_executor_defaults_legacy_students_without_status_inference(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("students", "0009_student_guardian_phone")]
    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    legacy_club_model = before_apps.get_model("clubs", "Club")
    legacy_student_model = before_apps.get_model("students", "Student")
    club = legacy_club_model.objects.create(name="Provenance migration club", city="Moscow", disciplines=[])
    student = legacy_student_model.objects.create(
        club_id=club.id,
        first_name="Legacy",
        last_name="Active",
        phone="+79009990001",
        status="active",
        lead_status=None,
    )

    executor = MigrationExecutor(connection)
    after = [("students", "0010_student_provenance_and_intake_commands")]
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    migrated_student_model = after_apps.get_model("students", "Student")

    migrated = migrated_student_model.objects.get(id=student.id)
    assert migrated.crm_entry_kind == "legacy_unknown"
    assert migrated.crm_entered_by_id is None
    assert migrated.became_student_at is None


@pytest.mark.django_db
def test_0010_backfill_uses_all_evidence_and_records_earliest_safe_provenance():
    migration = importlib.import_module("apps.students.migrations.0010_student_provenance_and_intake_commands")
    club = ClubFactory()
    evidence_student = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=None,
    )
    tariff = TariffFactory(club=club)
    now = timezone.now().replace(microsecond=0)
    first_payment = PaymentFactory(
        club=club,
        student=evidence_student,
        tariff=tariff,
        status=Payment.Status.CONFIRMED,
    )
    later_verified_payment = PaymentFactory(
        club=club,
        student=evidence_student,
        tariff=tariff,
        status=Payment.Status.CONFIRMED,
    )
    subscription = SubscriptionFactory(
        club=club,
        student=evidence_student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
    )
    group = TrainingGroupFactory(club=club)
    membership = TrainingGroupMembershipFactory(
        club=club,
        student=evidence_student,
        training_group=group,
    )
    first_at = now - timedelta(days=8)
    second_at = now - timedelta(days=9)
    subscription_at = now - timedelta(days=6)
    membership_at = now - timedelta(days=5)
    Payment.objects.filter(id=first_payment.id).update(created_at=first_at, verified_at=None)
    Payment.objects.filter(id=later_verified_payment.id).update(
        created_at=now - timedelta(days=4),
        verified_at=second_at,
    )
    Subscription.objects.filter(id=subscription.id).update(activated_at=subscription_at)
    type(membership).objects.filter(id=membership.id).update(created_at=membership_at)

    migration.backfill_became_student_at(django_apps, None)

    evidence_student.refresh_from_db()
    receipt = StudentProvenanceBackfillReceipt.objects.get(student=evidence_student)
    assert evidence_student.became_student_at == second_at
    assert receipt.evidence_type == StudentProvenanceBackfillReceipt.EvidenceType.CONFIRMED_PAYMENT
    assert receipt.evidence_id == later_verified_payment.id
    assert receipt.became_student_at == second_at


@pytest.mark.django_db
def test_0010_backfill_leaves_pending_rejected_trial_and_unreliable_membership_without_evidence():
    migration = importlib.import_module("apps.students.migrations.0010_student_provenance_and_intake_commands")
    club = ClubFactory()
    pending_or_rejected = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=None,
    )
    tariff = TariffFactory(club=club)
    PaymentFactory(club=club, student=pending_or_rejected, tariff=tariff, status=Payment.Status.PENDING)
    PaymentFactory(club=club, student=pending_or_rejected, tariff=tariff, status=Payment.Status.REJECTED)

    trial_only = StudentFactory(
        club=club,
        status=Student.Status.TRIAL,
        lead_status=None,
        became_student_at=None,
    )
    schedule = ScheduleFactory(club=club)
    ScheduleEnrollment.objects.create(
        club=club,
        student=trial_only,
        schedule=schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=timezone.localdate(),
        ends_on=timezone.localdate(),
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    CheckinFactory(
        club=club,
        student=trial_only,
        schedule=schedule,
        training_type=schedule.training_type,
        trainer=schedule.trainer,
        location=schedule.location,
        date=timezone.localdate(),
    )

    cancelled_or_ambiguous_membership = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=None,
    )
    group = TrainingGroupFactory(club=club)
    TrainingGroupMembershipFactory(
        club=club,
        student=cancelled_or_ambiguous_membership,
        training_group=group,
        status="cancelled",
    )
    second_group = TrainingGroupFactory(club=club)
    TrainingGroupMembershipFactory(
        club=club,
        student=cancelled_or_ambiguous_membership,
        training_group=second_group,
        status="active",
        source="migration",
    )

    paid_conversion_without_confirmation = []
    for payment_status in (Payment.Status.PENDING, Payment.Status.REJECTED):
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            lead_status=None,
            became_student_at=None,
        )
        paid_conversion_group = TrainingGroupFactory(club=club)
        paid_conversion_membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=paid_conversion_group,
            status="active",
            source="paid_conversion",
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=payment_status,
            conversion_group_membership=paid_conversion_membership,
            target_group_membership=paid_conversion_membership,
        )
        paid_conversion_without_confirmation.append(student)

    migration.backfill_became_student_at(django_apps, None)

    for student in (
        pending_or_rejected,
        trial_only,
        cancelled_or_ambiguous_membership,
        *paid_conversion_without_confirmation,
    ):
        student.refresh_from_db()
        assert student.became_student_at is None
        assert not StudentProvenanceBackfillReceipt.objects.filter(student=student).exists()
