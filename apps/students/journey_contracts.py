"""Dependency-light executable contracts for the unified client journey rollout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import import_module


class Workspace(StrEnum):
    ACTIVE_LEADS = "leads_active"
    ARCHIVED_LEADS = "leads_archived"
    STUDENTS = "students"
    EXCLUDED = "excluded"
    ANOMALY = "anomaly"


@dataclass(frozen=True)
class WorkspaceFacts:
    is_deleted: bool
    lead_status: str | None
    became_student_at: str | None
    status: str


@dataclass(frozen=True)
class WorkspaceClassification:
    workspace: Workspace
    reason_code: str = ""


def classify_workspace(*, facts: WorkspaceFacts) -> WorkspaceClassification:
    """Apply the D3 workspace truth table without importing persistence models."""

    if facts.is_deleted:
        return WorkspaceClassification(workspace=Workspace.EXCLUDED)
    if facts.lead_status is not None:
        return WorkspaceClassification(workspace=Workspace.ACTIVE_LEADS)
    if facts.became_student_at is not None:
        return WorkspaceClassification(workspace=Workspace.STUDENTS)
    if facts.status == "lost":
        return WorkspaceClassification(workspace=Workspace.ARCHIVED_LEADS)
    return WorkspaceClassification(
        workspace=Workspace.ANOMALY,
        reason_code="workspace_truth_table_anomaly",
    )


class ActionContext(StrEnum):
    PAYMENT_ACTION_REQUIRED = "payment_action_required"
    PENDING_PAYMENT_OR_PAY_AT_VISIT = "pending_payment_or_pay_at_visit"
    UPCOMING_PERSONAL_BOOKING = "upcoming_personal_booking"
    UPCOMING_TRIAL = "upcoming_trial"
    TRIAL_DONE = "trial_done"
    LEAD_TASK_OR_STAGE = "lead_task_or_stage"
    STUDENT_COMMERCIAL_SEGMENT = "student_commercial_segment"


ACTION_CONTEXT_PRIORITY = {
    ActionContext.PAYMENT_ACTION_REQUIRED: 1,
    ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT: 2,
    ActionContext.UPCOMING_PERSONAL_BOOKING: 3,
    ActionContext.UPCOMING_TRIAL: 4,
    ActionContext.TRIAL_DONE: 5,
    ActionContext.LEAD_TASK_OR_STAGE: 6,
    ActionContext.STUDENT_COMMERCIAL_SEGMENT: 7,
}
MAX_TIE_BREAK_AT = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ActionCandidate:
    context: ActionContext
    resource_id: int
    is_live: bool
    tie_break_at: datetime | None = None


def select_primary_action(*, candidates: tuple[ActionCandidate, ...]) -> ActionCandidate | None:
    """Choose the single D4 primary action by priority, time, then resource id."""

    live_candidates = [candidate for candidate in candidates if candidate.is_live]
    if not live_candidates:
        return None
    normalized_times: dict[int, datetime] = {}
    for candidate in live_candidates:
        tie_break_at = candidate.tie_break_at
        if tie_break_at is None:
            normalized_times[id(candidate)] = MAX_TIE_BREAK_AT
            continue
        if not isinstance(tie_break_at, datetime) or tie_break_at.tzinfo is None:
            raise ValueError("action_tie_break_at_must_be_timezone_aware")
        normalized_times[id(candidate)] = tie_break_at.astimezone(UTC)

    return min(
        live_candidates,
        key=lambda candidate: (
            ACTION_CONTEXT_PRIORITY[candidate.context],
            normalized_times[id(candidate)],
            candidate.resource_id,
        ),
    )


class PersonPaymentTransition(StrEnum):
    PENDING_MANUAL_ADMISSION = "pending_manual_admission"
    CONFIRMED_BEFORE_CHECKIN = "confirmed_before_checkin"
    CHECKIN_BEFORE_REVIEW = "checkin_before_review"
    PAY_AT_VISIT_CHECKIN = "pay_at_visit_checkin"
    TERMINAL_BEFORE_CHECKIN = "terminal_before_checkin"
    REJECTED_AFTER_CHECKIN = "rejected_after_checkin"
    ONLINE_CONFIRMED = "online_confirmed"


@dataclass(frozen=True)
class TransitionOutcome:
    person_workspace: Workspace
    person_status: str
    lead_status_disposition: str
    became_student: bool
    pipeline_task_disposition: str
    finance_admission_disposition: str
    creates_open_debt: bool
    restores_actionable_context: bool


TRANSITION_OUTCOMES = {
    PersonPaymentTransition.PENDING_MANUAL_ADMISSION: TransitionOutcome(
        person_workspace=Workspace.ACTIVE_LEADS,
        person_status="lead_unchanged",
        lead_status_disposition="preserve",
        became_student=False,
        pipeline_task_disposition="preserve_pipeline_and_snooze_open_task",
        finance_admission_disposition="pending_payment_subscription_and_owned_admission",
        creates_open_debt=False,
        restores_actionable_context=False,
    ),
    PersonPaymentTransition.CONFIRMED_BEFORE_CHECKIN: TransitionOutcome(
        person_workspace=Workspace.STUDENTS,
        person_status="active",
        lead_status_disposition="clear",
        became_student=True,
        pipeline_task_disposition="close_as_conversion",
        finance_admission_disposition="activate_owned_artifacts",
        creates_open_debt=False,
        restores_actionable_context=False,
    ),
    PersonPaymentTransition.CHECKIN_BEFORE_REVIEW: TransitionOutcome(
        person_workspace=Workspace.STUDENTS,
        person_status="active",
        lead_status_disposition="clear",
        became_student=True,
        pipeline_task_disposition="close_as_exact_attendance",
        finance_admission_disposition="link_reserved_debt_to_pending_payment",
        creates_open_debt=False,
        restores_actionable_context=False,
    ),
    PersonPaymentTransition.PAY_AT_VISIT_CHECKIN: TransitionOutcome(
        person_workspace=Workspace.STUDENTS,
        person_status="active",
        lead_status_disposition="clear",
        became_student=True,
        pipeline_task_disposition="close_as_paid_session_attendance",
        finance_admission_disposition="create_exact_personal_drop_in_debt_without_payment",
        creates_open_debt=True,
        restores_actionable_context=False,
    ),
    PersonPaymentTransition.TERMINAL_BEFORE_CHECKIN: TransitionOutcome(
        person_workspace=Workspace.ACTIVE_LEADS,
        person_status="lead_unchanged",
        lead_status_disposition="preserve",
        became_student=False,
        pipeline_task_disposition="restore_captured_state_and_due_policy",
        finance_admission_disposition="terminalize_pending_artifacts_without_debt",
        creates_open_debt=False,
        restores_actionable_context=True,
    ),
    PersonPaymentTransition.REJECTED_AFTER_CHECKIN: TransitionOutcome(
        person_workspace=Workspace.STUDENTS,
        person_status="active_unchanged",
        lead_status_disposition="remain_clear",
        became_student=True,
        pipeline_task_disposition="keep_pipeline_closed",
        finance_admission_disposition="release_reserved_settlement_and_reopen_exact_debt",
        creates_open_debt=True,
        restores_actionable_context=False,
    ),
    PersonPaymentTransition.ONLINE_CONFIRMED: TransitionOutcome(
        person_workspace=Workspace.STUDENTS,
        person_status="active",
        lead_status_disposition="clear",
        became_student=True,
        pipeline_task_disposition="close_as_conversion",
        finance_admission_disposition="bank_payment_order_remains_authority",
        creates_open_debt=False,
        restores_actionable_context=False,
    ),
}


class PersonalIntentState(StrEnum):
    PAY_AT_VISIT_BEFORE_CHECKIN = "pay_at_visit_before_checkin"
    LIVE_PAYMENT_BEFORE_CHECKIN = "live_payment_before_checkin"
    CONFIRMED_UNUSED = "confirmed_unused"
    CHECKED_IN = "checked_in"
    MANUAL_REJECTED_AFTER_CHECKIN = "manual_rejected_after_checkin"


@dataclass(frozen=True)
class CancellationRetryRule:
    booking_becomes_terminal: bool
    releases_slot: bool
    closes_live_payment_artifacts: bool
    preserves_entitlement: bool
    opens_debt: bool
    rewrites_attendance_history: bool
    creates_credit: bool
    requires_new_idempotency_key_for_retry: bool


CANCELLATION_RETRY_RULES = {
    PersonalIntentState.PAY_AT_VISIT_BEFORE_CHECKIN: CancellationRetryRule(
        True, True, False, False, False, False, False, True,
    ),
    PersonalIntentState.LIVE_PAYMENT_BEFORE_CHECKIN: CancellationRetryRule(
        True, True, True, False, False, False, False, True,
    ),
    PersonalIntentState.CONFIRMED_UNUSED: CancellationRetryRule(
        True, True, False, True, False, False, False, False,
    ),
    PersonalIntentState.CHECKED_IN: CancellationRetryRule(
        False, False, False, True, False, False, False, False,
    ),
    PersonalIntentState.MANUAL_REJECTED_AFTER_CHECKIN: CancellationRetryRule(
        False, False, False, False, True, False, False, True,
    ),
}


@dataclass(frozen=True)
class PaymentRetryContract:
    one_live_attempt_per_booking_across_methods: bool
    same_key_returns_same_result: bool
    deliberate_terminal_retry_requires_new_key: bool
    terminal_retry_states: tuple[str, ...]
    retry_requires_slot_available: bool
    availability_retry_creates_new_reservation: bool
    scheduled_drop_in_booking_is_preserved: bool
    live_bank_order_states: tuple[str, ...]
    confirmed_attempt_blocks_future_collection: bool
    booking_lock_serializes_cross_method_race: bool
    terminal_history_is_retained: bool


PAYMENT_RETRY_CONTRACT = PaymentRetryContract(
    one_live_attempt_per_booking_across_methods=True,
    same_key_returns_same_result=True,
    deliberate_terminal_retry_requires_new_key=True,
    terminal_retry_states=("failed", "cancelled", "expired", "rejected"),
    retry_requires_slot_available=True,
    availability_retry_creates_new_reservation=True,
    scheduled_drop_in_booking_is_preserved=True,
    live_bank_order_states=("created", "pending", "authorized", "manual_review"),
    confirmed_attempt_blocks_future_collection=True,
    booking_lock_serializes_cross_method_race=True,
    terminal_history_is_retained=True,
)


class LockLevel(StrEnum):
    ROLLOUT_CATALOG = "rollout_catalog"
    TRAINER = "trainer"
    STUDENT = "student"
    ATTENDANCE_OWNER = "attendance_owner"
    FINANCIAL_FAMILY = "financial_family"
    DEBT_CHECKIN_TASK_AUDIT = "debt_checkin_task_audit"


class LockResourceKind(StrEnum):
    ROLLOUT_CATALOG = "rollout_catalog"
    TRAINER = "trainer"
    STUDENT = "student"
    SLOT = "slot"
    RESERVATION = "reservation"
    BOOKING = "booking"
    ENROLLMENT = "enrollment"
    PAYMENT = "payment"
    SUBSCRIPTION = "subscription"
    BANK_PAYMENT_ORDER = "bank_payment_order"
    DEBT = "debt"
    CHECKIN = "checkin"
    TASK = "task"
    AUDIT = "audit"


LOCK_LEVELS = tuple(level.value for level in LockLevel)
LOCK_KIND_SEQUENCE = (
    LockResourceKind.ROLLOUT_CATALOG,
    LockResourceKind.TRAINER,
    LockResourceKind.STUDENT,
    LockResourceKind.SLOT,
    LockResourceKind.RESERVATION,
    LockResourceKind.BOOKING,
    LockResourceKind.ENROLLMENT,
    LockResourceKind.PAYMENT,
    LockResourceKind.SUBSCRIPTION,
    LockResourceKind.BANK_PAYMENT_ORDER,
    LockResourceKind.DEBT,
    LockResourceKind.CHECKIN,
    LockResourceKind.TASK,
    LockResourceKind.AUDIT,
)
LOCK_KIND_INDEX = {kind: index for index, kind in enumerate(LOCK_KIND_SEQUENCE)}
LOCK_KIND_LEVEL = {
    LockResourceKind.ROLLOUT_CATALOG: LockLevel.ROLLOUT_CATALOG,
    LockResourceKind.TRAINER: LockLevel.TRAINER,
    LockResourceKind.STUDENT: LockLevel.STUDENT,
    LockResourceKind.SLOT: LockLevel.ATTENDANCE_OWNER,
    LockResourceKind.RESERVATION: LockLevel.ATTENDANCE_OWNER,
    LockResourceKind.BOOKING: LockLevel.ATTENDANCE_OWNER,
    LockResourceKind.ENROLLMENT: LockLevel.ATTENDANCE_OWNER,
    LockResourceKind.PAYMENT: LockLevel.FINANCIAL_FAMILY,
    LockResourceKind.SUBSCRIPTION: LockLevel.FINANCIAL_FAMILY,
    LockResourceKind.BANK_PAYMENT_ORDER: LockLevel.FINANCIAL_FAMILY,
    LockResourceKind.DEBT: LockLevel.DEBT_CHECKIN_TASK_AUDIT,
    LockResourceKind.CHECKIN: LockLevel.DEBT_CHECKIN_TASK_AUDIT,
    LockResourceKind.TASK: LockLevel.DEBT_CHECKIN_TASK_AUDIT,
    LockResourceKind.AUDIT: LockLevel.DEBT_CHECKIN_TASK_AUDIT,
}


@dataclass(frozen=True)
class LockResource:
    level: LockLevel | str
    kind: LockResourceKind | str
    resource_id: int


def normalize_lock_plan(*, resources: tuple[LockResource, ...]) -> tuple[LockResource, ...]:
    """Validate exact D12 resource sublevels and sort IDs only within one kind."""

    previous_kind_index = -1
    for resource in resources:
        try:
            level = LockLevel(resource.level)
        except ValueError as error:
            raise ValueError(f"unknown_lock_level:{resource.level}") from error
        try:
            kind = LockResourceKind(resource.kind)
        except ValueError as error:
            raise ValueError(f"unknown_lock_kind:{resource.kind}") from error
        if LOCK_KIND_LEVEL[kind] != level:
            raise ValueError(f"lock_kind_level_mismatch:{level}:{kind}")
        if not isinstance(resource.resource_id, int) or resource.resource_id < 1:
            raise ValueError("lock_resource_id_must_be_positive")
        kind_index = LOCK_KIND_INDEX[kind]
        if kind_index < previous_kind_index:
            raise ValueError("lock_order_reversed")
        previous_kind_index = kind_index

    ordered: list[LockResource] = []
    for kind in LOCK_KIND_SEQUENCE:
        ordered.extend(
            sorted(
                (resource for resource in resources if resource.kind == kind),
                key=lambda resource: resource.resource_id,
            )
        )
    return tuple(ordered)

PERSONAL_METHOD_MATRIX = {
    "trainer": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
    "owner": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
    "admin": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
    "student": ("entitlement", "sbp"),
    "parent": ("entitlement", "sbp"),
}
STABLE_ERROR_CODES = (
    "duplicate_phone",
    "idempotency_conflict",
    "possible_child_duplicate_confirmation_required",
    "workspace_truth_table_anomaly",
    "personal_booking_tariff_not_configured",
    "personal_booking_tariff_ambiguous",
    "personal_offer_changed",
    "personal_booking_slot_conflict",
    "payment_attempt_in_progress",
    "personal_terms_incomplete",
    "trial_done_requires_checkin",
    "bank_order_manual_review",
    "student_scope_denied",
)
MIGRATION_SEQUENCE = (
    "expand",
    "dual_write",
    "audit_and_drain",
    "new_read",
    "cross_version_terminal_handler_window",
    "cleanup",
)
EXISTING_MUTATION_OWNERS = {
    "group_manual": ("apps.billing.service_modules.payment_creation", "create_payment"),
    "group_and_renewal_online": ("apps.billing.service_modules.bank_orders", "create_bank_payment_order"),
    "manual_review": ("apps.billing.service_modules.payment_review", "verify_payment"),
    "personal_entitlement": ("apps.attendance.services.enrollment", "book_personal_session"),
    "personal_online_reservation": (
        "apps.attendance.services.enrollment",
        "create_personal_booking_payment_reservation",
    ),
    "personal_online_bank_order": (
        "apps.billing.service_modules.bank_orders",
        "create_bank_payment_order",
    ),
    "pay_at_visit": ("apps.attendance.services.drop_in", "book_personal_drop_in"),
}


def verify_existing_mutation_owners() -> bool:
    """Prove the frozen owner map names importable exact implementation functions."""

    for module_name, function_name in EXISTING_MUTATION_OWNERS.values():
        function = getattr(import_module(module_name), function_name)
        if not callable(function) or function.__module__ != module_name:
            return False
    return True
