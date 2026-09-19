import logging
from datetime import date as date_cls
from datetime import datetime as datetime_cls
from datetime import timedelta as timedelta_cls

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    TrainingGroup,
    TrainingGroupMembership,
)
from apps.attendance.personal_offers import direct_personal_offer_payload, personal_offer_payload
from apps.attendance.schemas import (
    BatchCheckinIn,
    BatchCheckinResultOut,
    BookingCancelIn,
    CancelSessionIn,
    CheckinResultOut,
    CloseSessionIn,
    GroupSessionOut,
    GuestBookingIn,
    GuestBookingOptionOut,
    GuestVisitCandidateOut,
    GuestVisitIn,
    GuestVisitOut,
    KioskActivateIn,
    KioskActivateOut,
    KioskBrandingOut,
    KioskCheckinIn,
    KioskOptionsIn,
    KioskOptionsOut,
    KioskRosterStudentOut,
    KioskScheduleOut,
    OfflineSyncIn,
    OfflineSyncResultOut,
    PersonalAvailabilityBookIn,
    PersonalAvailabilityCapabilityOut,
    PersonalAvailabilityDirectOfferPreviewOut,
    PersonalAvailabilityDirectStaffIntentIn,
    PersonalAvailabilityDirectStaffIntentV2In,
    PersonalAvailabilityDropInBookIn,
    PersonalAvailabilityOfferPreviewOut,
    PersonalAvailabilityOptionOut,
    PersonalAvailabilityPaymentReservationCancelIn,
    PersonalAvailabilityPaymentReservationCreateIn,
    PersonalAvailabilityStaffBookIn,
    PersonalAvailabilityStaffIntentIn,
    PersonalAvailabilityStaffIntentV2In,
    PersonalAvailabilityStaffPaymentReservationIn,
    PersonalBookingOut,
    PersonalBookingPaymentReservationOut,
    PersonalBookingRescheduleIn,
    PersonalCommercialReceiptOut,
    PersonalCommercialReceiptV2Out,
    PersonalDropInBankPaymentOrderIn,
    PersonalDropInBookingOut,
    PersonalDropInNoShowIn,
    PersonalDropInPaymentIn,
    PersonalDropInPaymentLinkOut,
    PersonalSelfServiceCommandCardOut,
    PersonalSelfServiceCommandCollectionsOut,
    PersonalSelfServiceCommandIn,
    PersonalSelfServiceOptionOut,
    PhoneLookupIn,
    RescheduleIn,
    ScheduleCheckinStatusOut,
    ScheduleEnrollmentEndIn,
    ScheduleEnrollmentIn,
    ScheduleEnrollmentOut,
    ScheduleEnrollmentTransferIn,
    ScheduleEnrollmentTransferOut,
    ScheduleExceptionOut,
    ScheduleIn,
    ScheduleOccurrenceOut,
    ScheduleOut,
    ScheduleUpdate,
    SessionDetailOut,
    StudentMatchOut,
    StudentWithAlertsOut,
    SubstituteIn,
    TodayCheckinOut,
    TrainerPersonalAvailabilityBlockIn,
    TrainerPersonalAvailabilityGenerateIn,
    TrainerPersonalAvailabilityGenerateOut,
    TrainerPersonalAvailabilitySlotOut,
    TrainingGroupArchiveIn,
    TrainingGroupCreateIn,
    TrainingGroupMembershipCancelIn,
    TrainingGroupMembershipCreateIn,
    TrainingGroupMembershipLifecycleIn,
    TrainingGroupMembershipOut,
    TrainingGroupMembershipTransferIn,
    TrainingGroupOut,
    TrainingGroupReassignIn,
    TrainingGroupReconciliationApplyIn,
    TrainingGroupReconciliationApplyOut,
    TrainingGroupReconciliationInventoryOut,
    TrainingGroupReconciliationPreviewIn,
    TrainingGroupReconciliationPreviewOut,
    TrainingGroupRolloutTransitionIn,
    TrainingGroupRolloutTransitionOut,
)
from apps.attendance.selectors import (
    compute_checkin_alerts,
    get_guest_visit_candidates,
    get_kiosk_checkin_options,
    get_kiosk_roster,
    get_schedule_by_id,
    get_schedule_exceptions,
    get_schedule_occurrences_for_date,
    get_schedules,
    get_self_service_guest_booking_options,
    get_self_service_personal_availability_options,
    get_session_detail,
    get_students_for_schedule,
    get_today_checkins,
    get_trainer_personal_availability_calendar,
    get_unified_self_service_personal_options,
    list_schedule_enrollments,
    lookup_by_phone_suffix,
    personal_availability_slot_payload,
)
from apps.attendance.services import (
    PersonalDropInBookingResult,
    activate_kiosk,
    archive_training_group,
    batch_checkin,
    block_personal_availability_slot,
    book_guest_group_visit,
    book_personal_availability_slot,
    book_personal_drop_in,
    cancel_checkin,
    cancel_guest_booking,
    cancel_personal_availability_slot,
    cancel_personal_booking,
    cancel_personal_drop_in_booking,
    cancel_schedule_enrollment,
    cancel_session,
    cancel_training_group_membership,
    close_session_from_existing_checkins,
    create_checkin,
    create_personal_booking_payment_reservation,
    create_personal_drop_in_bank_payment_order,
    create_personal_drop_in_payment,
    create_schedule,
    create_training_group,
    create_training_group_membership,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    freeze_training_group_membership,
    generate_personal_availability_slots,
    get_personal_booking_payment_reservations,
    get_staff_direct_personal_offer,
    has_accepted_staff_personal_command,
    mark_personal_drop_in_no_show,
    reassign_training_group_responsibility,
    reschedule_personal_exact_booking,
    reschedule_session,
    submit_staff_direct_personal_intent,
    submit_staff_personal_intent,
    substitute_trainer,
    transfer_schedule_enrollment,
    transfer_training_group_membership,
    unblock_personal_availability_slot,
    unfreeze_schedule_enrollment,
    unfreeze_training_group_membership,
    update_schedule,
)
from apps.attendance.services.self_service import (
    execute_self_service_personal_command,
    get_self_service_personal_command,
    get_self_service_personal_commands,
    self_service_personal_command_card,
    self_service_personal_command_collections,
)
from apps.attendance.services.training_group_reconciliation import (
    TrainingGroupPreviewError,
    apply_training_group_reconciliation,
    build_training_group_reconciliation_preview,
)
from apps.attendance.services.training_groups import transition_training_group_rollout_for_owner
from apps.attendance.training_group_selectors import get_training_group_reconciliation_inventory
from apps.billing.models import BankPaymentOrder, Payment, TrainingType
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.services import cancel_bank_payment_order, get_or_create_club_settings
from apps.clubs.capabilities import (
    assert_v2_manual_admission_command_allowed,
    get_commercial_journey_capability,
    get_v1_commercial_journey_command_availability,
    get_v2_provider_command_availability,
    is_unified_client_journey_enabled,
)
from apps.clubs.models import Location
from apps.clubs.timezones import club_localdate
from apps.common.auth import KioskDeviceAuth
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import role_required
from apps.common.schemas import schema_sent_fields
from apps.students.models import Student
from apps.students.parent_selectors import get_parent_child
from apps.students.selectors import get_student_by_user

logger = logging.getLogger(__name__)

MAX_PERSONAL_AVAILABILITY_RANGE_DAYS = 90

router = Router(tags=["schedules"])
guest_booking_router = Router(tags=["guest-bookings"])
personal_booking_router = Router(tags=["personal-bookings"])
personal_availability_router = Router(tags=["personal-availability"])
personal_drop_in_router = Router(tags=["personal-drop-in-bookings"])
_SCHEDULE_UPDATE_FIELDS = (
    "day_of_week",
    "start_time",
    "end_time",
    "group_name",
    "trainer_id",
    "location_id",
    "training_type_id",
    "is_active",
    "one_time_date",
)


def _assert_v1_personal_staff_command_allowed(*, club, idempotency_key: str) -> None:
    """Deny a new legacy route call before it can claim any durable resource."""

    capability = get_commercial_journey_capability(club=club)
    if capability.protocol_version != "v2" and not capability.unified_client_journey_enabled:
        return
    replay_or_drain = has_accepted_staff_personal_command(
        club_id=club.id,
        command_key=idempotency_key,
    )
    availability = get_v1_commercial_journey_command_availability(
        capability=capability,
        accepted_replay_or_drain=replay_or_drain,
    )
    if not availability.allows_new_command:
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code=availability.code,
        )


def _assert_v2_personal_staff_command_allowed(
    *, club, payment_method: str, idempotency_key: str
) -> None:
    """Use manual or provider capability before a v2 command creates artifacts."""

    if has_accepted_staff_personal_command(
        club_id=club.id,
        command_key=idempotency_key,
    ):
        return

    capability = get_commercial_journey_capability(club=club)
    if payment_method in {"cash", "transfer"}:
        assert_v2_manual_admission_command_allowed(capability=capability)
        return
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    availability = get_v2_provider_command_availability(
        capability=capability,
        provider_creation_enabled=get_online_payment_capability().enabled,
    )
    if not availability.allows_new_command:
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code=availability.code,
        )


def _personal_v2_command_receipt(*, club, student_id: int, receipt: dict, command_replayed: bool) -> dict:
    """Add current lifecycle truth to the immutable personal command receipt."""

    student = Student.objects.for_club(club).only("status", "lead_status").get(id=student_id)
    payment = None
    payment_id = receipt.get("payment_id")
    if payment_id is not None:
        payment = Payment.objects.for_club(club).only("id", "status").filter(id=payment_id).first()

    if payment is not None and payment.status == Payment.Status.CONFIRMED:
        finance_state = "confirmed"
    elif payment is not None and payment.status == Payment.Status.REJECTED:
        finance_state = "rejected"
    elif receipt.get("payment_method") in {Payment.Method.CASH, Payment.Method.TRANSFER}:
        finance_state = "pending_manual"
    else:
        finance_state = {
            BankPaymentOrder.Status.CANCELLED: "cancelled",
            BankPaymentOrder.Status.EXPIRED: "expired",
            BankPaymentOrder.Status.FAILED: "failed",
        }.get(receipt.get("status"), "provider_pending")

    return {
        **receipt,
        "workspace_state": (
            "lead"
            if student.status in {Student.Status.LEAD, Student.Status.TRIAL} or student.lead_status is not None
            else "student"
        ),
        "finance_state": finance_state,
        "command_replayed": command_replayed,
    }


def _assert_trainer_owns_schedule(request, schedule: Schedule) -> None:
    """Verify trainer owns schedule. Owner/admin skip this check."""
    from apps.trainers.models import Trainer
    from apps.trainers.selectors import get_trainer_for_user

    try:
        trainer = get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")
    if schedule.trainer_id != trainer.id:
        raise HttpError(403, "Access denied: not your schedule")


def _get_current_trainer_id(request) -> int:
    from apps.trainers.models import Trainer
    from apps.trainers.selectors import get_trainer_for_user

    try:
        trainer = get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")
    return trainer.id


def _assert_trainer_guest_lead_scope(request, *, lead_id: int | None) -> None:
    if request._membership.role != "trainer" or lead_id is None:
        return
    trainer_id = _get_current_trainer_id(request)
    if (
        Student.objects.for_club(request.club)
        .filter(
            id=lead_id,
            status=Student.Status.LEAD,
            assigned_trainer_id=trainer_id,
            deleted_at__isnull=True,
        )
        .exists()
    ):
        return
    raise HttpError(403, "Lead is not available for guest booking")


def _assert_trainer_can_access_schedule(
    request,
    *,
    schedule_id: int,
    target_date: date_cls | None = None,
) -> None:
    if request._membership.role != "trainer":
        return

    if target_date is None:
        schedule = Schedule.objects.for_club(request.club).get(id=schedule_id)
        _assert_trainer_owns_schedule(request, schedule)
        return

    trainer_id = _get_current_trainer_id(request)
    occurrences = get_schedule_occurrences_for_date(
        club=request.club,
        target_date=target_date,
        trainer_id=trainer_id,
    )
    if not any(occurrence.schedule_id == schedule_id for occurrence in occurrences):
        raise HttpError(403, "Access denied: not your schedule")


def _assert_schedule_occurs_on_date(
    request,
    *,
    schedule_id: int,
    target_date: date_cls,
) -> None:
    occurrences = get_schedule_occurrences_for_date(
        club=request.club,
        target_date=target_date,
    )
    if not any(occurrence.schedule_id == schedule_id for occurrence in occurrences):
        raise HttpError(404, "Schedule occurrence not found")


def _get_unclosed_occurrences(request, session_date: date_cls) -> list[ScheduleOccurrenceOut]:
    from apps.attendance.models import GroupSession

    trainer_id = _get_current_trainer_id(request) if request._membership.role == "trainer" else None
    occurrences = get_schedule_occurrences_for_date(
        club=request.club,
        target_date=session_date,
        trainer_id=trainer_id,
    )
    if not occurrences:
        return []

    schedule_ids = [occurrence.schedule_id for occurrence in occurrences]
    closed_ids = set(
        GroupSession.objects.for_club(request.club)
        .filter(
            date=session_date,
            schedule_id__in=schedule_ids,
            closed_at__isnull=False,
        )
        .values_list("schedule_id", flat=True)
    )
    open_schedule_ids = [schedule_id for schedule_id in schedule_ids if schedule_id not in closed_ids]
    if not open_schedule_ids:
        return []

    unclosed: list[ScheduleOccurrenceOut] = []
    for occurrence in occurrences:
        if occurrence.schedule_id in closed_ids:
            continue
        unclosed.append(occurrence)
    return unclosed


def _get_unclosed_occurrences_for_range(
    request,
    *,
    date_from: date_cls,
    date_to: date_cls,
) -> list[ScheduleOccurrenceOut]:
    if date_to < date_from:
        return []
    if (date_to - date_from).days > 13:
        raise HttpError(400, "Unclosed sessions range is limited to 14 days")

    club_today = club_localdate(request.club)
    bounded_to = min(date_to, club_today - timedelta_cls(days=1))
    if bounded_to < date_from:
        return []

    unclosed: list[ScheduleOccurrenceOut] = []
    current_date = date_from
    while current_date <= bounded_to:
        unclosed.extend(_get_unclosed_occurrences(request, current_date))
        current_date += timedelta_cls(days=1)
    return unclosed


def _guest_visit_payload(result) -> dict:
    enrollment = result.enrollment
    student = enrollment.student
    return {
        "enrollment_id": enrollment.id,
        "student_id": student.id,
        "display_name": f"{student.first_name} {student.last_name}".strip(),
        "schedule_id": enrollment.schedule_id,
        "created_from": enrollment.created_from,
        "is_guest_visit": result.is_guest_visit,
        "starts_on": enrollment.starts_on,
        "ends_on": enrollment.ends_on,
        "created": result.created,
        "already_member": result.already_member,
        "origin": result.origin,
        "financial_preview": result.financial_preview,
    }


def _personal_booking_payload(result, *, availability_slot_id: int | None = None) -> dict:
    schedule = result.schedule
    enrollment = result.enrollment
    starts_at = datetime_cls.combine(schedule.one_time_date, schedule.start_time)
    ends_at = datetime_cls.combine(schedule.one_time_date, schedule.end_time)
    trainer_name = f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip()
    return {
        "schedule_id": schedule.id,
        "enrollment_id": enrollment.id,
        "availability_slot_id": availability_slot_id or result.availability_slot_id,
        "student_id": enrollment.student_id,
        "trainer_id": schedule.trainer_id,
        "trainer_name": trainer_name,
        "location_id": schedule.location_id,
        "location_name": schedule.location.name,
        "training_type_id": schedule.training_type_id,
        "training_type_name": schedule.training_type.name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "created_from": enrollment.created_from,
        "created": result.created,
    }


def _personal_drop_in_booking_payload(result) -> dict:
    booking = result.booking
    schedule = result.schedule
    links = list(
        PersonalDropInPaymentLink.objects.for_club(booking.club_id)
        .select_related("payment", "bank_payment_order")
        .filter(booking_id=booking.id)
        .order_by("-created_at", "-id")
    )
    latest_link = links[0] if links else None
    if booking.state == PersonalDropInBooking.State.ATTENDED:
        if booking.debt_id and booking.debt.resolved_at is None:
            financial_state = (
                "payment_pending" if latest_link and latest_link.payment.status == "pending" else "debt_open"
            )
        else:
            financial_state = "paid" if latest_link and latest_link.payment.status == "confirmed" else "covered"
    elif booking.state in {
        PersonalDropInBooking.State.CANCELLED,
        PersonalDropInBooking.State.NO_SHOW,
    }:
        if latest_link and latest_link.payment.status == "confirmed":
            financial_state = "paid"
        elif latest_link and latest_link.payment.status == "pending":
            financial_state = "payment_pending"
        else:
            financial_state = "not_due"
    elif latest_link and latest_link.payment.status == "confirmed":
        financial_state = "covered"
    elif latest_link and latest_link.payment.status == "pending":
        financial_state = "payment_pending"
    else:
        financial_state = "pay_at_club"
    return {
        "booking_id": booking.id,
        "schedule_id": schedule.id,
        "enrollment_id": booking.enrollment_id,
        "availability_slot_id": result.availability_slot_id,
        "student_id": booking.enrollment.student_id,
        "trainer_id": schedule.trainer_id,
        "trainer_name": f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip(),
        "location_id": schedule.location_id,
        "location_name": schedule.location.name,
        "training_type_id": schedule.training_type_id,
        "training_type_name": schedule.training_type.name,
        "tariff_id": booking.tariff_id,
        "tariff_name_snapshot": booking.tariff_name_snapshot,
        "price_snapshot": str(booking.price_snapshot),
        "starts_at": datetime_cls.combine(schedule.one_time_date, schedule.start_time),
        "ends_at": datetime_cls.combine(schedule.one_time_date, schedule.end_time),
        "attendance_state": booking.state,
        "financial_state": financial_state,
        "debt_id": booking.debt_id,
        "created": result.created,
    }


def _personal_drop_in_booking_result_for_existing(booking: PersonalDropInBooking) -> PersonalDropInBookingResult:
    enrollment = booking.enrollment
    return PersonalDropInBookingResult(
        booking=booking,
        schedule=enrollment.schedule,
        enrollment=enrollment,
        availability_slot_id=None,
        created=False,
    )


def _personal_drop_in_payment_link_payload(
    link: PersonalDropInPaymentLink,
    *,
    allowed_source: str | None = None,
) -> dict:
    order = link.bank_payment_order
    if allowed_source is not None and order is not None and order.source != allowed_source:
        raise HttpError(404, "Personal drop-in payment link not found")
    return {
        "id": link.id,
        "booking_id": link.booking_id,
        "payment_id": link.payment_id,
        "bank_payment_order_id": link.bank_payment_order_id,
        "subscription_id": link.payment.subscription_id,
        "payment_status": link.payment.status,
        "order_status": order.status if order else "",
        "provider_payment_url": order.provider_payment_url if order else "",
    }


def _assert_drop_in_booking_actor_scope(request, *, booking: PersonalDropInBooking) -> None:
    if request._membership.role != "trainer":
        return
    if booking.enrollment.schedule.trainer_id != _get_current_trainer_id(request):
        raise HttpError(403, "Access denied: not your personal booking")


def _personal_booking_payment_reservation_payload(
    reservation: PersonalBookingPaymentReservation,
    *,
    allowed_source: str | None = None,
) -> PersonalBookingPaymentReservationOut:
    order = reservation.bank_payment_order
    trainer_name = f"{reservation.trainer.first_name} {reservation.trainer.last_name}".strip()
    order_source_allowed = allowed_source is None or order is None or order.source == allowed_source
    return PersonalBookingPaymentReservationOut(
        id=reservation.id,
        student_id=reservation.student_id,
        trainer_id=reservation.trainer_id,
        trainer_name=trainer_name,
        location_id=reservation.location_id,
        location_name=reservation.location.name,
        training_type_id=reservation.training_type_id,
        training_type_name=reservation.training_type.name,
        tariff_id=reservation.tariff_id,
        tariff_name=reservation.tariff.name,
        availability_slot_id=reservation.availability_slot_id,
        starts_at=reservation.starts_at,
        ends_at=reservation.ends_at,
        status=reservation.status,
        expires_at=reservation.expires_at,
        payment_id=reservation.payment_id,
        bank_payment_order_id=reservation.bank_payment_order_id,
        subscription_id=reservation.subscription_id,
        schedule_id=reservation.schedule_id,
        enrollment_id=reservation.enrollment_id,
        provider_payment_url=order.provider_payment_url if order else "",
        amount_snapshot=str(order.amount_snapshot) if order else "",
        order_status=order.status if order else "",
        can_cancel=(
            reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
            and order is not None
            and order_source_allowed
            and order.status in {"created", "pending", "authorized"}
            and reservation.expires_at > timezone.now()
        ),
        created_at=reservation.created_at,
    )


def _get_self_booking_student(request, *, child_student_id: int | None) -> Student:
    role = request._membership.role
    if role == "student":
        if child_student_id is not None:
            raise BusinessLogicError(
                "child_student_id is only available for parent bookings",
                code="child_student_id_not_allowed",
            )
        return get_student_by_user(club=request.club, user_id=request.user.id)

    if role == "parent":
        if child_student_id is None:
            raise BusinessLogicError(
                "child_student_id is required for parent bookings",
                code="child_student_id_required",
            )
        return get_parent_child(
            user_id=request.user.id,
            club=request.club,
            student_id=child_student_id,
        )

    raise HttpError(403, "Access denied")


def _self_service_bank_payment_source(request) -> str:
    if request._membership.role == "parent":
        return BankPaymentOrder.Source.PARENT
    return BankPaymentOrder.Source.STUDENT


def _legacy_personal_booking_replay_exists(*, club, student_id: int, slot_id: int, idempotency_key: str | None) -> bool:
    """Allow only an exact legacy drain after the capability-on cutover."""

    key = (idempotency_key or "").strip()
    if not key:
        return False
    return ScheduleBookingEvent.objects.for_club(club).filter(
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        student_id=student_id,
        metadata__idempotency_key=key,
        metadata__availability_slot_id=slot_id,
        enrollment__status__in=(
            ScheduleEnrollment.Status.ACTIVE,
            ScheduleEnrollment.Status.TRIAL,
            ScheduleEnrollment.Status.FROZEN,
        ),
    ).exists()


def _legacy_personal_payment_replay_exists(*, club, student_id: int, slot_id: int, idempotency_key: str | None) -> bool:
    key = (idempotency_key or "").strip()
    if not key:
        return False
    return PersonalBookingPaymentReservation.objects.for_club(club).filter(
        idempotency_key=key,
        student_id=student_id,
        availability_slot_id=slot_id,
    ).exists()


def _get_booking_enrollment_for_cancel_scope(request, *, enrollment_id: int) -> ScheduleEnrollment:
    return ScheduleEnrollment.objects.for_club(request.club).select_related("student", "schedule").get(id=enrollment_id)


def _booking_cancel_origin(request) -> str:
    if request._membership.role == "student":
        return ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    if request._membership.role == "parent":
        return ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
    return ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION


def _assert_booking_cancel_actor_scope(
    request,
    *,
    enrollment: ScheduleEnrollment,
    reason: str,
) -> None:
    role = request._membership.role
    if role in {"owner", "admin"}:
        return
    if role == "trainer":
        trainer_id = _get_current_trainer_id(request)
        if enrollment.schedule.trainer_id != trainer_id:
            raise HttpError(403, "Access denied: not your booking")
        if not reason.strip():
            raise HttpError(400, "reason is required")
        return
    if role == "student":
        if enrollment.student.user_id == request.user.id:
            return
        raise HttpError(403, "Access denied: not your booking")
    if role == "parent":
        try:
            get_parent_child(
                user_id=request.user.id,
                club=request.club,
                student_id=enrollment.student_id,
            )
        except Student.DoesNotExist as exc:
            raise HttpError(403, "Access denied: not your child booking") from exc
        return
    raise HttpError(403, "Access denied")


@guest_booking_router.post("/{enrollment_id}/cancel/", response=ScheduleEnrollmentOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def cancel_guest_booking_endpoint(request, enrollment_id: int, payload: BookingCancelIn):
    enrollment = _get_booking_enrollment_for_cancel_scope(request, enrollment_id=enrollment_id)
    _assert_booking_cancel_actor_scope(
        request,
        enrollment=enrollment,
        reason=payload.reason,
    )
    result = cancel_guest_booking(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        actor_user_id=request.user.id,
        origin=_booking_cancel_origin(request),
        reason=payload.reason,
    )
    return result.enrollment


@personal_booking_router.post("/{enrollment_id}/cancel/", response=ScheduleEnrollmentOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def cancel_personal_booking_endpoint(request, enrollment_id: int, payload: BookingCancelIn):
    enrollment = _get_booking_enrollment_for_cancel_scope(request, enrollment_id=enrollment_id)
    _assert_booking_cancel_actor_scope(
        request,
        enrollment=enrollment,
        reason=payload.reason,
    )
    result = cancel_personal_booking(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        actor_user_id=request.user.id,
        origin=_booking_cancel_origin(request),
        reason=payload.reason,
    )
    return result.enrollment


@personal_booking_router.post("/{enrollment_id}/reschedule/", response=PersonalBookingOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def reschedule_personal_booking_endpoint(
    request,
    enrollment_id: int,
    payload: PersonalBookingRescheduleIn,
):
    """Move one existing entitlement or confirmed-bank exact personal booking."""

    enrollments = (
        ScheduleEnrollment.objects.for_club(request.club)
        .select_related(
            "schedule",
            "schedule__trainer",
            "schedule__location",
            "schedule__training_type",
        )
        .filter(id=enrollment_id, created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING)
    )
    if request._membership.role == "trainer":
        enrollments = enrollments.filter(schedule__trainer_id=_get_current_trainer_id(request))
    enrollment = enrollments.first()
    if enrollment is None:
        raise HttpError(404, "Not found")
    if PersonalDropInBooking.objects.for_club(request.club).filter(enrollment_id=enrollment.id).exists():
        raise HttpError(404, "Not found")
    result = reschedule_personal_exact_booking(
        club_id=request.club.id,
        enrollment_id=enrollment.id,
        destination_slot_id=payload.destination_slot_id,
        actor_user_id=request.user.id,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
    )
    return _personal_booking_payload(result, availability_slot_id=result.destination_slot_id)


@router.get("/", response=list[ScheduleOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_schedules(request):
    return get_schedules(club=request.club)


@router.get("/training-group-reconciliation/", response=TrainingGroupReconciliationInventoryOut)
@role_required("owner", "admin")
def training_group_reconciliation_inventory(request):
    return {"schedules": get_training_group_reconciliation_inventory(club=request.club)}


@router.post("/training-group-reconciliation/preview/", response=TrainingGroupReconciliationPreviewOut)
@role_required("owner", "admin")
def training_group_reconciliation_preview(request, payload: TrainingGroupReconciliationPreviewIn):
    try:
        return build_training_group_reconciliation_preview(
            club=request.club,
            schedule_ids=payload.schedule_ids,
            canonical_name=payload.canonical_name,
            responsible_trainer_id=payload.responsible_trainer_id,
            start_dates=[
                {"student_id": item.student_id, "starts_on": item.starts_on}
                for item in payload.start_dates
            ],
        )
    except TrainingGroupPreviewError as exc:
        raise HttpError(400, f"{exc.code}: {exc.message}")


@router.post("/training-group-reconciliation/apply/", response=TrainingGroupReconciliationApplyOut)
@role_required("owner", "admin")
def training_group_reconciliation_apply(request, payload: TrainingGroupReconciliationApplyIn):
    try:
        return apply_training_group_reconciliation(
            club=request.club,
            schedule_ids=payload.schedule_ids,
            canonical_name=payload.canonical_name,
            responsible_trainer_id=payload.responsible_trainer_id,
            start_dates=[
                {"student_id": item.student_id, "starts_on": item.starts_on}
                for item in payload.start_dates
            ],
            preview_digest=payload.preview_digest,
            actor_user_id=request.user.id,
            rationale=payload.rationale,
            idempotency_key=payload.idempotency_key,
        )
    except TrainingGroupPreviewError as exc:
        raise HttpError(400, f"{exc.code}: {exc.message}")


@router.post("/training-group-rollout/transition/", response=TrainingGroupRolloutTransitionOut)
@role_required("owner", "admin")
def training_group_rollout_transition(request, payload: TrainingGroupRolloutTransitionIn):
    try:
        state = transition_training_group_rollout_for_owner(
            club_id=request.club.id,
            target_mode=payload.target_mode,
            actor_id=request.user.id,
            rationale=payload.rationale,
            idempotency_key=payload.idempotency_key,
            rollout_gate_digest=payload.rollout_gate_digest,
        )
        return {"mode": state.mode, "reconciling_from_mode": state.reconciling_from_mode}
    except BusinessLogicError as exc:
        raise HttpError(400, f"{exc.code}: {exc.message}")


@router.get("/training-groups/", response=list[TrainingGroupOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_training_groups(request):
    return (
        TrainingGroup.objects.for_club(request.club)
        .select_related("responsible_trainer")
        .order_by("name", "id")
    )


@router.post("/training-groups/", response={201: TrainingGroupOut})
@role_required("owner", "admin")
def create_training_group_endpoint(request, payload: TrainingGroupCreateIn):
    group = create_training_group(
        club_id=request.club.id,
        name=payload.name,
        training_type_id=payload.training_type_id,
        location_id=payload.location_id,
        responsible_trainer_id=payload.responsible_trainer_id,
        actor_user_id=request.user.id,
    )
    return 201, group


@router.post("/training-groups/{training_group_id}/reassign/", response=TrainingGroupOut)
@role_required("owner", "admin")
def reassign_training_group_endpoint(request, training_group_id: int, payload: TrainingGroupReassignIn):
    return reassign_training_group_responsibility(
        club_id=request.club.id,
        training_group_id=training_group_id,
        responsible_trainer_id=payload.responsible_trainer_id,
    )


@router.post("/training-groups/{training_group_id}/archive/", response=TrainingGroupOut)
@role_required("owner", "admin")
def archive_training_group_endpoint(request, training_group_id: int, payload: TrainingGroupArchiveIn):
    try:
        return archive_training_group(
            club_id=request.club.id,
            training_group_id=training_group_id,
            actor_user_id=request.user.id,
            rationale=payload.rationale,
            idempotency_key=payload.idempotency_key,
        )
    except BusinessLogicError as exc:
        raise HttpError(400, f"{exc.code}: {exc.message}")


@router.get("/training-groups/memberships/", response=list[TrainingGroupMembershipOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_training_group_memberships(
    request,
    student_id: int | None = None,
    training_group_id: int | None = None,
):
    memberships = TrainingGroupMembership.objects.for_club(request.club).select_related(
        "student", "training_group"
    )
    if student_id is not None:
        memberships = memberships.filter(student_id=student_id)
    if training_group_id is not None:
        memberships = memberships.filter(training_group_id=training_group_id)
    return memberships.order_by("-starts_on", "-id")


@router.post("/training-groups/memberships/", response={201: TrainingGroupMembershipOut})
@role_required("owner", "admin")
def create_training_group_membership_endpoint(request, payload: TrainingGroupMembershipCreateIn):
    membership = create_training_group_membership(
        club_id=request.club.id,
        student_id=payload.student_id,
        training_group_id=payload.training_group_id,
        starts_on=payload.starts_on,
        source=payload.source,
        actor_user_id=request.user.id,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
    )
    return 201, membership


@router.post("/training-groups/memberships/{membership_id}/freeze/", response=TrainingGroupMembershipOut)
@role_required("owner", "admin")
def freeze_training_group_membership_endpoint(
    request, membership_id: int, payload: TrainingGroupMembershipLifecycleIn
):
    return freeze_training_group_membership(
        membership_id=membership_id,
        club_id=request.club.id,
        actor_user_id=request.user.id,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/training-groups/memberships/{membership_id}/unfreeze/", response=TrainingGroupMembershipOut)
@role_required("owner", "admin")
def unfreeze_training_group_membership_endpoint(
    request, membership_id: int, payload: TrainingGroupMembershipLifecycleIn
):
    return unfreeze_training_group_membership(
        membership_id=membership_id,
        club_id=request.club.id,
        actor_user_id=request.user.id,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/training-groups/memberships/{membership_id}/cancel/", response=TrainingGroupMembershipOut)
@role_required("owner", "admin")
def cancel_training_group_membership_endpoint(
    request, membership_id: int, payload: TrainingGroupMembershipCancelIn
):
    return cancel_training_group_membership(
        membership_id=membership_id,
        club_id=request.club.id,
        ends_on=payload.ends_on,
        actor_user_id=request.user.id,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/training-groups/memberships/{membership_id}/transfer/", response=list[TrainingGroupMembershipOut])
@role_required("owner", "admin")
def transfer_training_group_membership_endpoint(
    request, membership_id: int, payload: TrainingGroupMembershipTransferIn
):
    source, target = transfer_training_group_membership(
        membership_id=membership_id,
        club_id=request.club.id,
        target_training_group_id=payload.target_training_group_id,
        ends_on=payload.ends_on,
        actor_user_id=request.user.id,
        rationale=payload.rationale,
        idempotency_key=payload.idempotency_key,
    )
    return [source, target]


@router.post("/", response={201: ScheduleOut})
@role_required("owner", "admin", "trainer")
def create_schedule_endpoint(request, payload: ScheduleIn):
    trainer_id = payload.trainer_id
    if request._membership.role == "trainer":
        if not payload.one_time_date:
            raise HttpError(403, "Trainers can only create one-time schedules")
        # Force trainer_id to current user's trainer record
        from apps.trainers.models import Trainer
        from apps.trainers.selectors import get_trainer_for_user

        try:
            trainer = get_trainer_for_user(club=request.club, user=request.user)
        except Trainer.DoesNotExist:
            raise HttpError(403, "Trainer profile not found")
        trainer_id = trainer.id
    schedule = create_schedule(
        club_id=request.club.id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        group_name=payload.group_name,
        trainer_id=trainer_id,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        one_time_date=payload.one_time_date,
        training_group_id=payload.training_group_id,
        actor_user_id=request.user.id,
    )
    return 201, schedule


@router.get("/today/", response=list[ScheduleOccurrenceOut])
@role_required("owner", "admin", "trainer")
def today_sessions(request):
    trainer_id = _get_current_trainer_id(request) if request._membership.role == "trainer" else None
    return get_schedule_occurrences_for_date(
        club=request.club,
        target_date=club_localdate(request.club),
        trainer_id=trainer_id,
    )


@router.get("/by-date/", response=list[ScheduleOccurrenceOut])
@role_required("owner", "admin", "trainer")
def sessions_by_date(request, date: date_cls):
    trainer_id = _get_current_trainer_id(request) if request._membership.role == "trainer" else None
    return get_schedule_occurrences_for_date(
        club=request.club,
        target_date=date,
        trainer_id=trainer_id,
    )


@router.get("/unclosed/", response=list[ScheduleOccurrenceOut])
@role_required("owner", "admin", "trainer")
def unclosed_sessions(
    request,
    date: date_cls | None = None,
    date_from: date_cls | None = None,
    date_to: date_cls | None = None,
):
    if date_from is not None or date_to is not None:
        if date_from is None or date_to is None:
            raise HttpError(400, "date_from and date_to are required together")
        return _get_unclosed_occurrences_for_range(
            request,
            date_from=date_from,
            date_to=date_to,
        )
    if date is None:
        raise HttpError(400, "date is required")
    return _get_unclosed_occurrences(request, date)


@router.post("/{schedule_id}/guest-visits/", response={200: GuestVisitOut, 201: GuestVisitOut})
@role_required("owner", "admin", "trainer")
def book_guest_visit_endpoint(request, schedule_id: int, payload: GuestVisitIn):
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=schedule_id,
        target_date=payload.date,
    )
    _assert_trainer_guest_lead_scope(request, lead_id=payload.lead_id)
    result = book_guest_group_visit(
        club_id=request.club.id,
        schedule_id=schedule_id,
        target_date=payload.date,
        student_id=payload.student_id,
        lead_id=payload.lead_id,
        origin=payload.origin,
        actor_user_id=request.user.id,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _guest_visit_payload(result)


@router.post("/{schedule_id}/guest-bookings/", response={200: GuestVisitOut, 201: GuestVisitOut})
@role_required("student", "parent")
def book_self_service_guest_booking_endpoint(request, schedule_id: int, payload: GuestBookingIn):
    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    origin = (
        ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
        if request._membership.role == "parent"
        else ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    )
    result = book_guest_group_visit(
        club_id=request.club.id,
        schedule_id=schedule_id,
        target_date=payload.date,
        student_id=student.id,
        origin=origin,
        actor_user_id=request.user.id,
        idempotency_key=payload.idempotency_key,
        created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        allow_lead_conversion=False,
        require_financial_eligibility=True,
    )
    return (201 if result.created else 200), _guest_visit_payload(result)


@router.get("/guest-booking-options/", response=list[GuestBookingOptionOut])
@role_required("student", "parent")
def list_self_service_guest_booking_options_endpoint(
    request,
    date: date_cls,
    child_student_id: int | None = None,
):
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    return get_self_service_guest_booking_options(
        club=request.club,
        student_id=student.id,
        target_date=date,
    )


@personal_availability_router.get("/options/", response=list[PersonalAvailabilityOptionOut])
@role_required("student", "parent")
def list_self_service_personal_availability_options_endpoint(
    request,
    date: date_cls,
    child_student_id: int | None = None,
):
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    return get_self_service_personal_availability_options(
        club=request.club,
        student_id=student.id,
        target_date=date,
    )


@personal_availability_router.get("/self-service/options/", response=list[PersonalSelfServiceOptionOut])
@role_required("student", "parent")
def list_unified_self_service_personal_options_endpoint(
    request,
    date: date_cls,
    child_student_id: int | None = None,
):
    """Capability-on personal-only options; legacy options remain untouched."""

    student = _get_self_booking_student(request, child_student_id=child_student_id)
    if not is_unified_client_journey_enabled(club=request.club):
        raise BusinessLogicError(
            "Unified client journey is disabled for this club.",
            code="unified_client_journey_disabled",
        )
    return get_unified_self_service_personal_options(
        club=request.club,
        student_id=student.id,
        target_date=date,
    )


@personal_availability_router.get("/capability/", response=PersonalAvailabilityCapabilityOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def personal_availability_capability_endpoint(request):
    """Expose the effective personal-offer rollout without widening intake access."""

    capability = get_commercial_journey_capability(club=request.club)
    return {
        "enabled": capability.unified_client_journey_enabled,
        "staff_command_protocol_version": capability.protocol_version,
    }


@personal_availability_router.get(
    "/self-service/commands/",
    response=PersonalSelfServiceCommandCollectionsOut,
)
@role_required("student", "parent")
def list_unified_self_service_personal_commands_endpoint(
    request,
    child_student_id: int | None = None,
):
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    return self_service_personal_command_collections(
        club=request.club,
        commands=get_self_service_personal_commands(
            club=request.club,
            actor_user_id=request.user.id,
            student_id=student.id,
            source=_self_service_bank_payment_source(request),
        ),
    )


@personal_availability_router.get(
    "/self-service/commands/{command_id}/",
    response=PersonalSelfServiceCommandCardOut,
)
@role_required("student", "parent", conceal_denial=True)
def get_unified_self_service_personal_command_endpoint(
    request,
    command_id: int,
    child_student_id: int | None = None,
):
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    command = get_self_service_personal_command(
        club=request.club,
        actor_user_id=request.user.id,
        student_id=student.id,
        source=_self_service_bank_payment_source(request),
        command_id=command_id,
    )
    if command is None:
        raise HttpError(404, "Not found")
    return self_service_personal_command_card(club=request.club, command=command)


@personal_availability_router.get(
    "/payment-reservations/",
    response=list[PersonalBookingPaymentReservationOut],
)
@role_required("student", "parent")
def list_self_service_personal_payment_reservations_endpoint(
    request,
    child_student_id: int | None = None,
    status: str | None = None,
):
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    source = _self_service_bank_payment_source(request)
    return [
        _personal_booking_payment_reservation_payload(reservation, allowed_source=source)
        for reservation in get_personal_booking_payment_reservations(
            club_id=request.club.id,
            student_id=student.id,
            status=status,
            allowed_sources={source},
        )
    ]


@personal_availability_router.get(
    "/payment-reservations/{reservation_id}/",
    response=PersonalBookingPaymentReservationOut,
)
@role_required("student", "parent", conceal_denial=True)
def get_self_service_personal_payment_reservation_endpoint(
    request,
    reservation_id: int,
    child_student_id: int | None = None,
):
    """Read one reservation, including terminal states, without list inference."""
    student = _get_self_booking_student(request, child_student_id=child_student_id)
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .select_related(
            "student",
            "trainer",
            "location",
            "training_type",
            "tariff",
            "payment",
            "bank_payment_order",
            "subscription",
            "schedule",
            "enrollment",
        )
        .filter(
            id=reservation_id,
            student_id=student.id,
            bank_payment_order__source=_self_service_bank_payment_source(request),
        )
        .first()
    )
    if reservation is None:
        raise HttpError(404, "Not found")
    return _personal_booking_payment_reservation_payload(
        reservation,
        allowed_source=_self_service_bank_payment_source(request),
    )


def _resolve_personal_availability_trainer_id(request, trainer_id: int | None = None) -> int:
    if request._membership.role == "trainer":
        current_trainer_id = _get_current_trainer_id(request)
        if trainer_id is not None and trainer_id != current_trainer_id:
            raise HttpError(403, "Access denied: not your availability")
        return current_trainer_id
    if trainer_id is None:
        raise HttpError(400, "trainer_id is required")
    return trainer_id


def _assert_personal_availability_range(date_from: date_cls, date_to: date_cls) -> None:
    if date_to < date_from:
        raise HttpError(400, "date_from must be <= date_to")
    if date_to - date_from >= timedelta_cls(days=MAX_PERSONAL_AVAILABILITY_RANGE_DAYS):
        raise HttpError(
            400,
            f"date range must be shorter than {MAX_PERSONAL_AVAILABILITY_RANGE_DAYS} days",
        )


@personal_availability_router.get("/slots/", response=list[TrainerPersonalAvailabilitySlotOut])
@role_required("owner", "admin", "trainer")
def list_trainer_personal_availability_endpoint(
    request,
    date_from: date_cls,
    date_to: date_cls,
    trainer_id: int | None = None,
):
    _assert_personal_availability_range(date_from, date_to)
    resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    return get_trainer_personal_availability_calendar(
        club=request.club,
        trainer_id=resolved_trainer_id,
        date_from=date_from,
        date_to=date_to,
    )


@personal_availability_router.get("/offers/", response=PersonalAvailabilityOfferPreviewOut)
@role_required("owner", "admin", "trainer")
def get_personal_availability_offer_preview_endpoint(
    request,
    training_type_id: int,
    location_id: int,
    trainer_id: int | None = None,
    discount_id: int | None = None,
    slot_id: int | None = None,
):
    if not Location.objects.filter(id=location_id, club=request.club).exists():
        raise BusinessLogicError("Location does not belong to this club", code="location_club_mismatch")
    slot = None
    if slot_id is not None:
        slot = (
            PersonalAvailabilitySlot.objects.for_club(request.club)
            .select_related("trainer", "location", "training_type")
            .filter(id=slot_id)
            .first()
        )
        if slot is None:
            raise HttpError(404, "Personal availability slot not found")
        if slot.location_id != location_id or slot.training_type_id != training_type_id:
            raise BusinessLogicError("Slot does not match offer context", code="personal_offer_slot_mismatch")
        resolved_trainer_id = _resolve_personal_availability_trainer_id(request, slot.trainer_id)
    elif trainer_id is not None:
        resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    else:
        resolved_trainer_id = None
    offer = resolve_personal_booking_offer(
        club_id=request.club.id,
        trainer_id=resolved_trainer_id,
        training_type_id=training_type_id,
        location_id=location_id,
        discount_id=discount_id,
    )
    if slot is not None:
        return personal_offer_payload(slot=slot, offer=offer)
    return direct_personal_offer_payload(
        offer=offer,
    )


@personal_availability_router.get("/direct-offer/", response=PersonalAvailabilityDirectOfferPreviewOut)
@role_required("owner", "admin", "trainer")
def get_personal_availability_direct_offer_endpoint(
    request,
    trainer_id: int,
    starts_at: datetime_cls,
    ends_at: datetime_cls,
    location_id: int,
    training_type_id: int,
    discount_id: int | None = None,
):
    resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    return get_staff_direct_personal_offer(
        club_id=request.club.id,
        trainer_id=resolved_trainer_id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location_id,
        training_type_id=training_type_id,
        discount_id=discount_id,
    )


@personal_availability_router.post("/slots/generate/", response=TrainerPersonalAvailabilityGenerateOut)
@role_required("owner", "admin", "trainer")
def generate_trainer_personal_availability_endpoint(
    request,
    payload: TrainerPersonalAvailabilityGenerateIn,
):
    trainer_id = _resolve_personal_availability_trainer_id(request, payload.trainer_id)
    result = generate_personal_availability_slots(
        club_id=request.club.id,
        trainer_id=trainer_id,
        date_from=payload.date_from,
        date_to=payload.date_to,
        weekdays=payload.weekdays,
        start_time=payload.start_time,
        end_time=payload.end_time,
        slot_duration_minutes=payload.slot_duration_minutes,
        buffer_minutes=payload.buffer_minutes,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
    )
    return {
        "created": [personal_availability_slot_payload(slot) for slot in result.created],
        "skipped": [
            {
                "date": skipped.date,
                "starts_at": skipped.starts_at,
                "ends_at": skipped.ends_at,
                "reason_code": skipped.reason_code,
            }
            for skipped in result.skipped
        ],
    }


@personal_availability_router.post("/slots/{slot_id}/block/", response=TrainerPersonalAvailabilitySlotOut)
@role_required("owner", "admin", "trainer")
def block_trainer_personal_availability_endpoint(
    request,
    slot_id: int,
    payload: TrainerPersonalAvailabilityBlockIn,
    trainer_id: int | None = None,
):
    resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    slot = block_personal_availability_slot(
        club_id=request.club.id,
        trainer_id=resolved_trainer_id,
        slot_id=slot_id,
        reason=payload.reason,
    )
    return personal_availability_slot_payload(slot)


@personal_availability_router.post("/slots/{slot_id}/unblock/", response=TrainerPersonalAvailabilitySlotOut)
@role_required("owner", "admin", "trainer")
def unblock_trainer_personal_availability_endpoint(
    request,
    slot_id: int,
    trainer_id: int | None = None,
):
    resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    slot = unblock_personal_availability_slot(
        club_id=request.club.id,
        trainer_id=resolved_trainer_id,
        slot_id=slot_id,
    )
    return personal_availability_slot_payload(slot)


@personal_availability_router.post("/slots/{slot_id}/cancel/", response=TrainerPersonalAvailabilitySlotOut)
@role_required("owner", "admin", "trainer")
def cancel_trainer_personal_availability_endpoint(
    request,
    slot_id: int,
    trainer_id: int | None = None,
):
    resolved_trainer_id = _resolve_personal_availability_trainer_id(request, trainer_id)
    slot = cancel_personal_availability_slot(
        club_id=request.club.id,
        trainer_id=resolved_trainer_id,
        slot_id=slot_id,
    )
    return personal_availability_slot_payload(slot)


@personal_availability_router.post(
    "/payment-reservations/{reservation_id}/cancel/",
    response=PersonalBookingPaymentReservationOut,
)
@role_required("student", "parent")
def cancel_self_service_personal_payment_reservation_endpoint(
    request,
    reservation_id: int,
    payload: PersonalAvailabilityPaymentReservationCancelIn,
):
    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .select_related("bank_payment_order")
        .filter(id=reservation_id, student_id=student.id)
        .first()
    )
    if reservation is None:
        raise HttpError(404, "Reservation not found")
    if reservation.bank_payment_order_id is None:
        raise HttpError(400, "Reservation has no payment order")
    cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=reservation.bank_payment_order_id,
        actor_user_id=request.user.id,
        allowed_student_id=student.id,
        allowed_sources={_self_service_bank_payment_source(request)},
        reason="cancelled_by_self_service_personal_booking",
    )
    return _personal_booking_payment_reservation_payload(
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .select_related(
            "student",
            "trainer",
            "location",
            "training_type",
            "tariff",
            "payment",
            "bank_payment_order",
            "subscription",
            "schedule",
            "enrollment",
        )
        .get(id=reservation.id),
        allowed_source=_self_service_bank_payment_source(request),
    )


@personal_availability_router.post(
    "/self-service/commands/{command_id}/cancel/",
    response=PersonalSelfServiceCommandCardOut,
)
@role_required("student", "parent", conceal_denial=True)
def cancel_unified_self_service_personal_command_endpoint(
    request,
    command_id: int,
    payload: PersonalAvailabilityPaymentReservationCancelIn,
):
    """Cancel only the source- and child-scoped live SBP order for a command."""

    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    source = _self_service_bank_payment_source(request)
    command = get_self_service_personal_command(
        club=request.club,
        actor_user_id=request.user.id,
        student_id=student.id,
        source=source,
        command_id=command_id,
    )
    if command is None or command.reservation_id_snapshot is None:
        raise HttpError(404, "Not found")
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .select_related("bank_payment_order")
        .filter(id=command.reservation_id_snapshot, student_id=student.id)
        .first()
    )
    if reservation is None or reservation.bank_payment_order_id is None:
        raise HttpError(404, "Not found")
    cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=reservation.bank_payment_order_id,
        actor_user_id=request.user.id,
        allowed_student_id=student.id,
        allowed_sources={source},
        reason="cancelled_by_self_service_personal_command",
    )
    command = get_self_service_personal_command(
        club=request.club,
        actor_user_id=request.user.id,
        student_id=student.id,
        source=source,
        command_id=command_id,
    )
    assert command is not None
    return self_service_personal_command_card(club=request.club, command=command)


@personal_availability_router.post(
    "/{slot_id}/payment-reservations/",
    response={200: PersonalBookingPaymentReservationOut, 201: PersonalBookingPaymentReservationOut},
)
@role_required("student", "parent")
def create_self_service_personal_payment_reservation_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityPaymentReservationCreateIn,
):
    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    if is_unified_client_journey_enabled(club=request.club) and not _legacy_personal_payment_replay_exists(
        club=request.club,
        student_id=student.id,
        slot_id=slot_id,
        idempotency_key=payload.idempotency_key,
    ):
        raise BusinessLogicError(
            "Use the unified personal self-service command.",
            code="unified_personal_command_required",
        )
    slot = (
        PersonalAvailabilitySlot.objects.for_club(request.club)
        .select_related("trainer", "location", "training_type")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    reservation = create_personal_booking_payment_reservation(
        club_id=request.club.id,
        student_id=student.id,
        trainer_id=slot.trainer_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        location_id=slot.location_id,
        training_type_id=slot.training_type_id,
        tariff_id=payload.tariff_id,
        availability_slot_id=slot.id,
        offer_digest=payload.offer_digest,
        created_by_id=request.user.id,
        source=_self_service_bank_payment_source(request),
        idempotency_key=payload.idempotency_key,
        command_idempotency_key=payload.idempotency_key,
    )
    return 201, _personal_booking_payment_reservation_payload(
        reservation,
        allowed_source=_self_service_bank_payment_source(request),
    )


@personal_availability_router.post("/{slot_id}/book/", response={200: PersonalBookingOut, 201: PersonalBookingOut})
@role_required("student", "parent")
def book_self_service_personal_availability_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityBookIn,
):
    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    if is_unified_client_journey_enabled(club=request.club) and not _legacy_personal_booking_replay_exists(
        club=request.club,
        student_id=student.id,
        slot_id=slot_id,
        idempotency_key=payload.idempotency_key,
    ):
        raise BusinessLogicError(
            "Use the unified personal self-service command.",
            code="unified_personal_command_required",
        )
    origin = (
        ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
        if request._membership.role == "parent"
        else ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    )
    result = book_personal_availability_slot(
        club_id=request.club.id,
        slot_id=slot_id,
        student_id=student.id,
        actor_user_id=request.user.id,
        origin=origin,
        subscription_id=payload.subscription_id,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _personal_booking_payload(result, availability_slot_id=slot_id)


@personal_availability_router.post(
    "/self-service/slots/{slot_id}/command/",
    response={200: PersonalSelfServiceCommandCardOut, 201: PersonalSelfServiceCommandCardOut},
)
@role_required("student", "parent")
def execute_unified_self_service_personal_command_endpoint(
    request,
    slot_id: int,
    payload: PersonalSelfServiceCommandIn,
):
    """Execute the sole server-selected self-service action for an option."""

    student = _get_self_booking_student(request, child_student_id=payload.child_student_id)
    result = execute_self_service_personal_command(
        club=request.club,
        actor_user_id=request.user.id,
        source=_self_service_bank_payment_source(request),
        student_id=student.id,
        slot_id=slot_id,
        idempotency_key=payload.idempotency_key,
        offer_digest=payload.offer_digest,
    )
    return (
        201 if result.created else 200,
        self_service_personal_command_card(club=request.club, command=result.command),
    )


@personal_availability_router.post(
    "/slots/{slot_id}/staff-intents/",
    response={200: PersonalCommercialReceiptOut, 201: PersonalCommercialReceiptOut},
)
@role_required("owner", "admin", "trainer")
def submit_staff_personal_intent_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityStaffIntentIn,
):
    _assert_v1_personal_staff_command_allowed(
        club=request.club,
        idempotency_key=payload.idempotency_key,
    )
    slot = (
        PersonalAvailabilitySlot.objects.for_club(request.club)
        .select_related("trainer")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    if request._membership.role == "trainer":
        if slot.trainer_id != _get_current_trainer_id(request):
            raise HttpError(403, "Access denied: not your availability")
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = submit_staff_personal_intent(
        club_id=request.club.id,
        slot_id=slot_id,
        student_id=payload.student_id,
        payment_method=payload.payment_method,
        subscription_id=payload.subscription_id,
        offer_digest=payload.offer_digest,
        discount_id=payload.discount_id,
        idempotency_key=payload.idempotency_key,
        actor_user_id=request.user.id,
        bank_source={
            "trainer": BankPaymentOrder.Source.TRAINER,
            "admin": BankPaymentOrder.Source.ADMIN,
            "owner": BankPaymentOrder.Source.OWNER,
        }[request._membership.role],
        command_protocol_version="v1",
    )
    return (201 if result.created else 200), result.receipt


@personal_availability_router.post(
    "/v2/slots/{slot_id}/staff-intents/",
    response={200: PersonalCommercialReceiptV2Out, 201: PersonalCommercialReceiptV2Out},
)
@role_required("owner", "admin", "trainer")
def submit_staff_personal_intent_v2_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityStaffIntentV2In,
):
    _assert_v2_personal_staff_command_allowed(
        club=request.club,
        payment_method=payload.payment_method,
        idempotency_key=payload.idempotency_key,
    )
    slot = (
        PersonalAvailabilitySlot.objects.for_club(request.club)
        .select_related("trainer")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    if request._membership.role == "trainer":
        if slot.trainer_id != _get_current_trainer_id(request):
            raise HttpError(403, "Access denied: not your availability")
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = submit_staff_personal_intent(
        club_id=request.club.id,
        slot_id=slot_id,
        student_id=payload.student_id,
        payment_method=payload.payment_method,
        subscription_id=None,
        offer_digest=payload.offer_digest,
        discount_id=payload.discount_id,
        idempotency_key=payload.idempotency_key,
        actor_user_id=request.user.id,
        v2_manual_admission_command=payload.payment_method in {"cash", "transfer"},
        command_protocol_version="v2",
        bank_source={
            "trainer": BankPaymentOrder.Source.TRAINER,
            "admin": BankPaymentOrder.Source.ADMIN,
            "owner": BankPaymentOrder.Source.OWNER,
        }[request._membership.role],
    )
    return (201 if result.created else 200), _personal_v2_command_receipt(
        club=request.club,
        student_id=payload.student_id,
        receipt=result.receipt,
        command_replayed=not result.created,
    )


@personal_availability_router.post(
    "/staff-intents/direct/",
    response={200: PersonalCommercialReceiptOut, 201: PersonalCommercialReceiptOut},
)
@role_required("owner", "admin", "trainer")
def submit_staff_direct_personal_intent_endpoint(
    request,
    payload: PersonalAvailabilityDirectStaffIntentIn,
):
    _assert_v1_personal_staff_command_allowed(
        club=request.club,
        idempotency_key=payload.idempotency_key,
    )
    trainer_id = _resolve_personal_availability_trainer_id(request, payload.trainer_id)
    if request._membership.role == "trainer":
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = submit_staff_direct_personal_intent(
        club_id=request.club.id,
        student_id=payload.student_id,
        trainer_id=trainer_id,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        payment_method=payload.payment_method,
        subscription_id=payload.subscription_id,
        offer_digest=payload.offer_digest,
        discount_id=payload.discount_id,
        idempotency_key=payload.idempotency_key,
        actor_user_id=request.user.id,
        bank_source={
            "trainer": BankPaymentOrder.Source.TRAINER,
            "admin": BankPaymentOrder.Source.ADMIN,
            "owner": BankPaymentOrder.Source.OWNER,
        }[request._membership.role],
        command_protocol_version="v1",
    )
    return (201 if result.created else 200), result.receipt


@personal_availability_router.post(
    "/v2/staff-intents/direct/",
    response={200: PersonalCommercialReceiptV2Out, 201: PersonalCommercialReceiptV2Out},
)
@role_required("owner", "admin", "trainer")
def submit_staff_direct_personal_intent_v2_endpoint(
    request,
    payload: PersonalAvailabilityDirectStaffIntentV2In,
):
    _assert_v2_personal_staff_command_allowed(
        club=request.club,
        payment_method=payload.payment_method,
        idempotency_key=payload.idempotency_key,
    )
    trainer_id = _resolve_personal_availability_trainer_id(request, payload.trainer_id)
    if request._membership.role == "trainer":
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = submit_staff_direct_personal_intent(
        club_id=request.club.id,
        student_id=payload.student_id,
        trainer_id=trainer_id,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        payment_method=payload.payment_method,
        subscription_id=None,
        offer_digest=payload.offer_digest,
        discount_id=payload.discount_id,
        idempotency_key=payload.idempotency_key,
        actor_user_id=request.user.id,
        v2_manual_admission_command=payload.payment_method in {"cash", "transfer"},
        command_protocol_version="v2",
        bank_source={
            "trainer": BankPaymentOrder.Source.TRAINER,
            "admin": BankPaymentOrder.Source.ADMIN,
            "owner": BankPaymentOrder.Source.OWNER,
        }[request._membership.role],
    )
    return (201 if result.created else 200), _personal_v2_command_receipt(
        club=request.club,
        student_id=payload.student_id,
        receipt=result.receipt,
        command_replayed=not result.created,
    )


@personal_availability_router.post(
    "/slots/{slot_id}/book-client/",
    response={200: PersonalBookingOut, 201: PersonalBookingOut},
)
@role_required("owner", "admin", "trainer")
def book_client_personal_availability_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityStaffBookIn,
):
    slot = PersonalAvailabilitySlot.objects.for_club(request.club).select_related("trainer").filter(id=slot_id).first()
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    if request._membership.role == "trainer":
        if slot.trainer_id != _get_current_trainer_id(request):
            raise HttpError(403, "Access denied: not your availability")
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = book_personal_availability_slot(
        club_id=request.club.id,
        slot_id=slot_id,
        student_id=payload.student_id,
        actor_user_id=request.user.id,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        subscription_id=payload.subscription_id,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _personal_booking_payload(result, availability_slot_id=slot_id)


@personal_availability_router.post(
    "/slots/{slot_id}/staff-payment-reservations/",
    response={200: PersonalBookingPaymentReservationOut, 201: PersonalBookingPaymentReservationOut},
)
@role_required("owner", "admin", "trainer")
def create_staff_personal_payment_reservation_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityStaffPaymentReservationIn,
):
    slot = (
        PersonalAvailabilitySlot.objects.for_club(request.club)
        .select_related("trainer", "location", "training_type")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    if request._membership.role == "trainer":
        if slot.trainer_id != _get_current_trainer_id(request):
            raise HttpError(403, "Access denied: not your availability")
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    reservation = create_personal_booking_payment_reservation(
        club_id=request.club.id,
        student_id=payload.student_id,
        trainer_id=slot.trainer_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        location_id=slot.location_id,
        training_type_id=slot.training_type_id,
        tariff_id=payload.tariff_id,
        availability_slot_id=slot.id,
        offer_digest=payload.offer_digest,
        created_by_id=request.user.id,
        source={
            "trainer": BankPaymentOrder.Source.TRAINER,
            "admin": BankPaymentOrder.Source.ADMIN,
            "owner": BankPaymentOrder.Source.OWNER,
        }[request._membership.role],
        idempotency_key=payload.idempotency_key,
        command_idempotency_key=payload.idempotency_key,
    )
    return 201, _personal_booking_payment_reservation_payload(reservation)


@personal_availability_router.get(
    "/staff-payment-reservations/{reservation_id}/",
    response=PersonalBookingPaymentReservationOut,
)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def get_staff_personal_payment_reservation_endpoint(request, reservation_id: int):
    reservations = PersonalBookingPaymentReservation.objects.for_club(request.club).select_related(
        "student", "trainer", "location", "training_type", "tariff", "payment", "bank_payment_order",
        "subscription", "schedule", "enrollment",
    )
    if request._membership.role == "trainer":
        reservations = reservations.filter(trainer_id=_get_current_trainer_id(request))
    reservation = reservations.filter(id=reservation_id).first()
    if reservation is None:
        raise HttpError(404, "Not found")
    return _personal_booking_payment_reservation_payload(reservation)


@personal_availability_router.post(
    "/slots/{slot_id}/drop-in-bookings/",
    response={200: PersonalDropInBookingOut, 201: PersonalDropInBookingOut},
)
@role_required("owner", "admin", "trainer")
def create_personal_availability_drop_in_booking_endpoint(
    request,
    slot_id: int,
    payload: PersonalAvailabilityDropInBookIn,
):
    slot = (
        PersonalAvailabilitySlot.objects.for_club(request.club)
        .select_related("trainer", "location", "training_type")
        .filter(id=slot_id)
        .first()
    )
    if slot is None:
        raise HttpError(404, "Personal availability slot not found")
    if request._membership.role == "trainer":
        trainer_id = _get_current_trainer_id(request)
        if slot.trainer_id != trainer_id:
            raise HttpError(403, "Access denied: not your availability")
        from apps.students.scopes import actor_is_scoped_to_student

        if not actor_is_scoped_to_student(
            club=request.club,
            membership_role=request._membership.role,
            user=request.user,
            student_id=payload.student_id,
        ):
            raise HttpError(403, "Access denied: not your student")
    result = book_personal_drop_in(
        club_id=request.club.id,
        student_id=payload.student_id,
        trainer_id=slot.trainer_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        location_id=slot.location_id,
        training_type_id=slot.training_type_id,
        tariff_id=payload.tariff_id,
        actor_user_id=request.user.id,
        availability_slot_id=slot.id,
        offer_digest=payload.offer_digest,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _personal_drop_in_booking_payload(result)


@personal_drop_in_router.post("/{booking_id}/payments/", response={201: PersonalDropInPaymentLinkOut})
@role_required("owner", "admin", "trainer")
def create_personal_drop_in_payment_endpoint(request, booking_id: int, payload: PersonalDropInPaymentIn):
    if request._membership.role == "trainer" and len(payload.discount_ids) > 1:
        raise BusinessLogicError(
            "Тренер может применить только одну скидку к ручной оплате",
            code="trainer_multiple_discounts_not_allowed",
        )
    booking = (
        PersonalDropInBooking.objects.for_club(request.club).select_related("enrollment__schedule").get(id=booking_id)
    )
    _assert_drop_in_booking_actor_scope(request, booking=booking)
    link = create_personal_drop_in_payment(
        club_id=request.club.id,
        booking_id=booking.id,
        payment_method=payload.payment_method,
        created_by_id=request.user.id,
        discount_ids=payload.discount_ids,
        idempotency_key=payload.idempotency_key,
        debt_id=payload.debt_id,
    )
    link = (
        PersonalDropInPaymentLink.objects.for_club(request.club)
        .select_related("payment__subscription", "bank_payment_order")
        .get(id=link.id)
    )
    return 201, _personal_drop_in_payment_link_payload(
        link,
        allowed_source=(
            BankPaymentOrder.Source.TRAINER
            if request._membership.role == "trainer"
            else None
        ),
    )


@personal_drop_in_router.post("/{booking_id}/bank-payment-orders/", response={201: PersonalDropInPaymentLinkOut})
@role_required("owner", "admin", "trainer")
def create_personal_drop_in_bank_payment_order_endpoint(
    request,
    booking_id: int,
    payload: PersonalDropInBankPaymentOrderIn,
):
    booking = (
        PersonalDropInBooking.objects.for_club(request.club).select_related("enrollment__schedule").get(id=booking_id)
    )
    _assert_drop_in_booking_actor_scope(request, booking=booking)
    source = {
        "trainer": BankPaymentOrder.Source.TRAINER,
        "admin": BankPaymentOrder.Source.ADMIN,
        "owner": BankPaymentOrder.Source.OWNER,
    }[request._membership.role]
    link = create_personal_drop_in_bank_payment_order(
        club_id=request.club.id,
        booking_id=booking.id,
        source=source,
        created_by_id=request.user.id,
        idempotency_key=payload.idempotency_key,
        buyer_email=payload.buyer_email,
        buyer_phone=payload.buyer_phone,
        debt_id=payload.debt_id,
    )
    link = (
        PersonalDropInPaymentLink.objects.for_club(request.club)
        .select_related("payment__subscription", "bank_payment_order")
        .get(id=link.id)
    )
    return 201, _personal_drop_in_payment_link_payload(
        link,
        allowed_source=(
            BankPaymentOrder.Source.TRAINER
            if request._membership.role == "trainer"
            else None
        ),
    )


@personal_drop_in_router.get("/{booking_id}/payment-link/", response=PersonalDropInPaymentLinkOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def get_personal_drop_in_payment_link_endpoint(request, booking_id: int):
    """Resolve a staff-owned drop-in booking to its linked payment order only."""
    bookings = PersonalDropInBooking.objects.for_club(request.club).select_related("enrollment__schedule")
    if request._membership.role == "trainer":
        bookings = bookings.filter(enrollment__schedule__trainer_id=_get_current_trainer_id(request))
    booking = bookings.filter(id=booking_id).first()
    if booking is None:
        raise HttpError(404, "Not found")
    link = (
        PersonalDropInPaymentLink.objects.for_club(request.club)
        .select_related("payment__subscription", "bank_payment_order")
        .filter(booking_id=booking.id)
        .first()
    )
    if link is None:
        raise HttpError(404, "Not found")
    return _personal_drop_in_payment_link_payload(
        link,
        allowed_source=(
            BankPaymentOrder.Source.TRAINER
            if request._membership.role == "trainer"
            else None
        ),
    )


@personal_drop_in_router.get("/{booking_id}/", response=PersonalDropInBookingOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def get_personal_drop_in_booking_endpoint(request, booking_id: int):
    """Read the exact persistent booking referenced by a staff receipt."""
    bookings = PersonalDropInBooking.objects.for_club(request.club).select_related(
        "enrollment__schedule",
        "enrollment__schedule__trainer",
        "enrollment__schedule__location",
        "enrollment__schedule__training_type",
    )
    if request._membership.role == "trainer":
        bookings = bookings.filter(enrollment__schedule__trainer_id=_get_current_trainer_id(request))
    booking = bookings.filter(id=booking_id).first()
    if booking is None:
        raise HttpError(404, "Not found")
    return _personal_drop_in_booking_payload(_personal_drop_in_booking_result_for_existing(booking))


@personal_drop_in_router.post("/{booking_id}/cancel/", response=PersonalDropInBookingOut)
@role_required("owner", "admin", "trainer")
def cancel_personal_drop_in_booking_endpoint(request, booking_id: int, payload: BookingCancelIn):
    booking = (
        PersonalDropInBooking.objects.for_club(request.club)
        .select_related(
            "enrollment__schedule",
            "enrollment__schedule__trainer",
            "enrollment__schedule__location",
            "enrollment__schedule__training_type",
        )
        .get(id=booking_id)
    )
    _assert_drop_in_booking_actor_scope(request, booking=booking)
    cancelled = cancel_personal_drop_in_booking(
        club_id=request.club.id,
        booking_id=booking.id,
        actor_user_id=request.user.id,
        reason=payload.reason,
    )
    return _personal_drop_in_booking_payload(_personal_drop_in_booking_result_for_existing(cancelled))


@personal_drop_in_router.post("/{booking_id}/reschedule/", response=PersonalDropInBookingOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def reschedule_personal_drop_in_booking_endpoint(
    request,
    booking_id: int,
    payload: PersonalBookingRescheduleIn,
):
    """Move one complete exact drop-in booking; never invoke generic schedule reschedule."""

    bookings = PersonalDropInBooking.objects.for_club(request.club).select_related("enrollment__schedule")
    if request._membership.role == "trainer":
        bookings = bookings.filter(enrollment__schedule__trainer_id=_get_current_trainer_id(request))
    booking = bookings.filter(id=booking_id).first()
    if booking is None:
        raise HttpError(404, "Not found")
    result = reschedule_personal_exact_booking(
        club_id=request.club.id,
        enrollment_id=booking.enrollment_id,
        destination_slot_id=payload.destination_slot_id,
        actor_user_id=request.user.id,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
    )
    if result.booking is None:
        raise HttpError(404, "Not found")
    return _personal_drop_in_booking_payload(
        PersonalDropInBookingResult(
            booking=result.booking,
            schedule=result.schedule,
            enrollment=result.enrollment,
            created=False,
            availability_slot_id=result.destination_slot_id,
        )
    )


@personal_drop_in_router.post("/{booking_id}/no-show/", response=PersonalDropInBookingOut)
@role_required("owner", "admin", "trainer")
def mark_personal_drop_in_no_show_endpoint(request, booking_id: int, payload: PersonalDropInNoShowIn):
    booking = (
        PersonalDropInBooking.objects.for_club(request.club)
        .select_related(
            "enrollment__schedule",
            "enrollment__schedule__trainer",
            "enrollment__schedule__location",
            "enrollment__schedule__training_type",
        )
        .get(id=booking_id)
    )
    _assert_drop_in_booking_actor_scope(request, booking=booking)
    no_show = mark_personal_drop_in_no_show(
        club_id=request.club.id,
        booking_id=booking.id,
        actor_user_id=request.user.id,
        reason=payload.reason,
    )
    return _personal_drop_in_booking_payload(_personal_drop_in_booking_result_for_existing(no_show))


@router.get("/{schedule_id}/guest-visit-candidates/", response=list[GuestVisitCandidateOut])
@role_required("owner", "admin", "trainer")
def list_guest_visit_candidates(request, schedule_id: int, date: date_cls, q: str = ""):
    trainer_id = _get_current_trainer_id(request) if request._membership.role == "trainer" else None
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=schedule_id,
        target_date=date,
    )
    _assert_schedule_occurs_on_date(
        request,
        schedule_id=schedule_id,
        target_date=date,
    )
    return get_guest_visit_candidates(
        club=request.club,
        schedule_id=schedule_id,
        target_date=date,
        query=q,
        trainer_id=trainer_id,
    )


@router.get("/enrollments/", response=list[ScheduleEnrollmentOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_enrollments(
    request,
    student_id: int | None = None,
    schedule_id: int | None = None,
    status: str | None = None,
):
    if status is not None and status not in ScheduleEnrollment.Status.values:
        raise BusinessLogicError(
            "Invalid enrollment status",
            code="invalid_enrollment_status",
        )
    return list_schedule_enrollments(
        club=request.club,
        student_id=student_id,
        schedule_id=schedule_id,
        status=status,
    )


@router.post("/enrollments/", response={201: ScheduleEnrollmentOut})
@role_required("owner", "admin")
def create_enrollment_endpoint(request, payload: ScheduleEnrollmentIn):
    enrollment = enroll_student_in_schedule(
        club_id=request.club.id,
        student_id=payload.student_id,
        schedule_id=payload.schedule_id,
        status=payload.status,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        actor_user_id=request.user.id,
    )
    return 201, enrollment


@router.post("/enrollments/{enrollment_id}/transfer/", response=ScheduleEnrollmentTransferOut)
@role_required("owner", "admin")
def transfer_enrollment_endpoint(
    request,
    enrollment_id: int,
    payload: ScheduleEnrollmentTransferIn,
):
    closed_enrollment, new_enrollment = transfer_schedule_enrollment(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        target_schedule_id=payload.target_schedule_id,
        ends_on=payload.ends_on,
        actor_user_id=request.user.id,
    )
    return {
        "closed_enrollment": closed_enrollment,
        "new_enrollment": new_enrollment,
    }


@router.post("/enrollments/{enrollment_id}/cancel/", response=ScheduleEnrollmentOut)
@role_required("owner", "admin")
def cancel_enrollment_endpoint(
    request,
    enrollment_id: int,
    payload: ScheduleEnrollmentEndIn,
):
    return cancel_schedule_enrollment(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        ends_on=payload.ends_on,
        actor_user_id=request.user.id,
    )


@router.post("/enrollments/{enrollment_id}/freeze/", response=ScheduleEnrollmentOut)
@role_required("owner", "admin")
def freeze_enrollment_endpoint(request, enrollment_id: int):
    return freeze_schedule_enrollment(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        actor_user_id=request.user.id,
    )


@router.post("/enrollments/{enrollment_id}/unfreeze/", response=ScheduleEnrollmentOut)
@role_required("owner", "admin")
def unfreeze_enrollment_endpoint(request, enrollment_id: int):
    return unfreeze_schedule_enrollment(
        club_id=request.club.id,
        enrollment_id=enrollment_id,
        actor_user_id=request.user.id,
    )


@router.get("/{schedule_id}/", response=ScheduleOut)
@role_required("owner", "admin", "trainer")
def get_schedule_detail(request, schedule_id: int, date: date_cls | None = None):
    _assert_trainer_can_access_schedule(request, schedule_id=schedule_id, target_date=date)
    return get_schedule_by_id(club=request.club, schedule_id=schedule_id)


@router.put("/{schedule_id}/", response=ScheduleOut)
@role_required("owner", "admin", "trainer")
def update_schedule_endpoint(request, schedule_id: int, payload: ScheduleUpdate):
    fields = schema_sent_fields(payload, _SCHEDULE_UPDATE_FIELDS)
    if request._membership.role == "trainer":
        schedule_obj = Schedule.objects.for_club(request.club).get(id=schedule_id)
        _assert_trainer_owns_schedule(request, schedule_obj)
        if "trainer_id" in fields and fields["trainer_id"] != schedule_obj.trainer_id:
            raise HttpError(403, "Trainers cannot reassign schedules")
    return update_schedule(
        schedule_id=schedule_id,
        club_id=request.club.id,
        **fields,
    )


@router.post("/{schedule_id}/cancel/", response={201: ScheduleExceptionOut})
@role_required("owner", "admin", "trainer")
def cancel_session_endpoint(request, schedule_id: int, payload: CancelSessionIn):
    if request._membership.role == "trainer":
        schedule_obj = Schedule.objects.for_club(request.club).get(id=schedule_id)
        _assert_trainer_owns_schedule(request, schedule_obj)
    exc = cancel_session(
        club_id=request.club.id,
        schedule_id=schedule_id,
        date=payload.date,
        reason=payload.reason,
    )
    return 201, exc


@router.post("/{schedule_id}/reschedule/", response={201: ScheduleExceptionOut})
@role_required("owner", "admin", "trainer")
def reschedule_session_endpoint(request, schedule_id: int, payload: RescheduleIn):
    if request._membership.role == "trainer":
        schedule_obj = Schedule.objects.for_club(request.club).get(id=schedule_id)
        _assert_trainer_owns_schedule(request, schedule_obj)
    exc = reschedule_session(
        club_id=request.club.id,
        schedule_id=schedule_id,
        date=payload.date,
        new_date=payload.new_date,
        new_start_time=payload.new_start_time,
        new_end_time=payload.new_end_time,
        reason=payload.reason,
    )
    return 201, exc


@router.post("/{schedule_id}/substitute/", response={201: ScheduleExceptionOut})
@role_required("owner", "admin")
def substitute_trainer_endpoint(request, schedule_id: int, payload: SubstituteIn):
    exc = substitute_trainer(
        club_id=request.club.id,
        schedule_id=schedule_id,
        date=payload.date,
        substitute_trainer_id=payload.substitute_trainer_id,
        reason=payload.reason,
    )
    return 201, exc


@router.get("/{schedule_id}/students/", response=list[StudentWithAlertsOut])
@role_required("owner", "admin", "trainer")
def schedule_students(request, schedule_id: int, date: date_cls | None = None):
    _assert_trainer_can_access_schedule(request, schedule_id=schedule_id, target_date=date)
    return get_students_for_schedule(
        club=request.club,
        schedule_id=schedule_id,
        reference_date=date or club_localdate(request.club),
    )


@router.get("/{schedule_id}/checked-in/", response=ScheduleCheckinStatusOut)
@role_required("owner", "admin", "trainer")
def schedule_checked_in(request, schedule_id: int, date: str | None = None):
    from datetime import date as date_type

    from apps.attendance.selectors import get_already_checked_in_ids, get_has_group_session

    checkin_date = date_type.fromisoformat(date) if date else club_localdate(request.club)
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=schedule_id,
        target_date=checkin_date,
    )
    return {
        "student_ids": get_already_checked_in_ids(
            club=request.club,
            schedule_id=schedule_id,
            checkin_date=checkin_date,
        ),
        "has_group_session": get_has_group_session(
            club=request.club,
            schedule_id=schedule_id,
            session_date=checkin_date,
        ),
    }


@router.get("/{schedule_id}/session-detail/", response=SessionDetailOut)
@role_required("owner", "admin", "trainer")
def schedule_session_detail(request, schedule_id: int, date: date_cls):
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=schedule_id,
        target_date=date,
    )
    _assert_schedule_occurs_on_date(
        request,
        schedule_id=schedule_id,
        target_date=date,
    )
    return get_session_detail(
        club=request.club,
        schedule_id=schedule_id,
        session_date=date,
    )


@router.post("/{schedule_id}/sessions/close/", response=GroupSessionOut)
@role_required("owner", "admin", "trainer")
def close_schedule_session(request, schedule_id: int, payload: CloseSessionIn):
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=schedule_id,
        target_date=payload.date,
    )
    _assert_schedule_occurs_on_date(
        request,
        schedule_id=schedule_id,
        target_date=payload.date,
    )
    return close_session_from_existing_checkins(
        club_id=request.club.id,
        schedule_id=schedule_id,
        checkin_date=payload.date,
        actor_user_id=request.user.id,
        topic_tags=payload.topic_tags,
        notes=payload.notes,
    )


@router.get("/{schedule_id}/exceptions/", response=list[ScheduleExceptionOut])
@role_required("owner", "admin")
def list_exceptions(request, schedule_id: int):
    return list(get_schedule_exceptions(club=request.club, schedule_id=schedule_id))


# ──────────────────────────────────────────────
# Check-in endpoints
# ──────────────────────────────────────────────

checkin_router = Router(tags=["checkins"])

kiosk_auth = KioskDeviceAuth()


@checkin_router.post("/kiosk/activate/", auth=None, response=KioskActivateOut)
def kiosk_activate(request, payload: KioskActivateIn):
    return activate_kiosk(pin=payload.pin, throttle_key=_kiosk_activation_throttle_key(request))


def _kiosk_activation_throttle_key(request) -> str:
    remote_addr = request.META.get("REMOTE_ADDR") or "unknown"
    return f"ip:{remote_addr}"


@checkin_router.post("/kiosk/lookup/", auth=kiosk_auth, response=list[StudentMatchOut])
def kiosk_phone_lookup(request, payload: PhoneLookupIn):
    if len(payload.phone_suffix) != 4 or not payload.phone_suffix.isdigit():
        raise HttpError(400, "phone_suffix must be exactly 4 digits")
    return lookup_by_phone_suffix(club_id=request.club.id, phone_suffix=payload.phone_suffix)


@checkin_router.get("/kiosk/schedules/today/", auth=kiosk_auth, response=list[KioskScheduleOut])
def kiosk_today_schedules(request, date: date_cls | None = None):
    target_date = date or club_localdate(request.club)
    occurrences = get_schedule_occurrences_for_date(
        club=request.club,
        target_date=target_date,
    )
    return [
        {
            "schedule_id": occurrence.schedule_id,
            "effective_date": occurrence.effective_date,
            "start_time": occurrence.effective_start_time,
            "end_time": occurrence.effective_end_time,
            "group_name": occurrence.group_name,
            "trainer_name": occurrence.trainer_name,
            "location_name": occurrence.location_name,
            "training_type_id": occurrence.training_type_id,
            "training_type_name": occurrence.training_type_name,
        }
        for occurrence in occurrences
        if occurrence.training_type_id is not None
    ]


@checkin_router.get("/kiosk/roster/", auth=kiosk_auth, response=list[KioskRosterStudentOut])
def kiosk_roster(request):
    return get_kiosk_roster(club_id=request.club.id)


@checkin_router.post("/kiosk/options/", auth=kiosk_auth, response=KioskOptionsOut)
def kiosk_checkin_options(request, payload: KioskOptionsIn):
    return get_kiosk_checkin_options(
        club=request.club,
        student_id=payload.student_id,
        target_date=payload.date or club_localdate(request.club),
    )


@checkin_router.get("/kiosk/branding/", auth=kiosk_auth, response=KioskBrandingOut)
def kiosk_branding(request):
    settings = get_or_create_club_settings(request.club.id)
    logo_url = request.build_absolute_uri(settings.logo_file.url) if settings.logo_file else settings.logo_url
    return {
        "primary_color": settings.primary_color,
        "accent_color": settings.accent_color,
        "club_name_display": settings.club_name_display,
        "logo_url": logo_url,
    }


def _kiosk_checkin_payload(
    *,
    checkin_id: int,
    club_id: int,
    student_id: int,
    is_debt: bool,
    subscription_id: int | None,
    created: bool,
) -> dict:
    side_effects = _kiosk_side_effect_flags(
        checkin_id=checkin_id,
        club_id=club_id,
        created=created,
    )
    return {
        "checkin_id": checkin_id,
        "student_id": student_id,
        "is_debt": is_debt,
        "subscription_id": subscription_id,
        "alerts": [],
        "created": created,
        "duplicate": not created,
        "subscription_effect": "deducted" if created and subscription_id else "none",
        "debt_effect": "created" if created and is_debt else "none",
        "salary_queued": side_effects["salary_queued"],
        "parent_notification_queued": side_effects["parent_notification_queued"],
        "grade_progress_queued": side_effects["grade_progress_queued"],
        "group_analytics_queued": side_effects["group_analytics_queued"],
        "retention_auto_close_queued": side_effects["retention_auto_close_queued"],
        "post_trial_task_queued": side_effects["post_trial_task_queued"],
        "trainings_left_push_queued": side_effects["trainings_left_push_queued"],
    }


_CASCADE_EFFECT_FLAGS = {
    CheckinCascadeEvent.Effect.SALARY: "salary_queued",
    CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: "parent_notification_queued",
    CheckinCascadeEvent.Effect.GRADE_PROGRESS: "grade_progress_queued",
    CheckinCascadeEvent.Effect.GROUP_ANALYTICS: "group_analytics_queued",
    CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: "retention_auto_close_queued",
    CheckinCascadeEvent.Effect.POST_TRIAL_TASK: "post_trial_task_queued",
    CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: "trainings_left_push_queued",
}


def _kiosk_side_effect_flags(*, checkin_id: int, club_id: int, created: bool) -> dict[str, bool]:
    if not created:
        return {flag: False for flag in _CASCADE_EFFECT_FLAGS.values()}

    effects = set(
        CheckinCascadeEvent.objects.for_club(club_id)
        .filter(
            checkin_id=checkin_id,
            status=CheckinCascadeEvent.Status.QUEUED,
            expected=True,
        )
        .values_list("effect", flat=True)
    )
    return {flag: effect in effects for effect, flag in _CASCADE_EFFECT_FLAGS.items()}


def _get_existing_kiosk_checkin(*, club_id: int, student_id: int, schedule_id: int, checkin_date: date_cls):
    return (
        Checkin.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            schedule_id=schedule_id,
            date=checkin_date,
            deleted_at__isnull=True,
        )
        .first()
    )


@checkin_router.post("/kiosk/", auth=kiosk_auth, response=CheckinResultOut)
def kiosk_checkin(request, payload: KioskCheckinIn):
    try:
        result = create_checkin(
            club_id=request.club.id,
            student_id=payload.student_id,
            schedule_id=payload.schedule_id,
            training_type_id=payload.training_type_id,
            source="kiosk",
            checkin_date=payload.checkin_date,
        )
    except IntegrityError:
        # Idempotent: return existing checkin if duplicate (same student+schedule+date)
        existing = _get_existing_kiosk_checkin(
            club_id=request.club.id,
            student_id=payload.student_id,
            schedule_id=payload.schedule_id,
            checkin_date=payload.checkin_date or club_localdate(request.club),
        )
        if existing:
            return _kiosk_checkin_payload(
                checkin_id=existing.id,
                club_id=request.club.id,
                student_id=existing.student_id,
                is_debt=existing.is_debt,
                subscription_id=existing.subscription_id,
                created=False,
            )
        raise
    # Kiosk response: NO alerts
    return _kiosk_checkin_payload(
        checkin_id=result["checkin_id"],
        club_id=request.club.id,
        student_id=payload.student_id,
        is_debt=result["is_debt"],
        subscription_id=result["subscription_id"],
        created=result["created"],
    )


@checkin_router.post("/kiosk/guest-book-and-checkin/", auth=kiosk_auth, response=CheckinResultOut)
def kiosk_guest_book_and_checkin(request, payload: KioskCheckinIn):
    target_date = payload.checkin_date or club_localdate(request.club)
    options_payload = get_kiosk_checkin_options(
        club=request.club,
        student_id=payload.student_id,
        target_date=target_date,
    )
    option = next(
        (item for item in options_payload["options"] if item["schedule_id"] == payload.schedule_id),
        None,
    )
    if option is None:
        raise BusinessLogicError(
            "Тренировка сейчас недоступна",
            code="schedule_occurrence_not_found",
        )
    if option["training_type_id"] != payload.training_type_id:
        raise BusinessLogicError(
            "Тип тренировки не подходит для отметки",
            code="training_type_mismatch",
        )

    if option["self_checkin_status"] == "blocked" and option["reason_code"] == "already_checked_in":
        existing = _get_existing_kiosk_checkin(
            club_id=request.club.id,
            student_id=payload.student_id,
            schedule_id=payload.schedule_id,
            checkin_date=target_date,
        )
        if existing is not None:
            return _kiosk_checkin_payload(
                checkin_id=existing.id,
                club_id=request.club.id,
                student_id=existing.student_id,
                is_debt=existing.is_debt,
                subscription_id=existing.subscription_id,
                created=False,
            )

    student = (
        Student.objects.for_club(request.club)
        .filter(id=payload.student_id, deleted_at__isnull=True)
        .only("id", "status")
        .first()
    )
    if student is None or student.status not in {Student.Status.ACTIVE, Student.Status.TRIAL}:
        raise BusinessLogicError(
            "Ученик недоступен для kiosk guest booking",
            code="student_ineligible",
        )

    if option["self_checkin_status"] != "can_book_guest_visit":
        raise BusinessLogicError(
            "Гостевая запись недоступна для этой тренировки",
            code=option["reason_code"] or "kiosk_guest_booking_not_available",
        )

    idempotency_key = f"kiosk-guest-booking-{payload.student_id}-{payload.schedule_id}-{target_date.isoformat()}"
    with transaction.atomic():
        book_guest_group_visit(
            club_id=request.club.id,
            schedule_id=payload.schedule_id,
            target_date=target_date,
            student_id=payload.student_id,
            origin=ScheduleBookingEvent.Origin.WALK_IN_CHECKIN,
            actor_user_id=None,
            idempotency_key=idempotency_key,
            created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
            allow_lead_conversion=False,
            require_financial_eligibility=True,
        )
        result = create_checkin(
            club_id=request.club.id,
            student_id=payload.student_id,
            schedule_id=payload.schedule_id,
            training_type_id=payload.training_type_id,
            source="kiosk",
            checkin_date=target_date,
            _defer_async_until_commit=True,
        )

    return _kiosk_checkin_payload(
        checkin_id=result["checkin_id"],
        club_id=request.club.id,
        student_id=payload.student_id,
        is_debt=result["is_debt"],
        subscription_id=result["subscription_id"],
        created=result["created"],
    )


@checkin_router.post("/batch/", response=BatchCheckinResultOut)
@role_required("owner", "admin")
def batch_checkin_endpoint(request, payload: BatchCheckinIn):
    _assert_trainer_can_access_schedule(
        request,
        schedule_id=payload.schedule_id,
        target_date=payload.date,
    )
    result = batch_checkin(
        club_id=request.club.id,
        schedule_id=payload.schedule_id,
        checkin_date=payload.date,
        present_student_ids=payload.present_student_ids,
        training_type_id=payload.training_type_id,
        actor_user_id=request.user.id,
        topic_tags=payload.topic_tags,
        notes=payload.notes,
    )
    # Compute alerts for each checkin (trainer context) — single query
    checkin_ids = [c["checkin_id"] for c in result["checkins"]]
    checkins_by_id = {
        c.id: c
        for c in Checkin.objects.for_club(request.club)
        .select_related("student", "subscription")
        .filter(id__in=checkin_ids)
    }
    if set(checkin_ids) != set(checkins_by_id):
        raise BusinessLogicError(
            "Batch checkin returned records outside current club",
            code="batch_checkin_tenant_mismatch",
        )

    enriched = []
    for checkin_data in result["checkins"]:
        checkin_obj = checkins_by_id[checkin_data["checkin_id"]]
        alerts = compute_checkin_alerts(
            checkin=checkin_obj,
            student=checkin_obj.student,
            subscription=checkin_obj.subscription,
        )
        side_effects = _kiosk_side_effect_flags(
            checkin_id=checkin_data["checkin_id"],
            club_id=request.club.id,
            created=checkin_data["created"],
        )
        enriched.append(
            {
                "checkin_id": checkin_data["checkin_id"],
                "student_id": checkin_obj.student_id,
                "is_debt": checkin_data["is_debt"],
                "subscription_id": checkin_data["subscription_id"],
                "alerts": alerts,
                "created": checkin_data["created"],
                "duplicate": not checkin_data["created"],
                "subscription_effect": (
                    "deducted" if checkin_data["created"] and checkin_data["subscription_id"] else "none"
                ),
                "debt_effect": "created" if checkin_data["created"] and checkin_data["is_debt"] else "none",
                "salary_queued": side_effects["salary_queued"],
                "parent_notification_queued": side_effects["parent_notification_queued"],
                "grade_progress_queued": side_effects["grade_progress_queued"],
                "group_analytics_queued": side_effects["group_analytics_queued"],
                "retention_auto_close_queued": side_effects["retention_auto_close_queued"],
                "post_trial_task_queued": side_effects["post_trial_task_queued"],
                "trainings_left_push_queued": side_effects["trainings_left_push_queued"],
            }
        )
    return {"checkins": enriched, "group_session_id": result["group_session_id"]}


@checkin_router.post("/{checkin_id}/cancel/", response={200: dict})
@role_required("owner", "admin")
def cancel_checkin_endpoint(request, checkin_id: int):
    cancel_checkin(
        checkin_id=checkin_id,
        club_id=request.club.id,
        cancelled_by_user_id=request.user.id,
        user_role=request._membership.role,
    )
    return {"success": True}


def _offline_sync_error_code(exc: ObjectDoesNotExist | BusinessLogicError) -> str:
    if isinstance(exc, BusinessLogicError):
        return exc.code
    if isinstance(exc, Student.DoesNotExist):
        return "student_not_found"
    if isinstance(exc, Schedule.DoesNotExist):
        return "schedule_not_found"
    if isinstance(exc, TrainingType.DoesNotExist):
        return "training_type_not_found"
    return "object_not_found"


@checkin_router.post("/sync/", auth=kiosk_auth, response=OfflineSyncResultOut)
def offline_sync(request, payload: OfflineSyncIn):
    synced = 0
    failed = 0
    results = []
    for item in payload.checkins:
        checkin_date = item.checkin_date or club_localdate(request.club)
        try:
            result = create_checkin(
                club_id=request.club.id,
                student_id=item.student_id,
                schedule_id=item.schedule_id,
                training_type_id=item.training_type_id,
                source="kiosk",
                checkin_date=checkin_date,
            )
            synced += 1
            results.append(
                {
                    "client_id": item.client_id,
                    "idempotency_key": item.idempotency_key,
                    "student_id": item.student_id,
                    "success": True,
                    "checkin_id": result["checkin_id"],
                    "duplicate": not result["created"],
                    "error": None,
                    "retryable": False,
                }
            )
            continue
        except IntegrityError:
            existing = _get_existing_kiosk_checkin(
                club_id=request.club.id,
                student_id=item.student_id,
                schedule_id=item.schedule_id,
                checkin_date=checkin_date,
            )
            if existing:
                synced += 1
                results.append(
                    {
                        "client_id": item.client_id,
                        "idempotency_key": item.idempotency_key,
                        "student_id": item.student_id,
                        "success": True,
                        "checkin_id": existing.id,
                        "duplicate": True,
                        "error": None,
                        "retryable": False,
                    }
                )
                continue
            failed += 1
            results.append(
                {
                    "client_id": item.client_id,
                    "idempotency_key": item.idempotency_key,
                    "student_id": item.student_id,
                    "success": False,
                    "checkin_id": None,
                    "duplicate": False,
                    "error": "duplicate_checkin_conflict",
                    "retryable": False,
                }
            )
        except (ObjectDoesNotExist, BusinessLogicError) as e:
            error_code = _offline_sync_error_code(e)
            failed += 1
            results.append(
                {
                    "client_id": item.client_id,
                    "idempotency_key": item.idempotency_key,
                    "student_id": item.student_id,
                    "success": False,
                    "checkin_id": None,
                    "duplicate": False,
                    "error": error_code,
                    "retryable": error_code == "training_group_reconciling",
                }
            )
            logger.warning(
                "offline_sync_item_failed",
                extra={"student_id": item.student_id, "error_code": error_code},
            )
    return {"synced": synced, "failed": failed, "results": results}


@checkin_router.get("/today/", response=list[TodayCheckinOut])
@role_required("owner", "admin")
def today_checkins(request):
    return list(get_today_checkins(club=request.club))
