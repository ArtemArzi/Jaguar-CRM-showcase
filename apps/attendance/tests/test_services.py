import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.selectors import (
    _compute_alerts_for_students,
    get_expected_student_ids_for_schedule_date,
    get_schedule_occurrences_for_date,
    get_schedule_occurrences_for_range,
    get_schedules,
    get_student_schedule,
    get_student_schedule_occurrences_for_range,
    get_students_for_schedule,
    get_today_sessions,
    get_unclosed_sessions,
)
from apps.attendance.services import (
    batch_checkin,
    block_personal_availability_slot,
    book_guest_group_visit,
    book_personal_availability_slot,
    book_personal_drop_in,
    book_personal_session,
    cancel_checkin,
    cancel_guest_booking,
    cancel_personal_availability_slot,
    cancel_personal_booking,
    cancel_schedule_enrollment,
    cancel_session,
    close_personal_booking_payment_reservation_for_order,
    close_session_from_existing_checkins,
    confirm_personal_booking_payment_reservation_for_order,
    create_checkin,
    create_personal_booking_payment_reservation,
    create_personal_drop_in_payment,
    create_schedule,
    create_training_group_membership,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    freeze_training_group_membership,
    generate_personal_availability_slots,
    get_personal_booking_payment_reservations,
    reschedule_session,
    substitute_trainer,
    transfer_schedule_enrollment,
    unblock_personal_availability_slot,
    unfreeze_schedule_enrollment,
    unfreeze_training_group_membership,
    update_schedule,
)
from apps.attendance.services.training_group_reconciliation import (
    TrainingGroupPreviewError,
    apply_training_group_reconciliation,
    audit_training_groups,
    build_training_group_reconciliation_preview,
)
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    PersonalAvailabilitySlotFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.attendance.training_group_roster import (
    assert_expected_roster_comparison,
    assert_expected_roster_horizon_comparison,
    build_training_group_roster_audit_horizon,
    resolve_expected_roster_for_schedule_date,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    Payment,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TrainingType,
)
from apps.billing.services import create_payment, verify_payment
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import Club, ClubSettings
from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


def _aware_datetime(target_date: date, slot_time: time):
    return timezone.make_aware(datetime.combine(target_date, slot_time))


def _future_date_for_weekday(weekday: int) -> date:
    start = timezone.localdate() + timedelta(days=7)
    return start + timedelta(days=(weekday - start.weekday()) % 7)


@pytest.mark.django_db
class TestTrainingGroupReconciliationPreview:
    def test_preview_is_explicit_stable_and_mutation_free(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            group_name="Same legacy name",
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            group_name="Same legacy name",
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        source_enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        before_counts = {
            "groups": TrainingGroup.objects.for_club(club).count(),
            "memberships": TrainingGroupMembership.objects.for_club(club).count(),
            "mapping_events": TrainingGroupMappingEvent.objects.for_club(club).count(),
            "enrollments": ScheduleEnrollment.objects.for_club(club).count(),
            "rollout_states": TrainingGroupRolloutState.objects.for_club(club).count(),
        }

        first_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[second_schedule.id, first_schedule.id],
            canonical_name="Owner chosen name",
        )
        retry_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Owner chosen name",
        )

        assert first_preview["digest"] == retry_preview["digest"]
        assert first_preview["selected_schedule_ids"] == [first_schedule.id, second_schedule.id]
        assert first_preview["canonical_group"]["responsible_trainer_id"] == trainer.id
        assert first_preview["proposed_memberships"] == [
            {
                "student_id": student.id,
                "source_enrollment_ids": [source_enrollment.id],
                "source_payment_ids": [],
                "source_kinds": [ScheduleEnrollment.CreatedFrom.MANUAL],
                "status": ScheduleEnrollment.Status.ACTIVE,
                "starts_on": "2026-07-01",
                "earliest_legacy_starts_on": "2026-07-01",
                "start_date_source": "legacy_exact",
                "authority": TrainingGroupMembership.Authority.INDEPENDENT,
                "projection_schedule_ids": [first_schedule.id, second_schedule.id],
                "conflicts": [],
            }
        ]
        assert first_preview["proposed_roster_deltas"] == [
            {
                "student_id": student.id,
                "schedule_id": first_schedule.id,
                "effective_starts_on": "2026-07-01",
                "earliest_legacy_starts_on": "2026-07-01",
                "action": "link_existing",
                "requires_explicit_starts_on": False,
            },
            {
                "student_id": student.id,
                "schedule_id": second_schedule.id,
                "effective_starts_on": "2026-07-01",
                "earliest_legacy_starts_on": "2026-07-01",
                "action": "add_compatibility_projection",
                "requires_explicit_starts_on": False,
            },
        ]
        assert {
            "groups": TrainingGroup.objects.for_club(club).count(),
            "memberships": TrainingGroupMembership.objects.for_club(club).count(),
            "mapping_events": TrainingGroupMappingEvent.objects.for_club(club).count(),
            "enrollments": ScheduleEnrollment.objects.for_club(club).count(),
            "rollout_states": TrainingGroupRolloutState.objects.for_club(club).count(),
        } == before_counts

    def test_preview_requires_explicit_trainer_for_mixed_slots(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        first_trainer = TrainerFactory(club=club)
        second_trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=first_trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=second_trainer,
            day_of_week=2,
        )

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Explicit selection",
        )

        assert preview["canonical_group"]["responsible_trainer_id"] is None
        assert {conflict["code"] for conflict in preview["conflicts"]} == {
            "responsible_trainer_required"
        }
        explicit_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Explicit selection",
            responsible_trainer_id=second_trainer.id,
        )
        assert explicit_preview["canonical_group"]["responsible_trainer_id"] == second_trainer.id
        assert not explicit_preview["conflicts"]

    def test_preview_rejects_foreign_or_mixed_scope_schedule_ids(self, club, other_club):
        local_schedule = ScheduleFactory(club=club)
        foreign_schedule = ScheduleFactory(club=other_club)

        with pytest.raises(TrainingGroupPreviewError) as exc_info:
            build_training_group_reconciliation_preview(
                club=club,
                schedule_ids=[local_schedule.id, foreign_schedule.id],
                canonical_name="No cross tenant mapping",
            )

        assert exc_info.value.code == "unknown_schedule_ids"

    def test_preview_keeps_independent_authority_but_blocks_multi_paid_sources(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        schedules = [
            ScheduleFactory(
                club=club,
                training_type=training_type,
                location=location,
                trainer=trainer,
                day_of_week=day_of_week,
            )
            for day_of_week in (0, 1, 2)
        ]
        student = StudentFactory(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedules[0],
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        paid_sources = [
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=date(2026, 7, 1),
                created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            )
            for schedule in schedules[1:]
        ]
        for source in paid_sources:
            PaymentFactory(club=club, student=student, conversion_enrollment=source)

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[schedule.id for schedule in schedules],
            canonical_name="Independent but unresolved paid history",
        )

        membership = preview["proposed_memberships"][0]
        assert membership["authority"] == TrainingGroupMembership.Authority.INDEPENDENT
        assert {conflict["code"] for conflict in membership["conflicts"]} == {
            "mixed_or_multi_payment_ownership"
        }

    def test_preview_requires_explicit_start_for_differing_legacy_dates(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        for schedule, starts_on in (
            (first_schedule, date(2026, 7, 1)),
            (second_schedule, date(2026, 7, 8)),
        ):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=starts_on,
                created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
            )

        unresolved_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Start-date decision",
        )
        explicit_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Start-date decision",
            start_dates=[{"student_id": student.id, "starts_on": date(2026, 7, 8)}],
        )

        unresolved_membership = unresolved_preview["proposed_memberships"][0]
        assert unresolved_membership["starts_on"] is None
        assert unresolved_membership["earliest_legacy_starts_on"] == "2026-07-01"
        assert {conflict["code"] for conflict in unresolved_membership["conflicts"]} == {
            "conflicting_or_missing_start_date"
        }
        assert all(delta["effective_starts_on"] is None for delta in unresolved_preview["proposed_roster_deltas"])
        assert all(delta["requires_explicit_starts_on"] for delta in unresolved_preview["proposed_roster_deltas"])
        assert all(
            delta["effective_starts_on"] == "2026-07-08" and not delta["requires_explicit_starts_on"]
            for delta in explicit_preview["proposed_roster_deltas"]
        )
        assert unresolved_preview["digest"] != explicit_preview["digest"]

    def test_preview_blocks_manual_and_paid_source_without_owning_payment(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=second_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Missing paid owner",
        )

        membership = preview["proposed_memberships"][0]
        assert membership["authority"] == TrainingGroupMembership.Authority.INDEPENDENT
        assert {conflict["code"] for conflict in membership["conflicts"]} == {
            "mixed_or_multi_payment_ownership"
        }

    def test_preview_keeps_one_owned_paid_source_with_manual_authority_independent(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        paid_source = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=second_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        PaymentFactory(club=club, student=student, conversion_enrollment=paid_source)

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="One owned paid source",
        )

        membership = preview["proposed_memberships"][0]
        assert membership["authority"] == TrainingGroupMembership.Authority.INDEPENDENT
        assert not membership["conflicts"]

    def test_preview_does_not_default_an_inactive_common_trainer(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        inactive_trainer = TrainerFactory(club=club, is_active=False)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=inactive_trainer,
        )

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[schedule.id],
            canonical_name="Inactive trainer requires decision",
        )

        assert preview["canonical_group"]["responsible_trainer_id"] is None
        assert {conflict["code"] for conflict in preview["conflicts"]} == {
            "responsible_trainer_inactive"
        }

    def test_audit_is_aggregate_and_fails_closed_for_missing_rollout_state(self, club):
        TrainingGroupRolloutState.objects.for_club(club).delete()

        report = audit_training_groups(club=club)
        output = StringIO()
        with pytest.raises(CommandError):
            call_command(
                "audit_training_groups",
                "--club-id",
                str(club.id),
                "--fail-on-invalid",
                stdout=output,
            )

        assert report["invalid"] == ["missing_rollout_state"]
        command_report = json.loads(output.getvalue())
        assert command_report["invalid_club_ids"] == [club.id]


@pytest.mark.django_db
class TestTrainingGroupReconciliationApply:
    @pytest.fixture(autouse=True)
    def _enable_training_group_new_writes_for_confirmed_reconciliation(self, settings):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True

    def _set_reconciling(self, club) -> None:
        from apps.attendance.services.training_groups import (
            transition_training_group_rollout,
        )

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Test enters the real reconciliation epoch.",
            idempotency_key="test-reconciliation-epoch",
        )

    def _preview_with_manual_source(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        source = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Owner-confirmed migration group",
        )
        return preview, first_schedule, second_schedule, source, student

    def test_apply_is_digest_bound_atomic_and_retry_safe(self, club, owner_user):
        preview, first_schedule, second_schedule, source, student = self._preview_with_manual_source(club)
        self._set_reconciling(club)
        assert build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
        )["digest"] == preview["digest"]

        result = apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Owner approved exact legacy group mapping.",
            idempotency_key="s6-apply-manual-source",
        )
        retry = apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Owner approved exact legacy group mapping.",
            idempotency_key="s6-apply-manual-source",
        )

        first_schedule.refresh_from_db()
        second_schedule.refresh_from_db()
        source.refresh_from_db()
        membership = TrainingGroupMembership.objects.for_club(club).get(student=student)
        assert retry == result
        assert result["status"] == TrainingGroup.Status.ACTIVE
        assert first_schedule.training_group_id == second_schedule.training_group_id == result["training_group_id"]
        assert membership.authority == TrainingGroupMembership.Authority.INDEPENDENT
        assert membership.source == TrainingGroupMembership.Source.MIGRATION
        assert source.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert source.schedule_id == first_schedule.id
        assert source.training_group_membership_id == membership.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            training_group_membership=membership,
            schedule=second_schedule,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        ).count() == 1
        assert TrainingGroupMembershipEvent.objects.for_club(club).filter(
            membership=membership,
            source_enrollment_id=source.id,
        ).exists()
        assert TrainingGroupMappingEvent.objects.for_club(club).count() == 3
        reread = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
        )
        assert reread["digest"] == preview["digest"]
        assert audit_training_groups(club=club)["valid"] is True
        ScheduleEnrollment.objects.for_club(club).filter(id=source.id).update(
            training_group_membership=None
        )
        assert "invalid_migration_source_link" in audit_training_groups(club=club)["invalid"]
        ScheduleEnrollment.objects.for_club(club).filter(id=source.id).update(
            training_group_membership=membership
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club),
            mode=TrainingGroupRolloutState.Mode.SHADOW,
            reconciling_from_mode="",
        )
        freeze_schedule_enrollment(club_id=club.id, enrollment_id=source.id)
        membership.refresh_from_db()
        source.refresh_from_db()
        assert membership.status == TrainingGroupMembership.Status.FROZEN
        assert source.status == ScheduleEnrollment.Status.FROZEN
        assert source.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert audit_training_groups(club=club)["valid"] is True
        cancel_schedule_enrollment(
            club_id=club.id,
            enrollment_id=source.id,
            ends_on=date(2026, 7, 28),
        )
        membership.refresh_from_db()
        source.refresh_from_db()
        assert membership.status == TrainingGroupMembership.Status.CANCELLED
        assert source.status == ScheduleEnrollment.Status.CANCELLED
        assert source.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert audit_training_groups(club=club)["valid"] is True

    def test_apply_backfills_only_null_payment_links_with_exact_paid_source(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        source = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            location=location,
            scope=Tariff.Scope.LOCATION,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=SubscriptionFactory(club=club, student=student, tariff=tariff),
            target_schedule=first_schedule,
            target_start_date=source.starts_on,
            conversion_enrollment=source,
        )
        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Paid legacy mapping",
        )
        self._set_reconciling(club)

        apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Backfill one exact paid conversion source.",
            idempotency_key="s6-apply-paid-source",
        )

        payment.refresh_from_db()
        membership = TrainingGroupMembership.objects.for_club(club).get(student=student)
        assert membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
        assert payment.target_training_group_id == membership.training_group_id
        assert payment.target_group_membership_id == membership.id
        assert payment.conversion_group_membership_id == membership.id
        assert payment.group_membership_action_snapshot == ""

    @pytest.mark.parametrize(
        "terminal_status",
        [ScheduleEnrollment.Status.TRANSFERRED, ScheduleEnrollment.Status.CANCELLED],
    )
    def test_terminal_payment_history_outside_open_roster_is_preserved_during_apply(
        self, club, owner_user, terminal_status
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        open_manual_source = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        terminal_paid_source = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=terminal_status,
            starts_on=date(2026, 6, 1),
            ends_on=date(2026, 6, 30),
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            location=location,
            scope=Tariff.Scope.LOCATION,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            target_schedule=first_schedule,
            target_start_date=terminal_paid_source.starts_on,
            conversion_enrollment=terminal_paid_source,
        )

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name="Open manual source with preserved terminal payment history",
        )

        assert not preview["conflicts"]
        assert preview["proposed_memberships"][0]["source_enrollment_ids"] == [open_manual_source.id]
        self._set_reconciling(club)
        result = apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Backfill only the current manual roster source.",
            idempotency_key=f"terminal-history-{terminal_status}",
        )

        membership = TrainingGroupMembership.objects.for_club(club).get(student=student)
        open_manual_source.refresh_from_db()
        terminal_paid_source.refresh_from_db()
        payment.refresh_from_db()
        assert result["membership_count"] == 1
        assert result["linked_payment_count"] == 0
        assert membership.authority == TrainingGroupMembership.Authority.INDEPENDENT
        assert open_manual_source.training_group_membership_id == membership.id
        assert terminal_paid_source.training_group_membership_id is None
        assert payment.conversion_enrollment_id == terminal_paid_source.id
        assert payment.target_training_group_id is None
        assert payment.target_group_membership_id is None
        assert payment.conversion_group_membership_id is None
        assert payment.group_membership_action_snapshot == ""

    @pytest.mark.parametrize(
        ("mismatch", "payment_status", "target_start_date"),
        [
            ("club", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("student", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("schedule", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("source", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("status", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("ends_on", Payment.Status.CONFIRMED, date(2026, 6, 1)),
            ("payment_pending", Payment.Status.PENDING, date(2026, 6, 1)),
            ("payment_rejected", Payment.Status.REJECTED, date(2026, 6, 1)),
            ("target_start_date", Payment.Status.CONFIRMED, date(2026, 6, 2)),
        ],
    )
    def test_payment_conversion_outside_selected_roster_rejects_non_exact_terminal_history(
        self, club, other_club, owner_user, mismatch, payment_status, target_start_date
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        other_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        payment_student = StudentFactory(club=club)
        source_student = StudentFactory(club=club) if mismatch == "student" else payment_student
        source_club = other_club if mismatch == "club" else club
        if mismatch == "club":
            source_schedule = ScheduleFactory(club=other_club)
            source_student = StudentFactory(club=other_club)
        elif mismatch == "schedule":
            source_schedule = other_schedule
        else:
            source_schedule = target_schedule
        terminal_source = ScheduleEnrollment.objects.create(
            club=source_club,
            student=source_student,
            schedule=source_schedule,
            status=(
                ScheduleEnrollment.Status.FROZEN
                if mismatch == "status"
                else ScheduleEnrollment.Status.TRANSFERRED
            ),
            starts_on=date(2026, 6, 1),
            ends_on=None if mismatch == "ends_on" else date(2026, 6, 30),
            created_from=(
                ScheduleEnrollment.CreatedFrom.MANUAL
                if mismatch == "source"
                else ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
            ),
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            location=location,
            scope=Tariff.Scope.LOCATION,
        )
        PaymentFactory(
            club=club,
            student=payment_student,
            tariff=tariff,
            status=payment_status,
            target_schedule=target_schedule,
            target_start_date=target_start_date,
            conversion_enrollment=terminal_source,
        )

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[target_schedule.id],
            canonical_name="Fail closed terminal history validation",
        )

        assert {conflict["code"] for conflict in preview["conflicts"]} == {
            "payment_conversion_outside_selected_roster"
        }
        self._set_reconciling(club)
        with pytest.raises(BusinessLogicError) as exc_info:
            apply_training_group_reconciliation(
                club=club,
                schedule_ids=preview["selected_schedule_ids"],
                canonical_name=preview["canonical_group"]["name"],
                responsible_trainer_id=None,
                start_dates=[],
                preview_digest=preview["digest"],
                actor_user_id=owner_user.id,
                rationale="Reject every non-exact terminal history exception.",
                idempotency_key=f"terminal-history-negative-{mismatch}",
            )

        assert exc_info.value.code == "training_group_reconciliation_conflicts_unresolved"
        assert TrainingGroup.objects.for_club(club).count() == 0

    def test_forward_gate_rejects_roster_changes_after_owner_approved_apply(
        self, club, owner_user
    ):
        preview, first_schedule, _second_schedule, _source, _student = (
            self._preview_with_manual_source(club)
        )
        self._set_reconciling(club)
        result = apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Owner approved roster before the tamper check.",
            idempotency_key="owner-approved-roster-before-tamper",
        )
        target_date = _future_date_for_weekday(first_schedule.day_of_week)
        ScheduleEnrollment.objects.create(
            club=club,
            student=StudentFactory(club=club),
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )
        from apps.attendance.services.training_groups import (
            transition_training_group_rollout_for_owner,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout_for_owner(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=owner_user.id,
                rationale="Must reject unapproved roster change.",
                idempotency_key="owner-approved-roster-tampered-shadow",
                rollout_gate_digest=result["rollout_gate_digest"],
            )

        assert exc_info.value.code == "training_group_owner_delta_mismatch"
        assert TrainingGroupRolloutState.objects.for_club(club).get().mode == (
            TrainingGroupRolloutState.Mode.RECONCILING
        )

    def test_reconciliation_repairs_an_existing_mapped_group_after_containment(
        self, club, owner_user
    ):
        preview, first_schedule, second_schedule, source, _student = (
            self._preview_with_manual_source(club)
        )
        self._set_reconciling(club)
        initial = apply_training_group_reconciliation(
            club=club,
            schedule_ids=preview["selected_schedule_ids"],
            canonical_name=preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Create the owner-approved canonical mapping.",
            idempotency_key="mapped-repair-initial-apply",
        )
        membership = TrainingGroupMembership.objects.for_club(club).get()
        ScheduleEnrollment.objects.for_club(club).filter(
            schedule=second_schedule,
            training_group_membership=membership,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        ).delete()
        assert audit_training_groups(club=club)["invalid"] == [
            "incomplete_membership_projection_coverage"
        ]

        from apps.attendance.services.training_groups import (
            transition_training_group_rollout,
        )

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
            actor_id=owner_user.id,
            rationale="Contain the incomplete projection set.",
            idempotency_key="mapped-repair-enter-containment",
            forward_audit_failed=True,
            canonical_mutations_committed=True,
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=owner_user.id,
            rationale="Repair the mapped canonical group.",
            idempotency_key="mapped-repair-enter-reconciling",
        )
        repair_preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=[first_schedule.id, second_schedule.id],
            canonical_name=preview["canonical_group"]["name"],
        )

        repaired = apply_training_group_reconciliation(
            club=club,
            schedule_ids=repair_preview["selected_schedule_ids"],
            canonical_name=repair_preview["canonical_group"]["name"],
            responsible_trainer_id=None,
            start_dates=[],
            preview_digest=repair_preview["digest"],
            actor_user_id=owner_user.id,
            rationale="Restore the exact missing canonical projection.",
            idempotency_key="mapped-repair-apply",
        )

        source.refresh_from_db()
        assert repaired["training_group_id"] == initial["training_group_id"]
        assert repaired["repaired_existing_group"] is True
        assert repaired["projection_count"] == 1
        assert source.training_group_membership_id == membership.id
        assert (
            TrainingGroupMembershipEvent.objects.for_club(club)
            .filter(
                membership=membership,
                action__in=["backfilled", "source_linked"],
                source_enrollment_id=source.id,
            )
            .count()
            == 1
        )
        assert audit_training_groups(club=club)["valid"] is True

    def test_apply_rolls_back_every_visible_row_after_projection_failure(self, club, owner_user):
        preview, first_schedule, second_schedule, source, _student = self._preview_with_manual_source(club)
        self._set_reconciling(club)

        with patch(
            "apps.attendance.services.training_group_reconciliation._create_missing_backfill_projections",
            side_effect=BusinessLogicError("injected projection failure", code="injected_failure"),
        ):
            with pytest.raises(BusinessLogicError, match="injected projection failure"):
                apply_training_group_reconciliation(
                    club=club,
                    schedule_ids=preview["selected_schedule_ids"],
                    canonical_name=preview["canonical_group"]["name"],
                    responsible_trainer_id=None,
                    start_dates=[],
                    preview_digest=preview["digest"],
                    actor_user_id=owner_user.id,
                    rationale="Injected rollback proof.",
                    idempotency_key="s6-apply-rollback",
                )

        first_schedule.refresh_from_db()
        second_schedule.refresh_from_db()
        source.refresh_from_db()
        assert first_schedule.training_group_id is None
        assert second_schedule.training_group_id is None
        assert source.training_group_membership_id is None
        assert TrainingGroup.objects.for_club(club).count() == 0
        assert TrainingGroupMembership.objects.for_club(club).count() == 0
        assert TrainingGroupMappingEvent.objects.for_club(club).count() == 0

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S6 suite",
    )
    def test_postgresql_apply_quiesces_legacy_permanent_enrollment_without_lost_source(self, club, owner_user):
        preview, first_schedule, _second_schedule, _source, _student = self._preview_with_manual_source(club)
        late_student = StudentFactory(club=club)
        self._set_reconciling(club)
        gate = Barrier(2)

        def apply_mapping() -> tuple[str, object]:
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return (
                    "applied",
                    apply_training_group_reconciliation(
                        club=club,
                        schedule_ids=preview["selected_schedule_ids"],
                        canonical_name=preview["canonical_group"]["name"],
                        responsible_trainer_id=None,
                        start_dates=[],
                        preview_digest=preview["digest"],
                        actor_user_id=owner_user.id,
                        rationale="PostgreSQL mapping versus legacy permanent enrollment.",
                        idempotency_key="s6-postgresql-apply-enrollment",
                    ),
                )
            finally:
                close_old_connections()

        def create_late_legacy_source() -> tuple[str, object]:
            close_old_connections()
            try:
                gate.wait(timeout=10)
                enroll_student_in_schedule(
                    club_id=club.id,
                    student_id=late_student.id,
                    schedule_id=first_schedule.id,
                    starts_on=date(2026, 7, 1),
                    created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
                )
                return "late_source", "created"
            except BusinessLogicError as exc:
                return "late_source", exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = dict(executor.map(lambda operation: operation(), (apply_mapping, create_late_legacy_source)))

        assert outcomes["applied"]["training_group_id"]
        assert outcomes["late_source"] == "training_group_reconciling"
        assert not ScheduleEnrollment.objects.for_club(club).filter(
            student=late_student,
            schedule=first_schedule,
        ).exists()


@pytest.mark.django_db
class TestStudentAlerts:
    def test_reserved_debt_does_not_show_debtor_alert(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        payment = PaymentFactory(club=club, student=student, status=Payment.Status.PENDING)
        DebtFactory(club=club, student=student, checkin=checkin, settlement_payment=payment)

        alerts = _compute_alerts_for_students(students=[student], club=club)[student.id]

        assert all(alert["type"] != "debtor" for alert in alerts)


@pytest.mark.django_db
class TestCreateCheckin:
    @patch("apps.attendance.services.checkin.Checkin.objects.create")
    @patch("apps.attendance.services.checkin._existing_checkin_result")
    def test_create_checkin_returns_existing_on_duplicate_integrity_race(
        self,
        mock_existing_result,
        mock_create,
        club,
    ):
        checkin_date = date(2026, 4, 1)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=checkin_date.weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )
        duplicate_result = {
            "checkin_id": 123,
            "is_debt": False,
            "subscription_id": None,
            "created": False,
        }
        mock_existing_result.side_effect = [None, duplicate_result]
        mock_create.side_effect = IntegrityError("duplicate checkin")

        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="batch",
                checkin_date=checkin_date,
            )

        assert result == duplicate_result
        assert mock_existing_result.call_count == 2

    def test_kiosk_checkin_rejects_student_outside_expected_schedule_without_side_effects(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="kiosk",
                    checkin_date=date.today(),
                )

        assert exc_info.value.code == "student_schedule_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()

    def test_kiosk_checkin_allows_enrolled_active_student(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="kiosk",
                checkin_date=date.today(),
            )

        assert result["created"] is True
        assert Checkin.objects.filter(student=student, schedule=schedule).exists()

    def test_scheduled_checkin_requests_schedule_lock_before_validation(self, club, monkeypatch):
        target_date = date(2026, 4, 1)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        calls: list[str] = []
        schedule_lock_options: list[tuple[str, ...] | None] = []
        original_select_for_update = QuerySet.select_for_update

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model is Schedule:
                calls.append("schedule_lock")
                schedule_lock_options.append(kwargs.get("of"))
            return original_select_for_update(queryset, *args, **kwargs)

        def recording_validate_scheduled_checkin(**kwargs):
            calls.append("scheduled_validation")

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
        monkeypatch.setattr(
            "apps.attendance.services.checkin._validate_scheduled_checkin",
            recording_validate_scheduled_checkin,
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="batch",
                checkin_date=target_date,
            )

        assert result["created"] is True
        assert schedule_lock_options[0] == ("self",)
        assert calls.index("schedule_lock") < calls.index("scheduled_validation")

    @pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL emits FOR UPDATE SQL")
    def test_scheduled_checkin_locks_schedule_before_closed_session_validation(self, club):
        target_date = date(2026, 4, 1)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        with patch("apps.attendance.services.async_task"):
            with CaptureQueriesContext(connection) as queries:
                result = create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="batch",
                    checkin_date=target_date,
                )

        assert result["created"] is True
        sql = [query["sql"] for query in queries.captured_queries]
        schedule_lock_index = next(
            index
            for index, query in enumerate(sql)
            if 'FROM "attendance_schedule"' in query and "FOR UPDATE" in query
        )
        closed_session_read_index = next(
            index
            for index, query in enumerate(sql)
            if 'FROM "attendance_groupsession"' in query and '"closed_at" IS NOT NULL' in query
        )
        checkin_insert_index = next(
            index
            for index, query in enumerate(sql)
            if 'INSERT INTO "attendance_checkin"' in query
        )
        assert schedule_lock_index < closed_session_read_index < checkin_insert_index

    def test_kiosk_checkin_rejects_closed_group_session_before_side_effects(self, club):
        target_date = date.today()
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        GroupSessionFactory(
            club=club,
            schedule=schedule,
            date=target_date,
            closed_at=timezone.now(),
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="kiosk",
                    checkin_date=target_date,
                )

        assert exc_info.value.code == "group_session_closed"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    def test_create_checkin_rejects_closed_payroll_period_before_salary_side_effects(self, club, owner_user):
        from apps.trainers.services import close_trainer_payroll_period

        target_date = date(2026, 6, 10)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=8,
            trainings_used=0,
        )
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="manual",
                    checkin_date=target_date,
                )

        subscription.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert not Checkin.objects.filter(student=student, schedule=schedule, date=target_date).exists()
        assert not CheckinCascadeEvent.objects.for_club(club).filter(effect=CheckinCascadeEvent.Effect.SALARY).exists()
        assert subscription.trainings_left == 8
        assert subscription.trainings_used == 0
        mock_async.assert_not_called()

    def test_create_checkin_locks_payroll_scope_before_subscription_lookup(self, club, monkeypatch):
        target_date = date(2026, 6, 10)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=8,
            trainings_used=0,
        )
        calls: list[str] = []

        def lock_payroll_scope(*, club_id):
            assert club_id == club.id
            calls.append("payroll_lock")

        def assert_payroll_open(*, club_id, target_date):
            assert club_id == club.id
            calls.append("payroll_assert")

        monkeypatch.setattr(
            "apps.trainers.services.lock_trainer_payroll_mutation_scope",
            lock_payroll_scope,
        )
        monkeypatch.setattr(
            "apps.trainers.services.assert_trainer_payroll_date_open",
            assert_payroll_open,
        )
        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="manual",
                checkin_date=target_date,
            )

        assert result["created"] is True
        assert calls == [
            "payroll_lock",
            "payroll_assert",
        ]

    def test_kiosk_checkin_allows_trial_enrollment(self, club):
        student = StudentFactory(club=club, status="trial")
        training_type = TrainingTypeFactory(club=club, trial_free=True)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=date.today(),
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="kiosk",
                checkin_date=date.today(),
            )

        assert result["created"] is True
        assert result["is_debt"] is False

    def test_kiosk_checkin_rejects_frozen_enrollment_before_side_effects(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.FROZEN,
            starts_on=date.today(),
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=training_type.id,
                    source="kiosk",
                    checkin_date=date.today(),
                )

        assert exc_info.value.code == "enrollment_frozen"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    def test_kiosk_checkin_rejects_wrong_schedule_for_enrolled_student(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        enrolled_schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        wrong_schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=enrolled_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=wrong_schedule.id,
                    training_type_id=training_type.id,
                    source="kiosk",
                    checkin_date=date.today(),
                )

        assert exc_info.value.code == "student_schedule_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=wrong_schedule).exists()

    def test_kiosk_checkin_reactivates_enrolled_churned_student(self, club):
        student = StudentFactory(club=club, status="churned")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=date.today().weekday(),
            training_type=training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source="kiosk",
                checkin_date=date.today(),
            )

        student.refresh_from_db()
        assert result["created"] is True
        assert student.status == "trial"

    def test_batch_checkin_rejects_unassigned_student_before_partial_side_effects(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        enrolled = StudentFactory(club=club, status="active")
        unassigned = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=enrolled.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                batch_checkin(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=checkin_date,
                    present_student_ids=[enrolled.id, unassigned.id],
                    training_type_id=schedule.training_type_id,
                    actor_user_id=owner_user.id,
                )

        assert exc_info.value.code == "student_schedule_ineligible"
        assert not Checkin.objects.filter(schedule=schedule).exists()
        assert not Debt.objects.filter(student__in=[enrolled, unassigned]).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    def test_batch_checkin_rejects_frozen_enrollment_before_side_effects(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        frozen_student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=frozen_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.FROZEN,
            starts_on=checkin_date,
        )

        with patch("apps.attendance.services.async_task") as mock_async:
            with pytest.raises(BusinessLogicError) as exc_info:
                batch_checkin(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=checkin_date,
                    present_student_ids=[frozen_student.id],
                    training_type_id=schedule.training_type_id,
                    actor_user_id=owner_user.id,
                )

        assert exc_info.value.code == "enrollment_frozen"
        assert not Checkin.objects.filter(student=frozen_student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=frozen_student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    def test_batch_checkin_rejects_enrolled_lost_student(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        lost_student = StudentFactory(club=club, status="lost")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=lost_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                batch_checkin(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=checkin_date,
                    present_student_ids=[lost_student.id],
                    training_type_id=schedule.training_type_id,
                    actor_user_id=owner_user.id,
                )

        assert exc_info.value.code == "student_ineligible"
        lost_student.refresh_from_db()
        assert lost_student.status == "lost"
        assert not Checkin.objects.filter(student=lost_student, schedule=schedule).exists()

    def test_batch_checkin_rejects_non_occurring_schedule(self, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(
            club=club,
            day_of_week=(checkin_date.weekday() + 1) % 7,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        with patch("apps.attendance.services.async_task"):
            with pytest.raises(BusinessLogicError) as exc_info:
                batch_checkin(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=checkin_date,
                    present_student_ids=[student.id],
                    training_type_id=schedule.training_type_id,
                    actor_user_id=owner_user.id,
                )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()

    def test_batch_checkin_empty_list_rejects_non_occurring_schedule_without_closing_session(
        self,
        club,
        owner_user,
    ):
        checkin_date = date.today() - timedelta(days=7)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=(checkin_date.weekday() + 1) % 7,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=checkin_date,
                present_student_ids=[],
                training_type_id=schedule.training_type_id,
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not GroupSession.objects.for_club(club).filter(
            schedule=schedule,
            date=checkin_date,
        ).exists()

    def test_batch_checkin_locks_schedule_before_empty_session_close(self, club, owner_user, monkeypatch):
        checkin_date = date.today() - timedelta(days=7)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        calls: list[tuple[str, tuple[str, ...] | None]] = []
        original_select_for_update = QuerySet.select_for_update

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model is Schedule:
                calls.append(("schedule", kwargs.get("of")))
            elif queryset.model is GroupSession:
                calls.append(("group_session", kwargs.get("of")))
            return original_select_for_update(queryset, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)

        result = batch_checkin(
            club_id=club.id,
            schedule_id=schedule.id,
            checkin_date=checkin_date,
            present_student_ids=[],
            training_type_id=schedule.training_type_id,
            actor_user_id=owner_user.id,
        )

        assert result["checkins"] == []
        assert calls.index(("schedule", ("self",))) < calls.index(("group_session", None))
        session = GroupSession.objects.get(id=result["group_session_id"])
        assert session.attendee_count == 0
        assert session.close_source == GroupSession.CloseSource.BATCH


@pytest.mark.django_db
class TestCreateSchedule:
    def test_create_schedule(self, club):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        schedule = create_schedule(
            club_id=club.id,
            day_of_week=0,
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name="Adults Boxing",
            trainer_id=trainer.id,
            location_id=location.id,
            training_type_id=training_type.id,
        )
        assert isinstance(schedule, Schedule)
        assert schedule.club_id == club.id
        assert schedule.day_of_week == 0
        assert schedule.group_name == "Adults Boxing"
        assert schedule.trainer_id == trainer.id
        assert schedule.location_id == location.id
        assert schedule.training_type_id == training_type.id

    @pytest.mark.parametrize(
        ("start_time", "end_time"),
        [
            (time(10, 0), time(10, 0)),
            (time(11, 0), time(10, 0)),
        ],
    )
    def test_create_schedule_rejects_unsupported_time_range(self, club, start_time, end_time):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_schedule(
                club_id=club.id,
                day_of_week=0,
                start_time=start_time,
                end_time=end_time,
                group_name="Invalid duration",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )

        assert exc_info.value.code == "invalid_schedule_time_range"
        assert not Schedule.objects.for_club(club).filter(group_name="Invalid duration").exists()

    def test_schedule_time_range_is_enforced_at_database_boundary(self, club):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ScheduleFactory(
                    club=club,
                    start_time=time(10, 0),
                    end_time=time(10, 0),
                )

    def test_create_schedule_training_type_wrong_club(self, club):
        other_club = ClubFactory()
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=other_club)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_schedule(
                club_id=club.id,
                day_of_week=0,
                start_time=time(10, 0),
                end_time=time(11, 0),
                group_name="Test",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )
        assert exc_info.value.code == "training_type_club_mismatch"

    def test_create_schedule_training_type_inactive(self, club):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, is_active=False)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_schedule(
                club_id=club.id,
                day_of_week=0,
                start_time=time(10, 0),
                end_time=time(11, 0),
                group_name="Inactive Type",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )

        assert exc_info.value.code == "training_type_club_mismatch"

    def test_update_schedule_persists_training_type(self, club):
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        updated = update_schedule(
            schedule_id=schedule.id,
            club_id=club.id,
            training_type_id=training_type.id,
        )
        assert updated.training_type_id == training_type.id

    @pytest.mark.parametrize(
        ("start_time", "end_time"),
        [
            (time(10, 0), time(10, 0)),
            (time(11, 0), time(10, 0)),
        ],
    )
    def test_update_schedule_rejects_unsupported_time_range(self, club, start_time, end_time):
        schedule = ScheduleFactory(
            club=club,
            start_time=time(9, 0),
            end_time=time(10, 0),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            update_schedule(
                schedule_id=schedule.id,
                club_id=club.id,
                start_time=start_time,
                end_time=end_time,
            )

        assert exc_info.value.code == "invalid_schedule_time_range"
        schedule.refresh_from_db()
        assert schedule.start_time == time(9, 0)
        assert schedule.end_time == time(10, 0)

    def test_update_schedule_rejects_inactive_training_type(self, club):
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club, is_active=False)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_schedule(
                schedule_id=schedule.id,
                club_id=club.id,
                training_type_id=training_type.id,
            )

        assert exc_info.value.code == "training_type_club_mismatch"

    def test_update_schedule_rejects_duplicate_one_time_trainer_slot(self, club):
        target_date = date(2026, 4, 15)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        existing = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        target = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=date(2026, 4, 16),
            start_time=time(12, 0),
            end_time=time(13, 0),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            update_schedule(
                schedule_id=target.id,
                club_id=club.id,
                one_time_date=existing.one_time_date,
                start_time=existing.start_time,
                end_time=existing.end_time,
            )

        assert exc_info.value.code == "schedule_slot_conflict"

    def test_create_schedule_trainer_wrong_club(self, club):
        other_club = ClubFactory()
        trainer = TrainerFactory(club=other_club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_schedule(
                club_id=club.id,
                day_of_week=0,
                start_time=time(10, 0),
                end_time=time(11, 0),
                group_name="Test",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )
        assert exc_info.value.code == "trainer_club_mismatch"

    def test_create_schedule_location_wrong_club(self, club):
        other_club = ClubFactory()
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=other_club)
        training_type = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_schedule(
                club_id=club.id,
                day_of_week=0,
                start_time=time(10, 0),
                end_time=time(11, 0),
                group_name="Test",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )
        assert exc_info.value.code == "location_club_mismatch"


@pytest.mark.django_db
class TestScheduleEnrollment:
    def test_paid_conversion_enrollment_rejects_open_permanent_membership(self, club):
        from apps.attendance.services.enrollment import create_paid_conversion_enrollment

        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        existing = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 4, 1),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_paid_conversion_enrollment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                starts_on=date(2026, 4, 8),
            )

        assert exc_info.value.code == "manual_operational_admission_enrollment_conflict"
        existing.refresh_from_db()
        assert existing.starts_on == date(2026, 4, 1)
        assert existing.created_from == ScheduleEnrollment.CreatedFrom.MANUAL

    @pytest.mark.parametrize(
        ("action", "initial_status"),
        [
            ("cancel", ScheduleEnrollment.Status.ACTIVE),
            ("transfer", ScheduleEnrollment.Status.ACTIVE),
            ("freeze", ScheduleEnrollment.Status.ACTIVE),
            ("unfreeze", ScheduleEnrollment.Status.FROZEN),
        ],
    )
    def test_pending_payment_owned_enrollment_rejects_generic_lifecycle_mutations(
        self,
        club,
        action,
        initial_status,
    ):
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club)
        target_schedule = ScheduleFactory(club=club)
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=initial_status,
            starts_on=date(2026, 4, 1),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=TariffFactory(club=club),
            status=Payment.Status.PENDING,
            conversion_enrollment=enrollment,
        )
        enrollment_count = ScheduleEnrollment.objects.for_club(club).count()

        with pytest.raises(BusinessLogicError) as exc_info:
            if action == "cancel":
                cancel_schedule_enrollment(
                    club_id=club.id,
                    enrollment_id=enrollment.id,
                    ends_on=date(2026, 4, 2),
                )
            elif action == "transfer":
                transfer_schedule_enrollment(
                    club_id=club.id,
                    enrollment_id=enrollment.id,
                    target_schedule_id=target_schedule.id,
                    ends_on=date(2026, 4, 2),
                )
            elif action == "freeze":
                freeze_schedule_enrollment(club_id=club.id, enrollment_id=enrollment.id)
            else:
                unfreeze_schedule_enrollment(club_id=club.id, enrollment_id=enrollment.id)

        enrollment.refresh_from_db()
        assert exc_info.value.code == "pending_payment_enrollment_action_forbidden"
        assert enrollment.status == initial_status
        assert enrollment.ends_on is None
        assert ScheduleEnrollment.objects.for_club(club).count() == enrollment_count

    def test_enroll_student_in_schedule_creates_active_enrollment(self, club):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        enrollment = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 4, 1),
        )

        assert enrollment.club_id == club.id
        assert enrollment.student_id == student.id
        assert enrollment.schedule_id == schedule.id
        assert enrollment.status == ScheduleEnrollment.Status.ACTIVE
        assert enrollment.starts_on == date(2026, 4, 1)
        assert enrollment.ends_on is None
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.MANUAL

    def test_self_service_guest_booking_uses_rescheduled_effective_start_for_past_guard(self, club):
        club.timezone = "Europe/Moscow"
        club.save(update_fields=["timezone"])
        student = StudentFactory(club=club, status="active")
        original_date = date(2099, 1, 5)
        rescheduled_date = date(2099, 1, 6)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=original_date.weekday(),
            start_time=time(20, 0),
            end_time=time(21, 0),
            training_type=training_type,
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=original_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=rescheduled_date,
            new_start_time=time(10, 0),
            new_end_time=time(11, 0),
        )
        now = datetime(2099, 1, 6, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        with patch("apps.attendance.services.enrollment.timezone.now", return_value=now):
            with pytest.raises(BusinessLogicError) as exc_info:
                book_guest_group_visit(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    target_date=rescheduled_date,
                    student_id=student.id,
                    origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
                )

        assert exc_info.value.code == "self_booking_past_session"

    def test_personal_booking_naive_start_is_checked_in_club_timezone(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        student = StudentFactory(club=club, status="active")
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=8,
        )
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=8,
            expires_at=datetime(2099, 1, 20, 0, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg")),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        starts_at = datetime(2099, 1, 6, 10, 0)
        now = datetime(2099, 1, 6, 10, 30, tzinfo=ZoneInfo("Asia/Yekaterinburg"))

        with patch("apps.attendance.services.enrollment.timezone.now", return_value=now):
            with pytest.raises(BusinessLogicError) as exc_info:
                book_personal_session(
                    club_id=club.id,
                    student_id=student.id,
                    trainer_id=trainer.id,
                    starts_at=starts_at,
                    ends_at=datetime(2099, 1, 6, 11, 0),
                    location_id=location.id,
                    training_type_id=training_type.id,
                    subscription_id=subscription.id,
                )

        assert exc_info.value.code == "personal_booking_past_slot"

    def test_enroll_student_in_schedule_updates_existing_open_enrollment(self, club):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        first = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=date(2026, 4, 1),
        )

        second = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 4, 8),
        )

        assert second.id == first.id
        assert second.status == ScheduleEnrollment.Status.ACTIVE
        assert second.starts_on == date(2026, 4, 8)
        assert ScheduleEnrollment.objects.filter(student=student, schedule=schedule).count() == 1

    def test_enroll_student_in_schedule_rejects_wrong_club_schedule(self, club, other_club):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=other_club)

        with pytest.raises(BusinessLogicError) as exc_info:
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=date(2026, 4, 1),
            )

        assert exc_info.value.code == "schedule_club_mismatch"

    def test_assign_without_checkins_feeds_roster_and_student_schedule_selectors(self, club):
        target_date = date.today()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())

        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        roster = get_students_for_schedule(
            club=club,
            schedule_id=schedule.id,
            reference_date=target_date,
        )
        student_schedules = get_student_schedule(club=club, student_id=student.id)
        week_occurrences = get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student.id,
            date_from=target_date,
            date_to=target_date,
        )

        assert {row["id"] for row in roster} == {student.id}
        assert [item.id for item in student_schedules] == [schedule.id]
        assert [item.schedule_id for item in week_occurrences] == [schedule.id]

    def test_get_student_schedule_default_uses_club_local_today(self, club, monkeypatch):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        local_today = date(2026, 6, 29)
        monkeypatch.setattr(
            "apps.clubs.timezones.timezone.now",
            lambda: datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
        )
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=local_today.weekday())
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=local_today,
        )

        schedules = get_student_schedule(club=club, student_id=student.id)

        assert [item.id for item in schedules] == [schedule.id]

    def test_cancel_schedule_enrollment_removes_after_boundary_and_suppresses_legacy(
        self,
        club,
    ):
        reference_date = date.today()
        boundary = reference_date - timedelta(days=1)
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=reference_date.weekday())
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=reference_date - timedelta(days=14),
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=reference_date - timedelta(days=7),
        )

        closed = cancel_schedule_enrollment(
            club_id=club.id,
            enrollment_id=ScheduleEnrollment.objects.get(student=student, schedule=schedule).id,
            ends_on=boundary,
        )

        roster_ids = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=reference_date,
        )
        assert closed.status == ScheduleEnrollment.Status.CANCELLED
        assert closed.ends_on == boundary
        assert roster_ids == set()
        assert get_students_for_schedule(
            club=club,
            schedule_id=schedule.id,
            reference_date=reference_date,
        ) == []

    def test_transfer_schedule_enrollment_moves_after_boundary(self, club):
        reference_date = date.today()
        boundary = reference_date - timedelta(days=1)
        student = StudentFactory(club=club, status="active")
        old_schedule = ScheduleFactory(club=club, day_of_week=reference_date.weekday())
        new_schedule = ScheduleFactory(
            club=club,
            day_of_week=reference_date.weekday(),
            group_name="Transferred Group",
        )
        enrollment = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=old_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=reference_date - timedelta(days=14),
        )

        closed, opened = transfer_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment.id,
            target_schedule_id=new_schedule.id,
            ends_on=boundary,
        )

        old_roster = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=old_schedule.id,
            target_date=reference_date,
        )
        new_roster = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=new_schedule.id,
            target_date=reference_date,
        )
        student_schedule_ids = {
            item.id for item in get_student_schedule(club=club, student_id=student.id)
        }
        assert closed.status == ScheduleEnrollment.Status.TRANSFERRED
        assert closed.ends_on == boundary
        assert opened.schedule_id == new_schedule.id
        assert opened.starts_on == reference_date
        assert old_roster == set()
        assert new_roster == {student.id}
        assert old_schedule.id not in student_schedule_ids
        assert new_schedule.id in student_schedule_ids

    def test_freeze_and_unfreeze_schedule_enrollment(self, club):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        enrollment = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        frozen = freeze_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment.id,
        )
        unfrozen = unfreeze_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment.id,
        )

        assert frozen.status == ScheduleEnrollment.Status.FROZEN
        assert unfrozen.status == ScheduleEnrollment.Status.ACTIVE

    def test_lifecycle_services_reject_wrong_club_enrollment_or_target_schedule(
        self,
        club,
        other_club,
    ):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        other_student = StudentFactory(club=other_club, status="active")
        other_schedule = ScheduleFactory(club=other_club)
        enrollment = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )
        other_enrollment = enroll_student_in_schedule(
            club_id=other_club.id,
            student_id=other_student.id,
            schedule_id=other_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        with pytest.raises(BusinessLogicError) as target_exc:
            transfer_schedule_enrollment(
                club_id=club.id,
                enrollment_id=enrollment.id,
                target_schedule_id=other_schedule.id,
                ends_on=date.today(),
            )
        with pytest.raises(BusinessLogicError) as enrollment_exc:
            cancel_schedule_enrollment(
                club_id=club.id,
                enrollment_id=other_enrollment.id,
                ends_on=date.today(),
            )

        assert target_exc.value.code == "schedule_club_mismatch"
        assert enrollment_exc.value.code == "enrollment_not_found"

    @patch("apps.attendance.services.checkin.async_task")
    def test_book_guest_group_visit_creates_one_day_roster_and_keeps_checkin_rules(
        self,
        mock_async_task,
        club,
    ):
        target_date = date(2026, 4, 6)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        result = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.WALK_IN_CHECKIN,
            actor_user_id=None,
        )

        enrollment = result.enrollment
        assert result.created is True
        assert result.already_member is False
        assert result.is_guest_visit is True
        assert enrollment.starts_on == target_date
        assert enrollment.ends_on == target_date
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
        assert Checkin.objects.for_club(club).count() == 0
        assert Debt.objects.for_club(club).count() == 0
        assert get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        ) == {student.id}

        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=enrollment)
        assert event.event_type == ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED
        assert event.origin == ScheduleBookingEvent.Origin.WALK_IN_CHECKIN

        checkin_result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source=Checkin.Source.BATCH,
            checkin_date=target_date,
        )
        assert checkin_result["created"] is True
        assert Checkin.objects.for_club(club).count() == 1

    def test_book_guest_group_visit_is_idempotent_for_same_student_schedule_date(self, club):
        target_date = date(2026, 4, 6)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        first = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
        )
        second = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
        )

        assert second.created is False
        assert second.already_member is False
        assert second.enrollment.id == first.enrollment.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            starts_on=target_date,
            ends_on=target_date,
        ).count() == 1
        assert ScheduleBookingEvent.objects.for_club(club).filter(
            enrollment=first.enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED,
        ).count() == 1

    def test_book_guest_group_visit_can_record_self_booking_source(self, club):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        result = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
            allow_lead_conversion=False,
        )

        assert result.created is True
        assert result.is_guest_visit is True
        assert result.enrollment.created_from == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=result.enrollment)
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING

    def test_book_guest_group_visit_self_booking_does_not_convert_lead(self, club):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        lead = StudentFactory(club=club, status="lead")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_guest_group_visit(
                club_id=club.id,
                schedule_id=schedule.id,
                student_id=lead.id,
                target_date=target_date,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
                created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
                allow_lead_conversion=False,
            )

        assert exc_info.value.code == "student_ineligible"
        lead.refresh_from_db()
        assert lead.status == "lead"

    def test_book_guest_group_visit_self_booking_requires_subscription_or_dropin_policy(self, club):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_guest_group_visit(
                club_id=club.id,
                schedule_id=schedule.id,
                student_id=student.id,
                target_date=target_date,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
                created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
                require_financial_eligibility=True,
            )

        assert exc_info.value.code == "drop_in_price_required"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()

    def test_book_guest_group_visit_self_booking_allows_active_subscription_when_no_dropin(self, club):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        result = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
            require_financial_eligibility=True,
        )

        assert result.created is True
        assert result.enrollment.created_from == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING

    def test_book_guest_group_visit_self_booking_returns_existing_before_financial_guard(self, club):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        existing = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        result = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
            require_financial_eligibility=True,
        )

        assert result.created is False
        assert result.enrollment.id == existing.id

    def test_book_guest_group_visit_self_booking_rejects_past_session(self, club):
        target_date = timezone.localdate() - timedelta(days=1)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_guest_group_visit(
                club_id=club.id,
                schedule_id=schedule.id,
                student_id=student.id,
                target_date=target_date,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
                created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
                allow_lead_conversion=False,
            )

        assert exc_info.value.code == "self_booking_past_session"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()
        assert not ScheduleBookingEvent.objects.for_club(club).exists()

    def test_book_guest_group_visit_returns_permanent_member_without_mutating_enrollment(self, club):
        target_date = date(2026, 4, 6)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        permanent = enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date - timedelta(days=7),
        )

        result = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
        )

        permanent.refresh_from_db()
        assert result.created is False
        assert result.already_member is True
        assert result.enrollment.id == permanent.id
        assert permanent.ends_on is None
        assert permanent.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).count() == 1

    def test_book_guest_group_visit_blocks_closed_session(self, club):
        target_date = date(2026, 4, 6)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        GroupSessionFactory(
            club=club,
            schedule=schedule,
            date=target_date,
            closed_at=timezone.now(),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_guest_group_visit(
                club_id=club.id,
                schedule_id=schedule.id,
                student_id=student.id,
                target_date=target_date,
            )

        assert exc_info.value.code == "group_session_closed"

    def test_book_guest_group_visit_requires_group_occurrence(self, club):
        target_date = date(2026, 4, 6)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=personal_type,
        )
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_guest_group_visit(
                club_id=club.id,
                schedule_id=schedule.id,
                student_id=student.id,
                target_date=target_date,
            )

        assert exc_info.value.code == "guest_visit_requires_group_schedule"

    def test_book_personal_session_creates_one_time_schedule_and_dated_enrollment(self, club):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        result = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
            idempotency_key="personal-booking-1",
        )

        assert result.created is True
        assert result.schedule.one_time_date == target_date
        assert result.schedule.day_of_week == target_date.weekday()
        assert result.schedule.trainer_id == trainer.id
        assert result.schedule.location_id == location.id
        assert result.schedule.training_type_id == training_type.id
        assert result.schedule.training_type.kind == TrainingType.Kind.PERSONAL
        assert result.enrollment.student_id == student.id
        assert result.enrollment.starts_on == target_date
        assert result.enrollment.ends_on == target_date
        assert result.enrollment.created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
        assert get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=result.schedule.id,
            target_date=target_date,
        ) == {student.id}
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=result.enrollment)
        assert event.event_type == ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED
        assert event.origin == ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION
        assert event.effective_date == target_date
        assert event.metadata == {
            "idempotency_key": "personal-booking-1",
            "subscription_id": subscription.id,
        }

    def test_book_personal_session_consumes_exact_published_availability_slot(self, club):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        result = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
        )

        slot.refresh_from_db()
        assert result.availability_slot_id == slot.id
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == result.enrollment.id
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=result.enrollment)
        assert event.metadata["availability_slot_id"] == slot.id

    def test_book_personal_session_rejects_exact_unavailable_availability_slot(self, club):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.BLOCKED,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                subscription_id=subscription.id,
            )

        assert exc_info.value.code == "personal_availability_slot_unavailable"

    def test_book_personal_session_is_idempotent_for_same_slot(self, club):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        first = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
        )
        second = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
        )

        assert second.created is False
        assert second.schedule.id == first.schedule.id
        assert second.enrollment.id == first.enrollment.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        ).count() == 1
        assert Schedule.objects.for_club(club).filter(
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(10, 0),
            end_time=time(11, 0),
        ).count() == 1
        assert ScheduleBookingEvent.objects.for_club(club).filter(
            enrollment=first.enrollment,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        ).count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_book_personal_availability_slot_locks_legacy_subscription_without_nullable_component_lock(
        self,
        club,
        student_user,
    ):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        result = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            actor_user_id=student_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            subscription_id=subscription.id,
            idempotency_key="slot-booking-1",
        )

        assert result.created is True
        assert result.enrollment.student_id == student.id
        assert result.enrollment.created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
        assert result.schedule.trainer_id == trainer.id
        assert result.schedule.location_id == location.id
        assert result.schedule.training_type_id == training_type.id
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == result.enrollment.id
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=result.enrollment)
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
        assert event.metadata == {
            "availability_slot_id": slot.id,
            "idempotency_key": "slot-booking-1",
            "subscription_id": subscription.id,
        }

    def test_book_personal_availability_slot_is_idempotent_for_same_student(self, club, student_user):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        first = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            actor_user_id=student_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        )
        second = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            actor_user_id=student_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        )

        assert second.created is False
        assert second.schedule.id == first.schedule.id
        assert second.enrollment.id == first.enrollment.id
        assert ScheduleBookingEvent.objects.for_club(club).filter(
            enrollment=first.enrollment,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        ).count() == 1

    def test_book_personal_availability_slot_rejects_slot_booked_by_other_student(self, club):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        other_student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        SubscriptionFactory(club=club, student=other_student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        first = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            actor_user_id=None,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_availability_slot(
                club_id=club.id,
                slot_id=slot.id,
                student_id=other_student.id,
                actor_user_id=None,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            )

        assert exc_info.value.code == "personal_availability_slot_taken"
        assert Schedule.objects.for_club(club).filter(one_time_date=target_date).count() == 1
        assert ScheduleBookingEvent.objects.for_club(club).filter(enrollment=first.enrollment).count() == 1

    def test_book_personal_availability_slot_requires_subscription(self, club, student_user):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status="active")
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_availability_slot(
                club_id=club.id,
                slot_id=slot.id,
                student_id=student.id,
                actor_user_id=student_user.id,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            )

        assert exc_info.value.code == "subscription_not_available"
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert Schedule.objects.for_club(club).filter(one_time_date=target_date).count() == 0

    def test_book_personal_availability_slot_rejects_past_slot(self, club, student_user):
        target_date = timezone.localdate() - timedelta(days=1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_availability_slot(
                club_id=club.id,
                slot_id=slot.id,
                student_id=student.id,
                actor_user_id=student_user.id,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            )

        assert exc_info.value.code == "personal_availability_slot_past"
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student).exists()

    @pytest.mark.django_db(transaction=True)
    def test_personal_booking_payment_reservation_locks_legacy_subscription_without_nullable_component_lock(
        self,
        club,
        owner_user,
    ):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_personal_booking_payment_reservation(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                tariff_id=tariff.id,
                availability_slot_id=slot.id,
                created_by_id=owner_user.id,
                source=BankPaymentOrder.Source.OWNER,
            )

        assert exc_info.value.code == "personal_subscription_already_available"

    def test_create_personal_booking_payment_reservation_creates_order_and_holds_slot(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
            idempotency_key="personal-payment-hold-1",
        )

        reservation.refresh_from_db()
        slot.refresh_from_db()
        assert reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
        assert reservation.availability_slot_id == slot.id
        assert reservation.bank_payment_order.provider_payment_url
        assert reservation.payment.seller_trainer_id == trainer.id
        assert reservation.payment.package_owner_trainer_id == trainer.id
        assert slot.status == PersonalAvailabilitySlot.Status.HELD
        assert slot.booked_enrollment_id is None

    def test_close_personal_booking_payment_reservation_releases_held_slot(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(12, 0))
        ends_at = _aware_datetime(target_date, time(13, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
        )

        closed = close_personal_booking_payment_reservation_for_order(
            club_id=club.id,
            order_id=reservation.bank_payment_order_id,
            status=PersonalBookingPaymentReservation.Status.CANCELLED,
            reason="trainer_cancelled_link",
        )

        assert closed is not None
        closed.refresh_from_db()
        slot.refresh_from_db()
        assert closed.status == PersonalBookingPaymentReservation.Status.CANCELLED
        assert closed.last_error_message == "trainer_cancelled_link"
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot.booked_enrollment_id is None

    def test_manual_review_personal_payment_reservation_keeps_slot_blocked(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(12, 0))
        ends_at = _aware_datetime(target_date, time(13, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        other_student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
        )

        reviewed = close_personal_booking_payment_reservation_for_order(
            club_id=club.id,
            order_id=reservation.bank_payment_order_id,
            status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            reason="paid_callback_requires_review",
        )

        assert reviewed is not None
        reviewed.refresh_from_db()
        slot.refresh_from_db()
        assert reviewed.status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
        assert reviewed.last_error_message == "paid_callback_requires_review"
        assert slot.status == PersonalAvailabilitySlot.Status.HELD

        open_reservations = get_personal_booking_payment_reservations(
            club_id=club.id,
            student_id=student.id,
            status="open_actionable",
        )
        assert [item.id for item in open_reservations] == [reviewed.id]

        with pytest.raises(BusinessLogicError) as exc_info:
            create_personal_booking_payment_reservation(
                club_id=club.id,
                student_id=other_student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                tariff_id=tariff.id,
                created_by_id=owner_user.id,
                source=BankPaymentOrder.Source.OWNER,
            )

        assert exc_info.value.code == "personal_payment_reservation_slot_conflict"

    def test_manual_review_personal_payment_reservation_blocks_same_student_tariff(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        first_starts_at = _aware_datetime(target_date, time(14, 0))
        first_ends_at = _aware_datetime(target_date, time(15, 0))
        second_starts_at = _aware_datetime(target_date, time(16, 0))
        second_ends_at = _aware_datetime(target_date, time(17, 0))
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=first_starts_at,
            ends_at=first_ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
        )
        close_personal_booking_payment_reservation_for_order(
            club_id=club.id,
            order_id=reservation.bank_payment_order_id,
            status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_personal_booking_payment_reservation(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=second_starts_at,
                ends_at=second_ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                tariff_id=tariff.id,
                created_by_id=owner_user.id,
                source=BankPaymentOrder.Source.OWNER,
            )

        assert exc_info.value.code == "personal_payment_reservation_pending_exists"

    def test_confirm_personal_booking_payment_reservation_books_slot(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(14, 0))
        ends_at = _aware_datetime(target_date, time(15, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        reservation = create_personal_booking_payment_reservation(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            availability_slot_id=slot.id,
            created_by_id=owner_user.id,
            source=BankPaymentOrder.Source.STUDENT,
            idempotency_key="personal-payment-confirm-1",
        )
        reservation.payment.status = Payment.Status.CONFIRMED
        reservation.payment.save(update_fields=["status", "updated_at"])
        reservation.subscription.status = Subscription.Status.ACTIVE
        reservation.subscription.expires_at = timezone.now() + timedelta(days=30)
        reservation.subscription.save(update_fields=["status", "expires_at", "updated_at"])
        reservation.bank_payment_order.status = BankPaymentOrder.Status.APPROVED
        reservation.bank_payment_order.save(update_fields=["status", "updated_at"])

        confirmed = confirm_personal_booking_payment_reservation_for_order(
            club_id=club.id,
            order_id=reservation.bank_payment_order_id,
            actor_user_id=owner_user.id,
        )

        assert confirmed is not None
        confirmed.refresh_from_db()
        slot.refresh_from_db()
        assert confirmed.status == PersonalBookingPaymentReservation.Status.BOOKED
        assert confirmed.schedule_id is not None
        assert confirmed.enrollment_id is not None
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == confirmed.enrollment_id
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment_id=confirmed.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            metadata__availability_slot_id=slot.id,
        )
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING

    def test_one_time_schedule_trainer_slot_is_unique_at_database_level(self, club):
        target_date = date(2026, 4, 7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(10, 0),
            end_time=time(11, 0),
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ScheduleFactory(
                    club=club,
                    trainer=trainer,
                    location=location,
                    training_type=training_type,
                    one_time_date=target_date,
                    start_time=time(10, 0),
                    end_time=time(11, 0),
                )

        schedule.is_active = False
        schedule.save(update_fields=["is_active"])
        replacement = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        assert replacement.is_active is True

    def test_book_personal_session_rejects_overlapping_trainer_slot(self, club):
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        existing = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            day_of_week=target_date.weekday(),
            start_time=time(10, 30),
            end_time=time(11, 30),
        )
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=datetime.combine(target_date, time(10, 0)),
                ends_at=datetime.combine(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=training_type.id,
                subscription_id=subscription.id,
            )

        assert exc_info.value.code == "personal_booking_slot_conflict"
        assert Schedule.objects.for_club(club).filter(id=existing.id).exists()

    def test_book_personal_session_rejects_active_payment_reservation_overlap(self, club, owner_user):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        tariff = TariffFactory(training_type=training_type)
        reserved_student = StudentFactory(club=club, status="active")
        booking_student = StudentFactory(club=club, status="active")
        subscription = SubscriptionFactory(club=club, student=booking_student, tariff=tariff)
        PersonalBookingPaymentReservation.objects.create(
            club=club,
            student=reserved_student,
            trainer=trainer,
            location=location,
            training_type=training_type,
            tariff=tariff,
            starts_at=starts_at,
            ends_at=ends_at,
            expires_at=timezone.now() + timedelta(minutes=30),
            status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            created_by_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=booking_student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                subscription_id=subscription.id,
            )

        assert exc_info.value.code == "personal_payment_reservation_slot_conflict"

    def test_book_personal_session_rejects_past_slot(self, club):
        target_date = timezone.localdate() - timedelta(days=1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=_aware_datetime(target_date, time(10, 0)),
                ends_at=_aware_datetime(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=training_type.id,
                subscription_id=subscription.id,
            )

        assert exc_info.value.code == "personal_booking_past_slot"
        assert not Schedule.objects.for_club(club).filter(one_time_date=target_date).exists()

    def test_book_personal_session_requires_subscription(self, club):
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=datetime.combine(target_date, time(10, 0)),
                ends_at=datetime.combine(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=training_type.id,
            )

        assert exc_info.value.code == "subscription_required_for_personal_booking"

    def test_book_personal_session_rejects_group_training_type(self, club):
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=datetime.combine(target_date, time(10, 0)),
                ends_at=datetime.combine(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=group_type.id,
            )

        assert exc_info.value.code == "personal_booking_requires_personal_type"

    def test_book_personal_session_requires_trainer_location_and_rate(self, club):
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        student = StudentFactory(club=club, status="active")

        with pytest.raises(BusinessLogicError) as location_exc:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=datetime.combine(target_date, time(10, 0)),
                ends_at=datetime.combine(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=training_type.id,
            )
        assert location_exc.value.code == "trainer_location_required"

        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        with pytest.raises(BusinessLogicError) as rate_exc:
            book_personal_session(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=datetime.combine(target_date, time(10, 0)),
                ends_at=datetime.combine(target_date, time(11, 0)),
                location_id=location.id,
                training_type_id=training_type.id,
            )
        assert rate_exc.value.code == "trainer_rate_required"

    def test_cancel_guest_booking_marks_one_day_enrollment_cancelled_and_audits(self, club, owner_user):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        booking = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        result = cancel_guest_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment.id,
            actor_user_id=owner_user.id,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            reason="Family plans changed",
        )

        result.enrollment.refresh_from_db()
        assert result.event_created is True
        assert result.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        ) == set()
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment=booking.enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_CANCELLED,
        )
        assert event.actor_id == owner_user.id
        assert event.effective_date == target_date
        assert event.metadata == {"reason": "Family plans changed"}

    def test_cancel_guest_booking_rejects_past_booking(self, club, owner_user):
        target_date = timezone.localdate() - timedelta(days=1)
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())
        student = StudentFactory(club=club, status="active")
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_guest_booking(
                club_id=club.id,
                enrollment_id=enrollment.id,
                actor_user_id=owner_user.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            )

        assert exc_info.value.code == "booking_past_date"
        enrollment.refresh_from_db()
        assert enrollment.status == ScheduleEnrollment.Status.ACTIVE

    def test_cancel_personal_booking_deactivates_one_time_schedule_and_audits(self, club, owner_user):
        target_date = _future_date_for_weekday(1)
        starts_at = datetime.combine(target_date, time(10, 0))
        ends_at = datetime.combine(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        booking = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
        )

        result = cancel_personal_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment.id,
            actor_user_id=owner_user.id,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            reason="Trainer unavailable",
        )

        result.enrollment.refresh_from_db()
        booking.schedule.refresh_from_db()
        assert result.event_created is True
        assert result.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert booking.schedule.is_active is False
        assert get_schedule_occurrences_for_date(club=club, target_date=target_date) == []
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment=booking.enrollment,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        )
        assert event.metadata == {"reason": "Trainer unavailable"}

    def test_cancel_personal_booking_rejects_past_booking(self, club, owner_user):
        target_date = timezone.localdate() - timedelta(days=1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        student = StudentFactory(club=club, status="active")
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_personal_booking(
                club_id=club.id,
                enrollment_id=enrollment.id,
                actor_user_id=owner_user.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            )

        assert exc_info.value.code == "booking_past_date"
        enrollment.refresh_from_db()
        schedule.refresh_from_db()
        assert enrollment.status == ScheduleEnrollment.Status.ACTIVE
        assert schedule.is_active is True

    def test_cancel_personal_availability_booking_reopens_slot_for_another_student(
        self,
        club,
        owner_user,
    ):
        target_date = _future_date_for_weekday(1)
        starts_at = _aware_datetime(target_date, time(10, 0))
        ends_at = _aware_datetime(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        first_student = StudentFactory(club=club, status="active")
        second_student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=first_student, tariff=tariff)
        SubscriptionFactory(club=club, student=second_student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=starts_at,
            ends_at=ends_at,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        first_booking = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=first_student.id,
            actor_user_id=owner_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        )

        result = cancel_personal_booking(
            club_id=club.id,
            enrollment_id=first_booking.enrollment.id,
            actor_user_id=owner_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            reason="Student changed plans",
        )

        result.enrollment.refresh_from_db()
        first_booking.schedule.refresh_from_db()
        slot.refresh_from_db()
        assert result.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert first_booking.schedule.is_active is False
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot.booked_enrollment_id is None

        second_booking = book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=second_student.id,
            actor_user_id=owner_user.id,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        )

        assert second_booking.created is True
        assert second_booking.enrollment.student_id == second_student.id
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == second_booking.enrollment.id

    def test_cancel_booking_rejects_after_checkin(self, club, owner_user):
        target_date = _future_date_for_weekday(0)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        booking = book_guest_group_visit(
            club_id=club.id,
            schedule_id=schedule.id,
            student_id=student.id,
            target_date=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=training_type,
            date=target_date,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_guest_booking(
                club_id=club.id,
                enrollment_id=booking.enrollment.id,
                actor_user_id=owner_user.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            )

        assert exc_info.value.code == "booking_already_checked_in"
        booking.enrollment.refresh_from_db()
        assert booking.enrollment.status == ScheduleEnrollment.Status.ACTIVE

    def test_cancel_guest_booking_rejects_personal_booking(self, club, owner_user):
        target_date = _future_date_for_weekday(1)
        starts_at = datetime.combine(target_date, time(10, 0))
        ends_at = datetime.combine(target_date, time(11, 0))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status="active")
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        booking = book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            subscription_id=subscription.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_guest_booking(
                club_id=club.id,
                enrollment_id=booking.enrollment.id,
                actor_user_id=owner_user.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            )

        assert exc_info.value.code == "booking_type_mismatch"


@pytest.mark.django_db
class TestPersonalAvailabilityCalendarServices:
    def test_generate_personal_availability_slots_creates_requested_weekdays(self, club):
        monday = _future_date_for_weekday(0)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)

        result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=trainer.id,
            date_from=monday,
            date_to=monday + timedelta(days=6),
            weekdays=[0, 2],
            start_time=time(10, 0),
            end_time=time(11, 0),
            location_id=location.id,
            training_type_id=training_type.id,
        )

        assert len(result.created) == 2
        assert result.skipped == []
        assert [slot.starts_at.date().weekday() for slot in result.created] == [0, 2]
        assert {slot.status for slot in result.created} == {PersonalAvailabilitySlot.Status.PUBLISHED}

    def test_generate_personal_availability_slots_uses_club_timezone(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)

        result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=trainer.id,
            date_from=target_date,
            date_to=target_date,
            weekdays=[target_date.weekday()],
            start_time=time(10, 0),
            end_time=time(11, 0),
            location_id=location.id,
            training_type_id=training_type.id,
        )

        assert result.skipped == []
        slot = result.created[0]
        club_tz = ZoneInfo("Asia/Yekaterinburg")
        assert timezone.localtime(slot.starts_at, club_tz).time() == time(10, 0)
        assert timezone.localtime(slot.ends_at, club_tz).time() == time(11, 0)
        assert timezone.localtime(slot.starts_at, ZoneInfo("Europe/Moscow")).time() == time(8, 0)

    def test_generate_personal_availability_slots_slices_window_with_buffer(self, club):
        target_date = _future_date_for_weekday(4)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)

        result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=trainer.id,
            date_from=target_date,
            date_to=target_date,
            weekdays=[target_date.weekday()],
            start_time=time(10, 0),
            end_time=time(12, 0),
            slot_duration_minutes=45,
            buffer_minutes=15,
            location_id=location.id,
            training_type_id=training_type.id,
        )

        assert result.skipped == []
        assert [(slot.starts_at.time(), slot.ends_at.time()) for slot in result.created] == [
            (time(10, 0), time(10, 45)),
            (time(11, 0), time(11, 45)),
        ]

    def test_generate_personal_availability_slots_skips_schedule_and_slot_overlaps(self, club):
        first_date = timezone.localdate() + timedelta(days=10)
        second_date = first_date + timedelta(days=1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=first_date,
            start_time=time(10, 30),
            end_time=time(11, 30),
        )
        PersonalAvailabilitySlotFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(second_date, time(10, 15)),
            ends_at=_aware_datetime(second_date, time(10, 45)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=trainer.id,
            date_from=first_date,
            date_to=second_date,
            weekdays=[first_date.weekday(), second_date.weekday()],
            start_time=time(10, 0),
            end_time=time(11, 0),
            location_id=location.id,
            training_type_id=training_type.id,
        )

        assert result.created == []
        assert [skip.reason_code for skip in result.skipped] == ["schedule_overlap", "slot_overlap"]

    def test_generate_personal_availability_slots_respects_schedule_exceptions(self, club):
        target_date = _future_date_for_weekday(0)
        trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        TrainerLocationFactory(club=club, trainer=substitute, location=location)
        cancelled_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=group_type,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        ScheduleExceptionFactory(
            schedule=cancelled_schedule,
            date=target_date,
            exception_type=ScheduleException.ExceptionType.CANCELLED,
        )
        substitute_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=group_type,
            day_of_week=target_date.weekday(),
            start_time=time(12, 0),
            end_time=time(13, 0),
        )
        ScheduleExceptionFactory(
            schedule=substitute_schedule,
            date=target_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
            substitute_trainer=substitute,
        )

        cancelled_result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=trainer.id,
            date_from=target_date,
            date_to=target_date,
            weekdays=[target_date.weekday()],
            start_time=time(10, 0),
            end_time=time(11, 0),
            location_id=location.id,
            training_type_id=personal_type.id,
        )
        substitute_result = generate_personal_availability_slots(
            club_id=club.id,
            trainer_id=substitute.id,
            date_from=target_date,
            date_to=target_date,
            weekdays=[target_date.weekday()],
            start_time=time(12, 0),
            end_time=time(13, 0),
            location_id=location.id,
            training_type_id=personal_type.id,
        )

        assert len(cancelled_result.created) == 1
        assert cancelled_result.skipped == []
        assert substitute_result.created == []
        assert [skip.reason_code for skip in substitute_result.skipped] == ["schedule_overlap"]

    def test_generate_personal_availability_slots_rejects_group_training_type(self, club):
        target_date = _future_date_for_weekday(1)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)

        with pytest.raises(BusinessLogicError) as exc_info:
            generate_personal_availability_slots(
                club_id=club.id,
                trainer_id=trainer.id,
                date_from=target_date,
                date_to=target_date,
                weekdays=[target_date.weekday()],
                start_time=time(10, 0),
                end_time=time(11, 0),
                location_id=location.id,
                training_type_id=group_type.id,
            )

        assert exc_info.value.code == "personal_availability_requires_personal_type"

    def test_block_unblock_and_cancel_personal_availability_slot(self, club):
        target_date = _future_date_for_weekday(2)
        slot = PersonalAvailabilitySlotFactory(
            club=club,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        blocked = block_personal_availability_slot(
            club_id=club.id,
            trainer_id=slot.trainer_id,
            slot_id=slot.id,
            reason="doctor",
        )
        assert blocked.status == PersonalAvailabilitySlot.Status.BLOCKED
        assert blocked.block_reason == "doctor"

        unblocked = unblock_personal_availability_slot(
            club_id=club.id,
            trainer_id=slot.trainer_id,
            slot_id=slot.id,
        )
        assert unblocked.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert unblocked.block_reason == ""

        cancelled = cancel_personal_availability_slot(
            club_id=club.id,
            trainer_id=slot.trainer_id,
            slot_id=slot.id,
        )
        assert cancelled.status == PersonalAvailabilitySlot.Status.CANCELLED

    def test_block_personal_availability_slot_rejects_reserved_slot(self, club):
        target_date = _future_date_for_weekday(3)
        slot = PersonalAvailabilitySlotFactory(
            club=club,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.HELD,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            block_personal_availability_slot(
                club_id=club.id,
                trainer_id=slot.trainer_id,
                slot_id=slot.id,
            )

        assert exc_info.value.code == "personal_availability_slot_reserved"


@pytest.mark.django_db
class TestTrainingGroupMembershipRoster:
    @pytest.fixture(autouse=True)
    def _enable_training_group_new_writes_for_membership_roster(self, settings, club):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club),
            mode=TrainingGroupRolloutState.Mode.SHADOW,
            reconciling_from_mode="",
        )

    def _mapped_group(self, *, club, target_date):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        group = TrainingGroup.objects.create(
            club=club,
            name="Canonical adults",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        first_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
            location=location,
            trainer=trainer,
            training_group=group,
        )
        second_schedule = ScheduleFactory(
            club=club,
            day_of_week=(target_date.weekday() + 2) % 7,
            training_type=training_type,
            location=location,
            trainer=trainer,
            training_group=group,
        )
        return group, first_schedule, second_schedule

    def test_membership_creates_all_slot_projections_idempotently(self, club):
        target_date = date(2026, 8, 3)
        group, first_schedule, second_schedule = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        first = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-1",
        )
        retry = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-1",
        )

        assert retry.id == first.id
        projections = ScheduleEnrollment.objects.for_club(club).filter(training_group_membership=first)
        assert set(projections.values_list("schedule_id", flat=True)) == {first_schedule.id, second_schedule.id}
        assert all(
            source == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
            for source in projections.values_list("created_from", flat=True)
        )
        assert TrainingGroupMembershipEvent.objects.for_club(club).filter(membership=first).count() == 1

    def test_frozen_membership_is_visible_and_blocks_every_slot_without_booking_bypass(self, club):
        target_date = date(2026, 8, 3)
        group, first_schedule, second_schedule = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-freeze",
        )
        freeze_training_group_membership(
            membership_id=membership.id,
            club_id=club.id,
            actor_user_id=None,
            rationale="temporary freeze",
            idempotency_key="group-member-freeze-action",
        )

        first_roster = resolve_expected_roster_for_schedule_date(
            club=club,
            schedule_id=first_schedule.id,
            target_date=target_date,
        )
        second_date = target_date + timedelta(days=2)
        second_roster = resolve_expected_roster_for_schedule_date(
            club=club,
            schedule_id=second_schedule.id,
            target_date=second_date,
        )

        assert first_roster[student.id].blocked_reason == "training_group_membership_frozen"
        assert second_roster[student.id].blocked_reason == "training_group_membership_frozen"

    def test_reactivated_slot_fans_out_before_it_is_visible(self, club):
        target_date = date(2026, 8, 3)
        group, first_schedule, second_schedule = self._mapped_group(club=club, target_date=target_date)
        update_schedule(
            club_id=club.id,
            schedule_id=second_schedule.id,
            is_active=False,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-reactivation",
        )
        assert set(
            ScheduleEnrollment.objects.for_club(club)
            .filter(training_group_membership=membership)
            .values_list("schedule_id", flat=True)
        ) == {first_schedule.id}

        update_schedule(
            club_id=club.id,
            schedule_id=second_schedule.id,
            is_active=True,
        )

        assert set(
            ScheduleEnrollment.objects.for_club(club)
            .filter(training_group_membership=membership)
            .values_list("schedule_id", flat=True)
        ) == {first_schedule.id, second_schedule.id}

    def test_roster_comparison_returns_redacted_evidence_and_fails_closed(self, club):
        target_date = date(2026, 8, 3)
        group, first_schedule, _ = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-comparison",
        )

        evidence = assert_expected_roster_comparison(
            club=club,
            schedule_id=first_schedule.id,
            target_date=target_date,
            expected_student_ids={student.id},
            mode="owner_delta",
        )
        assert evidence["student_count"] == 1
        assert "student_ids_digest" in evidence
        with pytest.raises(BusinessLogicError, match="Expected roster comparison failed") as exc_info:
            assert_expected_roster_comparison(
                club=club,
                schedule_id=first_schedule.id,
                target_date=target_date,
                expected_student_ids=set(),
                mode="owner_delta",
            )
        assert exc_info.value.code == "training_group_owner_delta_mismatch"

    def test_membership_lifecycle_uses_club_date_and_rejects_invalid_transitions(self, club):
        target_date = date(2026, 8, 3)
        group, _first_schedule, _second_schedule = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-transition-create",
        )

        club_local_today = date(2026, 8, 10)
        with patch(
            "apps.attendance.services.training_group_memberships.club_localdate",
            return_value=club_local_today,
        ):
            frozen = freeze_training_group_membership(
                membership_id=membership.id,
                club_id=club.id,
                actor_user_id=None,
                rationale="temporary freeze",
                idempotency_key="group-member-transition-freeze",
            )
        assert frozen.status == TrainingGroupMembership.Status.FROZEN
        assert frozen.events.get(action="frozen").effective_date == club_local_today
        with pytest.raises(BusinessLogicError) as exc_info:
            freeze_training_group_membership(
                membership_id=membership.id,
                club_id=club.id,
                actor_user_id=None,
                rationale="repeat freeze",
                idempotency_key="group-member-transition-repeat-freeze",
            )
        assert exc_info.value.code == "training_group_membership_freeze_transition_invalid"

        unfrozen = unfreeze_training_group_membership(
            membership_id=membership.id,
            club_id=club.id,
            actor_user_id=None,
            rationale="return to training",
            idempotency_key="group-member-transition-unfreeze",
        )
        assert unfrozen.status == TrainingGroupMembership.Status.ACTIVE
        with pytest.raises(BusinessLogicError) as exc_info:
            unfreeze_training_group_membership(
                membership_id=membership.id,
                club_id=club.id,
                actor_user_id=None,
                rationale="repeat unfreeze",
                idempotency_key="group-member-transition-repeat-unfreeze",
            )
        assert exc_info.value.code == "training_group_membership_unfreeze_transition_invalid"

    def test_create_idempotency_rejects_changed_source_or_payment_owned_authority(self, club):
        target_date = date(2026, 8, 3)
        group, _first_schedule, _second_schedule = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="owner admission",
            idempotency_key="group-member-idempotency-create",
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            create_training_group_membership(
                club_id=club.id,
                student_id=student.id,
                training_group_id=group.id,
                starts_on=target_date,
                source=TrainingGroupMembership.Source.IMPORT,
                rationale="changed source retry",
                idempotency_key="group-member-idempotency-create",
            )
        assert exc_info.value.code == "training_group_membership_idempotency_conflict"

        payment_owned_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_training_group_membership(
                club_id=club.id,
                student_id=payment_owned_student.id,
                training_group_id=group.id,
                starts_on=target_date,
                source=TrainingGroupMembership.Source.PAID_CONVERSION,
                authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
                rationale="unsafe direct payment membership",
                idempotency_key="group-member-payment-owned",
            )
        assert exc_info.value.code == "training_group_payment_ownership_required"
        assert not TrainingGroupMembership.objects.for_club(club).filter(student=payment_owned_student).exists()

    def test_terminal_group_projection_is_never_independent_roster_authority(self, club):
        from apps.attendance.training_group_roster import _enrollment_is_effective

        target_date = date(2026, 8, 3)
        group, schedule, _second_schedule = self._mapped_group(club=club, target_date=target_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=target_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="projection classification",
            idempotency_key="group-member-terminal-projection",
        )
        projection = ScheduleEnrollment.objects.for_club(club).get(
            training_group_membership=membership,
            schedule=schedule,
        )
        projection.status = ScheduleEnrollment.Status.CANCELLED
        projection.ends_on = target_date
        assert not _enrollment_is_effective(projection, target_date=target_date)

    def test_audit_horizon_is_exact_bounded_and_comparison_is_redacted(self, club):
        as_of_date = date(2026, 8, 3)
        group, schedule, second_schedule = self._mapped_group(club=club, target_date=as_of_date)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=group.id,
            starts_on=as_of_date,
            source=TrainingGroupMembership.Source.MANUAL,
            rationale="audit horizon membership",
            idempotency_key="group-member-audit-horizon",
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=schedule.training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            date=date(2026, 7, 27),
        )
        GroupSessionFactory(
            club=club,
            schedule=schedule,
            trainer=schedule.trainer,
            date=date(2026, 7, 20),
        )
        debt_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=schedule.training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            date=date(2026, 7, 13),
        )
        DebtFactory(club=club, student=student, checkin=debt_checkin)
        PaymentFactory(
            club=club,
            student=student,
            tariff=TariffFactory(club=club, training_type=schedule.training_type),
            target_schedule=schedule,
            target_start_date=date(2026, 8, 17),
        )

        horizon = build_training_group_roster_audit_horizon(
            club=club,
            schedule_ids=[schedule.id],
            as_of_date=as_of_date,
        )
        assert horizon == {
            schedule.id: (
                date(2026, 7, 13),
                date(2026, 7, 20),
                date(2026, 7, 27),
                date(2026, 8, 3),
                date(2026, 8, 10),
                date(2026, 8, 17),
                date(2026, 8, 24),
                date(2026, 8, 31),
                date(2026, 9, 7),
                date(2026, 9, 14),
                date(2026, 9, 21),
            ),
            second_schedule.id: (
                date(2026, 7, 29),
                date(2026, 8, 5),
                date(2026, 8, 12),
                date(2026, 8, 19),
                date(2026, 8, 26),
                date(2026, 9, 2),
                date(2026, 9, 9),
                date(2026, 9, 16),
                date(2026, 9, 23),
            ),
        }
        expected_by_slot = {
            (schedule_id, target_date): set(
                resolve_expected_roster_for_schedule_date(
                    club=club,
                    schedule_id=schedule_id,
                    target_date=target_date,
                )
            )
            for schedule_id, dates in horizon.items()
            for target_date in dates
        }
        evidence = assert_expected_roster_horizon_comparison(
            club=club,
            schedule_ids=[schedule.id],
            as_of_date=as_of_date,
            expected_student_ids_by_slot=expected_by_slot,
            mode="legacy_parity",
        )
        assert evidence["slot_count"] == sum(len(dates) for dates in horizon.values())
        assert "horizon_digest" in evidence
        expected_by_slot[(schedule.id, as_of_date)] = set()
        with pytest.raises(BusinessLogicError) as exc_info:
            assert_expected_roster_horizon_comparison(
                club=club,
                schedule_ids=[schedule.id],
                as_of_date=as_of_date,
                expected_student_ids_by_slot=expected_by_slot,
                mode="legacy_parity",
            )
        assert exc_info.value.code == "training_group_legacy_parity_mismatch"
        with pytest.raises(BusinessLogicError) as exc_info:
            assert_expected_roster_comparison(
                club=club,
                schedule_id=schedule.id,
                target_date=as_of_date,
                expected_student_ids=set(),
                mode="unsafe_mode",
            )
        assert exc_info.value.code == "training_group_roster_comparison_mode_invalid"


@pytest.mark.django_db
class TestExpectedScheduleRoster:
    def test_expected_roster_includes_valid_enrollments_and_legacy_without_enrollment(self, club):
        target_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club)
        active_student = StudentFactory(club=club, status="active")
        trial_student = StudentFactory(club=club, status="active")
        frozen_student = StudentFactory(club=club, status="active")
        legacy_student = StudentFactory(club=club, status="active")

        for student, status in (
            (active_student, ScheduleEnrollment.Status.ACTIVE),
            (trial_student, ScheduleEnrollment.Status.TRIAL),
            (frozen_student, ScheduleEnrollment.Status.FROZEN),
        ):
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                status=status,
                starts_on=target_date,
            )
        CheckinFactory(
            club=club,
            student=legacy_student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_date - timedelta(days=7),
        )

        student_ids = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert student_ids == {
            active_student.id,
            trial_student.id,
            frozen_student.id,
            legacy_student.id,
        }

    def test_expected_roster_suppresses_legacy_when_enrollment_cancelled_or_ended(self, club):
        target_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club)
        cancelled_student = StudentFactory(club=club, status="active")
        ended_student = StudentFactory(club=club, status="active")

        for student in (cancelled_student, ended_student):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                trainer=schedule.trainer,
                location=schedule.location,
                date=target_date - timedelta(days=7),
            )
        ScheduleEnrollment.objects.create(
            club=club,
            student=cancelled_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=target_date - timedelta(days=30),
            ends_on=target_date - timedelta(days=1),
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=ended_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date - timedelta(days=30),
            ends_on=target_date - timedelta(days=1),
        )

        student_ids = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert student_ids == set()

    def test_expected_roster_keeps_tenant_isolation(self, club, other_club):
        target_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club)
        own_student = StudentFactory(club=club, status="active")
        other_schedule = ScheduleFactory(club=other_club)
        other_student = StudentFactory(club=other_club, status="active")

        enroll_student_in_schedule(
            club_id=club.id,
            student_id=own_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        enroll_student_in_schedule(
            club_id=other_club.id,
            student_id=other_student.id,
            schedule_id=other_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        CheckinFactory(
            club=other_club,
            student=other_student,
            schedule=other_schedule,
            trainer=other_schedule.trainer,
            location=other_schedule.location,
            date=target_date - timedelta(days=7),
        )

        student_ids = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert student_ids == {own_student.id}

    def test_expected_roster_allows_enrolled_churned_but_excludes_lost_students(self, club):
        target_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club)
        active_student = StudentFactory(club=club, status="active")
        lead_student = StudentFactory(club=club, status="lead")
        churned_student = StudentFactory(club=club, status="churned")
        legacy_churned_student = StudentFactory(club=club, status="churned")
        lost_student = StudentFactory(club=club, status="lost")

        for student in (active_student, lead_student, churned_student, lost_student):
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                status=ScheduleEnrollment.Status.TRIAL,
                starts_on=target_date,
            )
        CheckinFactory(
            club=club,
            student=legacy_churned_student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_date - timedelta(days=7),
        )

        student_ids = get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert student_ids == {active_student.id, lead_student.id, churned_student.id}


@pytest.mark.django_db
class TestUnclosedSessionsExpectedRoster:
    def test_enrolled_student_checkin_does_not_close_without_explicit_session_review(self, club):
        session_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        student = StudentFactory(club=club, status="active")
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=session_date,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=session_date,
        )

        assert get_unclosed_sessions(club=club, session_date=session_date) == [schedule]

        GroupSession.objects.create(
            club=club,
            schedule=schedule,
            date=session_date,
            trainer=schedule.trainer,
            attendee_count=1,
            closed_at=timezone.now(),
        )

        assert get_unclosed_sessions(club=club, session_date=session_date) == []

    def test_enrolled_active_and_trial_students_without_checkins_keep_session_unclosed(self, club):
        session_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        active_student = StudentFactory(club=club, status="active")
        trial_student = StudentFactory(club=club, status="trial")

        enroll_student_in_schedule(
            club_id=club.id,
            student_id=active_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=session_date,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=trial_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=session_date,
        )

        unclosed = get_unclosed_sessions(club=club, session_date=session_date)

        assert [item.id for item in unclosed] == [schedule.id]

    def test_legacy_checkin_students_still_keep_session_unclosed(self, club):
        session_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        legacy_student = StudentFactory(club=club, status="active")
        CheckinFactory(
            club=club,
            student=legacy_student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=session_date - timedelta(days=7),
        )

        unclosed = get_unclosed_sessions(club=club, session_date=session_date)

        assert [item.id for item in unclosed] == [schedule.id]


@pytest.mark.django_db
class TestCancelSession:
    def test_cancel_session(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        exc = cancel_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=session_date,
            reason="Holiday",
        )
        assert isinstance(exc, ScheduleException)
        assert exc.exception_type == "cancelled"
        assert exc.reason == "Holiday"
        assert exc.date == session_date

    def test_cancel_session_duplicate(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        cancel_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=session_date,
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
            )
        assert exc_info.value.code == "duplicate_exception"

    def test_cancel_session_rejects_past_session(self, club):
        schedule = ScheduleFactory(club=club)
        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=timezone.localdate() - timedelta(days=1),
            )

        assert exc_info.value.code == "schedule_session_past"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_cancel_session_rejects_live_checkins(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        CheckinFactory(
            club=club,
            schedule=schedule,
            training_type=schedule.training_type,
            date=session_date,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                reason="Trainer sick",
            )

        assert exc_info.value.code == "schedule_session_has_checkins"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule, date=session_date).exists()

    def test_cancel_session_rejects_non_occurring_schedule_date(self, club):
        schedule = ScheduleFactory(club=club, day_of_week=0)
        session_date = _future_date_for_weekday(1)

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                reason="Wrong date",
            )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_cancel_session_rejects_inactive_schedule_date(self, club):
        schedule = ScheduleFactory(club=club, is_active=False)
        session_date = _future_date_for_weekday(schedule.day_of_week)

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                reason="Inactive schedule",
            )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()


@pytest.mark.django_db
class TestRescheduleSession:
    def test_reschedule_session(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        exc = reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=session_date,
            new_date=session_date + timedelta(days=1),
            new_start_time=time(14, 0),
            new_end_time=time(15, 0),
        )
        assert exc.exception_type == "rescheduled"
        assert exc.new_date == session_date + timedelta(days=1)
        assert exc.new_start_time == time(14, 0)
        assert exc.new_end_time == time(15, 0)

    @pytest.mark.parametrize(
        ("new_start_time", "new_end_time"),
        [
            (time(14, 0), time(14, 0)),
            (time(15, 0), time(14, 0)),
        ],
    )
    def test_reschedule_session_rejects_unsupported_time_range(
        self,
        club,
        new_start_time,
        new_end_time,
    ):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)

        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                new_date=session_date + timedelta(days=1),
                new_start_time=new_start_time,
                new_end_time=new_end_time,
            )

        assert exc_info.value.code == "invalid_schedule_time_range"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_reschedule_session_rejects_another_natural_occurrence_date(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)

        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                new_date=session_date + timedelta(days=7),
                new_start_time=time(14, 0),
                new_end_time=time(15, 0),
            )

        assert exc_info.value.code == "schedule_occurrence_conflict"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_reschedule_session_rejects_past_session(self, club):
        schedule = ScheduleFactory(club=club)
        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=timezone.localdate() - timedelta(days=1),
                new_date=timezone.localdate() + timedelta(days=1),
                new_start_time=time(14, 0),
                new_end_time=time(15, 0),
            )

        assert exc_info.value.code == "schedule_session_past"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_reschedule_session_rejects_live_checkins(self, club):
        schedule = ScheduleFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        CheckinFactory(
            club=club,
            schedule=schedule,
            training_type=schedule.training_type,
            date=session_date,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                new_date=session_date + timedelta(days=1),
                new_start_time=time(14, 0),
                new_end_time=time(15, 0),
            )

        assert exc_info.value.code == "schedule_session_has_checkins"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule, date=session_date).exists()

    def test_reschedule_session_rejects_non_occurring_source_date(self, club):
        schedule = ScheduleFactory(club=club, day_of_week=0)
        session_date = _future_date_for_weekday(1)

        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                new_date=session_date + timedelta(days=1),
                new_start_time=time(14, 0),
                new_end_time=time(15, 0),
            )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()


@pytest.mark.django_db
class TestSubstituteTrainer:
    def test_substitute_trainer(self, club):
        schedule = ScheduleFactory(club=club)
        sub_trainer = TrainerFactory(club=club)
        session_date = _future_date_for_weekday(schedule.day_of_week)
        exc = substitute_trainer(
            club_id=club.id,
            schedule_id=schedule.id,
            date=session_date,
            substitute_trainer_id=sub_trainer.id,
        )
        assert exc.exception_type == "substitute"
        assert exc.substitute_trainer_id == sub_trainer.id

    def test_substitute_trainer_allows_rescheduled_session_new_date(self, club):
        schedule = ScheduleFactory(club=club, day_of_week=0)
        sub_trainer = TrainerFactory(club=club)
        old_date = _future_date_for_weekday(0)
        new_date = _future_date_for_weekday(1)
        reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=old_date,
            new_date=new_date,
            new_start_time=time(14, 0),
            new_end_time=time(15, 0),
        )

        exc = substitute_trainer(
            club_id=club.id,
            schedule_id=schedule.id,
            date=new_date,
            substitute_trainer_id=sub_trainer.id,
        )

        assert exc.exception_type == "substitute"
        assert exc.date == new_date
        assert exc.substitute_trainer_id == sub_trainer.id

    def test_substitute_trainer_rejects_non_occurring_schedule_date(self, club):
        schedule = ScheduleFactory(club=club, day_of_week=0)
        sub_trainer = TrainerFactory(club=club)
        session_date = _future_date_for_weekday(1)

        with pytest.raises(BusinessLogicError) as exc_info:
            substitute_trainer(
                club_id=club.id,
                schedule_id=schedule.id,
                date=session_date,
                substitute_trainer_id=sub_trainer.id,
            )

        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule).exists()

    def test_substitute_trainer_wrong_club(self, club):
        other_club = ClubFactory()
        schedule = ScheduleFactory(club=club)
        sub_trainer = TrainerFactory(club=other_club)
        with pytest.raises(BusinessLogicError) as exc_info:
            substitute_trainer(
                club_id=club.id,
                schedule_id=schedule.id,
                date=date(2026, 4, 1),
                substitute_trainer_id=sub_trainer.id,
            )
        assert exc_info.value.code == "trainer_club_mismatch"


@pytest.mark.django_db
class TestSelectors:
    def test_get_schedules_tenant_isolation(self, club, other_club):
        ScheduleFactory(club=club)
        ScheduleFactory(club=other_club)
        schedules = get_schedules(club=club)
        assert schedules.count() == 1
        assert schedules.first().club_id == club.id

    def test_get_today_sessions(self, club):
        today = date(2026, 4, 6)  # Monday = weekday 0
        schedule_today = ScheduleFactory(club=club, day_of_week=0)
        ScheduleFactory(club=club, day_of_week=2)  # Wednesday, not today

        sessions = get_today_sessions(club=club, today=today)
        assert sessions.count() == 1
        assert sessions.first().id == schedule_today.id

    def test_get_today_sessions_excludes_cancelled(self, club):
        today = date(2026, 4, 6)  # Monday = weekday 0
        schedule = ScheduleFactory(club=club, day_of_week=0)
        ScheduleExceptionFactory(
            schedule=schedule,
            date=today,
            exception_type="cancelled",
        )

        sessions = get_today_sessions(club=club, today=today)
        assert sessions.count() == 0

    def test_get_schedule_occurrences_hides_rescheduled_session_on_old_date(self, club):
        old_date = date(2026, 4, 6)
        new_date = date(2026, 4, 7)
        schedule = ScheduleFactory(club=club, day_of_week=0, start_time=time(18, 0), end_time=time(19, 0))
        ScheduleExceptionFactory(
            schedule=schedule,
            date=old_date,
            exception_type="rescheduled",
            new_date=new_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )

        occurrences = get_schedule_occurrences_for_date(club=club, target_date=old_date)

        assert occurrences == []

    def test_get_schedule_occurrences_materializes_rescheduled_session_on_new_date(self, club):
        old_date = date(2026, 4, 6)
        new_date = date(2026, 4, 7)
        schedule = ScheduleFactory(club=club, day_of_week=0, start_time=time(18, 0), end_time=time(19, 0))
        ScheduleExceptionFactory(
            schedule=schedule,
            date=old_date,
            exception_type="rescheduled",
            new_date=new_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )

        occurrences = get_schedule_occurrences_for_date(club=club, target_date=new_date)

        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence.schedule_id == schedule.id
        assert occurrence.effective_date == new_date
        assert occurrence.effective_start_time == time(20, 0)
        assert occurrence.effective_end_time == time(21, 0)
        assert occurrence.is_rescheduled is True

    def test_get_schedule_occurrences_materializes_rescheduled_session_with_substitute_on_new_date(self, club):
        old_date = date(2026, 4, 6)
        new_date = date(2026, 4, 7)
        owner_trainer = TrainerFactory(club=club)
        substitute_trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            trainer=owner_trainer,
            day_of_week=0,
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=old_date,
            exception_type="rescheduled",
            new_date=new_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=new_date,
            exception_type="substitute",
            substitute_trainer=substitute_trainer,
        )

        occurrences = get_schedule_occurrences_for_date(club=club, target_date=new_date)

        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence.schedule_id == schedule.id
        assert occurrence.effective_date == new_date
        assert occurrence.effective_start_time == time(20, 0)
        assert occurrence.effective_end_time == time(21, 0)
        assert occurrence.trainer_id == substitute_trainer.id
        assert occurrence.is_rescheduled is True
        assert occurrence.is_substitute is True

    def test_get_schedule_occurrences_uses_substitute_trainer_for_date(self, club):
        owner_trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(club=club)
        session_date = date(2026, 4, 6)
        schedule = ScheduleFactory(club=club, trainer=owner_trainer, day_of_week=0)
        ScheduleExceptionFactory(
            schedule=schedule,
            date=session_date,
            exception_type="substitute",
            substitute_trainer=substitute,
        )

        owner_occurrences = get_schedule_occurrences_for_date(
            club=club,
            target_date=session_date,
            trainer_id=owner_trainer.id,
        )
        substitute_occurrences = get_schedule_occurrences_for_date(
            club=club,
            target_date=session_date,
            trainer_id=substitute.id,
        )

        assert owner_occurrences == []
        assert len(substitute_occurrences) == 1
        assert substitute_occurrences[0].trainer_id == substitute.id

    def test_get_schedule_occurrences_for_range_combines_dates(self, club):
        monday = date(2026, 4, 6)
        tuesday = date(2026, 4, 7)
        schedule = ScheduleFactory(club=club, day_of_week=0)
        ScheduleExceptionFactory(
            schedule=schedule,
            date=monday,
            exception_type="rescheduled",
            new_date=tuesday,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )

        occurrences_by_date = get_schedule_occurrences_for_range(
            club=club,
            date_from=monday,
            date_to=tuesday,
        )

        assert set(occurrences_by_date.keys()) == {monday, tuesday}
        assert occurrences_by_date[monday] == []
        assert len(occurrences_by_date[tuesday]) == 1
        assert occurrences_by_date[tuesday][0].effective_date == tuesday

    def test_get_students_for_schedule_uses_reference_date_window(self, club):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        student_in_window = StudentFactory(club=club, status="active")
        student_outside_window = StudentFactory(club=club, status="active")

        with patch("apps.attendance.selectors.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 4, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

            CheckinFactory(
                club=club,
                student=student_in_window,
                schedule=schedule,
                trainer=trainer,
                location=schedule.location,
                date=date(2026, 3, 10),
            )
            CheckinFactory(
                club=club,
                student=student_outside_window,
                schedule=schedule,
                trainer=trainer,
                location=schedule.location,
                date=date(2026, 2, 20),
            )

            students = get_students_for_schedule(
                club=club,
                schedule_id=schedule.id,
                reference_date=date(2026, 4, 1),
            )

        assert [student["id"] for student in students] == [student_in_window.id]

    def test_get_students_for_schedule_uses_enrollment_before_checkin(self, club):
        schedule = ScheduleFactory(club=club)
        active_student = StudentFactory(club=club, status="active")
        trial_student = StudentFactory(club=club, status="trial")

        enroll_student_in_schedule(
            club_id=club.id,
            student_id=active_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 4, 1),
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=trial_student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=date(2026, 4, 1),
        )

        students = get_students_for_schedule(
            club=club,
            schedule_id=schedule.id,
            reference_date=date(2026, 4, 1),
        )

        assert {student["id"] for student in students} == {active_student.id, trial_student.id}

    def test_get_students_for_schedule_keeps_legacy_checkin_students_during_rollout(self, club):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        legacy_student = StudentFactory(club=club, status="active")
        enrolled_trial = StudentFactory(club=club, status="trial")

        CheckinFactory(
            club=club,
            student=legacy_student,
            schedule=schedule,
            trainer=trainer,
            location=schedule.location,
            date=date(2026, 3, 30),
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=enrolled_trial.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=date(2026, 4, 1),
        )

        students = get_students_for_schedule(
            club=club,
            schedule_id=schedule.id,
            reference_date=date(2026, 4, 1),
        )

        assert {student["id"] for student in students} == {legacy_student.id, enrolled_trial.id}

    def test_get_student_schedule_occurrences_uses_enrollment_before_checkin(self, club):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=0, start_time=time(18, 0), end_time=time(19, 0))
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 4, 6),
        )

        schedules = get_student_schedule(club=club, student_id=student.id)
        occurrences = get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student.id,
            date_from=date(2026, 4, 6),
            date_to=date(2026, 4, 6),
        )

        assert [item.id for item in schedules] == [schedule.id]
        assert len(occurrences) == 1
        assert occurrences[0].schedule_id == schedule.id
        assert occurrences[0].effective_date == date(2026, 4, 6)

    def test_get_student_schedule_includes_upcoming_enrollment(self, club):
        student = StudentFactory(club=club, status="trial")
        schedule = ScheduleFactory(club=club, one_time_date=date(2026, 4, 8))
        legacy_schedule = ScheduleFactory(club=club, day_of_week=0)
        CheckinFactory(
            club=club,
            student=student,
            schedule=legacy_schedule,
            trainer=legacy_schedule.trainer,
            location=legacy_schedule.location,
            date=date(2026, 3, 30),
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=date(2026, 4, 8),
        )

        with patch("apps.attendance.selectors.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 4, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            schedules = get_student_schedule(club=club, student_id=student.id)
            early_occurrences = get_student_schedule_occurrences_for_range(
                club=club,
                student_id=student.id,
                date_from=date(2026, 4, 1),
                date_to=date(2026, 4, 7),
            )
            trial_occurrences = get_student_schedule_occurrences_for_range(
                club=club,
                student_id=student.id,
                date_from=date(2026, 4, 8),
                date_to=date(2026, 4, 8),
            )

        assert {item.id for item in schedules} == {schedule.id, legacy_schedule.id}
        assert {item.schedule_id for item in early_occurrences} == {legacy_schedule.id}
        assert [item.schedule_id for item in trial_occurrences] == [schedule.id]


@pytest.mark.django_db
class TestPendingManualAdmissionCheckin:
    @pytest.fixture(autouse=True)
    def _enable_training_group_new_writes_for_pending_group_admission(self, settings):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True

    def _create_admission(
        self,
        *,
        club,
        owner_user,
        target_start_date,
        duration_days=2,
        trainings_limit=3,
        mapped: bool = False,
    ):
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
        )
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
        )
        if mapped:
            state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
                club=club,
                defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
            )
            update_training_group_rollout_state_for_test(
                TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
                mode=TrainingGroupRolloutState.Mode.SHADOW,
            )
            group = TrainingGroup.objects.create(
                club=club,
                name="Lock order group",
                training_type=training_type,
                location=schedule.location,
                responsible_trainer=schedule.trainer,
                status=TrainingGroup.Status.ACTIVE,
            )
            schedule.training_group = group
            schedule.save(update_fields=["training_group", "updated_at"])
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            trainings_limit=trainings_limit,
            duration_days=duration_days,
        )
        student = StudentFactory(club=club, status="active")
        ClubSettings.objects.update_or_create(
            club=club,
            defaults={
                "unified_client_journey_enabled": True,
                "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
            },
        )
        with override_settings(
            UNIFIED_CLIENT_JOURNEY_ENABLED=True,
            MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        ), patch("django_q.tasks.async_task"):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )
        return student, schedule, tariff, payment

    @patch("apps.attendance.services.async_task")
    def test_exact_pending_checkin_creates_nullable_reserved_debt_and_is_idempotent(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        target_start_date = _future_date_for_weekday(0)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )

        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=tariff.training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_start_date,
        )
        retry = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=tariff.training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_start_date,
        )

        debt = Debt.objects.for_club(club).get(checkin_id=result["checkin_id"])
        assert result["is_debt"] is True
        assert retry == {**result, "created": False}
        assert debt.tariff_price is None
        assert debt.required_tariff_id == tariff.id
        assert debt.settlement_payment_id == payment.id
        assert list(
            debt.settlement_events.filter(payment=payment).values_list("event_type", flat=True)
        ) == ["reserved"]
        assert list(
            debt.lifecycle_events.filter(payment=payment).values_list("event_type", flat=True)
        ) == ["reserved"]

    @patch("apps.attendance.services.async_task")
    def test_pending_admission_rejects_before_start_and_exclusive_expiry_day_without_generic_debt(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        target_start_date = _future_date_for_weekday(0)
        student, schedule, tariff, _payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            duration_days=2,
        )

        for invalid_date in (target_start_date - timedelta(days=1), target_start_date + timedelta(days=2)):
            with pytest.raises(BusinessLogicError):
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=tariff.training_type_id,
                    source=Checkin.Source.MANUAL,
                    checkin_date=invalid_date,
                )

        assert not Checkin.objects.for_club(club).filter(student=student).exists()
        assert not Debt.objects.for_club(club).filter(student=student).exists()

    @patch("apps.attendance.services.async_task")
    def test_batch_pending_admission_locks_complete_scope_in_reviewed_order_and_reconciling_is_side_effect_free(
        self,
        _mock_async,
        club,
        owner_user,
        monkeypatch,
    ):
        target_start_date = _future_date_for_weekday(0)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            mapped=True,
        )
        lock_calls: list[str] = []
        original_select_for_update = QuerySet.select_for_update
        original_create = QuerySet.create

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model in {
                Payment,
                Subscription,
                Trainer,
                Student,
                TrainingGroup,
                Schedule,
                TrainingGroupMembership,
                ScheduleEnrollment,
            }:
                lock_calls.append(queryset.model.__name__)
            return original_select_for_update(queryset, *args, **kwargs)

        def recording_create(queryset, *args, **kwargs):
            if queryset.model is Checkin:
                lock_calls.append("Checkin")
            return original_create(queryset, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
        monkeypatch.setattr(QuerySet, "create", recording_create)
        with (
            patch("apps.attendance.services.checkin._assert_session_close_allowed"),
            CaptureQueriesContext(connection) as queries,
        ):
            result = batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=target_start_date,
                present_student_ids=[student.id],
                training_type_id=tariff.training_type_id,
                actor_user_id=owner_user.id,
            )

        assert result["checkins"][0]["is_debt"] is True
        assert Debt.objects.for_club(club).filter(settlement_payment=payment).count() == 1
        expected_order = [
            "Student",
            "TrainingGroup",
            "Schedule",
            "TrainingGroupMembership",
            "ScheduleEnrollment",
            "Payment",
            "Subscription",
            "Checkin",
        ]
        assert [lock_calls.index(model) for model in expected_order] == sorted(
            lock_calls.index(model) for model in expected_order
        )
        if connection.vendor == "postgresql":
            locked_sql = [
                query["sql"].upper()
                for query in queries.captured_queries
                if "FOR UPDATE" in query["sql"].upper()
            ]
            table_order = [
                '"STUDENTS_STUDENT"',
                '"ATTENDANCE_TRAININGGROUP"',
                '"ATTENDANCE_SCHEDULE"',
                '"ATTENDANCE_TRAININGGROUPMEMBERSHIP"',
                '"ATTENDANCE_SCHEDULEENROLLMENT"',
                '"BILLING_PAYMENT"',
                '"BILLING_SUBSCRIPTION"',
            ]
            assert [
                next(index for index, sql in enumerate(locked_sql) if table_name in sql)
                for table_name in table_order
            ] == sorted(
                next(index for index, sql in enumerate(locked_sql) if table_name in sql)
                for table_name in table_order
            )

        update_training_group_rollout_state_for_test(

            TrainingGroupRolloutState.objects.for_club(club),
            mode=TrainingGroupRolloutState.Mode.RECONCILING,
        )
        before_payment_state = Payment.objects.for_club(club).get(id=payment.id).status
        with pytest.raises(BusinessLogicError) as exc_info:
            batch_checkin(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=target_start_date + timedelta(days=7),
                present_student_ids=[student.id],
                training_type_id=tariff.training_type_id,
                actor_user_id=owner_user.id,
            )
        assert exc_info.value.code == "training_group_reconciling"
        assert Checkin.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            date=target_start_date + timedelta(days=7),
        ).count() == 0
        assert Payment.objects.for_club(club).get(id=payment.id).status == before_payment_state

    @patch("apps.attendance.services.async_task")
    def test_pending_admission_for_another_schedule_does_not_block_exact_schedule(self, _mock_async, club, owner_user):
        target_start_date = _future_date_for_weekday(0)
        student, pending_schedule, tariff, pending_payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        pending_schedule.training_type.drop_in_price = Decimal("500.00")
        pending_schedule.training_type.save(update_fields=["drop_in_price", "updated_at"])
        exact_schedule = ScheduleFactory(
            club=club,
            training_type=pending_schedule.training_type,
            location=pending_schedule.location,
            trainer=pending_schedule.trainer,
            day_of_week=target_start_date.weekday(),
        )

        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=exact_schedule.id,
            training_type_id=tariff.training_type_id,
            source=Checkin.Source.MANUAL,
            checkin_date=target_start_date,
        )

        debt = Debt.objects.for_club(club).get(checkin_id=result["checkin_id"])
        assert result["is_debt"] is True
        assert debt.settlement_payment_id is None
        assert pending_payment.id not in (
            Debt.objects.for_club(club)
            .filter(checkin_id=result["checkin_id"])
            .values_list("settlement_payment_id", flat=True)
        )

    @patch("apps.attendance.services.async_task")
    def test_pending_admission_scope_change_fails_before_checkin_or_debt_write(
        self,
        _mock_async,
        club,
        owner_user,
        monkeypatch,
    ):
        target_start_date = _future_date_for_weekday(0)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        injected = False
        original_select_for_update = QuerySet.select_for_update

        def inject_candidate_after_identity_lock(queryset, *args, **kwargs):
            nonlocal injected
            result = original_select_for_update(queryset, *args, **kwargs)
            if queryset.model is ScheduleEnrollment and not injected:
                injected = True
                PaymentFactory(
                    club=club,
                    student=student,
                    tariff=tariff,
                    conversion_enrollment=payment.conversion_enrollment,
                    target_schedule=schedule,
                    target_start_date=target_start_date,
                    status=Payment.Status.PENDING,
                )
            return result

        monkeypatch.setattr(QuerySet, "select_for_update", inject_candidate_after_identity_lock)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source=Checkin.Source.KIOSK,
                checkin_date=target_start_date,
            )
        assert exc_info.value.code == "pending_manual_admission_scope_changed"
        assert not Checkin.objects.for_club(club).filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.for_club(club).filter(student=student).exists()

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL CI suite",
    )
    def test_postgresql_concurrent_pending_checkins_reserve_at_most_one_finite_credit(
        self,
        club,
        owner_user,
    ):
        target_start_date = _future_date_for_weekday(0)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            duration_days=14,
            trainings_limit=1,
        )
        gate = Barrier(2)

        def submit(checkin_date):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                create_checkin(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    training_type_id=tariff.training_type_id,
                    source=Checkin.Source.KIOSK,
                    checkin_date=checkin_date,
                )
                return "reserved"
            except BusinessLogicError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    submit,
                    (target_start_date, target_start_date + timedelta(days=7)),
                )
            )

        assert results.count("reserved") == 1
        assert results.count("subscription_component_limit_exceeded") == 1
        assert Debt.objects.for_club(club).filter(settlement_payment=payment).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S4 suite",
)
def test_debt_reservation_lock_order_contract(club, other_club, owner_user, monkeypatch):
    """Freeze the pending-admission debt reservation transaction and lock sequence."""
    from apps.attendance.services import checkin as checkin_service

    target_start_date = _future_date_for_weekday(0)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
        drop_in_price=None,
    )
    schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
        one_time_date=None,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("4000.00"),
        trainings_limit=3,
        duration_days=2,
    )
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    foreign_student = StudentFactory(club=other_club)
    foreign_debt = DebtFactory(
        club=other_club,
        student=foreign_student,
        checkin=CheckinFactory(club=other_club, student=foreign_student),
    )
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
        },
    )
    with override_settings(
        UNIFIED_CLIENT_JOURNEY_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    lock_calls: list[str] = []
    state_rechecks: list[tuple[str, tuple[str, ...]]] = []
    original_select_for_update = QuerySet.select_for_update
    original_pending_scope_recheck = checkin_service._assert_pending_manual_admission_scope_unchanged
    original_financial_scope_recheck = checkin_service._assert_checkin_create_financial_scope_unchanged
    tracked_models = {
        Club,
        Debt,
        TrainingGroupRolloutState,
        Payment,
        Subscription,
        SubscriptionComponent,
        Trainer,
        Student,
        Schedule,
        ScheduleEnrollment,
    }

    def recording_select_for_update(queryset, *args, **kwargs):
        if queryset.model in tracked_models:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    def recording_pending_scope_recheck(*args, **kwargs):
        state_rechecks.append(("pending", tuple(lock_calls)))
        return original_pending_scope_recheck(*args, **kwargs)

    def recording_financial_scope_recheck(*args, **kwargs):
        state_rechecks.append(("financial", tuple(lock_calls)))
        return original_financial_scope_recheck(*args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
    monkeypatch.setattr(
        checkin_service,
        "_assert_pending_manual_admission_scope_unchanged",
        recording_pending_scope_recheck,
    )
    monkeypatch.setattr(
        checkin_service,
        "_assert_checkin_create_financial_scope_unchanged",
        recording_financial_scope_recheck,
    )

    with patch("apps.attendance.services.async_task"), CaptureQueriesContext(connection) as queries:
        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_start_date,
        )

    expected_lock_order = [
        Club,
        TrainingGroupRolloutState,
        Trainer,
        Student,
        Schedule,
        ScheduleEnrollment,
        Payment,
        Subscription,
        SubscriptionComponent,
        Debt,
    ]
    expected_lock_names = [model.__name__ for model in expected_lock_order]
    assert [lock_calls.index(name) for name in expected_lock_names] == sorted(
        lock_calls.index(name) for name in expected_lock_names
    )
    assert [name for name, _locks_at_recheck in state_rechecks] == ["pending", "financial"]
    recheck_lock_names = expected_lock_names[:-1]
    for _name, locks_at_recheck in state_rechecks:
        assert [locks_at_recheck.index(name) for name in recheck_lock_names] == sorted(
            locks_at_recheck.index(name) for name in recheck_lock_names
        )
        assert Debt.__name__ not in locks_at_recheck

    sql = [query["sql"].upper() for query in queries.captured_queries]
    transaction_begin = sql.index("BEGIN")
    transaction_commit = next(
        index
        for index, statement in enumerate(sql[transaction_begin + 1 :], start=transaction_begin + 1)
        if statement == "COMMIT"
    )
    transaction_sql = sql[transaction_begin : transaction_commit + 1]
    locked_sql = [statement for statement in transaction_sql if "FOR UPDATE" in statement]
    assert transaction_sql[0] == "BEGIN"
    assert transaction_sql[-1] == "COMMIT"
    assert locked_sql
    table_order = [f'"{model._meta.db_table.upper()}"' for model in expected_lock_order]
    assert [
        next(index for index, statement in enumerate(locked_sql) if table_name in statement)
        for table_name in table_order
    ] == sorted(
        next(index for index, statement in enumerate(locked_sql) if table_name in statement)
        for table_name in table_order
    )

    debt_table = f'"{Debt._meta.db_table.upper()}"'
    lifecycle_table = f'"{DebtLifecycleEvent._meta.db_table.upper()}"'
    settlement_table = f'"{DebtSettlementEvent._meta.db_table.upper()}"'
    debt_lock_index = next(
        index
        for index, statement in enumerate(transaction_sql)
        if debt_table in statement and "FOR UPDATE" in statement
    )
    debt_insert_index = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {debt_table}")
    )
    lifecycle_insert_index = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {lifecycle_table}")
    )
    settlement_insert_index = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {settlement_table}")
    )
    assert debt_lock_index < debt_insert_index < lifecycle_insert_index < settlement_insert_index

    debt = Debt.objects.for_club(club).get(checkin_id=result["checkin_id"])
    assert result["is_debt"] is True
    assert debt.settlement_payment_id == payment.id
    assert list(debt.lifecycle_events.filter(payment=payment).values_list("event_type", flat=True)) == ["reserved"]
    assert list(debt.settlement_events.filter(payment=payment).values_list("event_type", flat=True)) == ["reserved"]
    foreign_debt.refresh_from_db()
    assert foreign_debt.settlement_payment_id is None


@pytest.mark.django_db
class TestCheckinFinancialLockOrdering:
    def test_cancel_locks_financial_roots_before_checkin(self, club, owner_user, monkeypatch):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(club=club, training_type=training_type, day_of_week=date.today().weekday())
        group = TrainingGroup.objects.create(
            club=club,
            name="Cancellation lock order group",
            training_type=training_type,
            location=schedule.location,
            responsible_trainer=schedule.trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        schedule.training_group = group
        schedule.save(update_fields=["training_group", "updated_at"])
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        TrainingGroupMembership.objects.create(
            club=club,
            student=student,
            training_group=group,
            starts_on=date.today(),
            source=TrainingGroupMembership.Source.MANUAL,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            starts_on=date.today(),
            status=ScheduleEnrollment.Status.ACTIVE,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            subscription=subscription,
            date=date.today(),
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
        )
        DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            settlement_payment=payment,
        )
        lock_calls: list[str] = []
        original_select_for_update = QuerySet.select_for_update

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model in {
                Payment,
                Subscription,
                Trainer,
                Student,
                TrainingGroup,
                Schedule,
                TrainingGroupMembership,
                ScheduleEnrollment,
                Checkin,
            }:
                lock_calls.append(queryset.model.__name__)
            return original_select_for_update(queryset, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
        with (
            patch("apps.attendance.services.async_task"),
            patch("apps.attendance.tasks.reverse_salary"),
            CaptureQueriesContext(connection) as queries,
        ):
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )

        expected_order = [
            "Trainer",
            "Student",
            "TrainingGroup",
            "Schedule",
            "TrainingGroupMembership",
            "ScheduleEnrollment",
            "Payment",
            "Subscription",
            "Checkin",
        ]
        assert [lock_calls.index(model) for model in expected_order] == sorted(
            lock_calls.index(model) for model in expected_order
        )
        if connection.vendor == "postgresql":
            locked_sql = [
                query["sql"].upper()
                for query in queries.captured_queries
                if "FOR UPDATE" in query["sql"].upper()
            ]
            table_order = [
                '"TRAINERS_TRAINER"',
                '"STUDENTS_STUDENT"',
                '"ATTENDANCE_TRAININGGROUP"',
                '"ATTENDANCE_SCHEDULE"',
                '"ATTENDANCE_TRAININGGROUPMEMBERSHIP"',
                '"ATTENDANCE_SCHEDULEENROLLMENT"',
                '"BILLING_PAYMENT"',
                '"BILLING_SUBSCRIPTION"',
                '"ATTENDANCE_CHECKIN"',
            ]
            assert [
                next(index for index, sql in enumerate(locked_sql) if table_name in sql)
                for table_name in table_order
            ] == sorted(
                next(index for index, sql in enumerate(locked_sql) if table_name in sql)
                for table_name in table_order
            )
        checkin.refresh_from_db()
        assert checkin.cancelled_at is not None

    @patch("apps.attendance.services.async_task")
    def test_subscription_backed_create_locks_financial_candidates_before_identity_and_checkin(
        self, _mock_async, club, monkeypatch
    ):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_date.weekday(),
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
        )
        component = SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=TariffComponentFactory(
                club=club,
                tariff=tariff,
                training_type=training_type,
                scope=Tariff.Scope.CLUB,
            ),
            training_type=training_type,
            scope=Tariff.Scope.CLUB,
            location=None,
        )
        lock_calls: list[str] = []
        original_select_for_update = QuerySet.select_for_update

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model in {
                PaymentRefundCase,
                BankPaymentOrder,
                Payment,
                Subscription,
                SubscriptionComponent,
                Trainer,
                Student,
                TrainingGroup,
                Schedule,
                TrainingGroupMembership,
                ScheduleEnrollment,
            }:
                lock_calls.append(queryset.model.__name__)
            return original_select_for_update(queryset, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source=Checkin.Source.MANUAL,
            checkin_date=target_date,
        )

        expected_order = [
            "Trainer",
            "Student",
            "Schedule",
            "Payment",
            "Subscription",
            "SubscriptionComponent",
            "BankPaymentOrder",
            "PaymentRefundCase",
        ]
        assert [lock_calls.index(model) for model in expected_order] == sorted(
            lock_calls.index(model) for model in expected_order
        )
        checkin = Checkin.objects.for_club(club).get(id=result["checkin_id"])
        assert checkin.subscription_id == subscription.id
        assert checkin.subscription_component_id == component.id

    @patch("apps.attendance.services.async_task")
    def test_personal_drop_in_create_locks_preloaded_roots_before_identity_and_checkin(
        self, _mock_async, club, owner_user, monkeypatch
    ):
        target_date = timezone.localdate() + timedelta(days=7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("1000.00"),
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("1000.00"),
            trainings_limit=1,
            scope=Tariff.Scope.CLUB,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=training_type,
            credits_total=1,
            scope=Tariff.Scope.CLUB,
            location=None,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis=Decimal("1000.00"),
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        starts_at = timezone.make_aware(datetime.combine(target_date, time(10, 0)))
        booking = book_personal_drop_in(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            actor_user_id=owner_user.id,
            idempotency_key="personal-lock-order",
        ).booking
        with patch("django_q.tasks.async_task"):
            link = create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="personal-lock-order-payment",
            )
        lock_calls: list[str] = []
        original_select_for_update = QuerySet.select_for_update

        def recording_select_for_update(queryset, *args, **kwargs):
            if queryset.model in {
                PaymentRefundCase,
                BankPaymentOrder,
                Payment,
                Subscription,
                SubscriptionComponent,
                Trainer,
                Student,
                TrainingGroup,
                Schedule,
                TrainingGroupMembership,
                ScheduleEnrollment,
                PersonalDropInBooking,
                PersonalDropInPaymentLink,
                Checkin,
            }:
                lock_calls.append(queryset.model.__name__)
            return original_select_for_update(queryset, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=booking.enrollment.schedule_id,
            training_type_id=training_type.id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )

        expected_order = [
            "Trainer",
            "Student",
            "Schedule",
            "ScheduleEnrollment",
            "PersonalDropInBooking",
            "PersonalDropInPaymentLink",
            "Payment",
            "Subscription",
            "SubscriptionComponent",
            "BankPaymentOrder",
            "PaymentRefundCase",
            "Checkin",
        ]
        assert [lock_calls.index(model) for model in expected_order] == sorted(
            lock_calls.index(model) for model in expected_order
        )
        assert result["is_debt"] is True
        assert link.payment_id == Debt.objects.for_club(club).get(checkin_id=result["checkin_id"]).settlement_payment_id



@pytest.mark.django_db
class TestTrainingGroupOperationalWriterGates:
    @pytest.fixture(autouse=True)
    def _enable_training_group_new_writes_for_existing_writer_scenarios(self, settings):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True

    def _set_rollout_mode(self, *, club, mode: str) -> TrainingGroupRolloutState:
        state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
            club=club,
            defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
            mode=mode)
        return TrainingGroupRolloutState.objects.for_club(club).get(id=state.id)

    def _group_schedule_kwargs(self, *, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        return {
            "club_id": club.id,
            "day_of_week": 1,
            "start_time": time(18, 0),
            "end_time": time(19, 0),
            "group_name": "S4 Canonical Group",
            "trainer_id": trainer.id,
            "location_id": location.id,
            "training_type_id": training_type.id,
        }

    def test_recurring_group_slot_fails_closed_without_rollout_state(self, club):
        TrainingGroupRolloutState.objects.for_club(club).delete()

        with pytest.raises(BusinessLogicError, match="rollout state is missing") as exc_info:
            create_schedule(**self._group_schedule_kwargs(club=club))

        assert exc_info.value.code == "training_group_rollout_state_missing"

    def test_off_mode_keeps_recurring_group_slot_legacy(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.OFF)

        schedule = create_schedule(**self._group_schedule_kwargs(club=club))

        assert schedule.training_group_id is None
        assert TrainingGroup.objects.for_club(club).count() == 0

    def test_switch_false_allows_safe_deactivation_but_rejects_expanding_mapped_writes(
        self, club, settings
    ):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        source_group = TrainingGroup.objects.create(
            club=club,
            name="Existing source canonical group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        target_group = TrainingGroup.objects.create(
            club=club,
            name="Existing target canonical group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        mapped_schedule = ScheduleFactory(
            club=club,
            training_group=source_group,
            trainer=trainer,
            location=location,
            training_type=training_type,
            day_of_week=1,
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        student = StudentFactory(club=club)
        membership = TrainingGroupMembership.objects.create(
            club=club,
            student=student,
            training_group=source_group,
            status=TrainingGroupMembership.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            source=TrainingGroupMembership.Source.MANUAL,
            authority=TrainingGroupMembership.Authority.INDEPENDENT,
        )
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
        schedule_count = Schedule.objects.for_club(club).count()
        membership_count = TrainingGroupMembership.objects.for_club(club).count()

        with pytest.raises(BusinessLogicError) as create_exc:
            create_schedule(
                club_id=club.id,
                day_of_week=3,
                start_time=time(18, 0),
                end_time=time(19, 0),
                group_name="Blocked mapped recurring slot",
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
                training_group_id=source_group.id,
            )
        deactivated = update_schedule(
            club_id=club.id,
            schedule_id=mapped_schedule.id,
            is_active=False,
        )
        with pytest.raises(BusinessLogicError) as update_exc:
            update_schedule(
                club_id=club.id,
                schedule_id=mapped_schedule.id,
                start_time=time(18, 30),
            )
        from apps.attendance.services.training_group_memberships import transfer_training_group_membership

        with pytest.raises(BusinessLogicError) as transfer_exc:
            transfer_training_group_membership(
                membership_id=membership.id,
                club_id=club.id,
                target_training_group_id=target_group.id,
                ends_on=date(2026, 7, 7),
                actor_user_id=None,
                rationale="Blocked transfer creates a new canonical membership.",
                idempotency_key="blocked-training-group-transfer",
            )

        assert {create_exc.value.code, update_exc.value.code, transfer_exc.value.code} == {
            "training_group_writes_disabled"
        }
        assert Schedule.objects.for_club(club).count() == schedule_count
        assert TrainingGroupMembership.objects.for_club(club).count() == membership_count
        mapped_schedule.refresh_from_db()
        membership.refresh_from_db()
        assert deactivated.is_active is False
        assert mapped_schedule.is_active is False
        assert mapped_schedule.start_time == time(18, 0)
        assert membership.status == TrainingGroupMembership.Status.ACTIVE

    def test_containment_allows_only_eligibility_reducing_membership_and_slot_actions(
        self, club, settings
    ):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.CONTAINMENT)
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        source_group = TrainingGroup.objects.create(
            club=club,
            name="Contained source group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        target_group = TrainingGroup.objects.create(
            club=club,
            name="Contained target group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        schedule = ScheduleFactory(
            club=club,
            training_group=source_group,
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        active_membership = TrainingGroupMembership.objects.create(
            club=club,
            student=StudentFactory(club=club),
            training_group=source_group,
            status=TrainingGroupMembership.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            source=TrainingGroupMembership.Source.MANUAL,
            authority=TrainingGroupMembership.Authority.INDEPENDENT,
        )
        frozen_membership = TrainingGroupMembership.objects.create(
            club=club,
            student=StudentFactory(club=club),
            training_group=source_group,
            status=TrainingGroupMembership.Status.FROZEN,
            starts_on=date(2026, 7, 1),
            source=TrainingGroupMembership.Source.MANUAL,
            authority=TrainingGroupMembership.Authority.INDEPENDENT,
        )
        for membership in (active_membership, frozen_membership):
            ScheduleEnrollment.objects.create(
                club=club,
                student=membership.student,
                schedule=schedule,
                training_group_membership=membership,
                status=membership.status,
                starts_on=membership.starts_on,
                created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )

        with pytest.raises(BusinessLogicError) as create_exc:
            create_training_group_membership(
                club_id=club.id,
                student_id=StudentFactory(club=club).id,
                training_group_id=source_group.id,
                starts_on=date(2026, 7, 29),
                source=TrainingGroupMembership.Source.MANUAL,
                idempotency_key="containment-create-blocked",
            )
        with pytest.raises(BusinessLogicError) as unfreeze_exc:
            unfreeze_training_group_membership(
                membership_id=frozen_membership.id,
                club_id=club.id,
                actor_user_id=None,
                rationale="Containment must not restore eligibility.",
                idempotency_key="containment-unfreeze-blocked",
            )
        from apps.attendance.services.training_group_memberships import (
            transfer_training_group_membership,
        )

        with pytest.raises(BusinessLogicError) as transfer_exc:
            transfer_training_group_membership(
                membership_id=active_membership.id,
                club_id=club.id,
                target_training_group_id=target_group.id,
                ends_on=date(2026, 7, 28),
                actor_user_id=None,
                rationale="Containment must not create target eligibility.",
                idempotency_key="containment-transfer-blocked",
            )

        freeze_training_group_membership(
            membership_id=active_membership.id,
            club_id=club.id,
            actor_user_id=None,
            rationale="Containment may reduce eligibility.",
            idempotency_key="containment-freeze-allowed",
        )
        deactivated = update_schedule(
            club_id=club.id,
            schedule_id=schedule.id,
            is_active=False,
        )
        with pytest.raises(BusinessLogicError) as reactivate_exc:
            update_schedule(
                club_id=club.id,
                schedule_id=schedule.id,
                is_active=True,
            )

        assert {
            create_exc.value.code,
            unfreeze_exc.value.code,
            transfer_exc.value.code,
            reactivate_exc.value.code,
        } == {"training_group_writes_disabled"}
        active_membership.refresh_from_db()
        frozen_membership.refresh_from_db()
        assert active_membership.status == TrainingGroupMembership.Status.FROZEN
        assert frozen_membership.status == TrainingGroupMembership.Status.FROZEN
        assert deactivated.is_active is False

    def test_reconciling_blocks_legacy_permanent_enrollment_before_schedule_lock(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.OFF)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        student = StudentFactory(club=club)
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.RECONCILING)

        with pytest.raises(BusinessLogicError) as exc_info:
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                starts_on=date(2026, 7, 1),
                created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
            )

        assert exc_info.value.code == "training_group_reconciling"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()

    def test_reconciling_blocks_recurring_group_schedule_update_then_allows_retry(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.OFF)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.RECONCILING)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_schedule(
                club_id=club.id,
                schedule_id=schedule.id,
                start_time=time(18, 30),
            )

        assert exc_info.value.code == "training_group_reconciling"
        schedule.refresh_from_db()
        assert schedule.start_time == time(18, 0)

        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        updated = update_schedule(
            club_id=club.id,
            schedule_id=schedule.id,
            start_time=time(18, 30),
        )
        assert updated.start_time == time(18, 30)

    def test_reconciling_blocks_ordinary_checkin_cancel_and_session_close_before_side_effects(
        self, club, owner_user
    ):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.OFF)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        student = StudentFactory(club=club)
        existing_checkin = CheckinFactory(club=club, student=student, schedule=schedule)
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.RECONCILING)

        with pytest.raises(BusinessLogicError) as create_exc:
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=schedule.training_type_id,
                source=Checkin.Source.MANUAL,
                checkin_date=existing_checkin.date,
            )
        with pytest.raises(BusinessLogicError) as cancel_exc:
            cancel_checkin(
                checkin_id=existing_checkin.id,
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
        with pytest.raises(BusinessLogicError) as close_exc:
            close_session_from_existing_checkins(
                club_id=club.id,
                schedule_id=schedule.id,
                checkin_date=existing_checkin.date,
                actor_user_id=owner_user.id,
            )

        assert {
            create_exc.value.code,
            cancel_exc.value.code,
            close_exc.value.code,
        } == {"training_group_reconciling"}
        existing_checkin.refresh_from_db()
        assert existing_checkin.cancelled_at is None
        assert not GroupSession.objects.for_club(club).filter(
            schedule=schedule,
            date=existing_checkin.date,
        ).exists()

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S6 suite",
    )
    @pytest.mark.parametrize(
        "writer",
        ("payment_verify", "schedule_update", "legacy_enrollment", "session_close"),
    )
    def test_postgresql_reconciling_blocks_then_shadow_retries_representative_writers(
        self, club, owner_user, writer
    ):
        """Prove the S6 gate is retry-safe across financial and attendance writers."""
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.OFF)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=TariffFactory(club=club, training_type=schedule.training_type),
            status=Subscription.Status.PENDING,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=subscription.tariff,
            subscription=subscription,
            recorded_by=owner_user,
            status=Payment.Status.PENDING,
        )
        existing_checkin = CheckinFactory(club=club, student=student, schedule=schedule)
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.RECONCILING)

        def execute_writer():
            if writer == "payment_verify":
                return verify_payment(
                    payment_id=payment.id,
                    club_id=club.id,
                    verified_by_id=owner_user.id,
                    action="confirm",
                )
            if writer == "schedule_update":
                return update_schedule(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    start_time=time(18, 30),
                )
            if writer == "legacy_enrollment":
                return enroll_student_in_schedule(
                    club_id=club.id,
                    student_id=student.id,
                    schedule_id=schedule.id,
                    starts_on=date(2026, 7, 1),
                    created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
                )
            with patch("apps.attendance.services.checkin._assert_session_close_allowed"):
                return close_session_from_existing_checkins(
                    club_id=club.id,
                    schedule_id=schedule.id,
                    checkin_date=existing_checkin.date,
                    actor_user_id=owner_user.id,
                )

        with pytest.raises(BusinessLogicError) as exc_info:
            execute_writer()
        assert exc_info.value.code == "training_group_reconciling"

        if writer == "payment_verify":
            payment.refresh_from_db()
            assert payment.status == Payment.Status.PENDING
        elif writer == "schedule_update":
            schedule.refresh_from_db()
            assert schedule.start_time == time(18, 0)
        elif writer == "legacy_enrollment":
            assert not ScheduleEnrollment.objects.for_club(club).filter(
                student=student,
                schedule=schedule,
            ).exists()
        else:
            assert not GroupSession.objects.for_club(club).filter(
                schedule=schedule,
                date=existing_checkin.date,
            ).exists()

        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        execute_writer()

        if writer == "payment_verify":
            payment.refresh_from_db()
            assert payment.status == Payment.Status.CONFIRMED
        elif writer == "schedule_update":
            schedule.refresh_from_db()
            assert schedule.start_time == time(18, 30)
        elif writer == "legacy_enrollment":
            assert ScheduleEnrollment.objects.for_club(club).filter(
                student=student,
                schedule=schedule,
            ).exists()
        else:
            assert GroupSession.objects.for_club(club).filter(
                schedule=schedule,
                date=existing_checkin.date,
            ).exists()

    def test_shadow_mode_creates_one_explicit_canonical_group_without_name_inference(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        kwargs = self._group_schedule_kwargs(club=club)

        first = create_schedule(**kwargs)
        second = create_schedule(**{**kwargs, "day_of_week": 3})

        assert first.training_group_id is not None
        assert second.training_group_id is not None
        assert first.training_group_id != second.training_group_id
        assert TrainingGroup.objects.for_club(club).count() == 2

    def test_containment_rejects_new_recurring_group_slot(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.CONTAINMENT)

        with pytest.raises(BusinessLogicError, match="writes are disabled") as exc_info:
            create_schedule(**self._group_schedule_kwargs(club=club))

        assert exc_info.value.code == "training_group_writes_disabled"

    def test_linked_slot_identity_change_is_rejected(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))

        with pytest.raises(BusinessLogicError, match="future replacement slot") as exc_info:
            update_schedule(
                club_id=club.id,
                schedule_id=schedule.id,
                group_name="Split brain",
            )

        assert exc_info.value.code == "training_group_linked_schedule_identity_protected"

    def test_new_slot_fans_out_open_membership_before_commit(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        first = create_schedule(**self._group_schedule_kwargs(club=club))
        student = StudentFactory(club=club)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=first.training_group_id,
            starts_on=date(2026, 7, 1),
            source=TrainingGroupMembership.Source.MANUAL,
            idempotency_key="s4-slot-fanout-membership",
        )

        second = create_schedule(
            **{
                **self._group_schedule_kwargs(club=club),
                "day_of_week": 3,
                "trainer_id": first.trainer_id,
                "location_id": first.location_id,
                "training_type_id": first.training_type_id,
                "training_group_id": first.training_group_id,
            }
        )

        assert set(
            ScheduleEnrollment.objects.for_club(club)
            .filter(training_group_membership=membership)
            .values_list("schedule_id", flat=True)
        ) == {first.id, second.id}

    def test_archive_requires_zero_open_state_and_records_one_idempotent_audit_event(self, club, owner_user):
        from apps.attendance.services import cancel_training_group_membership
        from apps.attendance.services.training_groups import archive_training_group

        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        archive_kwargs = {
            "club_id": club.id,
            "training_group_id": schedule.training_group_id,
            "actor_user_id": owner_user.id,
            "rationale": "Owner confirmed every archive precondition.",
            "idempotency_key": "s6-archive-group",
        }

        with pytest.raises(BusinessLogicError) as exc_info:
            archive_training_group(**archive_kwargs)
        assert exc_info.value.code == "training_group_archive_active_slots"

        update_schedule(club_id=club.id, schedule_id=schedule.id, is_active=False)
        student = StudentFactory(club=club)
        membership = create_training_group_membership(
            club_id=club.id,
            student_id=student.id,
            training_group_id=schedule.training_group_id,
            starts_on=date(2026, 7, 1),
            source=TrainingGroupMembership.Source.MANUAL,
            idempotency_key="s4-archive-membership",
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            archive_training_group(**archive_kwargs)
        assert exc_info.value.code == "training_group_archive_open_memberships"

        cancel_training_group_membership(
            club_id=club.id,
            membership_id=membership.id,
            ends_on=date(2026, 7, 27),
            actor_user_id=None,
            rationale="archive the canonical group",
            idempotency_key="s4-archive-cancel",
        )
        tariff = TariffFactory(
            club=club,
            training_type=schedule.training_type,
            location=schedule.location,
            scope="location",
        )
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            target_training_group=schedule.training_group,
            status=Payment.Status.PENDING,
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            archive_training_group(**archive_kwargs)
        assert exc_info.value.code == "training_group_archive_pending_payments"

        Payment.objects.for_club(club).filter(id=payment.id).update(status=Payment.Status.REJECTED)
        pending_order = BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.OWNER,
            status=BankPaymentOrder.Status.PENDING,
            amount_snapshot=payment.amount,
            purpose_snapshot="Archive precondition test",
            expires_at=timezone.now() + timedelta(days=1),
            created_by=owner_user,
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            archive_training_group(**archive_kwargs)
        assert exc_info.value.code == "training_group_archive_pending_orders"

        BankPaymentOrder.objects.for_club(club).filter(id=pending_order.id).update(
            status=BankPaymentOrder.Status.CANCELLED
        )
        archived = archive_training_group(**archive_kwargs)
        retry = archive_training_group(**archive_kwargs)

        assert archived.status == TrainingGroup.Status.ARCHIVED
        assert retry.id == archived.id
        archive_events = TrainingGroupMappingEvent.objects.for_club(club).filter(
            training_group=archived,
            action="archived",
        )
        assert archive_events.count() == 1
        assert archive_events.get().actor_id == owner_user.id

    def test_slot_fanout_scales_with_bounded_query_count(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        first = create_schedule(**self._group_schedule_kwargs(club=club))
        for index in range(24):
            student = StudentFactory(club=club)
            create_training_group_membership(
                club_id=club.id,
                student_id=student.id,
                training_group_id=first.training_group_id,
                starts_on=date(2026, 7, 1),
                source=TrainingGroupMembership.Source.MANUAL,
                idempotency_key=f"s4-scale-membership-{index}",
            )

        from apps.attendance.services.training_group_memberships import (
            fan_out_training_group_membership_projections,
        )

        with transaction.atomic():
            second = Schedule(
                club=club,
                day_of_week=3,
                start_time=first.start_time,
                end_time=first.end_time,
                group_name=first.group_name,
                trainer_id=first.trainer_id,
                location_id=first.location_id,
                training_type_id=first.training_type_id,
                training_group_id=first.training_group_id,
            )
            second.full_clean()
            second.save()
            with CaptureQueriesContext(connection) as queries:
                fan_out_training_group_membership_projections(
                    club_id=club.id,
                    training_group_id=first.training_group_id,
                )

        assert len(queries.captured_queries) <= 12
        assert ScheduleEnrollment.objects.for_club(club).filter(
            training_group_membership__training_group_id=first.training_group_id
        ).count() == 48
        assert second.training_group_id == first.training_group_id


    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL CI suite",
    )
    def test_postgresql_concurrent_membership_create_never_duplicates_projections(self, club):
        self._set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
        schedule = create_schedule(**self._group_schedule_kwargs(club=club))
        student = StudentFactory(club=club)
        gate = Barrier(2)

        def submit(key: str) -> str:
            close_old_connections()
            try:
                gate.wait(timeout=10)
                create_training_group_membership(
                    club_id=club.id,
                    student_id=student.id,
                    training_group_id=schedule.training_group_id,
                    starts_on=date(2026, 7, 1),
                    source=TrainingGroupMembership.Source.MANUAL,
                    idempotency_key=key,
                )
                return "created"
            except (BusinessLogicError, IntegrityError) as exc:
                return getattr(exc, "code", "integrity_error")
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, ("s4-concurrent-a", "s4-concurrent-b")))

        assert outcomes.count("created") == 1
        assert TrainingGroupMembership.objects.for_club(club).filter(
            student=student,
            training_group_id=schedule.training_group_id,
        ).count() == 1
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        ).count() == 1
