from __future__ import annotations

import datetime as datetime_module
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from ninja import Schema
from pydantic import ConfigDict, Field


class ScheduleIn(Schema):
    day_of_week: int  # 0=Monday..6=Sunday
    start_time: time
    end_time: time
    group_name: str
    trainer_id: int
    location_id: int
    training_type_id: int
    one_time_date: date | None = None
    training_group_id: int | None = None


class ScheduleOut(Schema):
    id: int
    day_of_week: int
    start_time: time
    end_time: time
    group_name: str
    training_group_id: int | None = None
    trainer_id: int
    trainer_name: str = ""
    location_id: int
    location_name: str = ""
    is_active: bool
    training_type_id: int | None = None
    one_time_date: date | None = None


    @staticmethod
    def resolve_group_name(obj) -> str:
        if hasattr(obj, "training_group") and obj.training_group:
            return obj.training_group.name
        return obj.group_name


    @staticmethod
    def resolve_trainer_name(obj) -> str:
        if hasattr(obj, "trainer") and obj.trainer:
            return f"{obj.trainer.first_name} {obj.trainer.last_name}"
        return ""

    @staticmethod
    def resolve_location_name(obj) -> str:
        if hasattr(obj, "location") and obj.location:
            return obj.location.name
        return ""


class TrainingGroupReconciliationInventoryItemOut(Schema):
    schedule_id: int
    group_name: str
    day_of_week: int
    start_time: str
    end_time: str
    is_active: bool
    training_type_id: int
    training_type_name: str
    location_id: int
    location_name: str
    trainer_id: int
    training_group_id: int | None = None
    permanent_enrollment_ids: list[int]
    payment_target_ids: list[int]
    refund_case_count: int
    history_counts: dict[str, int]


class TrainingGroupReconciliationInventoryOut(Schema):
    schedules: list[TrainingGroupReconciliationInventoryItemOut]


class TrainingGroupReconciliationStartDateIn(Schema):
    student_id: int
    starts_on: date


class TrainingGroupReconciliationPreviewIn(Schema):
    schedule_ids: list[int]
    canonical_name: str = Field(min_length=1, max_length=200)
    responsible_trainer_id: int | None = None
    start_dates: list[TrainingGroupReconciliationStartDateIn] = []


class TrainingGroupReconciliationPreviewOut(Schema):
    version: int
    digest: str
    canonical_group: dict
    selected_schedule_ids: list[int]
    proposed_memberships: list[dict]
    compatibility_projections: list[dict]
    proposed_roster_deltas: list[dict]
    conflicts: list[dict]


class TrainingGroupReconciliationApplyIn(TrainingGroupReconciliationPreviewIn):
    preview_digest: str = Field(min_length=64, max_length=64)
    rationale: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class TrainingGroupReconciliationApplyOut(Schema):
    training_group_id: int
    status: str
    preview_digest: str
    selected_schedule_ids: list[int]
    membership_count: int
    projection_count: int
    linked_payment_count: int
    roster_delta_digest: str
    rollout_gate_digest: str
    batch_id: str


class TrainingGroupRolloutTransitionIn(Schema):
    target_mode: str = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)
    rollout_gate_digest: str = ""


class TrainingGroupRolloutTransitionOut(Schema):
    mode: str
    reconciling_from_mode: str


class TrainingGroupCreateIn(Schema):
    name: str = Field(min_length=1, max_length=200)
    training_type_id: int
    location_id: int
    responsible_trainer_id: int


class TrainingGroupOut(Schema):
    id: int
    name: str
    training_type_id: int
    location_id: int
    responsible_trainer_id: int | None = None
    responsible_trainer_name: str = ""
    status: str


    @staticmethod
    def resolve_responsible_trainer_name(obj) -> str:
        trainer = getattr(obj, "responsible_trainer", None)
        return f"{trainer.first_name} {trainer.last_name}".strip() if trainer else ""


class TrainingGroupMembershipCreateIn(Schema):
    student_id: int
    training_group_id: int
    starts_on: date
    source: str = "manual"
    rationale: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class TrainingGroupMembershipLifecycleIn(Schema):
    rationale: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class TrainingGroupMembershipCancelIn(TrainingGroupMembershipLifecycleIn):
    ends_on: date


class TrainingGroupMembershipTransferIn(TrainingGroupMembershipLifecycleIn):
    target_training_group_id: int
    ends_on: date


class TrainingGroupMembershipOut(Schema):
    id: int
    student_id: int
    student_name: str = ""
    training_group_id: int
    training_group_name: str = ""
    status: str
    starts_on: date
    ends_on: date | None = None
    source: str
    authority: str


    @staticmethod
    def resolve_student_name(obj) -> str:
        student = getattr(obj, "student", None)
        return f"{student.first_name} {student.last_name}".strip() if student else ""


    @staticmethod
    def resolve_training_group_name(obj) -> str:
        group = getattr(obj, "training_group", None)
        return group.name if group else ""


class TrainingGroupReassignIn(Schema):
    responsible_trainer_id: int


class TrainingGroupArchiveIn(Schema):
    rationale: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ScheduleOccurrenceOut(Schema):
    schedule_id: int
    group_name: str
    effective_date: date
    effective_start_time: time
    effective_end_time: time
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    one_time_date: date | None = None
    is_rescheduled: bool = False
    is_substitute: bool = False
    training_type_id: int | None = None
    training_type_name: str = ""
    training_type_kind: str = ""
    enrollment_id: int | None = None
    training_group_membership_id: int | None = None
    created_from: str = ""
    can_cancel: bool = False


class KioskScheduleOut(Schema):
    schedule_id: int
    effective_date: date
    start_time: time
    end_time: time
    group_name: str
    trainer_name: str
    location_name: str
    training_type_id: int
    training_type_name: str


class KioskBrandingOut(Schema):
    primary_color: str
    accent_color: str
    club_name_display: str
    logo_url: str


class ScheduleUpdate(Schema):
    day_of_week: int | None = None
    start_time: time | None = None
    end_time: time | None = None
    group_name: str | None = None
    trainer_id: int | None = None
    location_id: int | None = None
    training_type_id: int | None = None
    is_active: bool | None = None
    one_time_date: date | None = None


class CancelSessionIn(Schema):
    date: date
    reason: str = ""


class CloseSessionIn(Schema):
    date: date
    topic_tags: list[str] = []
    notes: str = ""


class GroupSessionOut(Schema):
    id: int
    schedule_id: int
    date: date
    trainer_id: int
    attendee_count: int
    topic_tags: list[str]
    notes: str
    closed_at: datetime | None = None
    closed_by_id: int | None = None
    close_source: str = ""


class RescheduleIn(Schema):
    date: date
    new_date: date
    new_start_time: time
    new_end_time: time
    reason: str = ""


class SubstituteIn(Schema):
    date: date
    substitute_trainer_id: int
    reason: str = ""


class ScheduleExceptionOut(Schema):
    id: int
    schedule_id: int
    date: date
    exception_type: str
    reason: str
    new_date: date | None
    new_start_time: time | None
    new_end_time: time | None
    substitute_trainer_id: int | None
    substitute_trainer_name: str = ""

    @staticmethod
    def resolve_substitute_trainer_name(obj) -> str:
        if hasattr(obj, "substitute_trainer") and obj.substitute_trainer:
            return f"{obj.substitute_trainer.first_name} {obj.substitute_trainer.last_name}"
        return ""


class ScheduleEnrollmentIn(Schema):
    student_id: int
    schedule_id: int
    status: str = "active"
    starts_on: date | None = None
    ends_on: date | None = None


class ScheduleEnrollmentTransferIn(Schema):
    target_schedule_id: int
    ends_on: date


class ScheduleEnrollmentEndIn(Schema):
    ends_on: date


class ScheduleEnrollmentOut(Schema):
    id: int
    student_id: int
    student_name: str = ""
    schedule_id: int
    schedule_group_name: str = ""
    status: str
    starts_on: date | None = None
    ends_on: date | None = None
    trial_at: datetime | None = None
    created_from: str

    @staticmethod
    def resolve_student_name(obj) -> str:
        if hasattr(obj, "student") and obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}"
        return ""

    @staticmethod
    def resolve_schedule_group_name(obj) -> str:
        if hasattr(obj, "schedule") and obj.schedule:
            return obj.schedule.group_name
        return ""


class GuestVisitIn(Schema):
    date: date
    student_id: int | None = None
    lead_id: int | None = None
    idempotency_key: str | None = None
    origin: Literal["planned_session_action", "walk_in_checkin"] = "planned_session_action"


class GuestBookingIn(Schema):
    date: date
    child_student_id: int | None = None
    idempotency_key: str | None = None


class GuestBookingOptionOut(Schema):
    schedule_id: int
    date: date
    start_time: time
    end_time: time
    group_name: str
    trainer_name: str
    location_name: str
    training_type_id: int
    training_type_name: str
    booking_status: Literal["can_book", "already_booked", "blocked"]
    reason_code: str = ""
    financial_status: Literal["subscription", "trial_free", "drop_in_debt", "blocked"]
    subscription_id: int | None = None
    drop_in_price: str | None = None


class BookingCancelIn(Schema):
    reason: str = ""


class PersonalBookingRescheduleIn(Schema):
    destination_slot_id: int
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class GuestVisitFinancialPreviewOut(Schema):
    code: str
    message: str


class GuestVisitOut(Schema):
    enrollment_id: int
    student_id: int
    display_name: str
    schedule_id: int
    created_from: str
    is_guest_visit: bool
    starts_on: date | None = None
    ends_on: date | None = None
    created: bool
    already_member: bool
    origin: str
    financial_preview: GuestVisitFinancialPreviewOut


class GuestVisitCandidateOut(Schema):
    id: int
    kind: Literal["student", "lead"]
    first_name: str
    last_name: str
    masked_phone: str
    status: str


class PersonalBookingIn(Schema):
    starts_at: datetime
    ends_at: datetime
    location_id: int
    training_type_id: int
    trainer_id: int | None = None
    subscription_id: int | None = None
    idempotency_key: str | None = None


class PersonalBookingOut(Schema):
    schedule_id: int
    enrollment_id: int
    availability_slot_id: int | None = None
    student_id: int
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    starts_at: datetime
    ends_at: datetime
    created_from: str
    created: bool


class PersonalDropInBookingIn(Schema):
    starts_at: datetime
    ends_at: datetime
    location_id: int
    training_type_id: int
    tariff_id: int | None = None
    offer_digest: str | None = None
    trainer_id: int | None = None
    availability_slot_id: int | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityDropInBookIn(Schema):
    student_id: int
    tariff_id: int | None = None
    offer_digest: str | None = None
    idempotency_key: str | None = None


class PersonalDropInBookingOut(Schema):
    booking_id: int
    schedule_id: int
    enrollment_id: int
    availability_slot_id: int | None = None
    student_id: int
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    tariff_id: int
    tariff_name_snapshot: str
    price_snapshot: str
    starts_at: datetime
    ends_at: datetime
    attendance_state: str
    financial_state: str
    debt_id: int | None = None
    created: bool


class PersonalDropInPaymentIn(Schema):
    payment_method: Literal["cash", "transfer"]
    discount_ids: list[int] = []
    idempotency_key: str | None = None
    debt_id: int | None = None


class PersonalDropInBankPaymentOrderIn(Schema):
    idempotency_key: str | None = None
    buyer_email: str | None = None
    buyer_phone: str | None = None
    debt_id: int | None = None


class PersonalDropInPaymentLinkOut(Schema):
    id: int
    booking_id: int
    payment_id: int
    bank_payment_order_id: int | None = None
    subscription_id: int | None = None
    payment_status: str
    order_status: str = ""
    provider_payment_url: str = ""


class PersonalDropInNoShowIn(Schema):
    reason: str


class PersonalBookingPaymentReservationCreateIn(Schema):
    starts_at: datetime
    ends_at: datetime
    location_id: int
    training_type_id: int
    tariff_id: int | None = None
    offer_digest: str | None = None
    availability_slot_id: int | None = None
    trainer_id: int | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityPaymentReservationCreateIn(Schema):
    child_student_id: int | None = None
    tariff_id: int | None = None
    offer_digest: str | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityPaymentReservationCancelIn(Schema):
    child_student_id: int | None = None


class PersonalBookingPaymentReservationOut(Schema):
    id: int
    student_id: int
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    tariff_id: int
    tariff_name: str
    availability_slot_id: int | None = None
    starts_at: datetime
    ends_at: datetime
    status: str
    expires_at: datetime
    payment_id: int | None = None
    bank_payment_order_id: int | None = None
    subscription_id: int | None = None
    schedule_id: int | None = None
    enrollment_id: int | None = None
    provider_payment_url: str = ""
    amount_snapshot: str = ""
    order_status: str = ""
    can_cancel: bool = False
    created_at: datetime


class PersonalAvailabilityBookIn(Schema):
    child_student_id: int | None = None
    subscription_id: int | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityStaffBookIn(Schema):
    student_id: int
    subscription_id: int | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityStaffPaymentReservationIn(Schema):
    student_id: int
    tariff_id: int | None = None
    offer_digest: str | None = None
    idempotency_key: str | None = None


class PersonalAvailabilityStaffIntentIn(Schema):
    student_id: int
    payment_method: Literal["entitlement", "cash", "transfer", "sbp", "pay_at_visit"]
    subscription_id: int | None = None
    offer_digest: str | None = None
    discount_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)


class PersonalAvailabilityDirectStaffIntentIn(PersonalAvailabilityStaffIntentIn):
    trainer_id: int
    starts_at: datetime
    ends_at: datetime
    location_id: int
    training_type_id: int


class PersonalAvailabilityStaffIntentV2In(Schema):
    """Frozen v2 command boundary; mutation routes land in Slice 2."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["v2"]
    student_id: int
    payment_method: Literal["cash", "transfer", "sbp"]
    offer_digest: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=120)
    discount_id: int | None = None


class PersonalAvailabilityDirectStaffIntentV2In(PersonalAvailabilityStaffIntentV2In):
    trainer_id: int
    starts_at: datetime
    ends_at: datetime
    location_id: int
    training_type_id: int


class PersonalAvailabilityDirectOfferPreviewOut(Schema):
    offer_tariff_id: int
    offer_tariff_name: str
    offer_price: str
    offer_duration_days: int
    offer_scope: str
    offer_location_id: int | None = None
    offer_trainer_id: int | None = None
    offer_base_amount: str = ""
    offer_discount_id: int | None = None
    offer_discount_name: str = ""
    offer_discount_type: str = ""
    offer_discount_value: str = ""
    offer_discount_amount: str = ""
    offer_payable_amount: str = ""
    offer_digest: str
    offer_error_code: str = ""


class PersonalCommercialReceiptOut(Schema):
    kind: str
    booking_id: int | None = None
    reservation_id: int | None = None
    payment_id: int | None = None
    subscription_id: int | None = None
    bank_payment_order_id: int | None = None
    debt_id: int | None = None
    slot_id: int | None = None
    schedule_id: int | None = None
    training_group_id: int | None = None
    group_membership_id: int | None = None
    group_name: str = ""
    target_start_date: date | None = None
    renewed_from_subscription_id: int | None = None
    enrollment_id: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    tariff_id: int | None = None
    tariff_name: str = ""
    renewal_target_tariff_id: int | None = None
    renewal_target_tariff_name: str = ""
    renewal_target_price: Decimal | None = None
    amount: str = ""
    payment_method: str
    status: str
    provider_payment_url: str = ""
    allowed_actions: list[str]
    resource_route: str
    attempted_at: datetime


class PersonalCommercialReceiptV2Out(PersonalCommercialReceiptOut):
    """Strict v2 command receipt; lifecycle and replay remain independent."""

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


class TrainerPersonalAvailabilityGenerateIn(Schema):
    date_from: date
    date_to: date
    weekdays: list[int]
    start_time: time
    end_time: time
    slot_duration_minutes: int | None = None
    buffer_minutes: int = 0
    location_id: int
    training_type_id: int
    trainer_id: int | None = None


class TrainerPersonalAvailabilityBlockIn(Schema):
    reason: str = ""


class TrainerPersonalAvailabilitySlotOut(Schema):
    id: int
    date: date
    starts_at: datetime
    ends_at: datetime
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    training_type_kind: str
    status: Literal["published", "held", "booked", "blocked", "cancelled"]
    block_reason: str = ""
    booked_enrollment_id: int | None = None
    can_block: bool
    can_unblock: bool
    can_cancel: bool
    offer_tariff_id: int | None = None
    offer_tariff_name: str = ""
    offer_price: str = ""
    offer_duration_days: int | None = None
    offer_scope: str = ""
    offer_location_id: int | None = None
    offer_trainer_id: int | None = None
    offer_base_amount: str = ""
    offer_discount_id: int | None = None
    offer_discount_name: str = ""
    offer_discount_type: str = ""
    offer_discount_value: str = ""
    offer_discount_amount: str = ""
    offer_payable_amount: str = ""
    offer_digest: str = ""
    offer_error_code: str = ""


class TrainerPersonalAvailabilitySkippedOut(Schema):
    date: date
    starts_at: datetime
    ends_at: datetime
    reason_code: str


class TrainerPersonalAvailabilityGenerateOut(Schema):
    created: list[TrainerPersonalAvailabilitySlotOut]
    skipped: list[TrainerPersonalAvailabilitySkippedOut]


class PersonalAvailabilityOptionOut(Schema):
    slot_id: int
    date: date
    starts_at: datetime
    ends_at: datetime
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    booking_status: Literal["can_book", "can_pay", "blocked"]
    reason_code: str = ""
    subscription_id: int | None = None
    payment_tariff_id: int | None = None
    payment_tariff_name: str = ""
    payment_amount: str = ""
    offer_tariff_id: int | None = None
    offer_tariff_name: str = ""
    offer_price: str = ""
    offer_duration_days: int | None = None
    offer_scope: str = ""
    offer_location_id: int | None = None
    offer_trainer_id: int | None = None
    offer_base_amount: str = ""
    offer_discount_id: int | None = None
    offer_discount_name: str = ""
    offer_discount_type: str = ""
    offer_discount_value: str = ""
    offer_discount_amount: str = ""
    offer_payable_amount: str = ""
    offer_digest: str = ""
    offer_error_code: str = ""


class PersonalAvailabilityOfferPreviewOut(Schema):
    offer_tariff_id: int
    offer_tariff_name: str
    offer_price: str
    offer_duration_days: int
    offer_scope: str
    offer_location_id: int | None = None
    offer_trainer_id: int | None = None
    offer_base_amount: str = ""
    offer_discount_id: int | None = None
    offer_discount_name: str = ""
    offer_discount_type: str = ""
    offer_discount_value: str = ""
    offer_discount_amount: str = ""
    offer_payable_amount: str = ""
    offer_digest: str = ""
    offer_error_code: str = ""


class PersonalAvailabilityCapabilityOut(Schema):
    enabled: bool
    # This names only the route protocol, never owner-only readiness details.
    staff_command_protocol_version: Literal["v1", "v2", "invalid"]


class PersonalSelfServiceOptionOut(Schema):
    slot_id: int
    date: date
    starts_at: datetime
    ends_at: datetime
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    capability: Literal["can_book", "can_pay"]
    offer_tariff_name: str = ""
    offer_price: str = ""
    offer_digest: str = ""


class PersonalSelfServiceCommandIn(Schema):
    child_student_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    offer_digest: str | None = None


class PersonalSelfServiceCommandCardOut(Schema):
    command_id: int
    slot_id: int | None = None
    capability: Literal["can_book", "can_pay"]
    status: str
    starts_at: datetime
    ends_at: datetime
    booking_id: int | None = None
    reservation_id: int | None = None
    bank_payment_order_id: int | None = None
    provider_payment_url: str = ""
    amount_snapshot: str = ""
    order_status: str = ""
    allowed_actions: list[
        Literal[
            "open_bank_payment_order",
            "cancel_bank_payment_order",
            "retry_bank_payment",
            "view_booking",
        ]
    ] = Field(default_factory=list)


class PersonalSelfServiceCommandCollectionsOut(Schema):
    live: list[PersonalSelfServiceCommandCardOut] = Field(default_factory=list)
    latest_terminal: list[PersonalSelfServiceCommandCardOut] = Field(default_factory=list)


class ScheduleEnrollmentTransferOut(Schema):
    closed_enrollment: ScheduleEnrollmentOut
    new_enrollment: ScheduleEnrollmentOut


# ──────────────────────────────────────────────
# Check-in schemas
# ──────────────────────────────────────────────


class PhoneLookupIn(Schema):
    phone_suffix: str  # last 4 digits


class StudentMatchOut(Schema):
    id: int
    first_name: str
    last_name: str
    lookup_suffix: str
    lookup_suffixes: list[str] = Field(default_factory=list)
    masked_phone: str
    group_name: str = ""
    grade_name: str = ""
    subscription_name: str = ""
    subscription_status: str = ""
    trainings_left: int | None = None


class KioskRosterStudentOut(Schema):
    id: int
    first_name: str
    last_name: str
    lookup_suffix: str
    lookup_suffixes: list[str] = Field(default_factory=list)
    masked_phone: str
    group_name: str = ""
    grade_name: str = ""
    subscription_name: str = ""
    subscription_status: str = ""
    trainings_left: int | None = None


class KioskActivateIn(Schema):
    pin: str  # 6-digit PIN


class KioskActivateOut(Schema):
    token: str
    club_id: int
    club_name: str


class KioskCheckinIn(Schema):
    student_id: int
    schedule_id: int
    training_type_id: int
    checkin_date: date | None = None
    client_id: str | None = None
    idempotency_key: str | None = None


class KioskOptionsIn(Schema):
    student_id: int
    date: datetime_module.date | None = None


class KioskScheduleOptionOut(Schema):
    schedule_id: int
    effective_date: date
    start_time: time
    end_time: time
    group_name: str
    trainer_name: str
    location_name: str
    training_type_id: int
    training_type_name: str
    self_checkin_status: Literal["can_checkin", "can_book_guest_visit", "blocked"]
    reason_code: str = ""
    financial_status: Literal["subscription", "trial_free", "drop_in_debt", "blocked"]
    subscription_id: int | None = None
    drop_in_price: str | None = None
    existing_checkin_id: int | None = None
    checkin_window_status: Literal["too_early", "open", "closed"]
    checkin_opens_at: datetime
    checkin_closes_at: datetime


class KioskOptionsOut(Schema):
    student_id: int
    date: date
    options: list[KioskScheduleOptionOut]


class AlertOut(Schema):
    type: str
    icon: str
    message: str


class StudentWithAlertsOut(Schema):
    id: int
    first_name: str
    last_name: str
    enrollment_id: int | None = None
    training_group_membership_id: int | None = None
    created_from: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    is_guest_visit: bool = False
    enrollment_status: str | None = None
    checkin_blocked_reason: str | None = None
    alerts: list[AlertOut]


class SessionRosterStudentOut(StudentWithAlertsOut):
    checkin_status: Literal["checked_in", "waiting", "blocked"]
    checkin_id: int | None = None
    checkin_source: str = ""
    checked_in_at: datetime | None = None


class SessionSummaryOut(Schema):
    expected_count: int
    checked_in_count: int
    waiting_count: int
    blocked_count: int


class SessionDetailOut(Schema):
    schedule_id: int
    date: date
    occurrence: ScheduleOccurrenceOut
    is_closed: bool
    group_session_id: int | None = None
    closed_at: datetime | None = None
    closed_by_id: int | None = None
    close_source: str = ""
    can_close: bool
    close_allowed_at: datetime
    close_block_reason: str = ""
    summary: SessionSummaryOut
    roster: list[SessionRosterStudentOut]


class CheckinResultOut(Schema):
    checkin_id: int
    student_id: int
    is_debt: bool
    subscription_id: int | None = None
    alerts: list[AlertOut] = []
    created: bool = True
    duplicate: bool = False
    subscription_effect: str = "none"
    debt_effect: str = "none"
    salary_queued: bool = False
    parent_notification_queued: bool = False
    grade_progress_queued: bool = False
    group_analytics_queued: bool = False
    retention_auto_close_queued: bool = False
    post_trial_task_queued: bool = False
    trainings_left_push_queued: bool = False


class ScheduleCheckinStatusOut(Schema):
    student_ids: list[int]
    has_group_session: bool


class BatchCheckinIn(Schema):
    schedule_id: int
    date: date
    present_student_ids: list[int]
    training_type_id: int
    topic_tags: list[str] = []
    notes: str = ""


class BatchCheckinResultOut(Schema):
    checkins: list[CheckinResultOut]
    group_session_id: int


class OfflineSyncIn(Schema):
    checkins: list[KioskCheckinIn]


class OfflineSyncItemResult(Schema):
    client_id: str | None = None
    idempotency_key: str | None = None
    student_id: int
    success: bool
    checkin_id: int | None = None
    duplicate: bool = False
    error: str | None = None
    retryable: bool


class OfflineSyncResultOut(Schema):
    synced: int
    failed: int
    results: list[OfflineSyncItemResult]


class TodayCheckinOut(Schema):
    id: int
    student_id: int
    student_name: str = ""
    schedule_id: int
    date: date
    source: str
    is_debt: bool

    @staticmethod
    def resolve_student_name(obj) -> str:
        if hasattr(obj, "student") and obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}"
        return ""
