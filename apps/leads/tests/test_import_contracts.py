from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import apps.leads.api as leads_api
import apps.leads.service_modules._shared as shared_lifecycle
import apps.leads.service_modules.conversions as conversions_lifecycle
import apps.leads.service_modules.funnel as funnel_lifecycle
import apps.leads.service_modules.intake as intake_lifecycle
import apps.leads.service_modules.ownership as ownership_lifecycle
import apps.leads.service_modules.trials as trials_lifecycle
import apps.leads.services as lead_services

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = Path(__file__).resolve()
SERVICE_MODULE = "apps.leads.services"
SERVICE_PACKAGE = "apps.leads"
INTERNAL_PACKAGE = "apps.leads.service_modules"
INTERNAL_ROOT = ROOT / "apps" / "leads" / "service_modules"
CURRENT_REFACTOR_SLICE = 9

PUBLIC_INTERFACE = frozenset(
    {
        "LeadClaimConflictError",
        "create_lead",
        "create_landing_lead_intake",
        "claim_lead",
        "release_lead",
        "assign_lead",
        "update_lead_status",
        "is_exact_booked_trial_checkin",
        "complete_booked_trial_after_checkin",
        "book_trial",
        "convert_lead",
        "convert_lead_for_manual_operational_admission",
        "admit_lead_for_manual_operational_admission",
        "convert_lead_after_subscription_payment",
        "snooze_lead_for_pending_personal_payment",
        "restore_lead_after_terminal_personal_payment",
        "snooze_lead_for_pending_group_payment",
        "restore_lead_after_terminal_group_payment",
        "convert_lead_after_group_admission_checkin",
        "convert_lead_after_personal_attendance",
        "convert_lead_after_personal_payment_confirmation",
        "lose_lead",
        "record_contact_outcome",
        "reopen_lead",
    }
)

EXPECTED_PUBLIC_EXPORT_ORDER = (
    "LeadClaimConflictError",
    "create_lead",
    "create_landing_lead_intake",
    "claim_lead",
    "release_lead",
    "assign_lead",
    "update_lead_status",
    "is_exact_booked_trial_checkin",
    "complete_booked_trial_after_checkin",
    "book_trial",
    "convert_lead",
    "convert_lead_for_manual_operational_admission",
    "admit_lead_for_manual_operational_admission",
    "convert_lead_after_subscription_payment",
    "snooze_lead_for_pending_personal_payment",
    "restore_lead_after_terminal_personal_payment",
    "snooze_lead_for_pending_group_payment",
    "restore_lead_after_terminal_group_payment",
    "convert_lead_after_group_admission_checkin",
    "convert_lead_after_personal_attendance",
    "convert_lead_after_personal_payment_confirmation",
    "lose_lead",
    "record_contact_outcome",
    "reopen_lead",
)

FACADE_ADAPTERS = frozenset(
    {
        "create_lead",
        "create_landing_lead_intake",
        "update_lead_status",
        "complete_booked_trial_after_checkin",
        "book_trial",
        "convert_lead",
        "convert_lead_for_manual_operational_admission",
        "admit_lead_for_manual_operational_admission",
        "convert_lead_after_subscription_payment",
        "convert_lead_after_group_admission_checkin",
        "convert_lead_after_personal_attendance",
        "convert_lead_after_personal_payment_confirmation",
    }
)
FACADE_PRIVATE_PATCH_BINDINGS = frozenset(
    {
        "_finalize_lead_conversion_side_effects",
        "_queue_lead_intake_telegram",
        "_trigger_trial_done_side_effects",
    }
)
DIRECT_REEXPORTS = PUBLIC_INTERFACE - {"LeadClaimConflictError"} - FACADE_ADAPTERS
PUBLIC_OWNER_MODULES = {
    "LeadClaimConflictError": f"{INTERNAL_PACKAGE}._shared",
    "create_lead": f"{INTERNAL_PACKAGE}.intake",
    "create_landing_lead_intake": f"{INTERNAL_PACKAGE}.intake",
    "claim_lead": f"{INTERNAL_PACKAGE}.ownership",
    "release_lead": f"{INTERNAL_PACKAGE}.ownership",
    "assign_lead": f"{INTERNAL_PACKAGE}.ownership",
    "update_lead_status": f"{INTERNAL_PACKAGE}.ownership",
    "is_exact_booked_trial_checkin": f"{INTERNAL_PACKAGE}.trials",
    "complete_booked_trial_after_checkin": f"{INTERNAL_PACKAGE}.trials",
    "book_trial": f"{INTERNAL_PACKAGE}.trials",
    "convert_lead": f"{INTERNAL_PACKAGE}.conversions",
    "convert_lead_for_manual_operational_admission": f"{INTERNAL_PACKAGE}.conversions",
    "admit_lead_for_manual_operational_admission": f"{INTERNAL_PACKAGE}.conversions",
    "convert_lead_after_subscription_payment": f"{INTERNAL_PACKAGE}.conversions",
    "snooze_lead_for_pending_personal_payment": f"{INTERNAL_PACKAGE}.conversions",
    "restore_lead_after_terminal_personal_payment": f"{INTERNAL_PACKAGE}.conversions",
    "snooze_lead_for_pending_group_payment": f"{INTERNAL_PACKAGE}.conversions",
    "restore_lead_after_terminal_group_payment": f"{INTERNAL_PACKAGE}.conversions",
    "convert_lead_after_group_admission_checkin": f"{INTERNAL_PACKAGE}.conversions",
    "convert_lead_after_personal_attendance": f"{INTERNAL_PACKAGE}.conversions",
    "convert_lead_after_personal_payment_confirmation": f"{INTERNAL_PACKAGE}.conversions",
    "lose_lead": f"{INTERNAL_PACKAGE}.funnel",
    "record_contact_outcome": f"{INTERNAL_PACKAGE}.funnel",
    "reopen_lead": f"{INTERNAL_PACKAGE}.funnel",
}

PRIVATE_HELPERS = frozenset(
    {
        "_as_club_aware",
        "_book_personal_trial",
        "_book_personal_trial_locked",
        "_booking_time",
        "_clean_reason",
        "_convert_lead_after_payment",
        "_ensure_active_lead",
        "_ensure_expected_trainer_assignment",
        "_ensure_paid_conversion_eligible",
        "_finalize_lead_conversion_side_effects",
        "_get_active_trainer",
        "_get_lead_for_update",
        "_has_paid_active_subscription",
        "_personal_trial_group_name",
        "_personal_trial_slot_conflicts",
        "_queue_lead_intake_telegram",
        "_record_lifecycle_event",
        "_trigger_trial_done_side_effects",
    }
)

SHARED_PRIVATE_HELPERS = frozenset(
    {
        "_ensure_active_lead",
        "_ensure_expected_trainer_assignment",
        "_get_active_trainer",
        "_get_lead_for_update",
        "_record_lifecycle_event",
    }
)

OWNERSHIP_PRIVATE_HELPERS = frozenset({"_clean_reason"})
TRIALS_PRIVATE_HELPERS = frozenset(
    {
        "_as_club_aware",
        "_book_personal_trial",
        "_book_personal_trial_locked",
        "_booking_time",
        "_personal_trial_group_name",
        "_personal_trial_slot_conflicts",
        "_trigger_trial_done_side_effects",
    }
)
TRIALS_FACADE_COMPATIBILITY_HELPERS = frozenset({"_trigger_trial_done_side_effects"})
OWNERSHIP_IMPLEMENTATIONS = frozenset(
    {
        "claim_lead",
        "release_lead",
        "assign_lead",
        "update_lead_status",
    }
)
TRIALS_IMPLEMENTATIONS = frozenset(
    {"is_exact_booked_trial_checkin", "complete_booked_trial_after_checkin", "book_trial"}
)
INTAKE_PRIVATE_HELPERS = frozenset(
    {
        "_clean_optional_text",
        "_clean_required_text",
        "_hash_user_agent",
        "_queue_lead_intake_telegram",
        "_validate_consent",
        "_validate_phone",
        "_validate_preferred_format",
    }
)
INTAKE_IMPLEMENTATIONS = frozenset({"create_lead", "create_landing_lead_intake"})
FUNNEL_IMPLEMENTATIONS = frozenset(
    {"lose_lead", "record_contact_outcome", "reopen_lead"}
)
CONVERSIONS_PRIVATE_HELPERS = frozenset(
    {
        "_assert_group_manual_operational_admission_evidence",
        "_assert_pending_manual_payment_evidence",
        "_assert_personal_manual_operational_admission_evidence",
        "_convert_lead_after_payment",
        "_ensure_paid_conversion_eligible",
        "_finalize_lead_conversion_side_effects",
        "_has_paid_active_subscription",
        "_manual_operational_admission_evidence_error",
    }
)
CONVERSIONS_IMPLEMENTATIONS = frozenset(
    {
        "convert_lead",
        "admit_lead_for_manual_operational_admission",
        "convert_lead_after_group_admission_checkin",
        "convert_lead_after_personal_attendance",
        "convert_lead_after_personal_payment_confirmation",
        "convert_lead_after_subscription_payment",
        "convert_lead_for_manual_operational_admission",
        "restore_lead_after_terminal_group_payment",
        "restore_lead_after_terminal_personal_payment",
        "snooze_lead_for_pending_group_payment",
        "snooze_lead_for_pending_personal_payment",
    }
)

EXPECTED_SIGNATURES = {
    "assign_lead": "(*, club_id: 'int', student_id: 'int', trainer_id: 'int | None', "
    "actor_user_id: 'int | None' = None, reason: 'str' = '') -> 'Student'",
    "book_trial": "(*, club_id: 'int', student_id: 'int', trial_date=None, "
    "schedule_id: 'int | None' = None, occurrence_date=None, "
    "required_trainer_id: 'int | None' = None, mode: 'str' = 'group', "
    "starts_at=None, ends_at=None, trainer_id: 'int | None' = None, "
    "location_id: 'int | None' = None, training_type_id: 'int | None' = None, "
    "actor_user_id: 'int | None' = None, "
    "required_assigned_trainer_id: 'int | None' = None) -> 'Student'",
    "claim_lead": "(*, club_id: 'int', student_id: 'int', trainer_id: 'int', "
    "actor_user_id: 'int | None' = None) -> 'Student'",
    "complete_booked_trial_after_checkin": "(*, club_id: 'int', student_id: 'int', "
    "checkin=None) -> 'bool'",
    "convert_lead": "(*, club_id: 'int', student_id: 'int', "
    "actor_user_id: 'int | None' = None) -> 'Student'",
    "convert_lead_after_group_admission_checkin": "(*, club_id: 'int', "
    "student_id: 'int', schedule_id: 'int', checkin_id: 'int', "
    "actor_user_id: 'int | None') -> 'bool'",
    "convert_lead_after_personal_attendance": "(*, club_id: 'int', student_id: 'int', "
    "booking_id: 'int', checkin_id: 'int', payment_id: 'int | None', "
    "actor_user_id: 'int | None') -> 'bool'",
    "convert_lead_after_personal_payment_confirmation": "(*, club_id: 'int', "
    "student_id: 'int', payment_id: 'int', actor_user_id: 'int | None') -> 'bool'",
    "convert_lead_after_subscription_payment": "(*, club_id: 'int', student_id: 'int', "
    "actor_user_id: 'int | None' = None) -> 'bool'",
    "convert_lead_for_manual_operational_admission": "(*, club_id: 'int', "
    "student_id: 'int', actor_user_id: 'int | None' = None) -> 'bool'",
    "admit_lead_for_manual_operational_admission": "(*, evidence, occurred_at: 'datetime | None' = None) -> 'bool'",
    "create_landing_lead_intake": "(*, club_id: 'int', name: 'str', phone: 'str', "
    "goal: 'str', preferred_format: 'str', is_child: 'bool', consent: 'dict[str, Any]', "
    "source: 'dict[str, Any]', request_id: 'str', client_ip_hash: 'str', "
    "user_agent: 'str', idempotency_key: 'UUID | None') -> 'LeadIntakeEvent'",
    "create_lead": "(*, club_id: 'int', first_name: 'str', last_name: 'str' = '', "
    "phone: 'str', is_child: 'bool' = False, guardian_phone: 'str' = '', "
    "date_of_birth: 'date | None' = None, source: 'str' = 'other', "
    "assigned_trainer_id: 'int | None' = None, "
    "crm_entry_kind: 'str' = Student.CrmEntryKind.LEGACY_UNKNOWN, "
    "crm_entered_by_id: 'int | None' = None, "
    "confirm_distinct_child: 'bool' = False) -> 'Student'",
    "is_exact_booked_trial_checkin": "(*, club_id: 'int', student: 'Student', checkin, "
    "lock: 'bool' = False) -> 'bool'",
    "lose_lead": "(*, club_id: 'int', student_id: 'int', loss_reason: 'str', "
    "actor_user_id: 'int | None' = None, "
    "required_assigned_trainer_id: 'int | None' = None) -> 'Student'",
    "record_contact_outcome": "(*, club_id: 'int', student_id: 'int', outcome: 'str', "
    "due_date: 'date | None' = None, loss_reason: 'str' = '', notes: 'str' = '', "
    "actor_user_id: 'int | None' = None, "
    "required_assigned_trainer_id: 'int | None' = None) -> 'tuple[Student, str | None]'",
    "release_lead": "(*, club_id: 'int', student_id: 'int', trainer_id: 'int', reason: 'str', "
    "actor_user_id: 'int | None' = None) -> 'Student'",
    "reopen_lead": "(*, club_id: 'int', student_id: 'int', "
    "actor_user_id: 'int | None' = None, claim_trainer_id: 'int | None' = None, "
    "required_assigned_trainer_id: 'int | None' = None) -> 'Student'",
    "restore_lead_after_terminal_group_payment": "(*, club_id: 'int', student_id: 'int', "
    "payment_id: 'int', actor_user_id: 'int | None', outcome: 'str') -> 'None'",
    "restore_lead_after_terminal_personal_payment": "(*, club_id: 'int', student_id: 'int', "
    "payment_id: 'int', actor_user_id: 'int | None', outcome: 'str') -> 'None'",
    "snooze_lead_for_pending_group_payment": "(*, club_id: 'int', student_id: 'int', "
    "payment_id: 'int', actor_user_id: 'int | None') -> 'None'",
    "snooze_lead_for_pending_personal_payment": "(*, club_id: 'int', student_id: 'int', "
    "payment_id: 'int', actor_user_id: 'int | None') -> 'None'",
    "update_lead_status": "(*, club_id: 'int', student_id: 'int', new_status: 'str', "
    "actor_user_id: 'int | None' = None, "
    "required_assigned_trainer_id: 'int | None' = None) -> 'Student'",
}

EXPECTED_DIRECT_IMPORTS = (
    ("apps/attendance/services/checkin.py", ("complete_booked_trial_after_checkin",)),
    ("apps/attendance/services/checkin.py", ("convert_lead_after_group_admission_checkin",)),
    ("apps/attendance/services/checkin.py", ("convert_lead_after_group_admission_checkin",)),
    ("apps/attendance/services/checkin.py", ("convert_lead_after_group_admission_checkin",)),
    ("apps/attendance/services/checkin.py", ("is_exact_booked_trial_checkin",)),
    ("apps/attendance/services/drop_in.py", ("admit_lead_for_manual_operational_admission",)),
    ("apps/attendance/services/drop_in.py", ("convert_lead_after_personal_attendance",)),
    ("apps/attendance/tests/test_personal_drop_in.py", ("book_trial", "complete_booked_trial_after_checkin")),
    ("apps/billing/service_modules/bank_orders.py", ("restore_lead_after_terminal_group_payment",)),
    ("apps/billing/service_modules/bank_orders.py", ("restore_lead_after_terminal_personal_payment",)),
    (
        "apps/billing/service_modules/payment_creation.py",
        ("admit_lead_for_manual_operational_admission",),
    ),
    ("apps/billing/service_modules/payment_creation.py", ("convert_lead_for_manual_operational_admission",)),
    ("apps/billing/service_modules/payment_review.py", ("convert_lead_after_personal_payment_confirmation",)),
    ("apps/billing/service_modules/payment_review.py", ("convert_lead_after_subscription_payment",)),
    ("apps/billing/service_modules/payment_review.py", ("restore_lead_after_terminal_group_payment",)),
    ("apps/billing/service_modules/payment_review.py", ("restore_lead_after_terminal_personal_payment",)),
    ("apps/billing/service_modules/subscriptions.py", ("convert_lead_after_subscription_payment",)),
    ("apps/common/management/commands/prepare_trainer_lead_pool_lifecycle_e2e.py", ("create_landing_lead_intake",)),
    (
        "apps/leads/api.py",
        (
            "LeadClaimConflictError",
            "assign_lead",
            "book_trial",
            "claim_lead",
            "convert_lead",
            "create_lead",
            "lose_lead",
            "record_contact_outcome",
            "release_lead",
            "reopen_lead",
            "update_lead_status",
        ),
    ),
    ("apps/leads/public_api.py", ("create_landing_lead_intake",)),
    (
        "apps/leads/tests/test_group_admission_lifecycle.py",
        (
            "restore_lead_after_terminal_group_payment",
            "snooze_lead_for_pending_group_payment",
        ),
    ),
    ("apps/leads/tests/test_landing_intake_services.py", ("create_landing_lead_intake",)),
        (
            "apps/leads/tests/test_services.py",
            (
                "book_trial",
                "convert_lead",
            "convert_lead_after_subscription_payment",
            "convert_lead_for_manual_operational_admission",
            "create_lead",
            "lose_lead",
            "restore_lead_after_terminal_personal_payment",
            "snooze_lead_for_pending_personal_payment",
            "update_lead_status",
        ),
    ),
    (
        "apps/leads/tests/test_slice2_journey.py",
        (
            "LeadClaimConflictError",
            "book_trial",
            "claim_lead",
            "lose_lead",
            "record_contact_outcome",
            "reopen_lead",
            "update_lead_status",
        ),
    ),
    ("apps/pipelines/services.py", ("lose_lead",)),
    ("apps/students/intake_services.py", ("create_lead",)),
    ("apps/students/services.py", ("_finalize_lead_conversion_side_effects",)),
    ("apps/students/tests/test_identity_services.py", ("create_landing_lead_intake", "create_lead")),
    ("apps/students/tests/test_intake_services.py", ("create_landing_lead_intake",)),
    ("tests/test_real_stack_e2e_commands.py", ("book_trial", "create_lead", "update_lead_status")),
    ("tests/test_real_stack_e2e_commands.py", ("claim_lead", "lose_lead", "release_lead")),
    ("tests/test_real_stack_e2e_commands.py", ("create_landing_lead_intake",)),
)

EXPECTED_MODULE_ALIAS_IMPORTS = (("apps/leads/tests/test_services.py", "services"),)
EXPECTED_MODULE_ALIAS_ATTRIBUTES = (
    ("apps/leads/tests/test_services.py", "resolve_staff_intake_person_identity"),
)
SLICE_FIVE_ADDITIVE_DIRECT_IMPORTS = (
    ("apps/leads/tests/test_services.py", "record_contact_outcome"),
)
SLICE_SIX_ADDITIVE_DIRECT_IMPORTS = (
    ("apps/leads/tests/test_services.py", "complete_booked_trial_after_checkin"),
)
SLICE_SEVEN_ADDITIVE_DIRECT_IMPORTS: tuple[tuple[str, str], ...] = ()
SLICE_EIGHT_ADDITIVE_DIRECT_IMPORTS = (
    ("apps/attendance/services/drop_in.py", "snooze_lead_for_pending_personal_payment"),
    ("apps/attendance/tests/test_staff_personal_intents.py", "admit_lead_for_manual_operational_admission"),
    (
        "apps/billing/management/commands/reconcile_manual_operational_admission.py",
        "admit_lead_for_manual_operational_admission",
    ),
    ("apps/billing/service_modules/payment_creation.py", "snooze_lead_for_pending_group_payment"),
    ("apps/leads/tests/test_postgresql_refactor_gate.py", "restore_lead_after_terminal_group_payment"),
    ("apps/leads/tests/test_postgresql_refactor_gate.py", "restore_lead_after_terminal_personal_payment"),
    ("apps/leads/tests/test_postgresql_refactor_gate.py", "snooze_lead_for_pending_group_payment"),
    ("apps/leads/tests/test_postgresql_refactor_gate.py", "snooze_lead_for_pending_personal_payment"),
)
SLICE_NINE_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/attendance/tests/test_staff_personal_intents.py",
            "test_manual_operational_admission_rejects_mismatched_payment_recorder_before_person_mutation",
            "apps.leads.services.admit_lead_for_manual_operational_admission",
        ): 1,
        (
            "apps/attendance/tests/test_staff_personal_intents.py",
            "test_reconciliation_uses_exact_personal_payment_timestamp_and_is_idempotent",
            "apps.leads.services.admit_lead_for_manual_operational_admission",
        ): 1,
    }
)
BASELINE_STRING_PATCH_TARGETS = Counter(
    {
        "apps.leads.services._finalize_lead_conversion_side_effects": 2,
        "apps.leads.services._queue_lead_intake_telegram": 3,
        "apps.leads.services._trigger_trial_done_side_effects": 4,
        "apps.leads.services.timezone.now": 5,
    }
)
BASELINE_OBJECT_PATCH_TARGETS = Counter({"resolve_staff_intake_person_identity": 1})
BASELINE_FACADE_PATCH_BINDINGS = frozenset(
    {
        "_finalize_lead_conversion_side_effects",
        "_queue_lead_intake_telegram",
        "_trigger_trial_done_side_effects",
        "resolve_staff_intake_person_identity",
        "timezone.now",
    }
)
SLICE_THREE_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/leads/tests/test_services.py",
            "test_trial_done_callback_runs_inside_outer_transaction_before_rollback",
            "apps.leads.services._trigger_trial_done_side_effects",
        ): 1,
        (
            "apps/leads/tests/test_services.py",
            "test_trial_done_uses_facade_unified_journey_provider",
            "apps.leads.services.is_unified_client_journey_enabled",
        ): 1,
    }
)
SLICE_FOUR_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/leads/tests/test_landing_intake_services.py",
            "test_facade_queue_patch_controls_extracted_intake",
            "apps.leads.services._queue_lead_intake_telegram",
        ): 1,
        (
            "apps/leads/tests/test_landing_intake_services.py",
            "test_facade_clock_patch_controls_extracted_intake",
            "apps.leads.services.timezone.now",
        ): 1,
    }
)
SLICE_SIX_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/leads/tests/test_services.py",
            "test_completion_callback_runs_inside_outer_transaction_before_rollback",
            "apps.leads.services._trigger_trial_done_side_effects",
        ): 1,
    }
)
SLICE_SEVEN_LEADS_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/leads/tests/test_services.py",
            "test_conversion_finalizer_runs_inside_outer_transaction_before_rollback",
            "apps.leads.services._finalize_lead_conversion_side_effects",
        ): 1,
        (
            "apps/leads/tests/test_services.py",
            "test_paid_conversion_finalizer_runs_inside_outer_transaction_before_rollback",
            "apps.leads.services._finalize_lead_conversion_side_effects",
        ): 1,
    }
)
SLICE_SEVEN_STUDENTS_PROOF_PATCH_SITES = Counter(
    {
        (
            "apps/students/tests/test_services.py",
            "test_transition_status_keeps_facade_finalizer_on_commit_and_skips_outer_rollback",
            "apps.leads.services._finalize_lead_conversion_side_effects",
        ): 1,
    }
)

OWNERSHIP_LOG_EVENT_EXTRAS = {
    "lead_claimed": frozenset({"student_id", "club_id", "trainer_id"}),
    "lead_released": frozenset({"student_id", "club_id", "trainer_id"}),
    "lead_assigned": frozenset({"student_id", "club_id", "old_trainer_id", "new_trainer_id"}),
    "lead_status_updated": frozenset({"student_id", "club_id", "old_status", "new_status"}),
}
INTAKE_LOG_EVENT_EXTRAS = {
    "lead_created": frozenset({"student_id", "club_id"}),
    "landing_lead_intake_created": frozenset(
        {
            "event_id",
            "student_id",
            "club_id",
            "is_repeat_submission",
            "requires_owner_review",
        }
    ),
}
FUNNEL_LOG_EVENT_EXTRAS = {
    "lead_lost": frozenset({"student_id", "club_id", "loss_reason"}),
    "lead_contact_outcome_recorded": frozenset({"student_id", "club_id", "outcome"}),
}
TRIALS_LOG_EVENT_EXTRAS = {
    "trial_completed_from_checkin": frozenset({"student_id", "club_id"}),
    "trial_booked": frozenset({"student_id", "club_id", "schedule_id", "occurrence_date"}),
    "personal_trial_booked": frozenset({"student_id", "club_id", "schedule_id", "trial_date"}),
}
CONVERSIONS_LOG_EVENT_EXTRAS = {
    "lead_converted": frozenset({"student_id", "club_id"}),
    "lead_converted_after_payment": frozenset({"student_id", "club_id"}),
}

EXPECTED_STATIC_ERROR_CODES = frozenset(
    {
        "consent_required",
        "contact_due_date_in_past",
        "contact_due_date_required",
        "invalid_contact_outcome",
        "invalid_loss_reason",
        "invalid_personal_trial_time",
        "invalid_phone",
        "invalid_preferred_format",
        "invalid_transition",
        "invalid_trial_booking_mode",
        "lead_assignment_required",
        "lead_conversion_requires_paid_subscription",
        "lead_not_archived",
        "location_club_mismatch",
        "location_required",
        "loss_reason_required",
        "manual_operational_admission_evidence_invalid",
        "not_a_lead",
        "not_your_lead",
        "personal_trial_crosses_date",
        "personal_trial_not_supported",
        "personal_trial_requires_personal_type",
        "personal_trial_slot_conflict",
        "reason_required",
        "schedule_club_mismatch",
        "schedule_occurrence_not_found",
        "schedule_trainer_mismatch",
        "trainer_club_mismatch",
        "trainer_location_required",
        "trainer_rate_required",
        "trainer_required",
        "training_type_club_mismatch",
        "training_type_required",
        "trial_booking_requires_book_trial",
        "trial_done_requires_checkin",
        "trial_schedule_required",
        "trial_start_not_future",
        "trial_time_mismatch",
    }
)

TARGET_MODULES = frozenset({"_shared", "intake", "ownership", "trials", "conversions", "funnel"})
TARGET_DEPENDENCIES = {
    "_shared": frozenset(),
    "intake": frozenset({"_shared"}),
    "ownership": frozenset({"_shared"}),
    "trials": frozenset({"_shared"}),
    "conversions": frozenset({"_shared", "trials"}),
    "funnel": frozenset({"_shared"}),
}


def _tracked_python_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        ROOT / relative_path
        for relative_path in result.stdout.splitlines()
        if relative_path.endswith(".py") and ROOT / relative_path != CONTRACT_PATH
    ]


def _service_import_ledger() -> tuple[
    list[tuple[str, tuple[str, ...]]],
    list[tuple[str, str]],
    list[tuple[str, str]],
]:
    direct_imports: list[tuple[str, tuple[str, ...]]] = []
    module_aliases: dict[str, set[str]] = {}
    parsed_files: list[tuple[str, ast.Module]] = []

    for path in _tracked_python_paths():
        relative_path = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parsed_files.append((relative_path, tree))
        aliases = module_aliases.setdefault(relative_path, set())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == SERVICE_MODULE:
                direct_imports.append((relative_path, tuple(alias.name for alias in node.names)))
            elif isinstance(node, ast.ImportFrom) and node.module == SERVICE_PACKAGE:
                for alias in node.names:
                    if alias.name == "services":
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == SERVICE_MODULE:
                        aliases.add(alias.asname or alias.name.rsplit(".", maxsplit=1)[-1])

    module_imports = [
        (relative_path, alias)
        for relative_path, aliases in module_aliases.items()
        for alias in sorted(aliases)
    ]
    module_attributes = [
        (relative_path, node.attr)
        for relative_path, tree in parsed_files
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in module_aliases[relative_path]
    ]
    return sorted(direct_imports), sorted(module_imports), sorted(module_attributes)


def _split_slice_additive_imports(
    direct_imports: list[tuple[str, tuple[str, ...]]],
) -> tuple[
    list[tuple[str, tuple[str, ...]]],
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
]:
    slice_five_expected_additions = Counter(SLICE_FIVE_ADDITIVE_DIRECT_IMPORTS)
    slice_six_expected_additions = Counter(SLICE_SIX_ADDITIVE_DIRECT_IMPORTS)
    slice_seven_expected_additions = Counter(SLICE_SEVEN_ADDITIVE_DIRECT_IMPORTS)
    slice_eight_expected_additions = Counter(SLICE_EIGHT_ADDITIVE_DIRECT_IMPORTS)
    slice_five_actual_additions: Counter[tuple[str, str]] = Counter()
    slice_six_actual_additions: Counter[tuple[str, str]] = Counter()
    slice_seven_actual_additions: Counter[tuple[str, str]] = Counter()
    slice_eight_actual_additions: Counter[tuple[str, str]] = Counter()
    baseline_imports: list[tuple[str, tuple[str, ...]]] = []

    for relative_path, names in direct_imports:
        baseline_names = []
        for name in names:
            import_site = (relative_path, name)
            if import_site in slice_five_expected_additions:
                slice_five_actual_additions[import_site] += 1
            elif import_site in slice_six_expected_additions:
                slice_six_actual_additions[import_site] += 1
            elif import_site in slice_seven_expected_additions:
                slice_seven_actual_additions[import_site] += 1
            elif import_site in slice_eight_expected_additions:
                slice_eight_actual_additions[import_site] += 1
            else:
                baseline_names.append(name)
        if baseline_names:
            baseline_imports.append((relative_path, tuple(baseline_names)))

    return (
        sorted(baseline_imports),
        sorted(slice_five_actual_additions.elements()),
        sorted(slice_six_actual_additions.elements()),
        sorted(slice_seven_actual_additions.elements()),
        sorted(slice_eight_actual_additions.elements()),
    )


def _facade_module_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SERVICE_PACKAGE:
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "services")
        elif isinstance(node, ast.Import):
            aliases.update(
                alias.asname or alias.name.rsplit(".", maxsplit=1)[-1]
                for alias in node.names
                if alias.name == SERVICE_MODULE
            )
    return aliases


def _facade_patch_targets() -> tuple[Counter[str], Counter[str]]:
    string_targets: Counter[str] = Counter()
    object_targets: Counter[str] = Counter()
    for path in _tracked_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_aliases = _facade_module_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            call_name = node.func.id if isinstance(node.func, ast.Name) else ""
            if isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            target = node.args[0]
            if (
                call_name in {"patch", "setattr"}
                and isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and target.value.startswith(f"{SERVICE_MODULE}.")
            ):
                string_targets[target.value] += 1
            elif (
                call_name == "setattr"
                and isinstance(target, ast.Name)
                and target.id in module_aliases
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                object_targets[node.args[1].value] += 1
    return string_targets, object_targets


def _facade_proof_patch_sites(
    *,
    path: Path,
    expected_sites: Counter[tuple[str, str, str]],
) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    test_functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    sites: Counter[tuple[str, str, str]] = Counter()
    relative_path = path.relative_to(ROOT).as_posix()
    expected_test_names = {test_name for _, test_name, _ in expected_sites}

    for test_name in expected_test_names:
        for node in ast.walk(test_functions[test_name]):
            if not isinstance(node, ast.Call):
                continue
            call_name = node.func.id if isinstance(node.func, ast.Name) else ""
            if isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            if (
                call_name in {"patch", "setattr"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith(f"{SERVICE_MODULE}.")
            ):
                sites[(relative_path, test_name, node.args[0].value)] += 1
    return sites


def _slice_three_proof_patch_sites() -> Counter[tuple[str, str, str]]:
    return _facade_proof_patch_sites(
        path=ROOT / "apps" / "leads" / "tests" / "test_services.py",
        expected_sites=SLICE_THREE_PROOF_PATCH_SITES,
    )


def _slice_four_proof_patch_sites() -> Counter[tuple[str, str, str]]:
    return _facade_proof_patch_sites(
        path=ROOT / "apps" / "leads" / "tests" / "test_landing_intake_services.py",
        expected_sites=SLICE_FOUR_PROOF_PATCH_SITES,
    )


def _slice_six_proof_patch_sites() -> Counter[tuple[str, str, str]]:
    return _facade_proof_patch_sites(
        path=ROOT / "apps" / "leads" / "tests" / "test_services.py",
        expected_sites=SLICE_SIX_PROOF_PATCH_SITES,
    )


def _slice_seven_proof_patch_sites() -> Counter[tuple[str, str, str]]:
    return _facade_proof_patch_sites(
        path=ROOT / "apps" / "leads" / "tests" / "test_services.py",
        expected_sites=SLICE_SEVEN_LEADS_PROOF_PATCH_SITES,
    ) + _facade_proof_patch_sites(
        path=ROOT / "apps" / "students" / "tests" / "test_services.py",
        expected_sites=SLICE_SEVEN_STUDENTS_PROOF_PATCH_SITES,
    )


def _slice_nine_proof_patch_sites() -> Counter[tuple[str, str, str]]:
    return _facade_proof_patch_sites(
        path=ROOT / "apps" / "attendance" / "tests" / "test_staff_personal_intents.py",
        expected_sites=SLICE_NINE_PROOF_PATCH_SITES,
    )


def _logger_event_extras(source: str) -> list[tuple[str, frozenset[str]]]:
    tree = ast.parse(source)
    events: list[tuple[str, frozenset[str]]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.func.attr == "info"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        extra = next((keyword.value for keyword in node.keywords if keyword.arg == "extra"), None)
        if not isinstance(extra, ast.Dict) or not all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in extra.keys
        ):
            continue
        events.append(
            (
                node.args[0].value,
                frozenset(key.value for key in extra.keys),
            )
        )
    return events


def _module_path_label(path: Path) -> str:
    if path.is_relative_to(ROOT):
        return path.relative_to(ROOT).as_posix()
    return path.name


def _canonical_import_from(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    package_parts = INTERNAL_PACKAGE.split(".")
    parent_parts = package_parts[: len(package_parts) - node.level + 1]
    assert parent_parts, "relative import escapes the apps package"
    if node.module:
        parent_parts.extend(node.module.split("."))
    return ".".join(parent_parts)


def _canonical_relative_dynamic_target(target: str, *, package: str) -> str | None:
    relative_level = len(target) - len(target.lstrip("."))
    package_parts = package.split(".")
    parent_parts = package_parts[: len(package_parts) - relative_level + 1]
    if not parent_parts:
        return None
    target_suffix = target[relative_level:]
    if target_suffix:
        parent_parts.extend(target_suffix.split("."))
    return ".".join(parent_parts)


def _dynamic_import_package_context(node: ast.Call) -> str | None:
    package_arguments = [keyword.value for keyword in node.keywords if keyword.arg == "package"]
    if len(node.args) >= 2:
        package_arguments.append(node.args[1])
    if len(package_arguments) != 1:
        return None

    package_argument = package_arguments[0]
    if isinstance(package_argument, ast.Name) and package_argument.id == "__package__":
        return INTERNAL_PACKAGE
    if isinstance(package_argument, ast.Constant) and package_argument.value == INTERNAL_PACKAGE:
        return INTERNAL_PACKAGE
    return None


def _builtin_relative_import_target(node: ast.Call, target: str) -> str | None:
    level_arguments = [keyword.value for keyword in node.keywords if keyword.arg == "level"]
    if len(node.args) >= 5:
        level_arguments.append(node.args[4])
    if len(level_arguments) != 1:
        return None

    level_argument = level_arguments[0]
    if not isinstance(level_argument, ast.Constant) or not isinstance(level_argument.value, int):
        return None
    if level_argument.value <= 0:
        return None
    return _canonical_relative_dynamic_target(
        "." * level_argument.value + target,
        package=INTERNAL_PACKAGE,
    )


def _record_dynamic_import_target(
    *,
    target: str,
    relative_path: str,
    graph: dict[str, set[str]],
    module_name: str,
    facade_imports: list[str],
    package_root_imports: list[str],
) -> None:
    if target == SERVICE_MODULE:
        facade_imports.append(relative_path)
    elif target == INTERNAL_PACKAGE:
        package_root_imports.append(relative_path)
    elif target.startswith(f"{INTERNAL_PACKAGE}."):
        graph[module_name].add(target.removeprefix(f"{INTERNAL_PACKAGE}.").split(".")[0])


def _module_graph(
    internal_root: Path = INTERNAL_ROOT,
) -> tuple[dict[str, set[str]], list[str], list[str], list[str], list[str], list[str]]:
    module_paths = sorted(
        path for path in internal_root.glob("*.py") if path.name != "__init__.py"
    )
    module_names = {path.stem for path in module_paths}
    assert module_names <= TARGET_MODULES

    graph = {module_name: set() for module_name in module_names}
    facade_imports: list[str] = []
    package_root_imports: list[str] = []
    wildcard_imports: list[str] = []
    builtin_internal_imports: list[str] = []
    ambiguous_relative_dynamic_imports: list[str] = []

    for path in module_paths:
        relative_path = _module_path_label(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        import_module_calls = {"import_module"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib":
                        import_module_calls.add(f"{alias.asname or alias.name}.import_module")
                    if alias.name == SERVICE_MODULE:
                        facade_imports.append(relative_path)
                    elif alias.name == INTERNAL_PACKAGE:
                        package_root_imports.append(relative_path)
                    elif alias.name.startswith(f"{INTERNAL_PACKAGE}."):
                        graph[path.stem].add(alias.name.removeprefix(f"{INTERNAL_PACKAGE}.").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    wildcard_imports.append(relative_path)
                if node.module == "importlib":
                    for alias in node.names:
                        if alias.name == "import_module":
                            import_module_calls.add(alias.asname or alias.name)
                imported_module = _canonical_import_from(node)
                if imported_module == SERVICE_MODULE or (
                    imported_module == SERVICE_PACKAGE
                    and any(alias.name == "services" for alias in node.names)
                ):
                    facade_imports.append(relative_path)
                elif (
                    imported_module == INTERNAL_PACKAGE
                    or (
                        imported_module == SERVICE_PACKAGE
                        and any(alias.name == "service_modules" for alias in node.names)
                    )
                ):
                    package_root_imports.append(relative_path)
                elif imported_module.startswith(f"{INTERNAL_PACKAGE}."):
                    graph[path.stem].add(
                        imported_module.removeprefix(f"{INTERNAL_PACKAGE}.").split(".")[0]
                    )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"
            else:
                continue
            target = node.args[0]
            if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
                continue
            if call_name == "__import__":
                level_arguments = [
                    keyword.value for keyword in node.keywords if keyword.arg == "level"
                ]
                if len(node.args) >= 5:
                    level_arguments.append(node.args[4])
                if target.value.startswith(".") or level_arguments:
                    resolved_target = _builtin_relative_import_target(node, target.value)
                    if resolved_target is None:
                        ambiguous_relative_dynamic_imports.append(relative_path)
                    elif resolved_target == SERVICE_MODULE:
                        facade_imports.append(relative_path)
                    elif resolved_target == INTERNAL_PACKAGE or resolved_target.startswith(
                        f"{INTERNAL_PACKAGE}."
                    ):
                        builtin_internal_imports.append(relative_path)
                elif target.value == SERVICE_MODULE:
                    facade_imports.append(relative_path)
                elif target.value == INTERNAL_PACKAGE or target.value.startswith(
                    f"{INTERNAL_PACKAGE}."
                ):
                    builtin_internal_imports.append(relative_path)
                continue
            if call_name not in import_module_calls:
                continue
            if target.value.startswith("."):
                package_context = _dynamic_import_package_context(node)
                resolved_target = (
                    _canonical_relative_dynamic_target(target.value, package=package_context)
                    if package_context is not None
                    else None
                )
                if resolved_target is None:
                    ambiguous_relative_dynamic_imports.append(relative_path)
                    continue
            else:
                resolved_target = target.value
            _record_dynamic_import_target(
                target=resolved_target,
                relative_path=relative_path,
                graph=graph,
                module_name=path.stem,
                facade_imports=facade_imports,
                package_root_imports=package_root_imports,
            )

    return (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_internal_imports,
        ambiguous_relative_dynamic_imports,
    )


def _assert_acyclic(graph: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_name: str, path: tuple[str, ...] = ()) -> None:
        assert module_name not in visiting, " -> ".join((*path, module_name))
        if module_name in visited:
            return
        visiting.add(module_name)
        for dependency in sorted(graph[module_name]):
            visit(dependency, (*path, module_name))
        visiting.remove(module_name)
        visited.add(module_name)

    for module_name in sorted(graph):
        visit(module_name)


def _assert_internal_dependency_contract(
    graph: dict[str, set[str]],
    facade_imports: list[str],
    package_root_imports: list[str],
    wildcard_imports: list[str],
    builtin_internal_imports: list[str],
    ambiguous_relative_dynamic_imports: list[str],
) -> None:
    assert facade_imports == []
    assert package_root_imports == []
    assert wildcard_imports == []
    assert builtin_internal_imports == []
    assert ambiguous_relative_dynamic_imports == []
    assert set(graph) <= TARGET_MODULES
    for module_name, dependencies in graph.items():
        assert dependencies <= TARGET_DEPENDENCIES[module_name]
        assert dependencies <= set(graph)
    _assert_acyclic(graph)

    if CURRENT_REFACTOR_SLICE >= 8:
        assert ("conversions" in graph) and ("trials" in graph["conversions"])
    if CURRENT_REFACTOR_SLICE >= 7:
        assert "conversions" in graph
    if CURRENT_REFACTOR_SLICE >= 9:
        assert set(graph) == TARGET_MODULES
        assert {name: frozenset(dependencies) for name, dependencies in graph.items()} == TARGET_DEPENDENCIES


def test_facade_interface_signatures_and_compatibility_bindings_are_frozen():
    assert set(EXPECTED_SIGNATURES) | {"LeadClaimConflictError"} == PUBLIC_INTERFACE
    assert tuple(lead_services.__all__) == EXPECTED_PUBLIC_EXPORT_ORDER
    assert {name for name in PUBLIC_INTERFACE if hasattr(lead_services, name)} == PUBLIC_INTERFACE
    assert {
        name: str(inspect.signature(getattr(lead_services, name)))
        for name in EXPECTED_SIGNATURES
    } == EXPECTED_SIGNATURES

    assert lead_services.LeadClaimConflictError is leads_api.LeadClaimConflictError
    assert str(inspect.signature(lead_services.LeadClaimConflictError)) == (
        "(message: 'str' = 'Lead is already assigned')"
    )
    assert lead_services.LeadClaimConflictError().message == "Lead is already assigned"
    assert lead_services.VALID_TRANSITIONS == {
        "new": ["contacted"],
        "contacted": ["thinking"],
        "trial_booked": ["trial_done"],
        "trial_done": ["thinking"],
        "thinking": [],
    }
    assert lead_services.CONTACT_OUTCOMES == {
        "contacted",
        "no_answer",
        "follow_up",
        "book_trial",
        "sell_group",
        "sell_personal",
        "lost",
    }
    for name in (
        "_finalize_lead_conversion_side_effects",
        "_queue_lead_intake_telegram",
        "_trigger_trial_done_side_effects",
        "resolve_staff_intake_person_identity",
        "timezone",
    ):
        assert hasattr(lead_services, name)


def test_facade_callers_aliases_and_patch_targets_match_the_baseline_ledger():
    direct_imports, module_alias_imports, module_alias_attributes = _service_import_ledger()
    (
        baseline_direct_imports,
        slice_five_additive_imports,
        slice_six_additive_imports,
        slice_seven_additive_imports,
        slice_eight_additive_imports,
    ) = _split_slice_additive_imports(direct_imports)
    string_patch_targets, object_patch_targets = _facade_patch_targets()
    slice_three_proof_patch_sites = _slice_three_proof_patch_sites()
    slice_four_proof_patch_sites = _slice_four_proof_patch_sites()
    slice_six_proof_patch_sites = _slice_six_proof_patch_sites()
    slice_seven_proof_patch_sites = _slice_seven_proof_patch_sites()
    slice_nine_proof_patch_sites = _slice_nine_proof_patch_sites()
    proof_patch_sites = (
        slice_three_proof_patch_sites
        + slice_four_proof_patch_sites
        + slice_six_proof_patch_sites
        + slice_seven_proof_patch_sites
        + slice_nine_proof_patch_sites
    )
    proof_patch_targets = Counter(target for _, _, target in proof_patch_sites.elements())

    assert len(direct_imports) == 37
    assert sum(len(names) for _, names in direct_imports) == 73
    assert baseline_direct_imports == list(EXPECTED_DIRECT_IMPORTS)
    assert len(baseline_direct_imports) == 32
    assert sum(len(names) for _, names in baseline_direct_imports) == 63
    assert slice_five_additive_imports == list(SLICE_FIVE_ADDITIVE_DIRECT_IMPORTS)
    assert len(slice_five_additive_imports) == 1
    assert slice_six_additive_imports == list(SLICE_SIX_ADDITIVE_DIRECT_IMPORTS)
    assert len(slice_six_additive_imports) == 1
    assert slice_seven_additive_imports == list(SLICE_SEVEN_ADDITIVE_DIRECT_IMPORTS)
    assert len(slice_seven_additive_imports) == 0
    assert slice_eight_additive_imports == list(SLICE_EIGHT_ADDITIVE_DIRECT_IMPORTS)
    assert len(slice_eight_additive_imports) == 8
    assert module_alias_imports == list(EXPECTED_MODULE_ALIAS_IMPORTS)
    assert module_alias_attributes == list(EXPECTED_MODULE_ALIAS_ATTRIBUTES)
    assert slice_three_proof_patch_sites == SLICE_THREE_PROOF_PATCH_SITES
    assert slice_four_proof_patch_sites == SLICE_FOUR_PROOF_PATCH_SITES
    assert slice_six_proof_patch_sites == SLICE_SIX_PROOF_PATCH_SITES
    assert slice_seven_proof_patch_sites == (
        SLICE_SEVEN_LEADS_PROOF_PATCH_SITES + SLICE_SEVEN_STUDENTS_PROOF_PATCH_SITES
    )
    assert slice_nine_proof_patch_sites == SLICE_NINE_PROOF_PATCH_SITES
    assert string_patch_targets == BASELINE_STRING_PATCH_TARGETS + proof_patch_targets
    assert string_patch_targets - proof_patch_targets == BASELINE_STRING_PATCH_TARGETS
    assert sum(BASELINE_STRING_PATCH_TARGETS.values()) == 14
    assert object_patch_targets == BASELINE_OBJECT_PATCH_TARGETS
    assert {
        target.removeprefix(f"{SERVICE_MODULE}.") for target in BASELINE_STRING_PATCH_TARGETS
    } | set(object_patch_targets) == BASELINE_FACADE_PATCH_BINDINGS


def test_slice_four_facade_adapters_preserve_compatibility_bindings():
    create_lead_source = inspect.getsource(lead_services.create_lead)
    landing_intake_source = inspect.getsource(lead_services.create_landing_lead_intake)

    assert "intake_lifecycle.create_lead(" in create_lead_source
    assert (
        "resolve_staff_intake_person_identity=resolve_staff_intake_person_identity"
        in create_lead_source
    )
    assert "intake_lifecycle.create_landing_lead_intake(" in landing_intake_source
    assert "queue_lead_intake_telegram=_queue_lead_intake_telegram" in landing_intake_source
    assert "now=timezone.now" in landing_intake_source


def test_slice_six_facade_adapters_preserve_trial_patch_bindings():
    completion_source = inspect.getsource(lead_services.complete_booked_trial_after_checkin)
    booking_source = inspect.getsource(lead_services.book_trial)
    trigger_source = inspect.getsource(lead_services._trigger_trial_done_side_effects)

    assert lead_services.is_exact_booked_trial_checkin is trials_lifecycle.is_exact_booked_trial_checkin
    assert "trials_lifecycle.complete_booked_trial_after_checkin(" in completion_source
    assert "trigger_trial_done_side_effects=_trigger_trial_done_side_effects" in completion_source
    assert "trials_lifecycle.book_trial(" in booking_source
    assert "now=timezone.now" in booking_source
    assert "trials_lifecycle._trigger_trial_done_side_effects(" in trigger_source


def test_slice_six_facade_forwards_trial_calls_to_the_implementation_owner(monkeypatch):
    expected = object()
    completion_calls = []
    booking_calls = []

    def completion(**kwargs):
        completion_calls.append(kwargs)
        return expected

    def booking(**kwargs):
        booking_calls.append(kwargs)
        return expected

    monkeypatch.setattr(trials_lifecycle, "complete_booked_trial_after_checkin", completion)
    monkeypatch.setattr(trials_lifecycle, "book_trial", booking)

    checkin = object()
    assert lead_services.complete_booked_trial_after_checkin(
        club_id=1,
        student_id=2,
        checkin=checkin,
    ) is expected
    assert lead_services.book_trial(
        club_id=1,
        student_id=2,
        trial_date="trial-date",
        schedule_id=3,
        occurrence_date="occurrence-date",
        required_trainer_id=4,
        mode="group",
        starts_at="starts-at",
        ends_at="ends-at",
        trainer_id=5,
        location_id=6,
        training_type_id=7,
        actor_user_id=8,
        required_assigned_trainer_id=9,
    ) is expected

    assert completion_calls == [
        {
            "club_id": 1,
            "student_id": 2,
            "checkin": checkin,
            "trigger_trial_done_side_effects": lead_services._trigger_trial_done_side_effects,
        },
    ]
    assert booking_calls == [
        {
            "club_id": 1,
            "student_id": 2,
            "trial_date": "trial-date",
            "schedule_id": 3,
            "occurrence_date": "occurrence-date",
            "required_trainer_id": 4,
            "mode": "group",
            "starts_at": "starts-at",
            "ends_at": "ends-at",
            "trainer_id": 5,
            "location_id": 6,
            "training_type_id": 7,
            "actor_user_id": 8,
            "required_assigned_trainer_id": 9,
            "now": lead_services.timezone.now,
        },
    ]


def test_slice_seven_facade_adapters_preserve_conversion_patch_bindings():
    convert_source = inspect.getsource(lead_services.convert_lead)
    manual_source = inspect.getsource(lead_services.convert_lead_for_manual_operational_admission)
    subscription_source = inspect.getsource(lead_services.convert_lead_after_subscription_payment)
    personal_source = inspect.getsource(lead_services.convert_lead_after_personal_payment_confirmation)
    finalizer_source = inspect.getsource(lead_services._finalize_lead_conversion_side_effects)

    assert "conversions_lifecycle.convert_lead(" in convert_source
    assert "finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects" in convert_source
    assert "now=timezone.now" in convert_source
    assert "conversions_lifecycle.convert_lead_for_manual_operational_admission(" in manual_source
    assert "finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects" in manual_source
    assert "now=timezone.now" in manual_source
    assert "conversions_lifecycle.convert_lead_after_subscription_payment(" in subscription_source
    assert "finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects" in subscription_source
    assert "now=timezone.now" in subscription_source
    assert "conversions_lifecycle.convert_lead_after_personal_payment_confirmation(" in personal_source
    assert "now=timezone.now" in personal_source
    assert "conversions_lifecycle._finalize_lead_conversion_side_effects(" in finalizer_source


def test_slice_seven_facade_forwards_conversion_calls_to_the_implementation_owner(monkeypatch):
    expected = object()
    calls = {}

    def implementation(name):
        def call(**kwargs):
            calls[name] = kwargs
            return expected

        return call

    monkeypatch.setattr(conversions_lifecycle, "convert_lead", implementation("convert"))
    monkeypatch.setattr(
        conversions_lifecycle,
        "convert_lead_for_manual_operational_admission",
        implementation("manual"),
    )
    monkeypatch.setattr(
        conversions_lifecycle,
        "convert_lead_after_subscription_payment",
        implementation("subscription"),
    )
    monkeypatch.setattr(
        conversions_lifecycle,
        "convert_lead_after_personal_payment_confirmation",
        implementation("personal"),
    )

    assert lead_services.convert_lead(club_id=1, student_id=2, actor_user_id=3) is expected
    assert lead_services.convert_lead_for_manual_operational_admission(
        club_id=1,
        student_id=2,
        actor_user_id=3,
    ) is expected
    assert lead_services.convert_lead_after_subscription_payment(
        club_id=1,
        student_id=2,
        actor_user_id=3,
    ) is expected
    assert lead_services.convert_lead_after_personal_payment_confirmation(
        club_id=1,
        student_id=2,
        payment_id=4,
        actor_user_id=3,
    ) is expected

    assert calls == {
        "convert": {
            "club_id": 1,
            "student_id": 2,
            "actor_user_id": 3,
            "finalize_lead_conversion_side_effects": lead_services._finalize_lead_conversion_side_effects,
            "now": lead_services.timezone.now,
        },
        "manual": {
            "club_id": 1,
            "student_id": 2,
            "actor_user_id": 3,
            "finalize_lead_conversion_side_effects": lead_services._finalize_lead_conversion_side_effects,
            "now": lead_services.timezone.now,
        },
        "subscription": {
            "club_id": 1,
            "student_id": 2,
            "actor_user_id": 3,
            "finalize_lead_conversion_side_effects": lead_services._finalize_lead_conversion_side_effects,
            "now": lead_services.timezone.now,
        },
        "personal": {
            "club_id": 1,
            "student_id": 2,
            "payment_id": 4,
            "actor_user_id": 3,
            "now": lead_services.timezone.now,
        },
    }


def test_slice_eight_facade_adapters_preserve_compensation_and_attendance_bindings():
    group_conversion_source = inspect.getsource(lead_services.convert_lead_after_group_admission_checkin)
    personal_conversion_source = inspect.getsource(lead_services.convert_lead_after_personal_attendance)

    assert (
        lead_services.snooze_lead_for_pending_personal_payment
        is conversions_lifecycle.snooze_lead_for_pending_personal_payment
    )
    assert (
        lead_services.restore_lead_after_terminal_personal_payment
        is conversions_lifecycle.restore_lead_after_terminal_personal_payment
    )
    assert (
        lead_services.snooze_lead_for_pending_group_payment
        is conversions_lifecycle.snooze_lead_for_pending_group_payment
    )
    assert (
        lead_services.restore_lead_after_terminal_group_payment
        is conversions_lifecycle.restore_lead_after_terminal_group_payment
    )
    assert "conversions_lifecycle.convert_lead_after_group_admission_checkin(" in group_conversion_source
    assert "finalize_lead_conversion_side_effects=_finalize_lead_conversion_side_effects" in (
        group_conversion_source
    )
    assert "now=timezone.now" in group_conversion_source
    assert "conversions_lifecycle.convert_lead_after_personal_attendance(" in personal_conversion_source
    assert "is_exact_booked_trial_checkin=is_exact_booked_trial_checkin" in personal_conversion_source
    assert "now=timezone.now" in personal_conversion_source


def test_slice_eight_facade_forwards_compensation_and_attendance_calls(monkeypatch):
    expected = object()
    calls = {}

    def implementation(name):
        def call(**kwargs):
            calls[name] = kwargs
            return expected

        return call

    for function_name, call_name in (
        ("convert_lead_after_group_admission_checkin", "group_conversion"),
        ("convert_lead_after_personal_attendance", "personal_conversion"),
    ):
        monkeypatch.setattr(conversions_lifecycle, function_name, implementation(call_name))

    assert lead_services.convert_lead_after_group_admission_checkin(
        club_id=1,
        student_id=2,
        schedule_id=3,
        checkin_id=4,
        actor_user_id=5,
    ) is expected
    assert lead_services.convert_lead_after_personal_attendance(
        club_id=1,
        student_id=2,
        booking_id=3,
        checkin_id=4,
        payment_id=5,
        actor_user_id=6,
    ) is expected

    assert calls == {
        "group_conversion": {
            "club_id": 1,
            "student_id": 2,
            "schedule_id": 3,
            "checkin_id": 4,
            "actor_user_id": 5,
            "finalize_lead_conversion_side_effects": lead_services._finalize_lead_conversion_side_effects,
            "now": lead_services.timezone.now,
        },
        "personal_conversion": {
            "club_id": 1,
            "student_id": 2,
            "booking_id": 3,
            "checkin_id": 4,
            "payment_id": 5,
            "actor_user_id": 6,
            "is_exact_booked_trial_checkin": lead_services.is_exact_booked_trial_checkin,
            "now": lead_services.timezone.now,
        },
    }


def test_slice_six_dormant_personal_metadata_uses_validated_training_type_id():
    trials_path = Path(inspect.getsourcefile(trials_lifecycle._book_personal_trial_locked) or "")
    tree = ast.parse(trials_path.read_text(encoding="utf-8"), filename=str(trials_path))
    personal_booking = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_book_personal_trial_locked"
    )
    metadata_training_type_values = [
        value
        for node in ast.walk(personal_booking)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_record_lifecycle_event", "ScheduleBookingEvent"}
        for keyword in node.keywords
        if keyword.arg == "metadata" and isinstance(keyword.value, ast.Dict)
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "training_type_id"
    ]

    assert len(metadata_training_type_values) == 2
    assert all(
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "training_type"
        and value.attr == "id"
        for value in metadata_training_type_values
    )


def test_slice_four_dynamic_required_error_code_stays_in_intake_helper():
    intake_path = Path(inspect.getsourcefile(intake_lifecycle._clean_required_text) or "")
    assert intake_path == INTERNAL_ROOT / "intake.py"

    intake_tree = ast.parse(intake_path.read_text(encoding="utf-8"), filename=str(intake_path))
    clean_required_text = next(
        node
        for node in intake_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_clean_required_text"
    )
    error_code_values = [
        keyword.value
        for node in ast.walk(clean_required_text)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BusinessLogicError"
        for keyword in node.keywords
        if keyword.arg == "code"
    ]

    assert len(error_code_values) == 1
    code_value = error_code_values[0]
    assert isinstance(code_value, ast.JoinedStr)
    assert len(code_value.values) == 2
    field_name, suffix = code_value.values
    assert isinstance(field_name, ast.FormattedValue)
    assert isinstance(field_name.value, ast.Name)
    assert field_name.value.id == "field"
    assert isinstance(suffix, ast.Constant)
    assert suffix.value == "_required"


def test_slice_nine_facade_and_lifecycle_owners_preserve_contracts():
    service_path = ROOT / "apps" / "leads" / "services.py"
    shared_path = INTERNAL_ROOT / "_shared.py"
    intake_path = INTERNAL_ROOT / "intake.py"
    ownership_path = INTERNAL_ROOT / "ownership.py"
    trials_path = INTERNAL_ROOT / "trials.py"
    conversions_path = INTERNAL_ROOT / "conversions.py"
    funnel_path = INTERNAL_ROOT / "funnel.py"
    source = service_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(service_path))
    function_definitions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    shared_source = shared_path.read_text(encoding="utf-8")
    shared_tree = ast.parse(shared_source, filename=str(shared_path))
    shared_function_definitions = {
        node.name
        for node in shared_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    intake_source = intake_path.read_text(encoding="utf-8")
    intake_tree = ast.parse(intake_source, filename=str(intake_path))
    intake_function_definitions = {
        node.name
        for node in intake_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    ownership_source = ownership_path.read_text(encoding="utf-8")
    ownership_tree = ast.parse(ownership_source, filename=str(ownership_path))
    ownership_function_definitions = {
        node.name
        for node in ownership_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    trials_source = trials_path.read_text(encoding="utf-8")
    trials_tree = ast.parse(trials_source, filename=str(trials_path))
    trials_function_definitions = {
        node.name
        for node in trials_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    conversions_source = conversions_path.read_text(encoding="utf-8")
    conversions_tree = ast.parse(conversions_source, filename=str(conversions_path))
    conversions_function_definitions = {
        node.name
        for node in conversions_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    funnel_source = funnel_path.read_text(encoding="utf-8")
    funnel_tree = ast.parse(funnel_source, filename=str(funnel_path))
    funnel_function_definitions = {
        node.name
        for node in funnel_tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    static_error_codes = {
        keyword.value.value
        for module_tree in (
            tree,
            shared_tree,
            intake_tree,
            ownership_tree,
            trials_tree,
            conversions_tree,
            funnel_tree,
        )
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BusinessLogicError"
        for keyword in node.keywords
        if keyword.arg == "code"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }

    assert function_definitions == FACADE_ADAPTERS | FACADE_PRIVATE_PATCH_BINDINGS
    assert not any(isinstance(node, ast.With | ast.AsyncWith) for node in ast.walk(tree))
    assert ".objects" not in source
    assert "transaction" not in source
    assert "logger" not in source
    assert shared_function_definitions == SHARED_PRIVATE_HELPERS
    assert intake_function_definitions == INTAKE_IMPLEMENTATIONS | INTAKE_PRIVATE_HELPERS
    assert ownership_function_definitions == OWNERSHIP_IMPLEMENTATIONS | OWNERSHIP_PRIVATE_HELPERS
    assert trials_function_definitions == TRIALS_IMPLEMENTATIONS | TRIALS_PRIVATE_HELPERS
    assert conversions_function_definitions == CONVERSIONS_IMPLEMENTATIONS | CONVERSIONS_PRIVATE_HELPERS
    assert funnel_function_definitions == FUNNEL_IMPLEMENTATIONS
    assert {
        node.name
        for node in shared_tree.body
        if isinstance(node, ast.ClassDef)
    } == {"LeadClaimConflictError"}
    assert set(PUBLIC_OWNER_MODULES) == PUBLIC_INTERFACE
    for name in FACADE_ADAPTERS:
        assert getattr(lead_services, name).__module__ == SERVICE_MODULE
    for name in DIRECT_REEXPORTS:
        owner_module = importlib.import_module(PUBLIC_OWNER_MODULES[name])
        assert getattr(lead_services, name) is getattr(owner_module, name)
        assert getattr(lead_services, name).__module__ == PUBLIC_OWNER_MODULES[name]
    assert lead_services.LeadClaimConflictError is shared_lifecycle.LeadClaimConflictError
    assert lead_services.LeadClaimConflictError.__module__ == PUBLIC_OWNER_MODULES[
        "LeadClaimConflictError"
    ]
    assert lead_services.VALID_TRANSITIONS is ownership_lifecycle.VALID_TRANSITIONS
    assert lead_services.CONTACT_OUTCOMES is funnel_lifecycle.CONTACT_OUTCOMES
    assert intake_lifecycle.logger.name == SERVICE_MODULE
    assert ownership_lifecycle.logger.name == SERVICE_MODULE
    assert funnel_lifecycle.logger.name == SERVICE_MODULE
    assert trials_lifecycle.logger.name == SERVICE_MODULE
    assert conversions_lifecycle.logger.name == SERVICE_MODULE
    intake_log_events = _logger_event_extras(intake_source)
    ownership_log_events = _logger_event_extras(ownership_source)
    funnel_log_events = _logger_event_extras(funnel_source)
    trials_log_events = _logger_event_extras(trials_source)
    conversions_log_events = _logger_event_extras(conversions_source)
    assert len(intake_log_events) == len(INTAKE_LOG_EVENT_EXTRAS)
    assert dict(intake_log_events) == INTAKE_LOG_EVENT_EXTRAS
    assert len(ownership_log_events) == len(OWNERSHIP_LOG_EVENT_EXTRAS)
    assert dict(ownership_log_events) == OWNERSHIP_LOG_EVENT_EXTRAS
    assert len(funnel_log_events) == len(FUNNEL_LOG_EVENT_EXTRAS)
    assert dict(funnel_log_events) == FUNNEL_LOG_EVENT_EXTRAS
    assert len(trials_log_events) == len(TRIALS_LOG_EVENT_EXTRAS)
    assert dict(trials_log_events) == TRIALS_LOG_EVENT_EXTRAS
    assert len(conversions_log_events) == len(CONVERSIONS_LOG_EVENT_EXTRAS)
    assert dict(conversions_log_events) == CONVERSIONS_LOG_EVENT_EXTRAS
    assert static_error_codes == EXPECTED_STATIC_ERROR_CODES

    public_intake_source = inspect.getsource(intake_lifecycle.create_landing_lead_intake)
    transaction_start = public_intake_source.index("with transaction.atomic():")
    lock_index = public_intake_source.index("lock_club_person_identity_arbitration", transaction_start)
    idempotency_index = public_intake_source.index("LeadIntakeEvent.objects.for_club", transaction_start)
    resolve_index = public_intake_source.index("resolve_person_identity", transaction_start)
    assert lead_services.create_landing_lead_intake.__module__ == SERVICE_MODULE
    assert intake_lifecycle.create_landing_lead_intake.__module__ == f"{INTERNAL_PACKAGE}.intake"
    assert lock_index < idempotency_index < resolve_index

    ownership_status_source = inspect.getsource(ownership_lifecycle.update_lead_status)
    lead_lock_index = ownership_status_source.index("_get_lead_for_update")
    scope_revalidation_index = ownership_status_source.index("_ensure_expected_trainer_assignment")
    assert lead_lock_index < scope_revalidation_index

    for implementation in (
        trials_lifecycle.is_exact_booked_trial_checkin,
        trials_lifecycle.complete_booked_trial_after_checkin,
        trials_lifecycle.book_trial,
    ):
        assert implementation.__module__ == f"{INTERNAL_PACKAGE}.trials"
    assert "from apps.attendance.models import ScheduleEnrollment" in inspect.getsource(
        trials_lifecycle.is_exact_booked_trial_checkin
    )
    assert "from apps.attendance.services import enroll_student_in_schedule" in inspect.getsource(
        trials_lifecycle.book_trial
    )
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module == SERVICE_MODULE
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name == SERVICE_MODULE for alias in node.names)
        )
        for node in ast.walk(trials_tree)
    )

    for implementation in (
        conversions_lifecycle.convert_lead,
        conversions_lifecycle.admit_lead_for_manual_operational_admission,
        conversions_lifecycle.convert_lead_after_group_admission_checkin,
        conversions_lifecycle.convert_lead_after_personal_attendance,
        conversions_lifecycle.convert_lead_for_manual_operational_admission,
        conversions_lifecycle.convert_lead_after_subscription_payment,
        conversions_lifecycle.convert_lead_after_personal_payment_confirmation,
        conversions_lifecycle.restore_lead_after_terminal_group_payment,
        conversions_lifecycle.restore_lead_after_terminal_personal_payment,
        conversions_lifecycle.snooze_lead_for_pending_group_payment,
        conversions_lifecycle.snooze_lead_for_pending_personal_payment,
    ):
        assert implementation.__module__ == f"{INTERNAL_PACKAGE}.conversions"
    assert "from apps.billing.models import Subscription" in inspect.getsource(
        conversions_lifecycle._has_paid_active_subscription
    )
    assert "from apps.pipelines.services import cancel_pipeline" in inspect.getsource(
        conversions_lifecycle._finalize_lead_conversion_side_effects
    )
    assert "from apps.retention.services import auto_close_tasks_on_subscription" in inspect.getsource(
        conversions_lifecycle._finalize_lead_conversion_side_effects
    )
    assert "from apps.retention.services import auto_close_tasks_on_personal_admission" in inspect.getsource(
        conversions_lifecycle.convert_lead_after_personal_payment_confirmation
    )
    assert "from apps.leads.service_modules.trials import (" in conversions_source
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module == SERVICE_MODULE
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name == SERVICE_MODULE for alias in node.names)
        )
        for node in ast.walk(conversions_tree)
    )

    for implementation in (
        funnel_lifecycle.lose_lead,
        funnel_lifecycle.record_contact_outcome,
        funnel_lifecycle.reopen_lead,
    ):
        implementation_source = inspect.getsource(implementation)
        assert implementation.__module__ == f"{INTERNAL_PACKAGE}.funnel"
        assert implementation_source.index("_get_lead_for_update") < implementation_source.index(
            "_ensure_expected_trainer_assignment"
        )
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module == SERVICE_MODULE
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name == SERVICE_MODULE for alias in node.names)
        )
        for node in ast.walk(funnel_tree)
    )
    assert "from apps.pipelines.services import cancel_pipeline" in inspect.getsource(
        funnel_lifecycle.lose_lead
    )
    assert "student = lose_lead(" in inspect.getsource(
        funnel_lifecycle.record_contact_outcome
    )


def test_internal_package_is_empty_and_slice_nine_has_conversions_implementation():
    init_path = INTERNAL_ROOT / "__init__.py"

    assert init_path.read_text(encoding="utf-8") == ""
    assert sorted(path.name for path in INTERNAL_ROOT.glob("*.py")) == [
        "__init__.py",
        "_shared.py",
        "conversions.py",
        "funnel.py",
        "intake.py",
        "ownership.py",
        "trials.py",
    ]


@pytest.mark.parametrize("function_name", ("lose_lead", "record_contact_outcome", "reopen_lead"))
def test_slice_nine_facade_directly_reexports_funnel_calls(function_name):
    assert getattr(lead_services, function_name) is getattr(funnel_lifecycle, function_name)


def test_get_lead_for_update_retains_tenant_scoped_locking_query_chain():
    owner_path = Path(inspect.getsourcefile(shared_lifecycle._get_lead_for_update) or "")
    tree = ast.parse(owner_path.read_text(encoding="utf-8"), filename=str(owner_path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_get_lead_for_update"
    )
    return_node = next(node for node in ast.walk(function) if isinstance(node, ast.Return))
    call = return_node.value
    method_calls: list[ast.Call] = []

    while isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
        method_calls.append(call)
        call = call.func.value

    assert [method_call.func.attr for method_call in reversed(method_calls)] == [
        "for_club",
        "select_for_update",
        "get",
    ]
    for_club_call, lock_call, get_call = reversed(method_calls)
    assert len(for_club_call.args) == 1
    assert isinstance(for_club_call.args[0], ast.Name)
    assert for_club_call.args[0].id == "club_id"
    assert lock_call.args == []
    assert lock_call.keywords == []
    assert {keyword.arg for keyword in get_call.keywords} == {"id", "deleted_at__isnull"}
    assert next(
        keyword.value.value
        for keyword in get_call.keywords
        if keyword.arg == "deleted_at__isnull"
        and isinstance(keyword.value, ast.Constant)
    ) is True


def test_internal_dependency_graph_is_slice_aware_acyclic_and_fail_closed():
    _assert_internal_dependency_contract(*_module_graph())


def _fixture_internal_root(tmp_path: Path, source: str) -> Path:
    internal_root = tmp_path / "service_modules"
    internal_root.mkdir()
    (internal_root / "intake.py").write_text(source, encoding="utf-8")
    return internal_root


def test_dependency_graph_rejects_package_root_import_from_apps_leads(tmp_path: Path):
    internal_root = _fixture_internal_root(
        tmp_path,
        "from apps.leads import service_modules\n",
    )
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    assert package_root_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )


def test_dependency_graph_rejects_builtin_internal_dynamic_import(tmp_path: Path):
    internal_root = _fixture_internal_root(
        tmp_path,
        '__import__("apps.leads.service_modules._shared")\n',
    )
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    assert builtin_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )


@pytest.mark.parametrize(
    ("source", "expected_category"),
    [
        ("from .. import services\n", "facade"),
        ("from ..services import claim_lead\n", "facade"),
        ("from ..service_modules import _shared\n", "package_root"),
    ],
)
def test_dependency_graph_rejects_relative_imports_that_escape_the_internal_package(
    tmp_path: Path,
    source: str,
    expected_category: str,
):
    internal_root = _fixture_internal_root(tmp_path, source)
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    if expected_category == "facade":
        assert facade_imports == ["intake.py"]
    else:
        assert package_root_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )


@pytest.mark.parametrize(
    "source",
    [
        "from importlib import import_module\n"
        'import_module("..services", package=__package__)\n',
        "import importlib\n"
        'importlib.import_module("..services", __package__)\n',
    ],
)
def test_dependency_graph_rejects_relative_import_module_facade_escape(
    tmp_path: Path,
    source: str,
):
    internal_root = _fixture_internal_root(tmp_path, source)
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    assert facade_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )


def test_dependency_graph_rejects_relative_builtin_import_facade_escape(tmp_path: Path):
    internal_root = _fixture_internal_root(
        tmp_path,
        '__import__("services", globals(), locals(), [], 2)\n',
    )
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    assert facade_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )


def test_dependency_graph_rejects_relative_dynamic_import_without_package_context(tmp_path: Path):
    internal_root = _fixture_internal_root(
        tmp_path,
        "from importlib import import_module\n"
        'import_module("..services")\n',
    )
    (
        graph,
        facade_imports,
        package_root_imports,
        wildcard_imports,
        builtin_imports,
        ambiguous_imports,
    ) = _module_graph(internal_root)

    assert ambiguous_imports == ["intake.py"]
    with pytest.raises(AssertionError):
        _assert_internal_dependency_contract(
            graph,
            facade_imports,
            package_root_imports,
            wildcard_imports,
            builtin_imports,
            ambiguous_imports,
        )
