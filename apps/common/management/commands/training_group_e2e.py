"""Shared isolated-fixture helpers for canonical TrainingGroup real-stack packs."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import CommandError

from apps.attendance.models import TrainingGroupRolloutState
from apps.attendance.services.training_group_reconciliation import (
    apply_training_group_reconciliation,
    audit_training_groups,
    build_training_group_reconciliation_preview,
)
from apps.attendance.services.training_groups import transition_training_group_rollout_for_owner


def reconcile_fixture_group_to_active(
    *,
    club,
    actor_user_id: int,
    schedule_ids: list[int],
    canonical_name: str,
    responsible_trainer_id: int,
    idempotency_prefix: str,
    require_manual_operational_admission: bool = False,
) -> dict:
    """Map explicit fixture slots through the audited production rollout path."""
    if not settings.TRAINING_GROUP_NEW_WRITES_ENABLED:
        raise CommandError("TrainingGroup real-stack fixture requires new writes to be enabled.")
    if require_manual_operational_admission and not settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED:
        raise CommandError("TrainingGroup payment fixture requires manual operational admission to be enabled.")

    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    if state.mode != TrainingGroupRolloutState.Mode.OFF:
        raise CommandError("TrainingGroup real-stack fixture must start with rollout off.")

    transition_training_group_rollout_for_owner(
        club_id=club.id,
        target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
        actor_id=actor_user_id,
        rationale="Isolated real-stack canonical fixture mapping.",
        idempotency_key=f"{idempotency_prefix}-reconciling",
        rollout_gate_digest="",
    )
    preview = build_training_group_reconciliation_preview(
        club=club,
        schedule_ids=schedule_ids,
        canonical_name=canonical_name,
        responsible_trainer_id=responsible_trainer_id,
        start_dates=[],
    )
    if preview["conflicts"]:
        raise CommandError("TrainingGroup real-stack fixture preview has unresolved conflicts.")
    applied = apply_training_group_reconciliation(
        club=club,
        schedule_ids=schedule_ids,
        canonical_name=canonical_name,
        responsible_trainer_id=responsible_trainer_id,
        start_dates=[],
        preview_digest=preview["digest"],
        actor_user_id=actor_user_id,
        rationale="Isolated real-stack canonical fixture mapping.",
        idempotency_key=f"{idempotency_prefix}-apply",
    )
    if not audit_training_groups(club=club)["valid"]:
        raise CommandError("TrainingGroup real-stack fixture mapping audit is not clean.")

    for target_mode, suffix in (
        (TrainingGroupRolloutState.Mode.SHADOW, "shadow"),
        (TrainingGroupRolloutState.Mode.ACTIVE, "active"),
    ):
        transition_training_group_rollout_for_owner(
            club_id=club.id,
            target_mode=target_mode,
            actor_id=actor_user_id,
            rationale="Isolated real-stack canonical fixture activation.",
            idempotency_key=f"{idempotency_prefix}-{suffix}",
            rollout_gate_digest=applied["rollout_gate_digest"],
        )

    state.refresh_from_db()
    if state.mode != TrainingGroupRolloutState.Mode.ACTIVE:
        raise CommandError("TrainingGroup real-stack fixture did not reach active rollout.")
    return {
        "training_group_id": applied["training_group_id"],
        "selected_schedule_ids": applied["selected_schedule_ids"],
        "rollout_gate_digest": applied["rollout_gate_digest"],
        "mode": state.mode,
        "new_writes_enabled": True,
        "manual_operational_admission_enabled": require_manual_operational_admission,
    }
