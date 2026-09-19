from __future__ import annotations

# Re-exported so that tests using `@patch("apps.attendance.services.async_task")`
# continue to work after the services.py → services/ package split.
from django_q.tasks import async_task

from .availability import (
    block_personal_availability_slot,
    cancel_personal_availability_slot,
    generate_personal_availability_slots,
    unblock_personal_availability_slot,
)
from .checkin import (
    _deduct_subscription,
    _resolve_trainer,
    batch_checkin,
    cancel_checkin,
    close_session_from_existing_checkins,
    create_checkin,
)
from .drop_in import (
    PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX,
    PERSONAL_DROP_IN_ATTENDANCE_REASON_MAX_LENGTH,
    PersonalDropInAttendanceCorrectionResult,
    PersonalDropInBookingResult,
    apply_personal_drop_in_checkin,
    book_personal_drop_in,
    book_personal_drop_in_from_frozen_reservation,
    cancel_personal_drop_in_booking,
    create_personal_drop_in_bank_payment_order,
    create_personal_drop_in_payment,
    get_active_personal_drop_in_booking,
    get_locked_personal_drop_in_booking,
    mark_personal_drop_in_no_show,
    reconcile_personal_drop_in_payment_after_verification,
    record_personal_drop_in_attendance_correction,
    validate_personal_drop_in_tariff_contract,
)
from .enrollment import (
    OPEN_ENROLLMENT_STATUSES,
    TERMINAL_ENROLLMENT_STATUSES,
    GuestGroupVisitBooking,
    PersonalSessionBooking,
    book_guest_group_visit,
    book_personal_availability_slot,
    book_personal_session,
    cancel_guest_booking,
    cancel_personal_booking,
    cancel_schedule_enrollment,
    close_personal_booking_payment_reservation_for_order,
    confirm_personal_booking_payment_reservation_for_order,
    create_personal_booking_payment_reservation,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    get_personal_booking_payment_reservations,
    transfer_schedule_enrollment,
    unfreeze_schedule_enrollment,
)
from .kiosk import activate_kiosk, deactivate_kiosk, generate_kiosk_pin
from .personal_payment_corrections import replace_personal_payment_method
from .personal_reschedule import (
    PersonalBookingRescheduleResult,
    reschedule_personal_exact_booking,
)
from .schedule import (
    _UPDATE_SCHEDULE_FIELDS,
    _create_schedule_exception,
    cancel_session,
    create_schedule,
    delete_exception,
    reschedule_session,
    substitute_trainer,
    update_schedule,
)
from .staff_intents import (
    StaffPersonalIntentResult,
    get_personal_commercial_context,
    get_staff_direct_personal_offer,
    has_accepted_staff_personal_command,
    submit_staff_direct_personal_intent,
    submit_staff_personal_intent,
)
from .training_group_memberships import (
    cancel_training_group_membership,
    create_training_group_membership,
    fan_out_training_group_membership_projections,
    freeze_training_group_membership,
    lock_training_group_mutation_scope,
    transfer_training_group_membership,
    unfreeze_training_group_membership,
)
from .training_groups import (
    archive_training_group,
    create_training_group,
    reassign_training_group_responsibility,
)

__all__ = [
    "async_task",
    # availability
    "generate_personal_availability_slots",
    "block_personal_availability_slot",
    "unblock_personal_availability_slot",
    "cancel_personal_availability_slot",
    # personal drop-in
    "PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX",
    "PERSONAL_DROP_IN_ATTENDANCE_REASON_MAX_LENGTH",
    "PersonalDropInBookingResult",
    "PersonalDropInAttendanceCorrectionResult",
    "book_personal_drop_in",
    "book_personal_drop_in_from_frozen_reservation",
    "get_active_personal_drop_in_booking",
    "get_locked_personal_drop_in_booking",
    "apply_personal_drop_in_checkin",
    "create_personal_drop_in_payment",
    "create_personal_drop_in_bank_payment_order",
    "reconcile_personal_drop_in_payment_after_verification",
    "cancel_personal_drop_in_booking",
    "mark_personal_drop_in_no_show",
    "record_personal_drop_in_attendance_correction",
    "validate_personal_drop_in_tariff_contract",
    # personal exact-booking reschedule
    "PersonalBookingRescheduleResult",
    "reschedule_personal_exact_booking",
    # schedule
    "create_schedule",
    "update_schedule",
    "_create_schedule_exception",
    "cancel_session",
    "reschedule_session",
    "substitute_trainer",
    "delete_exception",
    "_UPDATE_SCHEDULE_FIELDS",
    # training-group memberships
    "lock_training_group_mutation_scope",
    "create_training_group_membership",
    "cancel_training_group_membership",
    "freeze_training_group_membership",
    "unfreeze_training_group_membership",
    "transfer_training_group_membership",
    "fan_out_training_group_membership_projections",
    "create_training_group",
    "archive_training_group",
    "reassign_training_group_responsibility",
    # enrollment
    "OPEN_ENROLLMENT_STATUSES",
    "TERMINAL_ENROLLMENT_STATUSES",
    "GuestGroupVisitBooking",
    "PersonalSessionBooking",
    "book_guest_group_visit",
    "book_personal_session",
    "book_personal_availability_slot",
    "create_personal_booking_payment_reservation",
    "get_personal_booking_payment_reservations",
    "close_personal_booking_payment_reservation_for_order",
    "confirm_personal_booking_payment_reservation_for_order",
    "cancel_guest_booking",
    "cancel_personal_booking",
    "enroll_student_in_schedule",
    "cancel_schedule_enrollment",
    "transfer_schedule_enrollment",
    "freeze_schedule_enrollment",
    "unfreeze_schedule_enrollment",
    # checkin
    "create_checkin",
    "_resolve_trainer",
    "_deduct_subscription",
    "cancel_checkin",
    "close_session_from_existing_checkins",
    "batch_checkin",
    # unified staff personal intent
    "StaffPersonalIntentResult",
    "submit_staff_personal_intent",
    "submit_staff_direct_personal_intent",
    "get_staff_direct_personal_offer",
    "get_personal_commercial_context",
    "has_accepted_staff_personal_command",
    "replace_personal_payment_method",
    # kiosk
    "generate_kiosk_pin",
    "activate_kiosk",
    "deactivate_kiosk",
]
