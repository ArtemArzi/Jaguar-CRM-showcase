from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.attendance.models import TrainingGroupRolloutState
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.clubs.commercial_journey import (
    commercial_journey_activation_readiness,
    transition_commercial_journey_protocol,
)
from apps.clubs.models import ClubSettings, CommercialJourneyProtocolTransition
from apps.clubs.tests.factories import ClubSettingsFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


def _enable_v2_activation_prerequisites(*, settings, club) -> None:
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=True)
    rollout = TrainingGroupRolloutState.objects.for_club(club).get()
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )


@pytest.mark.django_db
def test_protocol_transition_dry_run_is_read_only(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    stdout = StringIO()

    call_command(
        "transition_commercial_journey_protocol",
        "--club-id",
        str(club.id),
        "--to",
        "v2",
        "--rationale",
        "release readiness",
        "--idempotency-key",
        "protocol-v2-dry-run",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["applied"] is False
    assert payload["readiness"]["ready"] is True
    assert ClubSettings.objects.get(club=club).commercial_journey_protocol_version == "v1"
    assert not CommercialJourneyProtocolTransition.objects.filter(club=club).exists()


@pytest.mark.django_db
def test_protocol_transition_applies_once_and_replays_receipt(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    settings.PAYMENT_PROVIDER = ""
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
    settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED = False

    first = transition_commercial_journey_protocol(
        club_id=club.id,
        target_version="v2",
        rationale="accepted release",
        idempotency_key="protocol-v2-k1",
    )
    replay = transition_commercial_journey_protocol(
        club_id=club.id,
        target_version="v2",
        rationale="accepted release",
        idempotency_key="protocol-v2-k1",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt.id == first.receipt.id
    assert ClubSettings.objects.get(club=club).commercial_journey_protocol_version == "v2"
    assert CommercialJourneyProtocolTransition.objects.filter(club=club).count() == 1
    assert first.receipt.readiness_snapshot["activation_prerequisites"]["provider"] == {
        "enabled": False,
        "reason_code": "unknown_provider",
        "creation_enabled": False,
        "reconciliation_enabled": False,
        "reconciliation_available": False,
    }


@pytest.mark.django_db
def test_historical_replay_after_rollback_reports_current_state_without_reapplying(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    transition_commercial_journey_protocol(
        club_id=club.id,
        target_version="v2",
        rationale="accepted release",
        idempotency_key="protocol-v2-k1",
    )
    transition_commercial_journey_protocol(
        club_id=club.id,
        target_version="v1",
        rationale="rollback",
        idempotency_key="protocol-v1-k2",
    )
    stdout = StringIO()

    call_command(
        "transition_commercial_journey_protocol",
        "--club-id",
        str(club.id),
        "--to",
        "v2",
        "--rationale",
        "accepted release",
        "--idempotency-key",
        "protocol-v2-k1",
        "--apply",
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert payload["applied"] is False
    assert payload["replayed"] is True
    assert payload["current_version"] == "v1"
    assert payload["state_matches_receipt_target"] is False
    assert ClubSettings.objects.get(club=club).commercial_journey_protocol_version == "v1"
    assert CommercialJourneyProtocolTransition.objects.filter(club=club).count() == 2


@pytest.mark.django_db
def test_new_transition_key_cannot_record_a_noop(club):
    ClubSettingsFactory(club=club)

    with pytest.raises(BusinessLogicError) as exc_info:
        transition_commercial_journey_protocol(
            club_id=club.id,
            target_version="v1",
            rationale="not a transition",
            idempotency_key="protocol-v1-noop",
        )

    assert exc_info.value.code == "protocol_already_current"
    assert not CommercialJourneyProtocolTransition.objects.filter(club=club).exists()


@pytest.mark.django_db
def test_transition_receipts_reject_instance_and_bulk_mutation(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    result = transition_commercial_journey_protocol(
        club_id=club.id,
        target_version="v2",
        rationale="immutable receipt",
        idempotency_key="protocol-v2-immutable",
    )
    receipt = result.receipt

    receipt.rationale = "rewritten"
    with pytest.raises(ValidationError):
        receipt.save()
    with pytest.raises(ValidationError):
        receipt.delete()
    with pytest.raises(ValidationError):
        CommercialJourneyProtocolTransition.objects.for_club(club).filter(
            id=receipt.id
        ).update(rationale="rewritten")
    with pytest.raises(ValidationError):
        CommercialJourneyProtocolTransition.objects.unscoped().filter(
            id=receipt.id
        ).delete()

    assert CommercialJourneyProtocolTransition.objects.get(id=receipt.id).rationale == (
        "immutable receipt"
    )


@pytest.mark.django_db
def test_protocol_v2_activation_is_blocked_by_unreconciled_person(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=None,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        transition_commercial_journey_protocol(
            club_id=club.id,
            target_version="v2",
            rationale="must not activate",
            idempotency_key="protocol-v2-blocked",
        )

    assert exc_info.value.code == "commercial_journey_activation_not_ready"
    assert ClubSettings.objects.get(club=club).commercial_journey_protocol_version == "v1"
    assert not CommercialJourneyProtocolTransition.objects.filter(club=club).exists()


@pytest.mark.django_db
def test_protocol_v2_activation_requires_effective_unified_journey(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)

    readiness = commercial_journey_activation_readiness(club=club)

    assert readiness["ready"] is False
    assert readiness["activation_prerequisites"]["blocker_codes"] == [
        "effective_unified_client_journey_disabled"
    ]
    with pytest.raises(BusinessLogicError, match="activation readiness is blocked"):
        transition_commercial_journey_protocol(
            club_id=club.id,
            target_version="v2",
            rationale="must keep the effective gate enabled",
            idempotency_key="protocol-v2-unified-gate",
        )


@pytest.mark.django_db
def test_protocol_v2_activation_requires_manual_and_canonical_group_prerequisites(settings, club):
    ClubSettingsFactory(club=club)
    _enable_v2_activation_prerequisites(settings=settings, club=club)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
    rollout = TrainingGroupRolloutState.objects.for_club(club).get()
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.SHADOW,
    )

    readiness = commercial_journey_activation_readiness(club=club)

    assert readiness["ready"] is False
    assert readiness["activation_prerequisites"]["blocker_codes"] == [
        "manual_operational_admission_disabled",
        "training_group_rollout_not_active",
        "training_group_new_writes_disabled",
    ]
    assert readiness["activation_prerequisites"]["canonical_group_writes_enabled"] is False
    with pytest.raises(BusinessLogicError, match="activation readiness is blocked"):
        transition_commercial_journey_protocol(
            club_id=club.id,
            target_version="v2",
            rationale="must keep manual and group commands available",
            idempotency_key="protocol-v2-manual-group-gates",
        )
