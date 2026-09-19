from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from ninja import Field, Schema

from apps.attendance.schemas import PersonalCommercialReceiptOut, ScheduleOccurrenceOut


class StudentIn(Schema):
    first_name: str
    last_name: str
    phone: str = ""
    guardian_phone: str = ""
    email: str = ""
    date_of_birth: date | None = None
    is_child: bool = False
    source: str = "other"


class StudentIntakeIn(Schema):
    idempotency_key: UUID
    intake_kind: str
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(default="", max_length=100)
    phone: str = ""
    guardian_phone: str = ""
    date_of_birth: date | None = None
    is_child: bool = False
    source: str = "other"
    assigned_trainer_id: int | None = None
    confirm_distinct_child: bool = False


class StudentIntakeOut(Schema):
    result_kind: str
    target_workspace: str
    route: str | None
    identity_visibility: str
    allowed_action: str | None
    student_id: int | None = None
    detail: str | None = None
    code: str | None = None
    commercial_segment: Literal["no_crm_entitlement"] | None = None


class StudentIntakeCapabilityOut(Schema):
    enabled: bool
    # A fresh staff UI may use v1 only while the tenant explicitly says v1.
    # Unknown/stale capability data must never downgrade a cached v2 wizard.
    group_sale_command_protocol_version: Literal["v1", "v2", "invalid"]


class StudentCommercialContextOut(Schema):
    student_id: int
    attempts: list[PersonalCommercialReceiptOut]


class PersonalPaymentMethodCorrectionIn(Schema):
    reservation_id: int | None = None
    payment_id: int | None = None
    replacement_payment_method: Literal["cash", "transfer", "pay_at_visit"]
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)


class StudentOut(Schema):
    id: int
    first_name: str
    last_name: str
    phone: str
    guardian_phone: str
    email: str
    date_of_birth: date | None
    is_child: bool
    status: str
    source: str
    commercial_segment: Literal[
        "former",
        "at_risk",
        "pending_admission",
        "active_entitlement",
        "no_crm_entitlement",
    ] | None = None

    @staticmethod
    def resolve_commercial_segment(obj):
        return getattr(obj, "_commercial_segment", None)


class PersonSearchOut(Schema):
    id: int | None = None
    display_name: str | None = None
    masked_phone: str | None = None
    target_workspace: Literal["leads_active", "leads_archived", "students"] | None = None
    route: str | None = None
    identity_visibility: Literal["full", "masked", "none"]
    allowed_action: str | None = None
    commercial_segment: str | None = None


class StudentNoteIn(Schema):
    text: str


class StudentNoteOut(Schema):
    id: int
    text: str
    author_email: str
    created_at: str

    @staticmethod
    def resolve_author_email(obj) -> str:
        return obj.author.email if obj.author else ""

    @staticmethod
    def resolve_created_at(obj) -> str:
        return obj.created_at.isoformat()


class AccountAccessSummaryOut(Schema):
    role: str
    status: str
    username: str
    must_change_password: bool
    issued_at: datetime
    reset_at: datetime | None


class AccountAccessOpenIn(Schema):
    parent_phone: str | None = None


class AccountAccessIssueOut(AccountAccessSummaryOut):
    student_id: int
    temporary_password: str | None = None
    created_user: bool
    created_membership: bool
    created_access: bool


class OperationalAdmissionOut(Schema):
    payment_id: int
    payment_status: str
    payment_method: str
    subscription_status: str | None
    enrollment_status: str
    group_label: str
    training_group_id: int | None = None
    group_membership_id: int | None = None
    start_date: date
    checkin_ready: bool
    account_access_eligible: bool
    covered_visit_count: int


class OperationalAdmissionV2Out(Schema):
    """Additive union; v1 stays group-only for cached clients."""

    kind: Literal["group", "personal"]
    payment_id: int
    recorded_by_id: int
    payment_status: str
    payment_method: str
    subscription_status: str | None
    start_date: date
    checkin_ready: bool
    account_access_eligible: bool
    is_qualifying: bool
    group_label: str | None = None
    training_group_id: int | None = None
    group_membership_id: int | None = None
    enrollment_status: str | None = None
    booking_id: int | None = None
    session_id: int | None = None
    booking_state: str | None = None


class CoveredVisitOut(Schema):
    payment_id: int
    debt_id: int
    checkin_id: int
    training_type_name: str
    checkin_date: date
    coverage_state: str
    is_payable: bool


class CabinetFinancialOut(Schema):
    operational_admission: OperationalAdmissionOut | None
    operational_admissions: list[OperationalAdmissionOut]
    operational_admission_v2: OperationalAdmissionV2Out | None = None
    operational_admissions_v2: list[OperationalAdmissionV2Out] = []
    covered_visits: list[CoveredVisitOut]


class StudentDetailOut(StudentOut):
    contraindications: str
    notes: list[StudentNoteOut] = []
    account_access: AccountAccessSummaryOut | None = None
    operational_admission: OperationalAdmissionOut | None = None
    operational_admission_v2: OperationalAdmissionV2Out | None = None
    account_access_eligible: bool = False
    has_parent_user: bool = False
    can_manage_sensitive_actions: bool = False
    can_manage_account_access: bool = True
    can_manage_feedback: bool = True

    @staticmethod
    def resolve_has_parent_user(obj) -> bool:
        return bool(getattr(obj, "parent_user_id", None))

    @staticmethod
    def resolve_can_manage_account_access(obj) -> bool:
        return bool(getattr(obj, "_can_manage_account_access", True))

    @staticmethod
    def resolve_operational_admission(obj):
        admission = getattr(obj, "_operational_admission", None)
        return admission.as_dict() if admission else None

    @staticmethod
    def resolve_operational_admission_v2(obj):
        admission = getattr(obj, "_operational_admission_v2", None)
        return admission.as_dict() if admission else None

    @staticmethod
    def resolve_account_access_eligible(obj) -> bool:
        return bool(getattr(obj, "_account_access_eligible", False))

    @staticmethod
    def resolve_can_manage_sensitive_actions(obj) -> bool:
        return bool(getattr(obj, "_can_manage_sensitive_actions", False))

    @staticmethod
    def resolve_can_manage_feedback(obj) -> bool:
        return bool(getattr(obj, "_can_manage_feedback", True))

    @staticmethod
    def resolve_account_access(obj):
        if getattr(obj, "_hide_account_access", False):
            return None

        target_role = "parent" if obj.is_child else "student"
        accesses = obj.account_accesses.all()
        access = next((item for item in accesses if item.role == target_role), None)
        if access is None:
            return None
        return AccountAccessSummaryOut(
            role=access.role,
            status=access.status,
            username=access.user.username,
            must_change_password=access.must_change_password,
            issued_at=access.issued_at,
            reset_at=access.reset_at,
        )


class StudentUpdate(Schema):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    guardian_phone: str | None = None
    email: str | None = None
    date_of_birth: date | None = None
    is_child: bool | None = None
    source: str | None = None
    contraindications: str | None = None


class StatusTransitionIn(Schema):
    new_status: str


class ImportResultOut(Schema):
    created: int
    skipped: int
    errors: list[str]


# ──────────────────────────────────────────────
# Student self-service schemas
# ──────────────────────────────────────────────


class StudentMeOut(Schema):
    id: int
    first_name: str
    last_name: str
    phone: str
    email: str
    status: str
    is_child: bool
    date_of_birth: date | None


class StudentSubscriptionOut(Schema):
    id: int
    tariff_id: int
    tariff_name: str
    trainings_used: int
    trainings_total: int | None
    trainings_left: int | None
    expires_at: datetime | None
    status: str
    freeze_status: str | None = None
    renewal_target_tariff_id: int | None = None
    renewal_target_tariff_name: str = ""
    renewal_target_price: Decimal | None = None


class StudentDebtOut(Schema):
    id: int
    checkin_id: int
    tariff_price: Decimal | None
    reason: str
    training_type_name: str
    checkin_date: date
    created_at: datetime


class StudentScheduleItemOut(Schema):
    id: int
    day_of_week: int
    start_time: str
    end_time: str
    group_name: str
    trainer_name: str
    location_name: str


class StudentAttendanceOut(Schema):
    id: int
    date: date
    group_name: str
    trainer_name: str
    location_name: str
    training_type_name: str
    start_time: str


class AttendanceSummaryOut(Schema):
    attended_count: int
    items: list[StudentAttendanceOut]


class StudentWeekScheduleOut(ScheduleOccurrenceOut):
    pass


class StudentPersonalBookingOut(Schema):
    schedule_id: int
    enrollment_id: int
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
    status: str
    booking_id: int | None = None
    booking_kind: str = "entitlement"
    attendance_state: str = "scheduled"
    financial_state: str = "covered"
    price_snapshot: str | None = None
    tariff_id: int | None = None
    debt_id: int | None = None
    payment_id: int | None = None
    bank_payment_order_id: int | None = None
    payment_status: str | None = None
    order_status: str | None = None
    provider_payment_url: str = ""
    can_manage: bool
    can_cancel_payment: bool = False
    can_cancel: bool = False
    can_mark_no_show: bool = False
    can_reschedule: bool = False
    next_action_label: str | None = None
