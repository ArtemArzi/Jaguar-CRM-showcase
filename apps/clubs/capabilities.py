"""Server-owned capability checks for additive club rollouts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from django.conf import settings
from django.db import DatabaseError

from apps.clubs.models import Club, ClubSettings
from apps.common.exceptions import BusinessLogicError

# These are protocol markers, not registered handlers.  Slice 2/4 owns the
# mutation routes; freezing their paths here prevents a cached v2 client from
# silently reaching a legacy parser in the meantime.
PERSONAL_STAFF_SLOT_INTENT_V2_ROUTE = "/personal-availability/v2/slots/{slot_id}/staff-intents/"
PERSONAL_STAFF_DIRECT_INTENT_V2_ROUTE = "/personal-availability/v2/staff-intents/direct/"
GROUP_SALE_MANUAL_V2_ROUTE = "/billing/v2/group-sales/manual/"
GROUP_SALE_BANK_ORDER_V2_ROUTE = "/billing/v2/group-sales/bank-orders/"


@dataclass(frozen=True)
class CommercialJourneyCapability:
    """Read-only, fail-closed tenant capability projection for command guards."""

    protocol_version: Literal["v1", "v2", "invalid"]
    unified_client_journey_enabled: bool
    manual_operational_admission_enabled: bool
    v2_commercial_journey_enabled: bool
    v2_manual_admission_enabled: bool


@dataclass(frozen=True)
class CommercialJourneyCommandAvailability:
    """The only statuses a versioned command guard may project before writes."""

    allows_new_command: bool
    code: Literal[
        "legacy_allowed",
        "available",
        "commercial_journey_unavailable",
        "client_upgrade_required",
        "replay_or_drain",
    ]


def get_commercial_journey_capability(*, club: Club | int) -> CommercialJourneyCapability:
    """Project durable protocol plus base and manual-specific v2 gates.

    Missing settings, corrupted protocol values and database errors must deny
    v2 writes.  The projection deliberately does not activate a tenant or
    create a fallback command family.
    """

    club_id = club.id if isinstance(club, Club) else club
    try:
        row = (
            ClubSettings.objects.filter(club_id=club_id)
            .values(
                "unified_client_journey_enabled",
                "commercial_journey_protocol_version",
            )
            .first()
        )
    except DatabaseError:
        row = None

    protocol_version = row.get("commercial_journey_protocol_version") if row else None
    if protocol_version not in {
        ClubSettings.CommercialJourneyProtocol.V1,
        ClubSettings.CommercialJourneyProtocol.V2,
    }:
        return CommercialJourneyCapability(
            protocol_version="invalid",
            unified_client_journey_enabled=False,
            manual_operational_admission_enabled=False,
            v2_commercial_journey_enabled=False,
            v2_manual_admission_enabled=False,
        )

    unified_enabled = bool(getattr(settings, "UNIFIED_CLIENT_JOURNEY_ENABLED", False)) and bool(
        row["unified_client_journey_enabled"]
    )
    manual_enabled = bool(getattr(settings, "MANUAL_OPERATIONAL_ADMISSION_ENABLED", False))
    v2_commercial_journey_enabled = (
        protocol_version == ClubSettings.CommercialJourneyProtocol.V2
        and unified_enabled
    )
    return CommercialJourneyCapability(
        protocol_version=protocol_version,
        unified_client_journey_enabled=unified_enabled,
        manual_operational_admission_enabled=manual_enabled,
        v2_commercial_journey_enabled=v2_commercial_journey_enabled,
        v2_manual_admission_enabled=(v2_commercial_journey_enabled and manual_enabled),
    )


def get_v1_commercial_journey_command_availability(
    *,
    capability: CommercialJourneyCapability,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Return the pre-write decision for a legacy v1 command.

    Existing accepted command keys are read/drain lifecycle operations.  They
    remain available even if a current emergency gate is off; only *new*
    commercial writes are denied by this function.
    """

    if accepted_replay_or_drain:
        return CommercialJourneyCommandAvailability(True, "replay_or_drain")
    if capability.protocol_version == ClubSettings.CommercialJourneyProtocol.V2:
        return CommercialJourneyCommandAvailability(False, "client_upgrade_required")
    if not capability.unified_client_journey_enabled:
        return CommercialJourneyCommandAvailability(False, "commercial_journey_unavailable")
    return CommercialJourneyCommandAvailability(True, "legacy_allowed")


def get_v2_manual_admission_command_availability(
    *,
    capability: CommercialJourneyCapability,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Gate cash/transfer operational admission; SBP must not call this guard."""

    if accepted_replay_or_drain:
        return CommercialJourneyCommandAvailability(True, "replay_or_drain")
    if capability.v2_manual_admission_enabled:
        return CommercialJourneyCommandAvailability(True, "available")
    return CommercialJourneyCommandAvailability(False, "commercial_journey_unavailable")


def get_v2_provider_command_availability(
    *,
    capability: CommercialJourneyCapability,
    provider_creation_enabled: bool,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Gate SBP commands from provider readiness, never the manual gate."""

    if accepted_replay_or_drain:
        return CommercialJourneyCommandAvailability(True, "replay_or_drain")
    if capability.v2_commercial_journey_enabled and provider_creation_enabled:
        return CommercialJourneyCommandAvailability(True, "available")
    return CommercialJourneyCommandAvailability(False, "commercial_journey_unavailable")


def _get_v2_group_sale_command_availability(
    *,
    command_availability: CommercialJourneyCommandAvailability,
    training_group_rollout_mode: str,
    training_group_new_writes_enabled: bool,
    accepted_replay_or_drain: bool,
) -> CommercialJourneyCommandAvailability:
    """Apply canonical group gates after manual or provider command semantics."""

    if accepted_replay_or_drain:
        return CommercialJourneyCommandAvailability(True, "replay_or_drain")
    if not command_availability.allows_new_command:
        return command_availability
    if training_group_rollout_mode != "active" or not training_group_new_writes_enabled:
        return CommercialJourneyCommandAvailability(False, "commercial_journey_unavailable")
    return CommercialJourneyCommandAvailability(True, "available")


def get_v2_group_manual_admission_command_availability(
    *,
    capability: CommercialJourneyCapability,
    training_group_rollout_mode: str,
    training_group_new_writes_enabled: bool,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Gate canonical cash/transfer admission with its manual-specific gate."""

    return _get_v2_group_sale_command_availability(
        command_availability=get_v2_manual_admission_command_availability(
            capability=capability,
            accepted_replay_or_drain=accepted_replay_or_drain,
        ),
        training_group_rollout_mode=training_group_rollout_mode,
        training_group_new_writes_enabled=training_group_new_writes_enabled,
        accepted_replay_or_drain=accepted_replay_or_drain,
    )


def get_v2_group_provider_command_availability(
    *,
    capability: CommercialJourneyCapability,
    provider_creation_enabled: bool,
    training_group_rollout_mode: str,
    training_group_new_writes_enabled: bool,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Gate canonical SBP with provider and canonical group readiness only.

    The attendance app remains the owner of rollout-state persistence.  Passing
    its already-projected mode into this pure guard keeps tenant protocol
    decisions centralized without creating a clubs -> attendance dependency.
    """

    return _get_v2_group_sale_command_availability(
        command_availability=get_v2_provider_command_availability(
            capability=capability,
            provider_creation_enabled=provider_creation_enabled,
            accepted_replay_or_drain=accepted_replay_or_drain,
        ),
        training_group_rollout_mode=training_group_rollout_mode,
        training_group_new_writes_enabled=training_group_new_writes_enabled,
        accepted_replay_or_drain=accepted_replay_or_drain,
    )


def assert_v2_manual_admission_command_allowed(
    *,
    capability: CommercialJourneyCapability,
    accepted_replay_or_drain: bool = False,
) -> CommercialJourneyCommandAvailability:
    """Raise the typed pre-write denial for cash/transfer admission only."""

    availability = get_v2_manual_admission_command_availability(
        capability=capability,
        accepted_replay_or_drain=accepted_replay_or_drain,
    )
    if not availability.allows_new_command:
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code=availability.code,
        )
    return availability


def is_unified_client_journey_enabled(*, club: Club | int) -> bool:
    """Return the effective journey capability for one club, failing closed."""

    return get_commercial_journey_capability(club=club).unified_client_journey_enabled
