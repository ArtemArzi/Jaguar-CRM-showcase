from datetime import UTC, datetime

import pytest

from apps.students.journey_contracts import (
    ACTION_CONTEXT_PRIORITY,
    CANCELLATION_RETRY_RULES,
    EXISTING_MUTATION_OWNERS,
    LOCK_LEVELS,
    MIGRATION_SEQUENCE,
    PAYMENT_RETRY_CONTRACT,
    PERSONAL_METHOD_MATRIX,
    STABLE_ERROR_CODES,
    TRANSITION_OUTCOMES,
    ActionCandidate,
    ActionContext,
    CancellationRetryRule,
    LockLevel,
    LockResource,
    LockResourceKind,
    PaymentRetryContract,
    PersonalIntentState,
    PersonPaymentTransition,
    TransitionOutcome,
    Workspace,
    WorkspaceFacts,
    classify_workspace,
    normalize_lock_plan,
    select_primary_action,
    verify_existing_mutation_owners,
)


def test_workspace_contract_classifies_d3_truth_table_from_facts_only():
    cases = (
        (
            WorkspaceFacts(True, "new", "2026-08-12T00:00:00+00:00", "lost"),
            (Workspace.EXCLUDED, ""),
        ),
        (
            WorkspaceFacts(False, "new", "2026-08-12T00:00:00+00:00", "lost"),
            (Workspace.ACTIVE_LEADS, ""),
        ),
        (
            WorkspaceFacts(False, None, "2026-08-12T00:00:00+00:00", "lost"),
            (Workspace.STUDENTS, ""),
        ),
        (
            WorkspaceFacts(False, None, None, "lost"),
            (Workspace.ARCHIVED_LEADS, ""),
        ),
        (
            WorkspaceFacts(False, None, None, "active"),
            (Workspace.ANOMALY, "workspace_truth_table_anomaly"),
        ),
    )

    assert tuple(
        (classification.workspace, classification.reason_code)
        for facts, _expected in cases
        for classification in (classify_workspace(facts=facts),)
    ) == tuple(expected for _facts, expected in cases)


def test_action_contract_uses_priority_then_time_then_resource_id():
    assert ACTION_CONTEXT_PRIORITY == {
        ActionContext.PAYMENT_ACTION_REQUIRED: 1,
        ActionContext.PENDING_PAYMENT_OR_PAY_AT_VISIT: 2,
        ActionContext.UPCOMING_PERSONAL_BOOKING: 3,
        ActionContext.UPCOMING_TRIAL: 4,
        ActionContext.TRIAL_DONE: 5,
        ActionContext.LEAD_TASK_OR_STAGE: 6,
        ActionContext.STUDENT_COMMERCIAL_SEGMENT: 7,
    }
    at_noon = datetime(2026, 8, 20, 12, tzinfo=UTC)
    selected = select_primary_action(
        candidates=(
            ActionCandidate(ActionContext.LEAD_TASK_OR_STAGE, 9, True, at_noon),
            ActionCandidate(ActionContext.PAYMENT_ACTION_REQUIRED, 4, True, at_noon),
            ActionCandidate(ActionContext.PAYMENT_ACTION_REQUIRED, 3, True, at_noon),
            ActionCandidate(ActionContext.UPCOMING_TRIAL, 1, False, at_noon),
        ),
    )

    assert selected is not None
    assert selected.context == ActionContext.PAYMENT_ACTION_REQUIRED
    assert selected.resource_id == 3
    assert ACTION_CONTEXT_PRIORITY[selected.context] == 1


def test_action_contract_compares_timezone_offsets_by_instant_and_rejects_naive_values():
    earlier_instant = ActionCandidate(
        ActionContext.UPCOMING_TRIAL,
        9,
        True,
        datetime.fromisoformat("2026-08-20T09:00:00+05:00"),
    )
    later_instant = ActionCandidate(
        ActionContext.UPCOMING_TRIAL,
        1,
        True,
        datetime.fromisoformat("2026-08-20T08:00:00+00:00"),
    )

    assert select_primary_action(candidates=(later_instant, earlier_instant)) == earlier_instant
    with pytest.raises(ValueError, match="action_tie_break_at_must_be_timezone_aware"):
        select_primary_action(
            candidates=(
                ActionCandidate(
                    ActionContext.UPCOMING_TRIAL,
                    1,
                    True,
                    datetime(2026, 8, 20, 8),
                ),
            ),
        )
    with pytest.raises(ValueError, match="action_tie_break_at_must_be_timezone_aware"):
        select_primary_action(
            candidates=(ActionCandidate(ActionContext.UPCOMING_TRIAL, 1, True, "malformed"),),
        )


def test_transition_contract_freezes_every_d10_outcome():
    assert TRANSITION_OUTCOMES == {
        PersonPaymentTransition.PENDING_MANUAL_ADMISSION: TransitionOutcome(
            Workspace.ACTIVE_LEADS,
            "lead_unchanged",
            "preserve",
            False,
            "preserve_pipeline_and_snooze_open_task",
            "pending_payment_subscription_and_owned_admission",
            False,
            False,
        ),
        PersonPaymentTransition.CONFIRMED_BEFORE_CHECKIN: TransitionOutcome(
            Workspace.STUDENTS,
            "active",
            "clear",
            True,
            "close_as_conversion",
            "activate_owned_artifacts",
            False,
            False,
        ),
        PersonPaymentTransition.CHECKIN_BEFORE_REVIEW: TransitionOutcome(
            Workspace.STUDENTS,
            "active",
            "clear",
            True,
            "close_as_exact_attendance",
            "link_reserved_debt_to_pending_payment",
            False,
            False,
        ),
        PersonPaymentTransition.PAY_AT_VISIT_CHECKIN: TransitionOutcome(
            Workspace.STUDENTS,
            "active",
            "clear",
            True,
            "close_as_paid_session_attendance",
            "create_exact_personal_drop_in_debt_without_payment",
            True,
            False,
        ),
        PersonPaymentTransition.TERMINAL_BEFORE_CHECKIN: TransitionOutcome(
            Workspace.ACTIVE_LEADS,
            "lead_unchanged",
            "preserve",
            False,
            "restore_captured_state_and_due_policy",
            "terminalize_pending_artifacts_without_debt",
            False,
            True,
        ),
        PersonPaymentTransition.REJECTED_AFTER_CHECKIN: TransitionOutcome(
            Workspace.STUDENTS,
            "active_unchanged",
            "remain_clear",
            True,
            "keep_pipeline_closed",
            "release_reserved_settlement_and_reopen_exact_debt",
            True,
            False,
        ),
        PersonPaymentTransition.ONLINE_CONFIRMED: TransitionOutcome(
            Workspace.STUDENTS,
            "active",
            "clear",
            True,
            "close_as_conversion",
            "bank_payment_order_remains_authority",
            False,
            False,
        ),
    }


def test_cancellation_and_retry_contracts_freeze_every_d11_rule():
    assert CANCELLATION_RETRY_RULES == {
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
    assert PAYMENT_RETRY_CONTRACT == PaymentRetryContract(
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


def test_lock_role_error_migration_and_owner_contracts_are_explicit_and_dependency_light():
    assert LOCK_LEVELS == (
        "rollout_catalog",
        "trainer",
        "student",
        "attendance_owner",
        "financial_family",
        "debt_checkin_task_audit",
    )
    assert PERSONAL_METHOD_MATRIX == {
        "trainer": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
        "owner": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
        "admin": ("entitlement", "sbp", "cash", "transfer", "pay_at_visit"),
        "student": ("entitlement", "sbp"),
        "parent": ("entitlement", "sbp"),
    }
    assert STABLE_ERROR_CODES == (
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
    assert MIGRATION_SEQUENCE == (
        "expand",
        "dual_write",
        "audit_and_drain",
        "new_read",
        "cross_version_terminal_handler_window",
        "cleanup",
    )
    assert EXISTING_MUTATION_OWNERS["manual_review"] == (
        "apps.billing.service_modules.payment_review",
        "verify_payment",
    )
    assert verify_existing_mutation_owners() is True


def test_lock_contract_freezes_d12_resource_sublevels_before_cross_kind_ids():
    plan = normalize_lock_plan(
        resources=(
            LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.SLOT, 9),
            LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.RESERVATION, 12),
            LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.BOOKING, 1),
            LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.ENROLLMENT, 1),
            LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.PAYMENT, 9),
            LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.SUBSCRIPTION, 1),
            LockResource(
                LockLevel.FINANCIAL_FAMILY,
                LockResourceKind.BANK_PAYMENT_ORDER,
                1,
            ),
        ),
    )

    assert plan == (
        LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.SLOT, 9),
        LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.RESERVATION, 12),
        LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.BOOKING, 1),
        LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.ENROLLMENT, 1),
        LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.PAYMENT, 9),
        LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.SUBSCRIPTION, 1),
        LockResource(
            LockLevel.FINANCIAL_FAMILY,
            LockResourceKind.BANK_PAYMENT_ORDER,
            1,
        ),
    )


def test_lock_contract_sorts_ids_only_within_the_same_resource_kind():
    plan = normalize_lock_plan(
        resources=(
            LockResource(LockLevel.STUDENT, LockResourceKind.STUDENT, 9),
            LockResource(LockLevel.STUDENT, LockResourceKind.STUDENT, 2),
        ),
    )

    assert plan == (
        LockResource(LockLevel.STUDENT, LockResourceKind.STUDENT, 2),
        LockResource(LockLevel.STUDENT, LockResourceKind.STUDENT, 9),
    )


def test_lock_contract_rejects_unknown_mismatched_and_reversed_resource_order():
    with pytest.raises(ValueError, match="lock_order_reversed"):
        normalize_lock_plan(
            resources=(
                LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.BOOKING, 1),
                LockResource(LockLevel.ATTENDANCE_OWNER, LockResourceKind.SLOT, 9),
            ),
        )
    with pytest.raises(ValueError, match="lock_order_reversed"):
        normalize_lock_plan(
            resources=(
                LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.SUBSCRIPTION, 1),
                LockResource(LockLevel.FINANCIAL_FAMILY, LockResourceKind.PAYMENT, 9),
            ),
        )
    with pytest.raises(ValueError, match="unknown_lock_level"):
        normalize_lock_plan(
            resources=(LockResource("unknown", LockResourceKind.STUDENT, 1),),
        )
    with pytest.raises(ValueError, match="unknown_lock_kind"):
        normalize_lock_plan(
            resources=(LockResource(LockLevel.STUDENT, "unknown", 1),),
        )
    with pytest.raises(ValueError, match="lock_kind_level_mismatch"):
        normalize_lock_plan(
            resources=(LockResource(LockLevel.STUDENT, LockResourceKind.PAYMENT, 1),),
        )
