from datetime import date, datetime
from uuid import UUID

from ninja import Schema


class CreateLeadIn(Schema):
    first_name: str
    last_name: str = ""
    phone: str = ""
    guardian_phone: str = ""
    date_of_birth: date | None = None
    is_child: bool = False
    source: str = "other"
    assigned_trainer_id: int | None = None


class UpdateLeadStatusIn(Schema):
    status: str


class BookTrialIn(Schema):
    mode: str = "group"
    trial_date: datetime | None = None
    schedule_id: int | None = None
    occurrence_date: date | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    trainer_id: int | None = None
    location_id: int | None = None
    training_type_id: int | None = None


class LoseLeadIn(Schema):
    loss_reason: str


class ReleaseLeadIn(Schema):
    reason: str


class AssignLeadIn(Schema):
    trainer_id: int | None = None
    reason: str = ""


class ContactOutcomeIn(Schema):
    outcome: str
    due_date: date | None = None
    loss_reason: str = ""
    notes: str = ""


class LeadActionOut(Schema):
    kind: str
    label: str
    supporting_text: str
    target_resource_type: str | None = None
    target_resource_id: int | None = None
    context: str


class LeadActionContextOut(Schema):
    primary_action: LeadActionOut | None = None
    active_context: str | None = None
    secondary_capabilities: list[LeadActionOut] = []


class LeadOut(Schema):
    id: int
    first_name: str
    last_name: str
    phone: str
    guardian_phone: str
    is_child: bool
    status: str
    lead_status: str | None
    loss_reason: str | None
    assigned_trainer_id: int | None
    trial_date: datetime | None
    source: str
    created_at: datetime
    workspace: str = "leads_active"
    primary_action: LeadActionOut | None = None


class ContactOutcomeOut(Schema):
    lead: LeadOut
    action_context: LeadActionContextOut | None = None
    next_flow: str | None = None


class LeadPoolOut(Schema):
    id: int
    first_name: str
    last_name: str
    masked_phone: str
    is_child: bool
    status: str
    lead_status: str | None
    loss_reason: str | None
    assigned_trainer_id: int | None
    trial_date: datetime | None
    source: str
    created_at: datetime


class LeadFunnelOut(Schema):
    new: int = 0
    contacted: int = 0
    trial_booked: int = 0
    trial_done: int = 0
    thinking: int = 0


class LandingLeadConsentIn(Schema):
    personal_data: bool
    privacy_policy_version: str
    consent_text_hash: str


class LandingLeadSourceIn(Schema):
    page: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    utm_term: str = ""


class LandingLeadIntakeIn(Schema):
    name: str
    phone: str
    goal: str
    preferred_format: str
    is_child: bool = False
    consent: LandingLeadConsentIn
    source: LandingLeadSourceIn
    idempotency_key: UUID | None = None
    hp_field: str = ""


class LeadIntakeAcceptedDataOut(Schema):
    id: int
    status: str


class LeadIntakeAcceptedOut(Schema):
    data: LeadIntakeAcceptedDataOut
