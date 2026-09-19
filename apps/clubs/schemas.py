from typing import Literal

from ninja import Schema


class RegisterClubIn(Schema):
    name: str
    city: str
    disciplines: list[str] = []
    location_name: str = ""


class ClubOut(Schema):
    id: int
    name: str
    city: str
    disciplines: list[str]
    timezone: str
    is_active: bool


class LocationOut(Schema):
    id: int
    name: str
    address: str


class ClubUpdateIn(Schema):
    name: str | None = None
    city: str | None = None
    disciplines: list[str] | None = None


class MemberOut(Schema):
    id: int
    user_email: str
    role: str


class CommercialJourneyCapabilityOut(Schema):
    commercial_journey_protocol_version: Literal["v1", "v2", "invalid"]
    unified_client_journey_enabled: bool
    manual_operational_admission_enabled: bool
    v2_commercial_journey_enabled: bool
    v2_manual_admission_enabled: bool
    v1_new_command_status: Literal[
        "legacy_allowed",
        "commercial_journey_unavailable",
        "client_upgrade_required",
    ]
    v2_personal_manual_command_status: Literal["available", "commercial_journey_unavailable"]
    v2_personal_sbp_command_status: Literal["available", "commercial_journey_unavailable"]
    v2_group_manual_command_status: Literal["available", "commercial_journey_unavailable"]
    v2_group_sbp_command_status: Literal["available", "commercial_journey_unavailable"]
    accepted_replay_status: Literal["replay_or_drain"]


class CommercialJourneyCommandResultOut(Schema):
    """Shared v2 outcome vocabulary; replay is deliberately orthogonal."""

    workspace_state: Literal["student", "lead"]
    finance_state: Literal[
        "pending_manual",
        "provider_pending",
        "confirmed",
        "rejected",
        "cancelled",
        "expired",
        "failed",
    ]
    command_replayed: bool
