from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.attendance.models import (
    CheckinCascadeEvent,
    PersonalAvailabilitySlot,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.services.training_groups import (
    get_training_group_rollout_state,
    transition_training_group_rollout,
    transition_training_group_rollout_for_owner,
)
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
)
from apps.billing.tests.factories import TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerEarningFactory, TrainerFactory


@pytest.mark.django_db
class TestCheckinModel:
    def test_checkin_unique_constraint(self, club):
        checkin = CheckinFactory(club=club)
        with pytest.raises(IntegrityError):
            CheckinFactory(
                club=club,
                student=checkin.student,
                schedule=checkin.schedule,
                date=checkin.date,
            )

    def test_checkin_allows_after_soft_delete(self, club):
        checkin = CheckinFactory(club=club)
        checkin.soft_delete()
        # Should not raise -- soft-deleted checkin allows new one
        new_checkin = CheckinFactory(
            club=club,
            student=checkin.student,
            schedule=checkin.schedule,
            date=checkin.date,
        )
        assert new_checkin.id != checkin.id


@pytest.mark.django_db
class TestGroupSessionModel:
    def test_group_session_unique_constraint(self, club):
        session = GroupSessionFactory(club=club)
        with pytest.raises(IntegrityError):
            GroupSessionFactory(
                club=club,
                schedule=session.schedule,
                date=session.date,
            )


@pytest.mark.django_db
class TestScheduleEnrollmentModel:
    def test_clean_rejects_cross_club_student_schedule_and_invalid_dates(self, club, other_club):
        enrollment = ScheduleEnrollment(
            club=club,
            student=StudentFactory(club=other_club),
            schedule=ScheduleFactory(club=other_club),
            starts_on=date(2026, 7, 10),
            ends_on=date(2026, 7, 9),
        )

        with pytest.raises(ValidationError) as exc_info:
            enrollment.full_clean()

        assert set(exc_info.value.message_dict) >= {"student", "schedule", "ends_on"}

    def test_clean_accepts_same_club_open_enrollment(self, club):
        enrollment = ScheduleEnrollment(
            club=club,
            student=StudentFactory(club=club),
            schedule=ScheduleFactory(club=club),
            starts_on=date(2026, 7, 10),
        )

        enrollment.full_clean()


@pytest.mark.django_db
class TestTrainingGroupModels:
    def test_training_group_rejects_foreign_resources_and_non_group_type(self, club, other_club):
        group = TrainingGroupFactory.build(
            club=club,
            training_type=TrainingTypeFactory(club=other_club),
            location=LocationFactory(club=other_club),
            responsible_trainer=TrainerFactory(club=other_club),
        )

        with pytest.raises(ValidationError) as exc_info:
            group.full_clean()

        assert {"training_type", "location", "responsible_trainer"} <= set(exc_info.value.message_dict)

    def test_active_training_group_requires_active_responsible_trainer(self, club):
        trainer = TrainerFactory(club=club, is_active=False)
        group = TrainingGroupFactory.build(club=club, responsible_trainer=trainer)

        with pytest.raises(ValidationError) as exc_info:
            group.full_clean()

        assert "responsible_trainer" in exc_info.value.message_dict

    def test_one_time_schedule_cannot_link_training_group(self, club):
        group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory.build(
            club=club,
            training_type=group.training_type,
            location=group.location,
            training_group=group,
            one_time_date=date(2026, 7, 10),
        )

        with pytest.raises(ValidationError) as exc_info:
            schedule.full_clean()

        assert "training_group" in exc_info.value.message_dict

    def test_schedule_reassignment_with_history_fails_loud(self, club):
        group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
            training_group=group,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=StudentFactory(club=club),
            schedule=schedule,
            starts_on=date(2026, 7, 10),
        )
        replacement_group = TrainingGroupFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
        )
        schedule.training_group = replacement_group

        with pytest.raises(ValidationError):
            schedule.save()

    def test_membership_rejects_foreign_student_and_invalid_dates(self, club, other_club):
        membership = TrainingGroupMembershipFactory.build(
            club=club,
            student=StudentFactory(club=other_club),
            training_group=TrainingGroupFactory(club=other_club),
            starts_on=date(2026, 7, 10),
            ends_on=date(2026, 7, 9),
        )

        with pytest.raises(ValidationError) as exc_info:
            membership.full_clean()

        assert {"student", "training_group", "ends_on"} <= set(exc_info.value.message_dict)

    def test_open_membership_is_unique_per_student_and_group(self, club):
        group = TrainingGroupFactory(club=club)
        student = StudentFactory(club=club)
        TrainingGroupMembershipFactory(club=club, student=student, training_group=group)

        with pytest.raises(IntegrityError):
            TrainingGroupMembershipFactory(club=club, student=student, training_group=group)

    def test_group_projection_requires_matching_group_membership(self, club):
        group = TrainingGroupFactory(club=club)
        membership = TrainingGroupMembershipFactory(club=club, training_group=group)
        schedule = ScheduleFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
            training_group=group,
        )
        enrollment = ScheduleEnrollment(
            club=club,
            student=membership.student,
            schedule=schedule,
            starts_on=membership.starts_on,
            training_group_membership=membership,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        with pytest.raises(ValidationError) as exc_info:
            enrollment.full_clean()

        assert "created_from" in exc_info.value.message_dict
        enrollment.created_from = ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        enrollment.full_clean()

    def test_preserved_migration_source_can_link_group_membership(self, club):
        group = TrainingGroupFactory(club=club)
        membership = TrainingGroupMembershipFactory(
            club=club,
            training_group=group,
            source=TrainingGroupMembership.Source.MIGRATION,
        )
        schedule = ScheduleFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
            training_group=group,
        )
        enrollment = ScheduleEnrollment(
            club=club,
            student=membership.student,
            schedule=schedule,
            starts_on=membership.starts_on,
            training_group_membership=membership,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        enrollment.full_clean()

    def test_training_group_events_are_append_only_and_tenant_unique(self, club):
        event = TrainingGroupRolloutEvent.objects.create(
            club=club,
            previous_mode=TrainingGroupRolloutState.Mode.OFF,
            new_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            rationale="Start owner-approved preview.",
            idempotency_key="rollout-event-1",
        )
        event.rationale = "Changed"

        with pytest.raises(ValidationError):
            event.save()
        with pytest.raises(ValidationError):
            event.delete()
        with pytest.raises(ValidationError):
            TrainingGroupRolloutEvent.objects.filter(pk=event.pk).update(rationale="Changed")
        with pytest.raises(ValidationError):
            TrainingGroupRolloutEvent.objects.filter(pk=event.pk).delete()
        with pytest.raises(IntegrityError):
            TrainingGroupRolloutEvent.objects.create(
                club=club,
                previous_mode=TrainingGroupRolloutState.Mode.OFF,
                new_mode=TrainingGroupRolloutState.Mode.RECONCILING,
                rationale="Duplicate key.",
                idempotency_key="rollout-event-1",
            )

    def test_mapping_and_membership_events_reject_cross_club_ownership(self, club, other_club):
        foreign_group = TrainingGroupFactory(club=other_club)
        mapping_event = TrainingGroupMappingEvent(
            club=club,
            training_group=foreign_group,
            batch_id="12345678-1234-1234-1234-123456789abc",
            action="previewed",
            rationale="Explicit reconciliation preview.",
            idempotency_key="mapping-event-1",
        )
        membership_event = TrainingGroupMembershipEvent(
            club=club,
            membership=TrainingGroupMembershipFactory(club=other_club),
            action="created",
            effective_date=date(2026, 7, 10),
            rationale="Explicit owner admission.",
            idempotency_key="membership-event-1",
        )

        with pytest.raises(ValidationError):
            mapping_event.full_clean()
        with pytest.raises(ValidationError):
            membership_event.full_clean()


@pytest.mark.django_db
class TestTrainingGroupRolloutTransitions:
    @patch("django_q.tasks.async_task")
    def test_reconciling_to_shadow_allows_only_the_deferred_provider_replay_queue(
        self,
        mock_async,
        club,
        django_capture_on_commit_callbacks,
    ):
        from django.utils import timezone

        from apps.billing.management.commands.audit_operational_admissions import _audit_club
        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent

        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="reconciliation-test-deferred",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        audit = _audit_club(club=club)
        assert audit["clean"] is False
        assert audit["invalid_state_counts"] == {"deferred_provider_event": 1}
        assert audit["deferred_provider_event_count"] == 1
        assert audit["legacy_unlinked_pending_manual_count"] == 0

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Start reconciliation before an operational audit gate test.",
            idempotency_key="actual-operational-audit-enter",
        )

        with django_capture_on_commit_callbacks(execute=True):
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="Only the deferred provider replay queue may leave reconciliation for shadow.",
                idempotency_key="actual-operational-audit-deferred-queue",
                forward_audit_passed=True,
            )

        assert TrainingGroupRolloutState.objects.for_club(club).get().mode == TrainingGroupRolloutState.Mode.SHADOW
        mock_async.assert_called_once_with(
            "apps.billing.tasks.replay_deferred_bank_payment_provider_events_task",
            club_id=club.id,
        )
        assert _audit_club(club=club)["clean"] is False

    def test_reconciling_to_shadow_rejects_deferred_provider_queue_with_any_other_audit_failure(
        self,
        club,
        owner_user,
    ):
        from django.utils import timezone

        from apps.billing.management.commands.audit_operational_admissions import _audit_club
        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent, Payment, Subscription
        from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory

        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="reconciliation-test-deferred",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        schedule = ScheduleFactory(club=club)
        tariff = TariffFactory(club=club, training_type=schedule.training_type)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=date(2026, 8, 1),
        )
        audit = _audit_club(club=club)
        assert audit["invalid_state_counts"]["deferred_provider_event"] == 1
        assert audit["invalid_state_counts"]["missing_enrollment"] == 1
        assert audit["legacy_unlinked_pending_manual_count"] == 1

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Start reconciliation before a mixed operational audit gate test.",
            idempotency_key="actual-operational-audit-mixed-enter",
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="A deferred queue cannot mask another operational audit failure.",
                idempotency_key="actual-operational-audit-mixed-block",
                forward_audit_passed=True,
            )

        assert exc_info.value.code == "training_group_rollout_prerequisite_missing"

    def test_deferred_provider_queue_cannot_authorize_containment_or_later_strict_exits(self, club):
        from django.utils import timezone

        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent

        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="reconciliation-test-deferred",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Open the deferred-only reconciliation epoch.",
            idempotency_key="deferred-only-enter",
        )
        with pytest.raises(BusinessLogicError) as containment_exc:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
                actor_id=None,
                rationale="A replay queue is not a failed forward audit for containment.",
                idempotency_key="deferred-only-containment-block",
                canonical_mutations_committed=True,
                forward_audit_failed=False,
            )
        assert containment_exc.value.code == "training_group_rollout_prerequisite_missing"

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.SHADOW,
            actor_id=None,
            rationale="The deferred-only replay queue may leave reconciliation for shadow.",
            idempotency_key="deferred-only-shadow",
            forward_audit_passed=True,
        )
        with pytest.raises(BusinessLogicError) as active_exc:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.ACTIVE,
                actor_id=None,
                rationale="Shadow cannot become active until the deferred queue drains.",
                idempotency_key="deferred-only-active-block",
            )
        assert active_exc.value.code == "training_group_rollout_prerequisite_missing"

    def test_owner_adapter_marks_deferred_only_as_forward_pass_only_for_reconciling_shadow(self, club):
        from django.utils import timezone

        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent

        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="reconciliation-test-deferred",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Open reconciliation for owner deferred-only classification.",
            idempotency_key="owner-deferred-only-enter",
        )

        with patch("apps.attendance.services.training_groups.transition_training_group_rollout") as transition:
            transition_training_group_rollout_for_owner(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=1,
                rationale="Owner forwards only the replay queue to shadow.",
                idempotency_key="owner-deferred-only-shadow",
                rollout_gate_digest="",
            )
        assert transition.call_args.kwargs["forward_audit_passed"] is True
        assert transition.call_args.kwargs["forward_audit_failed"] is False

        with patch("apps.attendance.services.training_groups.transition_training_group_rollout") as transition:
            transition_training_group_rollout_for_owner(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
                actor_id=1,
                rationale="Owner cannot turn only the replay queue into containment authority.",
                idempotency_key="owner-deferred-only-containment",
                rollout_gate_digest="",
            )
        assert transition.call_args.kwargs["forward_audit_passed"] is False
        assert transition.call_args.kwargs["forward_audit_failed"] is False

    def test_containment_to_shadow_stays_strict_until_the_deferred_queue_drains(self, club):
        from django.utils import timezone

        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent

        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="reconciliation-test-deferred",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Open reconciliation before a containment repair gate test.",
            idempotency_key="deferred-containment-enter",
        )
        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
            actor_id=None,
            rationale="Contain the synthetic partial-commit recovery state.",
            idempotency_key="deferred-containment-exit",
            canonical_mutations_committed=True,
            forward_audit_failed=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="Containment cannot exit while the replay queue remains deferred.",
                idempotency_key="deferred-containment-shadow-block",
                repair_audits_passed=True,
            )

        assert exc_info.value.code == "training_group_rollout_prerequisite_missing"

    def test_reconciling_to_shadow_requires_clean_training_group_audit(self, club):
        from apps.attendance.models import Schedule
        from apps.attendance.services.training_group_reconciliation import audit_training_groups

        group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
            trainer=group.responsible_trainer,
            training_group=group,
        )
        TrainingGroupMappingEvent.objects.create(
            club=club,
            batch_id=uuid4(),
            training_group=group,
            schedule=schedule,
            action="schedule_mapped",
            previous_group_snapshot={},
            new_group_snapshot={"training_group_id": group.id},
            rationale="Create an auditable invalid-state fixture.",
            idempotency_key="actual-group-audit-dirty-event",
        )
        Schedule.objects.for_club(club).filter(id=schedule.id).update(training_group=None)
        assert audit_training_groups(club=club)["valid"] is False

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Start reconciliation before a training-group audit gate test.",
            idempotency_key="actual-group-audit-enter",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="Caller flags cannot bypass a failed training group audit.",
                idempotency_key="actual-group-audit-block",
                forward_audit_passed=True,
            )

        assert exc_info.value.code == "training_group_rollout_prerequisite_missing"

    def test_reconciling_to_shadow_rejects_legacy_source_on_nonmigration_membership(
        self,
        club,
    ):
        from apps.attendance.services.training_group_reconciliation import (
            audit_training_groups,
        )

        group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            training_type=group.training_type,
            location=group.location,
            trainer=group.responsible_trainer,
            training_group=group,
        )
        membership = TrainingGroupMembershipFactory(
            club=club,
            training_group=group,
            source=TrainingGroupMembership.Source.MANUAL,
        )
        projection = ScheduleEnrollment.objects.create(
            club=club,
            student=membership.student,
            schedule=schedule,
            training_group_membership=membership,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=membership.starts_on,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )
        ScheduleEnrollment.objects.for_club(club).filter(id=projection.id).update(
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL
        )

        audit = audit_training_groups(club=club)
        assert audit["valid"] is False
        assert "invalid_membership_projection" in audit["invalid"]

        transition_training_group_rollout(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=None,
            rationale="Open a forward gate around malformed linked provenance.",
            idempotency_key="invalid-source-enter-reconciling",
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="A malformed linked source must fail the forward audit.",
                idempotency_key="invalid-source-shadow-blocked",
                forward_audit_passed=True,
            )

        assert exc_info.value.code == "training_group_rollout_prerequisite_missing"

    def test_missing_rollout_state_fails_closed(self, club):
        TrainingGroupRolloutState.objects.for_club(club).delete()

        with pytest.raises(BusinessLogicError) as exc_info:
            get_training_group_rollout_state(club_id=club.id)

        assert exc_info.value.code == "training_group_rollout_state_missing"

    def test_direct_state_mutation_and_skipped_transition_fail_loud(self, club):
        state = get_training_group_rollout_state(club_id=club.id)
        state.mode = TrainingGroupRolloutState.Mode.SHADOW

        with pytest.raises(ValidationError):
            state.save()
        with pytest.raises(ValidationError):
            TrainingGroupRolloutState.objects.for_club(club).filter(pk=state.pk).update(
                mode=TrainingGroupRolloutState.Mode.SHADOW
            )
        with pytest.raises(ValidationError):
            TrainingGroupRolloutState.objects.unscoped().filter(pk=state.pk).update(
                mode=TrainingGroupRolloutState.Mode.SHADOW
            )
        with pytest.raises(ValidationError):
            TrainingGroupRolloutState.objects.for_club(club).filter(pk=state.pk)._update_for_transition(
                transition_token=object(),
                mode=TrainingGroupRolloutState.Mode.SHADOW,
            )
        state.refresh_from_db()
        assert state.mode == TrainingGroupRolloutState.Mode.OFF
        with pytest.raises(BusinessLogicError) as exc_info:
            transition_training_group_rollout(
                club_id=club.id,
                target_mode=TrainingGroupRolloutState.Mode.SHADOW,
                actor_id=None,
                rationale="Skip is invalid.",
                idempotency_key="skip-state",
            )

        assert exc_info.value.code == "training_group_rollout_transition_invalid"

    def test_transition_graph_is_audited_and_idempotent(self, club):
        def transition(target_mode, key, **kwargs):
            return transition_training_group_rollout(
                club_id=club.id,
                target_mode=target_mode,
                actor_id=None,
                rationale="Bounded rollout test transition.",
                idempotency_key=key,
                **kwargs,
            )

        state = transition(TrainingGroupRolloutState.Mode.RECONCILING, "off-to-reconciling")
        same_state = transition(TrainingGroupRolloutState.Mode.RECONCILING, "off-to-reconciling")
        assert same_state.id == state.id
        state = transition(TrainingGroupRolloutState.Mode.OFF, "reconciling-to-off")
        assert state.mode == TrainingGroupRolloutState.Mode.OFF
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "off-to-reconciling-2")
        transition(
            TrainingGroupRolloutState.Mode.SHADOW,
            "reconciling-to-shadow",
            forward_audit_passed=True,
        )
        transition(TrainingGroupRolloutState.Mode.ACTIVE, "shadow-to-active")
        transition(TrainingGroupRolloutState.Mode.CONTAINMENT, "active-to-containment")
        transition(TrainingGroupRolloutState.Mode.RECONCILING, "containment-to-reconciling")
        transition(
            TrainingGroupRolloutState.Mode.CONTAINMENT,
            "reconciling-to-containment",
            canonical_mutations_committed=True,
            forward_audit_failed=True,
        )
        state = transition(
            TrainingGroupRolloutState.Mode.SHADOW,
            "containment-to-shadow",
            repair_audits_passed=True,
        )

        assert state.mode == TrainingGroupRolloutState.Mode.SHADOW
        assert TrainingGroupRolloutEvent.objects.for_club(club).count() == 9

    def test_reconciliation_exit_queues_deferred_provider_replay_once_for_each_allowed_target(
        self,
        club,
        django_capture_on_commit_callbacks,
    ):
        def transition(target_mode, key, **kwargs):
            return transition_training_group_rollout(
                club_id=club.id,
                target_mode=target_mode,
                actor_id=None,
                rationale="Queue deferred provider replay after a valid reconciliation exit.",
                idempotency_key=key,
                **kwargs,
            )

        with patch("django_q.tasks.async_task") as mock_async:
            with django_capture_on_commit_callbacks(execute=True):
                transition(TrainingGroupRolloutState.Mode.RECONCILING, "queue-off-enter")
            mock_async.assert_not_called()
            with django_capture_on_commit_callbacks(execute=True):
                transition(TrainingGroupRolloutState.Mode.OFF, "queue-off-exit")
            mock_async.assert_called_once_with(
                "apps.billing.tasks.replay_deferred_bank_payment_provider_events_task",
                club_id=club.id,
            )

            mock_async.reset_mock()
            with django_capture_on_commit_callbacks(execute=True):
                transition(TrainingGroupRolloutState.Mode.RECONCILING, "queue-shadow-enter")
            with django_capture_on_commit_callbacks(execute=True):
                transition(
                    TrainingGroupRolloutState.Mode.SHADOW,
                    "queue-shadow-exit",
                    forward_audit_passed=True,
                )
            mock_async.assert_called_once_with(
                "apps.billing.tasks.replay_deferred_bank_payment_provider_events_task",
                club_id=club.id,
            )

            mock_async.reset_mock()
            transition(TrainingGroupRolloutState.Mode.ACTIVE, "queue-active-enter")
            transition(TrainingGroupRolloutState.Mode.CONTAINMENT, "queue-containment-enter")
            with django_capture_on_commit_callbacks(execute=True):
                transition(TrainingGroupRolloutState.Mode.RECONCILING, "queue-containment-reconciling")
            with django_capture_on_commit_callbacks(execute=True):
                transition(
                    TrainingGroupRolloutState.Mode.CONTAINMENT,
                    "queue-containment-exit",
                    canonical_mutations_committed=True,
                    forward_audit_failed=True,
                )
            mock_async.assert_called_once_with(
                "apps.billing.tasks.replay_deferred_bank_payment_provider_events_task",
                club_id=club.id,
            )

    def test_invalid_or_non_exit_rollout_transitions_never_queue_provider_replay(
        self,
        club,
        django_capture_on_commit_callbacks,
    ):
        with patch("django_q.tasks.async_task") as mock_async:
            with django_capture_on_commit_callbacks(execute=True):
                transition_training_group_rollout(
                    club_id=club.id,
                    target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
                    actor_id=None,
                    rationale="Entering reconciliation must not queue replay.",
                    idempotency_key="queue-no-enter",
                )
            mock_async.assert_not_called()
            with pytest.raises(BusinessLogicError):
                transition_training_group_rollout(
                    club_id=club.id,
                    target_mode=TrainingGroupRolloutState.Mode.OFF,
                    actor_id=None,
                    rationale="Invalid abort after canonical work.",
                    idempotency_key="queue-invalid-abort",
                    canonical_mutations_committed=True,
                )


@pytest.mark.django_db
class TestScheduleBookingEventModel:
    def test_clean_rejects_cross_club_relations(self, club, other_club):
        foreign_student = StudentFactory(club=other_club)
        foreign_schedule = ScheduleFactory(club=other_club)
        foreign_enrollment = ScheduleEnrollment.objects.create(
            club=other_club,
            student=foreign_student,
            schedule=foreign_schedule,
            starts_on=date(2026, 7, 10),
        )
        event = ScheduleBookingEvent(
            club=club,
            enrollment=foreign_enrollment,
            schedule=foreign_schedule,
            student=foreign_student,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            effective_date=date(2026, 7, 10),
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert set(exc_info.value.message_dict) >= {"enrollment", "schedule", "student"}

    def test_clean_rejects_schedule_and_student_that_do_not_match_enrollment(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            starts_on=date(2026, 7, 10),
        )
        event = ScheduleBookingEvent(
            club=club,
            enrollment=enrollment,
            schedule=ScheduleFactory(club=club),
            student=StudentFactory(club=club),
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            effective_date=date(2026, 7, 10),
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert set(exc_info.value.message_dict) >= {"schedule", "student"}

    def test_clean_accepts_event_matching_enrollment(self, club):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            starts_on=date(2026, 7, 10),
        )
        event = ScheduleBookingEvent(
            club=club,
            enrollment=enrollment,
            schedule=schedule,
            student=student,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            effective_date=date(2026, 7, 10),
        )

        event.full_clean()


@pytest.mark.django_db
class TestPersonalAvailabilitySlotModel:
    def test_clean_rejects_cross_club_resources_and_booking(self, club, other_club):
        foreign_student = StudentFactory(club=other_club)
        foreign_schedule = ScheduleFactory(club=other_club)
        foreign_enrollment = ScheduleEnrollment.objects.create(
            club=other_club,
            student=foreign_student,
            schedule=foreign_schedule,
            starts_on=date(2026, 7, 10),
        )
        slot = PersonalAvailabilitySlot(
            club=club,
            trainer=TrainerFactory(club=other_club),
            location=LocationFactory(club=other_club),
            training_type=TrainingTypeFactory(club=other_club),
            starts_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
            ends_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
            booked_enrollment=foreign_enrollment,
        )

        with pytest.raises(ValidationError) as exc_info:
            slot.full_clean()

        assert set(exc_info.value.message_dict) >= {
            "trainer",
            "location",
            "training_type",
            "booked_enrollment",
        }

    def test_clean_rejects_end_time_before_or_equal_start_time(self, club):
        starts_at = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
        slot = PersonalAvailabilitySlot(
            club=club,
            trainer=TrainerFactory(club=club),
            location=LocationFactory(club=club),
            training_type=TrainingTypeFactory(club=club),
            starts_at=starts_at,
            ends_at=starts_at,
        )

        with pytest.raises(ValidationError) as exc_info:
            slot.full_clean()

        assert "ends_at" in exc_info.value.message_dict

    def test_clean_rejects_cross_date_range(self, club):
        slot = PersonalAvailabilitySlot(
            club=club,
            trainer=TrainerFactory(club=club),
            location=LocationFactory(club=club),
            training_type=TrainingTypeFactory(club=club),
            starts_at=datetime(2026, 7, 10, 23, 0, tzinfo=UTC),
            ends_at=datetime(2026, 7, 11, 0, 30, tzinfo=UTC),
        )

        with pytest.raises(ValidationError) as exc_info:
            slot.full_clean()

        assert "ends_at" in exc_info.value.message_dict

    def test_clean_accepts_same_club_same_day_slot(self, club):
        slot = PersonalAvailabilitySlot(
            club=club,
            trainer=TrainerFactory(club=club),
            location=LocationFactory(club=club),
            training_type=TrainingTypeFactory(club=club),
            starts_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
            ends_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
        )

        slot.full_clean()


@pytest.mark.django_db
class TestCheckinCascadeEventModel:
    def test_clean_rejects_cross_club_checkin(self, club, other_club):
        event = CheckinCascadeEvent(
            club=club,
            checkin=CheckinFactory(club=other_club),
            effect=CheckinCascadeEvent.Effect.SALARY,
            task_name="apps.attendance.tasks.enqueue_salary_update",
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "checkin" in exc_info.value.message_dict

    def test_clean_accepts_same_club_checkin(self, club):
        event = CheckinCascadeEvent(
            club=club,
            checkin=CheckinFactory(club=club),
            effect=CheckinCascadeEvent.Effect.SALARY,
            task_name="apps.attendance.tasks.enqueue_salary_update",
        )

        event.full_clean()


@pytest.mark.django_db
class TestTrainerEarningModel:
    def test_trainer_earning_one_to_one(self, club):
        earning = TrainerEarningFactory(club=club)
        with pytest.raises(IntegrityError):
            TrainerEarningFactory(
                club=club,
                checkin=earning.checkin,
            )


@pytest.mark.django_db
class TestStudentLastVisitDate:
    def test_student_last_visit_date_field(self, club):
        student = StudentFactory(
            club=club,
            first_name="Test",
            last_name="Student",
            phone="+79001234567",
        )
        assert student.last_visit_date is None
        student.last_visit_date = date(2026, 3, 19)
        student.save()
        student.refresh_from_db()
        assert student.last_visit_date == date(2026, 3, 19)
