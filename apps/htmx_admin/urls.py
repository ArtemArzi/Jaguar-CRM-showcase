from django.contrib.auth.decorators import login_not_required
from django.shortcuts import render
from django.urls import path

from apps.common.permissions import management_view_required
from apps.htmx_admin import views
from apps.htmx_admin.views import student_imports, student_operations, student_refunds, trainer_settlements
from apps.htmx_admin.views.schedule import (
    training_group_archive,
    training_group_reconciliation,
)


@login_not_required
def _diag_view(request):
    return render(request, "_diag.html")


# Login/logout — no role check needed (pre-auth)
urlpatterns = [
    path("students/opening-import/", student_imports.import_home, name="student-opening-home"),
    path("students/opening-import/template/", student_imports.import_template, name="student-opening-template"),
    path("students/opening-import/single/", student_imports.import_single, name="student-opening-single"),
    path("students/opening-import/<int:batch_id>/", student_imports.import_batch, name="student-opening-batch"),
    path(
        "students/opening-import/<int:batch_id>/validate/",
        student_imports.import_validate,
        name="student-opening-validate",
    ),
    path("students/opening-import/<int:batch_id>/apply/", student_imports.import_apply, name="student-opening-apply"),
    path(
        "students/opening-import/<int:batch_id>/export/", student_imports.import_export, name="student-opening-export"
    ),
    path(
        "students/opening-import/<int:batch_id>/items/<int:item_id>/",
        student_imports.import_item,
        name="student-opening-item",
    ),
    path(
        "students/<int:student_id>/payments/<int:payment_id>/refund/",
        student_refunds.student_payment_refund,
        name="student-payment-refund",
    ),
    path(
        "students/<int:student_id>/refunds/<int:refund_id>/payroll/",
        student_refunds.student_refund_payroll,
        name="student-refund-payroll",
    ),
    path("diag/", _diag_view, name="diag"),
    path("login/", views.admin_login, name="admin-login"),
    path("logout/", views.admin_logout, name="admin-logout"),
    # Dashboard
    path("", management_view_required(views.dashboard_home), name="dashboard-home"),
    path("metrics/", management_view_required(views.dashboard_metrics_partial), name="dashboard-metrics"),
    # Students
    path("students/", management_view_required(views.student_list), name="student-list"),
    path("students/<int:student_id>/card/", management_view_required(views.student_card), name="student-card"),
    path(
        "students/<int:student_id>/subscriptions/<int:subscription_id>/correct/",
        student_operations.subscription_correction,
        name="student-subscription-correction",
    ),
    path(
        "students/<int:student_id>/subscriptions/<int:subscription_id>/renew/",
        student_operations.subscription_renew,
        name="student-subscription-renew",
    ),
    path(
        "students/<int:student_id>/attendance/record/",
        student_operations.attendance_record,
        name="student-attendance-record",
    ),
    path(
        "students/<int:student_id>/attendance/<int:checkin_id>/cancel/",
        student_operations.attendance_cancel,
        name="student-attendance-cancel",
    ),
    path("students/<int:student_id>/history/", student_operations.operation_history, name="student-operation-history"),
    path(
        "students/<int:student_id>/parent-invite/",
        management_view_required(views.student_parent_invite),
        name="student-parent-invite",
    ),
    path(
        "students/<int:student_id>/account-access/open/",
        management_view_required(views.student_account_access_open),
        name="student-account-access-open",
    ),
    path(
        "students/<int:student_id>/account-access/reset/",
        management_view_required(views.student_account_access_reset),
        name="student-account-access-reset",
    ),
    path(
        "students/<int:student_id>/status/",
        management_view_required(views.student_status_change),
        name="student-status-change",
    ),
    path("students/<int:student_id>/note/", management_view_required(views.student_add_note), name="student-add-note"),
    path("students/<int:student_id>/edit/", management_view_required(views.student_edit), name="student-edit"),
    path(
        "students/<int:student_id>/documents/mark/",
        management_view_required(views.student_document_mark),
        name="student-document-mark",
    ),
    path(
        "students/<int:student_id>/documents/upload/",
        management_view_required(views.student_document_upload),
        name="student-document-upload",
    ),
    path(
        "students/<int:student_id>/documents/<int:student_document_id>/open/",
        management_view_required(views.student_document_open),
        name="student-document-open",
    ),
    path("students/create/", management_view_required(views.student_create), name="student-create"),
    path("students/import/", management_view_required(views.import_upload), name="student-import"),
    path("students/import/confirm/", management_view_required(views.import_confirm), name="student-import-confirm"),
    # Schedule
    path("schedule/", management_view_required(views.schedule_view), name="schedule-view"),
    path(
        "training-groups/reconciliation/",
        management_view_required(training_group_reconciliation),
        name="training-group-reconciliation",
    ),
    path(
        "training-groups/<int:training_group_id>/archive/",
        management_view_required(training_group_archive),
        name="training-group-archive",
    ),
    path("schedule/create/", management_view_required(views.schedule_create), name="schedule-create"),
    path("schedule/<int:schedule_id>/edit/", management_view_required(views.schedule_edit), name="schedule-edit"),
    path("schedule/<int:schedule_id>/detail/", management_view_required(views.session_detail), name="session-detail"),
    path(
        "schedule/personal-drop-ins/<int:booking_id>/attendance/",
        management_view_required(views.personal_drop_in_attendance_correction),
        name="personal-drop-in-attendance-correction",
    ),
    path("schedule/<int:schedule_id>/enroll/", management_view_required(views.session_enroll), name="session-enroll"),
    path("schedule/<int:schedule_id>/cancel/", management_view_required(views.session_cancel), name="session-cancel"),
    path(
        "schedule/enrollments/<int:enrollment_id>/cancel/",
        management_view_required(views.schedule_enrollment_cancel_admin),
        name="schedule-enrollment-cancel",
    ),
    path(
        "schedule/enrollments/<int:enrollment_id>/freeze/",
        management_view_required(views.schedule_enrollment_freeze_admin),
        name="schedule-enrollment-freeze",
    ),
    path(
        "schedule/enrollments/<int:enrollment_id>/unfreeze/",
        management_view_required(views.schedule_enrollment_unfreeze_admin),
        name="schedule-enrollment-unfreeze",
    ),
    path(
        "schedule/enrollments/<int:enrollment_id>/transfer/",
        management_view_required(views.schedule_enrollment_transfer_admin),
        name="schedule-enrollment-transfer",
    ),
    path(
        "schedule/<int:schedule_id>/reschedule/",
        management_view_required(views.session_reschedule),
        name="session-reschedule",
    ),
    path(
        "schedule/<int:schedule_id>/substitute/",
        management_view_required(views.session_substitute),
        name="session-substitute",
    ),
    path(
        "schedule/<int:schedule_id>/revert/",
        management_view_required(views.exception_revert),
        name="exception-revert",
    ),
    path(
        "checkin/<int:checkin_id>/cancel/",
        management_view_required(views.checkin_cancel_admin),
        name="checkin-cancel",
    ),
    path("trainers/sessions/checkins/", management_view_required(views.session_checkins), name="session-checkins"),
    # Billing
    path("billing/", management_view_required(views.debtor_list), name="debtor-list"),
    path("billing/debtors/export/", management_view_required(views.debtor_export), name="debtor-export"),
    path(
        "billing/debts/<int:debt_id>/write-off/",
        management_view_required(views.debtor_write_off_action),
        name="debtor-write-off",
    ),
    path("billing/payments/", management_view_required(views.payment_list), name="payment-list"),
    path(
        "billing/payments/<int:payment_id>/verify/",
        management_view_required(views.verify_payment_action),
        name="verify-payment",
    ),
    path(
        "billing/bank-payment-orders/<int:order_id>/review/",
        management_view_required(views.bank_payment_order_review_action),
        name="bank-payment-order-review",
    ),
    path(
        "billing/bank-payment-orders/<int:order_id>/reconcile/",
        management_view_required(views.bank_payment_order_reconcile_action),
        name="bank-payment-order-reconcile",
    ),
    path(
        "billing/payment-refund-cases/<int:case_id>/approve/",
        management_view_required(views.payment_refund_case_approve_action),
        name="payment-refund-case-approve",
    ),
    path(
        "billing/payment-refunds/<int:refund_id>/complete-payroll/",
        management_view_required(views.payment_refund_payroll_action),
        name="payment-refund-payroll",
    ),
    # Subscriptions
    path("billing/subscriptions/", management_view_required(views.subscription_list), name="subscription-list"),
    path(
        "billing/subscriptions/create/",
        management_view_required(views.create_subscription_view),
        name="subscription-create",
    ),
    path(
        "billing/subscriptions/<int:subscription_id>/freeze/",
        management_view_required(views.freeze_subscription_view),
        name="subscription-freeze",
    ),
    path(
        "billing/freezes/<int:freeze_id>/approve/",
        management_view_required(views.approve_freeze_view),
        name="freeze-approve",
    ),
    path(
        "billing/freezes/<int:freeze_id>/reject/",
        management_view_required(views.reject_freeze_view),
        name="freeze-reject",
    ),
    path(
        "billing/subscriptions/<int:subscription_id>/unfreeze/",
        management_view_required(views.unfreeze_subscription_view),
        name="subscription-unfreeze",
    ),
    # Trainers
    path("trainers/", management_view_required(views.trainer_list), name="trainer-list"),
    path("trainers/sessions/", management_view_required(views.trainer_sessions), name="trainer-sessions"),
    path("trainers/create/", management_view_required(views.trainer_create), name="trainer-create"),
    path(
        "trainers/earnings/<int:earning_id>/correction/",
        management_view_required(views.trainer_earning_correction),
        name="trainer-earning-correction",
    ),
    path(
        "trainers/<int:trainer_id>/payroll-close/",
        management_view_required(views.trainer_payroll_close_period),
        name="trainer-payroll-close",
    ),
    path("trainers/<int:trainer_id>/", management_view_required(views.trainer_detail), name="trainer-detail"),
    path("trainers/<int:trainer_id>/settlements/", trainer_settlements.settlement_form, name="trainer-settlement-form"),
    path(
        "trainers/<int:trainer_id>/settlements/reconcile/<int:case_id>/",
        trainer_settlements.settlement_reconciliation,
        name="trainer-settlement-reconciliation",
    ),
    path("trainers/<int:trainer_id>/edit/", management_view_required(views.trainer_edit), name="trainer-edit"),
    path("trainers/<int:trainer_id>/rates/", management_view_required(views.trainer_rates), name="trainer-rates"),
    path("trainers/<int:trainer_id>/salary/", management_view_required(views.trainer_salary), name="trainer-salary"),
    # Reports
    path("reports/", management_view_required(views.pnl_report), name="pnl-report"),
    path("reports/export/", management_view_required(views.pnl_export_excel), name="pnl-export"),
    path("reports/expense/create/", management_view_required(views.expense_create), name="expense-create"),
    path(
        "reports/expense/<int:expense_id>/delete/",
        management_view_required(views.expense_delete),
        name="expense-delete",
    ),
    # Onboarding
    path("onboarding/", management_view_required(views.onboarding_wizard), name="onboarding-wizard"),
    path("onboarding/step/<int:step>/", management_view_required(views.onboarding_step), name="onboarding-step"),
    path("onboarding/skip/<int:step>/", management_view_required(views.onboarding_skip), name="onboarding-skip"),
    path("onboarding/finish/", management_view_required(views.onboarding_finish), name="onboarding-finish"),
    # Retention
    path("retention/", management_view_required(views.retention_tasks), name="retention-tasks"),
    path(
        "retention/<int:task_id>/comments/",
        management_view_required(views.retention_task_comments),
        name="retention-task-comments",
    ),
    # Notifications
    path("notifications/", management_view_required(views.push_notifications), name="push-notifications"),
    path("notifications/preview/", management_view_required(views.push_preview), name="push-preview"),
    # Settings (multi-tab)
    path("settings/", management_view_required(views.settings_redirect), name="club-settings"),
    path("settings/general/", management_view_required(views.settings_general_view), name="settings-general"),
    path("settings/billing/", management_view_required(views.settings_billing_view), name="settings-billing"),
    path("settings/catalog/", management_view_required(views.settings_catalog_view), name="settings-catalog"),
    path("settings/documents/", management_view_required(views.settings_documents_view), name="settings-documents"),
    path(
        "settings/notifications/",
        management_view_required(views.settings_notifications_view),
        name="settings-notifications",
    ),
    path("settings/kiosk/", management_view_required(views.settings_kiosk_view), name="settings-kiosk"),
    path("settings/kiosk/generate-pin/", management_view_required(views.kiosk_generate_pin), name="kiosk-generate-pin"),
    path("settings/kiosk/deactivate/", management_view_required(views.kiosk_deactivate), name="kiosk-deactivate"),
    # Notification templates
    path(
        "settings/notifications/templates/<int:template_id>/form/",
        views.notification_template_form,
        name="notification-template-form",
    ),
    path(
        "settings/notifications/templates/<int:template_id>/toggle/",
        views.notification_template_toggle,
        name="notification-template-toggle",
    ),
    # Training Type CRUD
    path(
        "settings/billing/training-types/form/",
        management_view_required(views.training_type_form),
        name="training-type-form",
    ),
    path(
        "settings/billing/training-types/<int:training_type_id>/form/",
        management_view_required(views.training_type_form),
        name="training-type-edit",
    ),
    path(
        "settings/billing/training-types/<int:training_type_id>/toggle/",
        management_view_required(views.training_type_toggle),
        name="training-type-toggle",
    ),
    # Catalog CRUD: Locations
    path("settings/catalog/locations/form/", views.location_form, name="location-form"),
    path("settings/catalog/locations/<int:location_id>/form/", views.location_form, name="location-edit"),
    path("settings/catalog/locations/<int:location_id>/delete/", views.location_delete, name="location-delete"),
    # Catalog CRUD: Grade Systems
    path("settings/catalog/grades/form/", views.grade_system_form, name="grade-system-form"),
    path(
        "settings/catalog/grades/<int:grade_system_id>/delete/",
        views.grade_system_delete,
        name="grade-system-delete",
    ),
    # Catalog CRUD: Grades
    path(
        "settings/catalog/grades/<int:grade_system_id>/grade/form/",
        views.grade_form,
        name="grade-form",
    ),
    path(
        "settings/catalog/grades/<int:grade_system_id>/grade/<int:grade_id>/form/",
        views.grade_form,
        name="grade-edit",
    ),
    path(
        "settings/catalog/grades/<int:grade_system_id>/grade/<int:grade_id>/delete/",
        views.grade_delete,
        name="grade-delete",
    ),
    # Documents CRUD
    path("settings/documents/types/form/", views.document_type_form, name="document-type-form"),
    path("settings/documents/types/<int:document_type_id>/form/", views.document_type_form, name="document-type-edit"),
    path(
        "settings/documents/types/<int:document_type_id>/toggle/",
        views.document_type_toggle,
        name="document-type-toggle",
    ),
    # Billing CRUD: Tariffs
    path("settings/billing/tariffs/form/", views.tariff_form, name="tariff-form"),
    path("settings/billing/tariffs/<int:tariff_id>/form/", views.tariff_form, name="tariff-edit"),
    path(
        "settings/billing/tariffs/<int:tariff_id>/price-revision/",
        views.tariff_price_revision_form,
        name="tariff-price-revision",
    ),
    path(
        "settings/billing/tariffs/archive/",
        views.tariff_archive_view,
        name="tariff-archive",
    ),
    path("settings/billing/tariffs/<int:tariff_id>/toggle/", views.tariff_toggle, name="tariff-toggle"),
    # Billing CRUD: Discounts
    path("settings/billing/discounts/form/", views.discount_form, name="discount-form"),
    path("settings/billing/discounts/<int:discount_id>/form/", views.discount_form, name="discount-edit"),
    path("settings/billing/discounts/<int:discount_id>/toggle/", views.discount_toggle, name="discount-toggle"),
]
