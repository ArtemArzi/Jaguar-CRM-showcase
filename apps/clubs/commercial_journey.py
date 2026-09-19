from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from apps.attendance.models import TrainingGroupRolloutState
from apps.billing.management.commands.audit_operational_admissions import _audit_club
from apps.billing.service_modules.payment_readiness import get_online_payment_capability
from apps.clubs.capabilities import get_commercial_journey_capability
from apps.clubs.models import (
    Club,
    ClubSettings,
    CommercialJourneyProtocolTransition,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.journey_readiness import audit_unified_client_journey_readiness


@dataclass(frozen=True)
class CommercialJourneyTransitionResult:
    receipt: CommercialJourneyProtocolTransition
    replayed: bool
    effective_version: str


def commercial_journey_activation_readiness(*, club: Club, lock_group_rollout: bool = False) -> dict:
    journey = audit_unified_client_journey_readiness(club_ids=(club.id,))
    operational = _audit_club(club=club)
    prerequisites = _activation_prerequisites(
        club=club,
        lock_group_rollout=lock_group_rollout,
    )
    return {
        "ready": bool(
            journey["is_ready"] and operational["clean"] and prerequisites["is_ready"]
        ),
        "journey": {
            "is_ready": journey["is_ready"],
            "invalid_dimension_count": journey["invalid_dimension_count"],
            "blocker_counts": journey["blocker_counts"],
        },
        "operational_admissions": {
            "clean": operational["clean"],
            "invalid_state_counts": operational["invalid_state_counts"],
            "legacy_unlinked_pending_manual_count": operational[
                "legacy_unlinked_pending_manual_count"
            ],
            "reconciliation_required_count": operational[
                "reconciliation_required_count"
            ],
        },
        "activation_prerequisites": prerequisites,
    }


def _activation_prerequisites(*, club: Club, lock_group_rollout: bool) -> dict:
    """Project v2 write prerequisites without making provider access mandatory.

    A protocol receipt is only useful if the customer journey, manual admission,
    and canonical group-sale command family are available immediately after the
    version flip. Online payment remains optional: its safe capability snapshot
    is evidence for the operator, rather than an activation blocker.
    """

    capability = get_commercial_journey_capability(club=club)
    rollout_states = TrainingGroupRolloutState.objects.for_club(club)
    if lock_group_rollout:
        rollout_states = rollout_states.select_for_update(of=("self",))
    rollout_state = rollout_states.first()
    rollout_mode = rollout_state.mode if rollout_state is not None else "missing"
    training_group_new_writes_enabled = bool(
        getattr(settings, "TRAINING_GROUP_NEW_WRITES_ENABLED", False)
    )
    canonical_group_writes_enabled = bool(
        rollout_state is not None
        and rollout_state.mode == TrainingGroupRolloutState.Mode.ACTIVE
        and training_group_new_writes_enabled
    )
    blocker_codes = []
    if not capability.unified_client_journey_enabled:
        blocker_codes.append("effective_unified_client_journey_disabled")
    if not capability.manual_operational_admission_enabled:
        blocker_codes.append("manual_operational_admission_disabled")
    if rollout_state is None:
        blocker_codes.append("training_group_rollout_state_missing")
    elif rollout_state.mode != TrainingGroupRolloutState.Mode.ACTIVE:
        blocker_codes.append("training_group_rollout_not_active")
    if not training_group_new_writes_enabled:
        blocker_codes.append("training_group_new_writes_disabled")

    provider = get_online_payment_capability()
    return {
        "is_ready": not blocker_codes,
        "blocker_codes": blocker_codes,
        "effective_unified_client_journey_enabled": capability.unified_client_journey_enabled,
        "manual_operational_admission_enabled": capability.manual_operational_admission_enabled,
        "training_group_rollout_mode": rollout_mode,
        "training_group_new_writes_enabled": training_group_new_writes_enabled,
        "canonical_group_writes_enabled": canonical_group_writes_enabled,
        "provider": {
            "enabled": provider.enabled,
            "reason_code": provider.reason_code,
            "creation_enabled": provider.creation_enabled,
            "reconciliation_enabled": provider.reconciliation_enabled,
            "reconciliation_available": provider.reconciliation_available,
        },
    }


def transition_commercial_journey_protocol(
    *,
    club_id: int,
    target_version: str,
    rationale: str,
    idempotency_key: str,
    actor_user_id: int | None = None,
) -> CommercialJourneyTransitionResult:
    key = idempotency_key.strip()
    reason = rationale.strip()
    if target_version not in ClubSettings.CommercialJourneyProtocol.values:
        raise BusinessLogicError("Unsupported commercial journey protocol", code="protocol_invalid")
    if not key or len(key) > 120:
        raise BusinessLogicError("A stable transition key is required", code="idempotency_key_required")
    if not reason or len(reason) > 500:
        raise BusinessLogicError("A bounded transition rationale is required", code="rationale_required")

    with transaction.atomic():
        club = Club.objects.select_for_update(of=("self",)).get(id=club_id)
        settings_row = ClubSettings.objects.select_for_update(of=("self",)).get(club_id=club_id)
        existing = CommercialJourneyProtocolTransition.objects.filter(
            club_id=club_id,
            idempotency_key=key,
        ).first()
        if existing is not None:
            if (
                existing.target_version != target_version
                or existing.rationale != reason
                or existing.actor_id != actor_user_id
            ):
                raise BusinessLogicError(
                    "Transition key was already used for another command",
                    code="idempotency_conflict",
                )
            return CommercialJourneyTransitionResult(
                receipt=existing,
                replayed=True,
                effective_version=settings_row.commercial_journey_protocol_version,
            )

        previous_version = settings_row.commercial_journey_protocol_version
        if previous_version == target_version:
            raise BusinessLogicError(
                "Commercial journey protocol already has the requested version",
                code="protocol_already_current",
            )

        readiness = commercial_journey_activation_readiness(club=club, lock_group_rollout=True)
        if (
            target_version == ClubSettings.CommercialJourneyProtocol.V2
            and not readiness["ready"]
        ):
            raise BusinessLogicError(
                "Commercial journey v2 activation readiness is blocked",
                code="commercial_journey_activation_not_ready",
            )

        settings_row.commercial_journey_protocol_version = target_version
        settings_row.save(
            update_fields=["commercial_journey_protocol_version", "updated_at"]
        )
        receipt = CommercialJourneyProtocolTransition.objects.create(
            club=club,
            previous_version=previous_version,
            target_version=target_version,
            rationale=reason,
            idempotency_key=key,
            actor_id=actor_user_id,
            readiness_snapshot=readiness,
        )
        return CommercialJourneyTransitionResult(
            receipt=receipt,
            replayed=False,
            effective_version=target_version,
        )
