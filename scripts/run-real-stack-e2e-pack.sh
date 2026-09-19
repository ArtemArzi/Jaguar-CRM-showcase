#!/usr/bin/env bash
# Select and run one or all named real-stack E2E packs through the canonical runner.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$PROJECT_DIR/scripts/run-real-stack-e2e.sh"

PACKS=(
  base-kiosk
  checkin-lifecycle
  kiosk-negative
  kiosk-guest-book-checkin
  public-lead-intake
  student-parent-self-booking
  trainer-lead-pool-lifecycle
  trainer-student-cockpit-scope
  trainer-student-create-edit
  unified-client-commercial-journey
  trainer-batch
  owner-batch-checkin
  owner-dashboard-business
  dashboard-onboarding
  dashboard-student-management
  student-operations
  tariff-price-revisions
  student-opening-import
  manual-refunds
  trainer-settlements
  dashboard-access-subscriptions
  payment-confirm
  bank-payment-link
  training-group-containment
  refund-resolution
  pwa-ux-ledger-smoke
  account-access-login
  trainer-package-transfer
  subscription-payout-policy
  hybrid-package-entitlements
  trainer-guest-personal-booking
  trainer-personal-intent
  trainer-personal-drop-in
  trainer-payroll-close-correction
  payment-reject
  debt-writeoff
  freeze-lifecycle
  trainer-lead-trial
  student-parent-reflection
  tenant-negative-matrix
  offline-kiosk-sync
  schedule-exception-visibility
  student-feedback
  parent-invite-feedback
  document-checklist-upload
  push-notification-preferences
  mass-notifications
  checkin-cancel
  retention-task-lifecycle
  automatic-notification-lifecycle
  owner-notification-template-settings
  club-settings-business-config
  owner-trainer-rate-grid
)

usage() {
  cat <<'USAGE'
Usage: bash scripts/run-real-stack-e2e-pack.sh [--list|--describe] PACK [runner args...]

PACK can be one of the named real-stack packs or "all".
Runner args are passed to scripts/run-real-stack-e2e.sh, for example --dry-run.

Examples:
  bash scripts/run-real-stack-e2e-pack.sh --list
  bash scripts/run-real-stack-e2e-pack.sh --describe
  bash scripts/run-real-stack-e2e-pack.sh payment-confirm --dry-run
  bash scripts/run-real-stack-e2e-pack.sh all --dry-run
USAGE
}

list_packs() {
  printf 'all\n'
  printf '%s\n' "${PACKS[@]}"
}

describe_packs() {
  local pack
  local mapping
  for pack in "${PACKS[@]}"; do
    mapping="$(resolve_pack "$pack")"
    printf '%s\t%s\n' "$pack" "$mapping"
  done
}

resolve_pack() {
  local pack="$1"
  case "$pack" in
    base-kiosk)
      printf '%s\t%s\n' prepare_real_stack_e2e real-stack-kiosk.spec.ts
      ;;
    checkin-lifecycle)
      printf '%s\t%s\n' prepare_checkin_lifecycle_e2e real-stack-checkin-lifecycle.spec.ts
      ;;
    kiosk-negative)
      printf '%s\t%s\n' prepare_kiosk_negative_e2e real-stack-kiosk-negative.spec.ts
      ;;
    kiosk-guest-book-checkin)
      printf '%s\t%s\n' prepare_kiosk_guest_book_checkin_e2e real-stack-kiosk-guest-book-checkin.spec.ts
      ;;
    public-lead-intake)
      printf '%s\t%s\n' prepare_public_lead_intake_e2e real-stack-public-lead-intake.spec.ts
      ;;
    student-parent-self-booking)
      printf '%s\t%s\n' prepare_student_parent_self_booking_e2e real-stack-student-parent-self-booking.spec.ts
      ;;
    trainer-lead-pool-lifecycle)
      printf '%s\t%s\n' prepare_trainer_lead_pool_lifecycle_e2e real-stack-trainer-lead-pool-lifecycle.spec.ts
      ;;
    trainer-student-cockpit-scope)
      printf '%s\t%s\n' prepare_trainer_student_cockpit_scope_e2e real-stack-trainer-student-cockpit-scope.spec.ts
      ;;
    trainer-student-create-edit)
      printf '%s\t%s\n' prepare_trainer_student_create_edit_e2e real-stack-trainer-student-create-edit.spec.ts
      ;;
    unified-client-commercial-journey)
      printf '%s\t%s\n' prepare_unified_client_commercial_journey_e2e real-stack-unified-client-commercial-journey.spec.ts
      ;;
    trainer-batch)
      printf '%s\t%s\n' prepare_trainer_batch_e2e real-stack-trainer-batch.spec.ts
      ;;
    owner-batch-checkin)
      printf '%s\t%s\n' prepare_owner_batch_checkin_e2e real-stack-owner-batch-checkin.spec.ts
      ;;
    owner-dashboard-business)
      printf '%s\t%s\n' prepare_owner_dashboard_business_e2e real-stack-owner-dashboard-business.spec.ts
      ;;
    dashboard-onboarding)
      printf '%s\t%s\n' prepare_dashboard_onboarding_e2e real-stack-dashboard-onboarding.spec.ts
      ;;
    dashboard-student-management)
      printf '%s\t%s\n' prepare_dashboard_student_management_e2e real-stack-dashboard-student-management.spec.ts
      ;;
    manual-refunds)
      printf '%s\t%s\n' prepare_manual_refunds_e2e real-stack-manual-refunds.spec.ts
      ;;
    student-opening-import)
      printf '%s\t%s\n' prepare_student_opening_import_e2e real-stack-student-opening-import.spec.ts
      ;;
    student-operations)
      printf '%s\t%s\n' prepare_student_operations_e2e real-stack-student-operations.spec.ts
      ;;
    tariff-price-revisions)
      printf '%s\t%s\n' prepare_tariff_price_revisions_e2e real-stack-tariff-price-revisions.spec.ts
      ;;
    trainer-settlements)
      printf '%s\t%s\n' prepare_trainer_settlements_e2e real-stack-trainer-settlements.spec.ts
      ;;
    dashboard-access-subscriptions)
      printf '%s\t%s\n' prepare_dashboard_access_subscriptions_e2e real-stack-dashboard-access-subscriptions.spec.ts
      ;;
    payment-confirm)
      printf '%s\t%s\n' prepare_payment_confirm_e2e real-stack-payment-confirm.spec.ts
      ;;
    bank-payment-link)
      printf '%s\t%s\n' prepare_bank_payment_link_e2e real-stack-bank-payment-link.spec.ts
      ;;
    training-group-containment)
      printf '%s\t%s\n' prepare_training_group_containment_e2e real-stack-training-group-containment.spec.ts
      ;;
    refund-resolution)
      printf '%s\t%s\n' prepare_refund_resolution_e2e real-stack-refund-resolution.spec.ts
      ;;
    pwa-ux-ledger-smoke)
      printf '%s\t%s\n' prepare_pwa_ux_ledger_smoke_e2e real-stack-pwa-ux-ledger-smoke.spec.ts
      ;;
    account-access-login)
      printf '%s\t%s\n' prepare_account_access_login_e2e real-stack-account-access-login.spec.ts
      ;;
    trainer-package-transfer)
      printf '%s\t%s\n' prepare_trainer_package_transfer_e2e real-stack-trainer-package-transfer.spec.ts
      ;;
    subscription-payout-policy)
      printf '%s\t%s\n' prepare_subscription_payout_policy_e2e real-stack-subscription-payout-policy.spec.ts
      ;;
    hybrid-package-entitlements)
      printf '%s\t%s\n' prepare_hybrid_package_entitlements_e2e real-stack-hybrid-package-entitlements.spec.ts
      ;;
    trainer-guest-personal-booking)
      printf '%s\t%s\n' prepare_trainer_guest_personal_booking_e2e real-stack-trainer-guest-personal-booking.spec.ts
      ;;
    trainer-personal-intent)
      printf '%s\t%s\n' prepare_trainer_personal_intent_e2e real-stack-trainer-personal-intent.spec.ts
      ;;
    trainer-personal-drop-in)
      printf '%s\t%s\n' prepare_trainer_personal_drop_in_e2e real-stack-trainer-personal-drop-in.spec.ts
      ;;
    trainer-payroll-close-correction)
      printf '%s\t%s\n' prepare_trainer_payroll_close_correction_e2e real-stack-trainer-payroll-close-correction.spec.ts
      ;;
    payment-reject)
      printf '%s\t%s\n' prepare_payment_reject_e2e real-stack-payment-reject.spec.ts
      ;;
    debt-writeoff)
      printf '%s\t%s\n' prepare_debt_writeoff_e2e real-stack-debt-writeoff.spec.ts
      ;;
    freeze-lifecycle)
      printf '%s\t%s\n' prepare_freeze_lifecycle_e2e real-stack-freeze-lifecycle.spec.ts
      ;;
    trainer-lead-trial)
      printf '%s\t%s\n' prepare_trainer_lead_trial_e2e real-stack-trainer-lead-trial.spec.ts
      ;;
    student-parent-reflection)
      printf '%s\t%s\n' prepare_student_parent_reflection_e2e real-stack-student-parent-reflection.spec.ts
      ;;
    tenant-negative-matrix)
      printf '%s\t%s\n' prepare_tenant_negative_matrix_e2e real-stack-tenant-negative-matrix.spec.ts
      ;;
    offline-kiosk-sync)
      printf '%s\t%s\n' prepare_offline_kiosk_sync_e2e real-stack-offline-kiosk-sync.spec.ts
      ;;
    schedule-exception-visibility)
      printf '%s\t%s\n' prepare_schedule_exception_visibility_e2e real-stack-schedule-exception-visibility.spec.ts
      ;;
    student-feedback)
      printf '%s\t%s\n' prepare_student_feedback_e2e real-stack-student-feedback.spec.ts
      ;;
    parent-invite-feedback)
      printf '%s\t%s\n' prepare_parent_invite_feedback_e2e real-stack-parent-invite-feedback.spec.ts
      ;;
    document-checklist-upload)
      printf '%s\t%s\n' prepare_document_checklist_upload_e2e real-stack-document-checklist-upload.spec.ts
      ;;
    push-notification-preferences)
      printf '%s\t%s\n' prepare_push_notification_preferences_e2e real-stack-push-notification-preferences.spec.ts
      ;;
    mass-notifications)
      printf '%s\t%s\n' prepare_mass_notifications_e2e real-stack-mass-notifications.spec.ts
      ;;
    checkin-cancel)
      printf '%s\t%s\n' prepare_checkin_cancel_e2e real-stack-checkin-cancel.spec.ts
      ;;
    retention-task-lifecycle)
      printf '%s\t%s\n' prepare_retention_task_lifecycle_e2e real-stack-retention-task-lifecycle.spec.ts
      ;;
    automatic-notification-lifecycle)
      printf '%s\t%s\n' prepare_automatic_notification_lifecycle_e2e real-stack-automatic-notification-lifecycle.spec.ts
      ;;
    owner-notification-template-settings)
      printf '%s\t%s\n' prepare_owner_notification_template_settings_e2e real-stack-owner-notification-template-settings.spec.ts
      ;;
    club-settings-business-config)
      printf '%s\t%s\n' prepare_club_settings_business_config_e2e real-stack-club-settings-business-config.spec.ts
      ;;
    owner-trainer-rate-grid)
      printf '%s\t%s\n' prepare_owner_trainer_rate_grid_e2e real-stack-owner-trainer-rate-grid.spec.ts
      ;;
    *)
      printf 'Unknown real-stack pack: %s\n' "$pack" >&2
      return 1
      ;;
  esac
}

run_pack() {
  local pack="$1"
  shift

  local mapping
  mapping="$(resolve_pack "$pack")"

  local fixture_command
  local test_match
  IFS=$'\t' read -r fixture_command test_match <<<"$mapping"

  printf '[real-stack-pack] real-stack pack: %s\n' "$pack"

  if [[ "$pack" == "public-lead-intake" ]]; then
    export CORS_ALLOWED_ORIGINS="https://jaguar-fight-club.ru,http://localhost:5173"
    export TELEGRAM_BOT_TOKEN=""
    export TELEGRAM_LEAD_CHAT_ID=""
    export TELEGRAM_LEAD_MESSAGE_THREAD_ID="0"
  fi

  local manual_operational_admission_enabled="false"
  local student_admin_corrections_enabled="false"
  local student_opening_import_enabled="false"
  if [[ "$pack" == "student-opening-import" ]]; then
    student_opening_import_enabled="true"
  fi
  local trainer_settlements_enabled="false"
  if [[ "$pack" == "trainer-settlements" || "$pack" == "student-opening-import" ]]; then
    trainer_settlements_enabled="true"
  fi
  if [[ "$pack" == "student-operations" || "$pack" == "tariff-price-revisions" || "$pack" == "manual-refunds" || "$pack" == "student-opening-import" ]]; then
    student_admin_corrections_enabled="true"
  fi
  if [[ "$pack" == "account-access-login" || "$pack" == "owner-dashboard-business" || "$pack" == "payment-confirm" || "$pack" == "payment-reject" || "$pack" == "bank-payment-link" || "$pack" == "training-group-containment" || "$pack" == "refund-resolution" || "$pack" == "schedule-exception-visibility" || "$pack" == "unified-client-commercial-journey" ]]; then
    manual_operational_admission_enabled="true"
  fi

  local training_group_new_writes_enabled="false"
  if [[ "$pack" == "student-opening-import" || "$pack" == "kiosk-negative" || "$pack" == "owner-dashboard-business" || "$pack" == "payment-confirm" || "$pack" == "payment-reject" || "$pack" == "bank-payment-link" || "$pack" == "training-group-containment" || "$pack" == "refund-resolution" || "$pack" == "schedule-exception-visibility" || "$pack" == "unified-client-commercial-journey" ]]; then
    training_group_new_writes_enabled="true"
  fi

  local mock_online_payment_enabled="false"
  if [[ "$pack" == "bank-payment-link" || "$pack" == "training-group-containment" || "$pack" == "refund-resolution" || "$pack" == "pwa-ux-ledger-smoke" || "$pack" == "trainer-personal-intent" || "$pack" == "student-parent-self-booking" || "$pack" == "unified-client-commercial-journey" ]]; then
    mock_online_payment_enabled="true"
  fi

  local unified_client_journey_enabled="false"
  if [[ "$pack" == "trainer-student-create-edit" || "$pack" == "payment-reject" || "$pack" == "unified-client-commercial-journey" || "$pack" == "trainer-personal-intent" || "$pack" == "student-parent-self-booking" ]]; then
    unified_client_journey_enabled="true"
  fi

  if [[ -n "${REAL_STACK_E2E_LOG_DIR:-}" ]]; then
    mkdir -p "$REAL_STACK_E2E_LOG_DIR/$pack"
    REAL_STACK_E2E_LOG_DIR="$REAL_STACK_E2E_LOG_DIR/$pack" \
      STUDENT_OPENING_IMPORT_ENABLED="$student_opening_import_enabled" \
      STUDENT_ADMIN_CORRECTIONS_ENABLED="$student_admin_corrections_enabled" \
      TRAINER_SETTLEMENTS_ENABLED="$trainer_settlements_enabled" \
      MANUAL_OPERATIONAL_ADMISSION_ENABLED="$manual_operational_admission_enabled" \
      TRAINING_GROUP_NEW_WRITES_ENABLED="$training_group_new_writes_enabled" \
      MOCK_PAYMENT_ORDER_CREATION_ENABLED="$mock_online_payment_enabled" \
      MOCK_PAYMENT_WEBHOOKS_ENABLED="$mock_online_payment_enabled" \
      UNIFIED_CLIENT_JOURNEY_ENABLED="$unified_client_journey_enabled" \
      REAL_STACK_E2E_FIXTURE_COMMAND="$fixture_command" \
      REAL_STACK_E2E_TEST_MATCH="$test_match" \
      bash "$RUNNER" "$@"
  else
    STUDENT_OPENING_IMPORT_ENABLED="$student_opening_import_enabled" \
      STUDENT_ADMIN_CORRECTIONS_ENABLED="$student_admin_corrections_enabled" \
    TRAINER_SETTLEMENTS_ENABLED="$trainer_settlements_enabled" \
    MANUAL_OPERATIONAL_ADMISSION_ENABLED="$manual_operational_admission_enabled" \
    TRAINING_GROUP_NEW_WRITES_ENABLED="$training_group_new_writes_enabled" \
    MOCK_PAYMENT_ORDER_CREATION_ENABLED="$mock_online_payment_enabled" \
    MOCK_PAYMENT_WEBHOOKS_ENABLED="$mock_online_payment_enabled" \
    UNIFIED_CLIENT_JOURNEY_ENABLED="$unified_client_journey_enabled" \
    REAL_STACK_E2E_FIXTURE_COMMAND="$fixture_command" \
      REAL_STACK_E2E_TEST_MATCH="$test_match" \
      bash "$RUNNER" "$@"
  fi
}

if (($# == 0)); then
  usage >&2
  exit 2
fi

case "$1" in
  --help|-h)
    usage
    exit 0
    ;;
  --list)
    list_packs
    exit 0
    ;;
  --describe)
    describe_packs
    exit 0
    ;;
esac

pack="$1"
shift

if [[ "$pack" == "all" ]]; then
  if [[ -z "${REAL_STACK_E2E_LOG_DIR:-}" ]]; then
    REAL_STACK_E2E_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jaguar-real-stack-e2e-packs.XXXXXX")"
    export REAL_STACK_E2E_LOG_DIR
  else
    mkdir -p "$REAL_STACK_E2E_LOG_DIR"
  fi

  printf '%s\n' "${PACKS[@]}" >"$REAL_STACK_E2E_LOG_DIR/all-real-stack-packs.tsv"
  for current_pack in "${PACKS[@]}"; do
    run_pack "$current_pack" "$@"
  done
else
  run_pack "$pack" "$@"
fi
