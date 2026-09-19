from datetime import date as date_cls
from datetime import datetime as datetime_cls
from datetime import timedelta

from django.utils import timezone
from ninja import File, Router, UploadedFile
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
)
from apps.attendance.schemas import (
    PersonalBookingIn,
    PersonalBookingOut,
    PersonalBookingPaymentReservationCreateIn,
    PersonalBookingPaymentReservationOut,
    PersonalCommercialReceiptOut,
    PersonalDropInBookingIn,
    PersonalDropInBookingOut,
)
from apps.attendance.selectors import (
    can_reschedule_student_personal_booking,
    get_student_attendance,
    get_student_schedule,
    get_student_schedule_occurrences_for_range,
    get_student_upcoming_personal_bookings,
)
from apps.attendance.services import (
    PersonalSessionBooking,
    book_personal_drop_in,
    book_personal_session,
    create_personal_booking_payment_reservation,
    get_personal_booking_payment_reservations,
    get_personal_commercial_context,
    replace_personal_payment_method,
)
from apps.billing.models import Subscription, SubscriptionFreeze
from apps.billing.schemas import BankPaymentOrderOut, SelfServiceBankPaymentOrderCreateIn
from apps.billing.selectors import get_bank_payment_orders, get_student_open_debts, get_student_subscriptions
from apps.billing.service_modules.renewals import get_renewal_offer
from apps.billing.services import cancel_bank_payment_order, create_bank_payment_order
from apps.clubs.capabilities import (
    get_commercial_journey_capability,
    is_unified_client_journey_enabled,
)
from apps.clubs.models import ClubMembership
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import role_required
from apps.feedback.schemas import FormOut, SelfServiceResponseOut, SubmitSelfResponseIn
from apps.feedback.selectors import get_active_form
from apps.feedback.services import submit_feedback_response
from apps.students.access_services import open_account_access_for_student, reset_account_access_for_student
from apps.students.duplicates import DuplicateStudentError
from apps.students.intake_services import restore_soft_deleted_person, submit_student_intake
from apps.students.person_search import search_people
from apps.students.schemas import (
    AccountAccessIssueOut,
    AccountAccessOpenIn,
    AttendanceSummaryOut,
    CabinetFinancialOut,
    ImportResultOut,
    PersonalPaymentMethodCorrectionIn,
    PersonSearchOut,
    StatusTransitionIn,
    StudentAttendanceOut,
    StudentCommercialContextOut,
    StudentDebtOut,
    StudentDetailOut,
    StudentIn,
    StudentIntakeCapabilityOut,
    StudentIntakeIn,
    StudentIntakeOut,
    StudentMeOut,
    StudentNoteIn,
    StudentNoteOut,
    StudentOut,
    StudentPersonalBookingOut,
    StudentScheduleItemOut,
    StudentSubscriptionOut,
    StudentUpdate,
    StudentWeekScheduleOut,
)
from apps.students.scopes import (
    actor_can_manage_student_account_access,
    actor_can_manage_student_sensitive_actions,
    actor_can_read_student_detail,
    actor_is_scoped_to_student,
    assert_actor_is_scoped_to_student,
    get_current_trainer_id_for_user,
    trainer_manual_operational_admission_scope_filter,
    trainer_student_scope_filter,
)
from apps.students.selectors import (
    COMMERCIAL_SEGMENTS,
    filter_by_commercial_segment,
    filter_students_by_query,
    get_cabinet_financial_read_model,
    get_student_by_id,
    get_student_by_user,
    get_student_detail,
    get_student_notes,
    get_student_workspace,
    get_students,
    with_commercial_segment,
)
from apps.students.services import (
    add_student_note,
    create_student,
    delete_student,
    import_students_from_excel,
    transition_status,
    update_student,
)

router = Router(tags=["students"])


def _mark_bank_payment_order_cancel_scope(orders, *, allowed_sources: set[str]):
    order_list = list(orders)
    for order in order_list:
        order.can_cancel_source_allowed = order.source in allowed_sources
        # Refresh only coalesces this actor-scoped exact order onto durable
        # reconciliation; unlike cancellation it is safe across reused sources.
        order.can_refresh_source_allowed = True
        order.payment_action_mode = "self_service"
    return order_list


def _account_access_issue_out(result) -> AccountAccessIssueOut:
    return AccountAccessIssueOut(
        student_id=result.access.student_id,
        role=result.access.role,
        status=result.access.status,
        username=result.user.username,
        must_change_password=result.access.must_change_password,
        issued_at=result.access.issued_at,
        reset_at=result.access.reset_at,
        temporary_password=result.temporary_password,
        created_user=result.created_user,
        created_membership=result.created_membership,
        created_access=result.created_access,
    )


def _personal_booking_out(result: PersonalSessionBooking) -> dict:
    schedule = result.schedule
    enrollment = result.enrollment
    starts_at = datetime_cls.combine(schedule.one_time_date, schedule.start_time)
    ends_at = datetime_cls.combine(schedule.one_time_date, schedule.end_time)
    trainer_name = f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip()
    return {
        "schedule_id": schedule.id,
        "enrollment_id": enrollment.id,
        "availability_slot_id": result.availability_slot_id,
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


def _personal_drop_in_booking_out(
    booking: PersonalDropInBooking,
    *,
    created: bool,
) -> dict:
    schedule = booking.enrollment.schedule
    latest_link = (
        PersonalDropInPaymentLink.objects.for_club(booking.club_id)
        .select_related("payment")
        .filter(booking_id=booking.id)
        .order_by("-created_at", "-id")
        .first()
    )
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
        "availability_slot_id": None,
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
        "created": created,
    }


def _student_personal_booking_out(
    enrollment,
    *,
    current_trainer_id: int | None = None,
) -> StudentPersonalBookingOut:
    schedule = enrollment.schedule
    starts_at = datetime_cls.combine(schedule.one_time_date, schedule.start_time)
    ends_at = datetime_cls.combine(schedule.one_time_date, schedule.end_time)
    trainer_name = f"{schedule.trainer.first_name} {schedule.trainer.last_name}".strip()
    can_manage = current_trainer_id is None or schedule.trainer_id == current_trainer_id
    drop_in = getattr(enrollment, "personal_drop_in_booking", None)
    payment_reservation = getattr(enrollment, "personal_payment_reservation", None)
    can_reschedule = can_reschedule_student_personal_booking(
        enrollment=enrollment,
        can_manage=can_manage,
    )
    if drop_in is not None:
        links = getattr(drop_in, "prefetched_payment_links", [])
        latest_link = links[0] if links else None
        latest_order = latest_link.bank_payment_order if latest_link else None
        pending_payment = bool(latest_link and latest_link.payment.status == "pending")
        if drop_in.state == PersonalDropInBooking.State.ATTENDED:
            if drop_in.debt_id and drop_in.debt.resolved_at is None:
                financial_state = "payment_pending" if pending_payment else "debt_open"
            else:
                financial_state = "paid" if latest_link and latest_link.payment.status == "confirmed" else "covered"
        elif drop_in.state in {
            PersonalDropInBooking.State.CANCELLED,
            PersonalDropInBooking.State.NO_SHOW,
        }:
            if latest_link and latest_link.payment.status == "confirmed":
                financial_state = "paid"
            elif pending_payment:
                financial_state = "payment_pending"
            else:
                financial_state = "not_due"
        elif latest_link and latest_link.payment.status == "confirmed":
            financial_state = "covered"
        elif pending_payment:
            financial_state = "payment_pending"
        else:
            financial_state = "pay_at_club"
        local_now = timezone.localtime(timezone.now(), club_zoneinfo(schedule.club))
        start_at = timezone.make_aware(starts_at, club_zoneinfo(schedule.club))
        end_at = timezone.make_aware(ends_at, club_zoneinfo(schedule.club))
        can_cancel = (
            drop_in.state == PersonalDropInBooking.State.SCHEDULED and start_at > local_now and not pending_payment
        )
        can_mark_no_show = drop_in.state == PersonalDropInBooking.State.SCHEDULED and end_at <= local_now
        if drop_in.state in {
            PersonalDropInBooking.State.CANCELLED,
            PersonalDropInBooking.State.NO_SHOW,
        }:
            next_action_label = None
        elif can_mark_no_show:
            next_action_label = "Не пришёл"
        elif financial_state == "debt_open":
            next_action_label = "Принять оплату"
        elif financial_state == "pay_at_club":
            next_action_label = "Предоплата"
        elif financial_state == "payment_pending":
            next_action_label = "Оплата ожидает подтверждения"
        else:
            next_action_label = None
        return StudentPersonalBookingOut(
            schedule_id=schedule.id,
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            trainer_id=schedule.trainer_id,
            trainer_name=trainer_name,
            location_id=schedule.location_id,
            location_name=schedule.location.name,
            training_type_id=schedule.training_type_id,
            training_type_name=schedule.training_type.name,
            starts_at=starts_at,
            ends_at=ends_at,
            created_from=enrollment.created_from,
            status=enrollment.status,
            booking_id=drop_in.id,
            booking_kind="drop_in",
            attendance_state=drop_in.state,
            financial_state=financial_state,
            price_snapshot=str(drop_in.price_snapshot),
            tariff_id=drop_in.tariff_id,
            debt_id=drop_in.debt_id,
            payment_id=latest_link.payment_id if can_manage and latest_link else None,
            bank_payment_order_id=latest_link.bank_payment_order_id if can_manage and latest_link else None,
            payment_status=latest_link.payment.status if latest_link else None,
            order_status=latest_order.status if latest_order else None,
            provider_payment_url=latest_order.provider_payment_url if can_manage and latest_order else "",
            can_manage=can_manage,
            can_cancel_payment=can_manage and bool(
                latest_order
                and latest_link.payment.status == "pending"
                and latest_order.status in {"created", "pending", "authorized"}
                and latest_order.subscription.status == "pending"
            ),
            can_cancel=can_manage and can_cancel,
            can_mark_no_show=can_manage and can_mark_no_show,
            can_reschedule=can_reschedule,
            next_action_label=next_action_label if can_manage else None,
        )
    return StudentPersonalBookingOut(
        schedule_id=schedule.id,
        enrollment_id=enrollment.id,
        student_id=enrollment.student_id,
        trainer_id=schedule.trainer_id,
        trainer_name=trainer_name,
        location_id=schedule.location_id,
        location_name=schedule.location.name,
        training_type_id=schedule.training_type_id,
        training_type_name=schedule.training_type.name,
        starts_at=starts_at,
        ends_at=ends_at,
        created_from=enrollment.created_from,
        status=enrollment.status,
        booking_kind="online_payment" if payment_reservation is not None else "entitlement",
        can_manage=can_manage,
        can_reschedule=can_reschedule,
    )


def _personal_booking_payment_reservation_out(
    reservation: PersonalBookingPaymentReservation,
) -> PersonalBookingPaymentReservationOut:
    order = reservation.bank_payment_order
    trainer_name = f"{reservation.trainer.first_name} {reservation.trainer.last_name}".strip()
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
            and order.status in {"created", "pending", "authorized"}
            and reservation.expires_at > timezone.now()
        ),
        created_at=reservation.created_at,
    )


def _get_current_trainer_id(request) -> int:
    return get_current_trainer_id_for_user(club=request.club, user=request.user)


def _actor_is_scoped_to_student(request, *, student_id: int) -> bool:
    return actor_is_scoped_to_student(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _account_access_actor_is_scoped(request, *, student_id: int) -> bool:
    return actor_can_manage_student_account_access(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _detail_read_actor_is_scoped(request, *, student_id: int) -> bool:
    return actor_can_read_student_detail(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _sensitive_action_actor_is_scoped(request, *, student_id: int) -> bool:
    return actor_can_manage_student_sensitive_actions(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _assert_trainer_student_actor_scope(request, *, student_id: int) -> None:
    assert_actor_is_scoped_to_student(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _assert_account_access_actor_scope(request, *, student_id: int) -> None:
    if not _account_access_actor_is_scoped(request, student_id=student_id):
        raise HttpError(403, "Access denied: not your student")


def _assert_personal_booking_actor_scope(request, *, student_id: int) -> None:
    if request._membership.role == ClubMembership.Role.TRAINER:
        if not _actor_is_scoped_to_student(request, student_id=student_id):
            raise HttpError(403, "Access denied: not your student")


def _student_duplicate_response(request, exc: DuplicateStudentError):
    existing = exc.existing_student
    can_open = request._membership.role != ClubMembership.Role.TRAINER
    duplicate_scope = "club"

    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = _get_current_trainer_id(request)
        can_open = (
            get_students(club=request.club)
            .filter(id=existing.id, deleted_at__isnull=True)
            .filter(trainer_student_scope_filter(trainer_id))
            .exists()
        )
        if can_open:
            duplicate_scope = "own"
        elif existing.lead_status is not None and existing.assigned_trainer_id is None:
            duplicate_scope = "pool"
        else:
            duplicate_scope = "other"

    payload = {
        "detail": "Этот телефон уже есть в CRM",
        "code": exc.code,
        "duplicate_scope": duplicate_scope,
        "can_open_existing": can_open,
    }
    if can_open:
        payload["existing_student"] = {
            "id": existing.id,
            "display_name": " ".join(part for part in [existing.first_name, existing.last_name] if part),
            "is_child": existing.is_child,
            "status": existing.status,
            "lead_status": existing.lead_status,
            "assigned_trainer_id": existing.assigned_trainer_id,
        }
    return 409, payload


# ──────────────────────────────────────────────
# Student self-service endpoints
# ──────────────────────────────────────────────


@router.get("/me/", response=StudentMeOut)
@role_required("student")
def my_profile(request):
    return get_student_by_user(club=request.club, user_id=request.user.id)


@router.get("/me/subscriptions/", response=list[StudentSubscriptionOut])
@role_required("student")
def my_subscriptions(request):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    subs = list(get_student_subscriptions(club=request.club, student_id=student.id))
    pending_freeze_ids = set(
        SubscriptionFreeze.objects.for_club(request.club)
        .filter(
            subscription_id__in=[sub.id for sub in subs],
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )
        .values_list("subscription_id", flat=True)
    )
    result = []
    for subscription in subs:
        renewal_offer = get_renewal_offer(
            club_id=request.club.id,
            source_tariff=subscription.tariff,
        )
        renewal_target_tariff_id = renewal_offer.target_tariff_id if renewal_offer.is_available else None
        renewal_target_tariff_name = renewal_offer.target_tariff_name if renewal_offer.is_available else ""
        renewal_target_price = renewal_offer.target_price if renewal_offer.is_available else None
        result.append(
            StudentSubscriptionOut(
                id=subscription.id,
                tariff_id=subscription.tariff_id,
                tariff_name=subscription.tariff.name,
                trainings_used=subscription.trainings_used,
                trainings_total=subscription.tariff.trainings_limit,
                trainings_left=subscription.trainings_left,
                expires_at=subscription.expires_at,
                status=subscription.status,
                freeze_status=(
                    SubscriptionFreeze.FreezeStatus.PENDING
                    if subscription.id in pending_freeze_ids
                    else None
                ),
                renewal_target_tariff_id=renewal_target_tariff_id,
                renewal_target_tariff_name=renewal_target_tariff_name,
                renewal_target_price=renewal_target_price,
            )
        )
    return result


@router.get("/me/debts/", response=list[StudentDebtOut])
@role_required("student")
def my_debts(request):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    debts = get_student_open_debts(club=request.club, student_id=student.id)
    return [
        StudentDebtOut(
            id=debt.id,
            checkin_id=debt.checkin_id,
            tariff_price=debt.tariff_price,
            reason=debt.reason,
            training_type_name=debt.checkin.training_type.name,
            checkin_date=debt.checkin.date,
            created_at=debt.created_at,
        )
        for debt in debts
    ]


@router.get("/me/financial-state/", response=CabinetFinancialOut)
@role_required("student")
def my_financial_state(request):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    return get_cabinet_financial_read_model(club=request.club, student=student)


@router.post("/me/bank-payment-orders/", response={200: BankPaymentOrderOut, 201: BankPaymentOrderOut})
@role_required("student")
def create_my_bank_payment_order(request, payload: SelfServiceBankPaymentOrderCreateIn):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    renewal = payload.renewed_from_subscription_id is not None
    if renewal and not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Contextual renewal is unavailable")
    if renewal and not (payload.idempotency_key or "").strip():
        raise BusinessLogicError(
            "Продление требует стабильный ключ команды",
            code="idempotency_key_required",
        )
    if renewal and (payload.tariff_id is not None or payload.debt_ids):
        raise BusinessLogicError(
            "Точное продление не принимает тариф или произвольный долг",
            code="renewal_client_terms_forbidden",
        )
    if (
        not renewal
        and is_unified_client_journey_enabled(club=request.club)
        and payload.tariff_id is not None
        and Subscription.objects.for_club(request.club)
        .filter(
            student_id=student.id,
            tariff_id=payload.tariff_id,
            deleted_at__isnull=True,
        )
        .exists()
    ):
        raise BusinessLogicError(
            "Контекстное продление требует точный исходный абонемент",
            code="renewal_source_required",
        )
    if not renewal and payload.tariff_id is None:
        raise BusinessLogicError("Укажите тариф", code="tariff_required")
    order = create_bank_payment_order(
        club_id=request.club.id,
        student_id=student.id,
        tariff_id=payload.tariff_id,
        source="student",
        created_by_id=request.user.id,
        discount_ids=[],
        debt_ids=payload.debt_ids,
        buyer_email=payload.buyer_email,
        buyer_phone=payload.buyer_phone,
        allow_new_self_service_subscription=renewal,
        command_idempotency_key=payload.idempotency_key,
        renewed_from_subscription_id=payload.renewed_from_subscription_id,
        expected_target_tariff_id=payload.expected_target_tariff_id,
        expected_target_price=payload.expected_target_price,
    )
    return (
        200 if getattr(order, "_command_replayed", False) else 201
    ), _mark_bank_payment_order_cancel_scope([order], allowed_sources={"student"})[0]


@router.get("/me/bank-payment-orders/", response=list[BankPaymentOrderOut])
@role_required("student")
def my_bank_payment_orders(request, status: str | None = None):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    return _mark_bank_payment_order_cancel_scope(
        get_bank_payment_orders(club=request.club, student_id=student.id, status=status),
        allowed_sources={"student"},
    )


@router.get("/me/bank-payment-orders/{order_id}/", response=BankPaymentOrderOut)
@role_required("student", conceal_denial=True)
def my_bank_payment_order_detail(request, order_id: int):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    order = get_bank_payment_orders(club=request.club, student_id=student.id).filter(id=order_id).first()
    if order is None:
        raise HttpError(404, "Not found")
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"student"})[0]


@router.post("/me/bank-payment-orders/{order_id}/refresh/", response=BankPaymentOrderOut)
@role_required("student", conceal_denial=True)
def refresh_my_bank_payment_order(request, order_id: int):
    """Coalesce a local status-refresh request without direct provider I/O."""

    student = get_student_by_user(club=request.club, user_id=request.user.id)
    order = get_bank_payment_orders(club=request.club, student_id=student.id).filter(
        id=order_id,
    ).first()
    if order is None:
        raise HttpError(404, "Not found")
    from apps.billing.service_modules.provider_events import request_provider_reconciliation

    request_provider_reconciliation(
        club_id=request.club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    order.refresh_from_db()
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"student"})[0]


@router.post("/me/bank-payment-orders/{order_id}/cancel/", response=BankPaymentOrderOut)
@role_required("student", conceal_denial=True)
def cancel_my_bank_payment_order(request, order_id: int):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    if not get_bank_payment_orders(club=request.club, student_id=student.id).filter(
        id=order_id,
        source="student",
    ).exists():
        raise HttpError(404, "Not found")
    order = cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=order_id,
        actor_user_id=request.user.id,
        allowed_student_id=student.id,
        allowed_sources={"student"},
    )
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"student"})[0]


@router.get("/me/schedule/", response=list[StudentScheduleItemOut])
@role_required("student")
def my_schedule(request):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    schedules = get_student_schedule(club=request.club, student_id=student.id)
    return [
        StudentScheduleItemOut(
            id=s.id,
            day_of_week=s.day_of_week,
            start_time=s.start_time.strftime("%H:%M"),
            end_time=s.end_time.strftime("%H:%M"),
            group_name=s.training_group.name if s.training_group_id else s.group_name,
            trainer_name=f"{s.trainer.first_name} {s.trainer.last_name}",
            location_name=s.location.name,
        )
        for s in schedules
    ]


@router.get("/me/schedule-week/", response=list[StudentWeekScheduleOut])
@role_required("student")
def my_schedule_week(request, week_start: date_cls):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    week_end = week_start + timedelta(days=6)
    return get_student_schedule_occurrences_for_range(
        club=request.club,
        student_id=student.id,
        date_from=week_start,
        date_to=week_end,
    )


@router.get("/me/attendance/", response=AttendanceSummaryOut)
@role_required("student")
def my_attendance(request, month: str | None = None, limit: int = 50, offset: int = 0):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    month_date = None
    if month:
        try:
            year, m = month.split("-")
            month_date = date_cls(int(year), int(m), 1)
        except (ValueError, TypeError):
            raise HttpError(400, "Invalid month format. Use YYYY-MM.")
    if month_date is None:
        if limit < 1 or limit > 200:
            raise HttpError(400, "limit must be between 1 and 200")
        if offset < 0:
            raise HttpError(400, "offset must be greater than or equal to 0")
    checkins = get_student_attendance(club=request.club, student_id=student.id, month=month_date)
    total_count = checkins.count()
    page = checkins if month_date is not None else checkins[offset : offset + limit]
    items = [
        StudentAttendanceOut(
            id=c.id,
            date=c.date,
            group_name=c.schedule.group_name,
            trainer_name=f"{c.trainer.first_name} {c.trainer.last_name}",
            location_name=c.schedule.location.name,
            training_type_name=c.training_type.name,
            start_time=c.schedule.start_time.strftime("%H:%M"),
        )
        for c in page
    ]
    return {"attended_count": total_count, "items": items}


@router.get("/me/feedback/form/", response={200: FormOut | None})
@role_required("student")
def my_feedback_form(request):
    get_student_by_user(club=request.club, user_id=request.user.id)
    return 200, get_active_form(club=request.club)


@router.post(
    "/me/feedback/submit/",
    response={200: SelfServiceResponseOut, 201: SelfServiceResponseOut},
)
@role_required("student")
def submit_my_feedback(request, payload: SubmitSelfResponseIn):
    student = get_student_by_user(club=request.club, user_id=request.user.id)
    response = submit_feedback_response(
        club_id=request.club.id,
        form_id=payload.form_id,
        student_id=student.id,
        answers=[
            {
                "question_id": answer.question_id,
                "rating_value": answer.rating_value,
                "bool_value": answer.bool_value,
                "text_value": answer.text_value,
            }
            for answer in payload.answers
        ],
    )
    already_submitted = not getattr(response, "_created", True)
    response.already_submitted = already_submitted
    return (200 if already_submitted else 201), response


# ──────────────────────────────────────────────
# Staff endpoints
# ──────────────────────────────────────────────


@router.get("/intakes/capability", response=StudentIntakeCapabilityOut)
@role_required("owner", "admin", "trainer")
def student_intake_capability_endpoint(request):
    """Expose the dual rollout gate without trusting client-side state."""

    capability = get_commercial_journey_capability(club=request.club)
    return {
        "enabled": capability.unified_client_journey_enabled,
        "group_sale_command_protocol_version": capability.protocol_version,
    }


@router.post("/intakes/", response={200: StudentIntakeOut, 201: StudentIntakeOut, 409: StudentIntakeOut})
@role_required("owner", "admin", "trainer")
def submit_student_intake_endpoint(request, payload: StudentIntakeIn):
    if not is_unified_client_journey_enabled(club=request.club):
        raise BusinessLogicError(
            "Unified client journey is disabled for this club",
            code="unified_client_journey_disabled",
        )
    actor_trainer_id = (
        _get_current_trainer_id(request)
        if request._membership.role == ClubMembership.Role.TRAINER
        else None
    )
    result = submit_student_intake(
        club_id=request.club.id,
        actor_user_id=request.user.id,
        actor_role=request._membership.role,
        actor_trainer_id=actor_trainer_id,
        idempotency_key=payload.idempotency_key,
        intake_kind=payload.intake_kind,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
        guardian_phone=payload.guardian_phone,
        date_of_birth=payload.date_of_birth,
        is_child=payload.is_child,
        source=payload.source,
        assigned_trainer_id=payload.assigned_trainer_id,
        confirm_distinct_child=payload.confirm_distinct_child,
    )
    if result.is_conflict:
        return 409, result.as_receipt()
    return (200 if result.replayed else 201), result.as_receipt()


@router.post("/intakes/{student_id}/restore", response=StudentIntakeOut)
@role_required("owner", "admin")
def restore_soft_deleted_person_endpoint(request, student_id: int):
    if not is_unified_client_journey_enabled(club=request.club):
        raise BusinessLogicError(
            "Unified client journey is disabled for this club",
            code="unified_client_journey_disabled",
        )
    return restore_soft_deleted_person(
        club_id=request.club.id,
        student_id=student_id,
        actor_user_id=request.user.id,
    ).as_receipt()


@router.get("/search/", response=list[PersonSearchOut])
@role_required("owner", "admin", "trainer")
def search_people_endpoint(request, q: str, limit: int = 20):
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Person search is unavailable")
    if limit < 1 or limit > 50:
        raise HttpError(400, "limit must be between 1 and 50")
    actor_trainer_id = (
        _get_current_trainer_id(request)
        if request._membership.role == ClubMembership.Role.TRAINER
        else None
    )
    return search_people(
        club=request.club,
        query=q,
        actor_role=request._membership.role,
        actor_trainer_id=actor_trainer_id,
        limit=limit,
    )


@router.get("/{student_id}/commercial-context/", response=StudentCommercialContextOut)
@role_required("owner", "admin", "trainer")
def get_student_commercial_context_endpoint(request, student_id: int):
    _assert_trainer_student_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    trainer_id = _get_current_trainer_id(request) if request._membership.role == "trainer" else None
    return {
        "student_id": student_id,
        "attempts": get_personal_commercial_context(
            club_id=request.club.id,
            student_id=student_id,
            trainer_id=trainer_id,
            actor_role=request._membership.role,
        ),
    }


@router.post(
    "/{student_id}/personal-commercial-attempts/replace-payment-method/",
    response=PersonalCommercialReceiptOut,
)
@role_required("owner", "admin", "trainer")
def replace_student_personal_payment_method_endpoint(
    request,
    student_id: int,
    payload: PersonalPaymentMethodCorrectionIn,
):
    """Append a server-authorized correction; never edit a payment in place."""

    _assert_trainer_student_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    try:
        return replace_personal_payment_method(
            club_id=request.club.id,
            student_id=student_id,
            actor_user_id=request.user.id,
            actor_role=request._membership.role,
            reservation_id=payload.reservation_id,
            payment_id=payload.payment_id,
            replacement_payment_method=payload.replacement_payment_method,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except BusinessLogicError as exc:
        if exc.code in {
            "personal_payment_reservation_not_found",
            "personal_payment_not_found",
        }:
            raise HttpError(404, "Not found")
        raise HttpError(409, exc.message)


@router.get("/", response=list[StudentOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_students(
    request,
    status: str | None = None,
    q: str | None = None,
    workspace: str | None = None,
    commercial_segment: str | None = None,
):
    unified_enabled = is_unified_client_journey_enabled(club=request.club)
    if unified_enabled:
        if workspace not in {None, "students"}:
            raise HttpError(400, "Invalid student workspace")
        qs = with_commercial_segment(
            queryset=get_student_workspace(club=request.club),
            club=request.club,
        )
        if commercial_segment:
            if commercial_segment not in COMMERCIAL_SEGMENTS:
                raise HttpError(400, "Invalid commercial segment")
            qs = filter_by_commercial_segment(
                queryset=qs,
                commercial_segment=commercial_segment,
            )
    else:
        qs = get_students(club=request.club)
    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = _get_current_trainer_id(request)
        qs = qs.filter(
            trainer_student_scope_filter(trainer_id)
            | trainer_manual_operational_admission_scope_filter(
                club=request.club,
                trainer_id=trainer_id,
                user_id=request.user.id,
            )
        ).distinct()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = filter_students_by_query(queryset=qs, query=q)
    return qs


@router.post("/", response={201: StudentOut, 409: dict})
@role_required("owner", "trainer")
def create_student_endpoint(request, payload: StudentIn):
    assigned_trainer_id = None
    if request._membership.role == ClubMembership.Role.TRAINER:
        assigned_trainer_id = _get_current_trainer_id(request)
    try:
        student = create_student(
            club_id=request.club.id,
            assigned_trainer_id=assigned_trainer_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone=payload.phone,
            guardian_phone=payload.guardian_phone,
            email=payload.email,
            date_of_birth=payload.date_of_birth,
            is_child=payload.is_child,
            source=payload.source,
        )
    except DuplicateStudentError as exc:
        return _student_duplicate_response(request, exc)
    return 201, student


@router.post("/import/", response=ImportResultOut)
@role_required("owner")
def import_students_endpoint(request, file: UploadedFile = File(...)):
    result = import_students_from_excel(club_id=request.club.id, file=file)
    return result


@router.get("/{student_id}/", response=StudentDetailOut)
@role_required("owner", "admin", "trainer")
def get_student_detail_endpoint(request, student_id: int):
    if not _detail_read_actor_is_scoped(request, student_id=student_id):
        raise HttpError(403, "Access denied: not your student")
    student = get_student_detail(club=request.club, student_id=student_id)
    can_manage_account_access = _account_access_actor_is_scoped(request, student_id=student_id)
    can_manage_sensitive_actions = _sensitive_action_actor_is_scoped(request, student_id=student_id)
    student._can_manage_sensitive_actions = can_manage_sensitive_actions
    student._can_manage_account_access = can_manage_account_access
    student._can_manage_feedback = can_manage_sensitive_actions
    if not can_manage_account_access:
        student._hide_account_access = True
    return student


@router.post("/{student_id}/account-access/open/", response={200: AccountAccessIssueOut, 201: AccountAccessIssueOut})
@role_required("owner", "admin", "trainer")
def open_account_access_endpoint(request, student_id: int, payload: AccountAccessOpenIn):
    _assert_account_access_actor_scope(request, student_id=student_id)
    result = open_account_access_for_student(
        club_id=request.club.id,
        student_id=student_id,
        parent_phone=payload.parent_phone,
        issued_by_id=request.user.id,
    )
    return (201 if result.created_access else 200), _account_access_issue_out(result)


@router.post("/{student_id}/account-access/reset/", response=AccountAccessIssueOut)
@role_required("owner", "admin", "trainer")
def reset_account_access_endpoint(request, student_id: int):
    _assert_account_access_actor_scope(request, student_id=student_id)
    result = reset_account_access_for_student(
        club_id=request.club.id,
        student_id=student_id,
        reset_by_id=request.user.id,
    )
    return _account_access_issue_out(result)


@router.get("/{student_id}/personal-bookings/", response=list[StudentPersonalBookingOut])
@role_required("owner", "admin", "trainer")
def list_personal_bookings_endpoint(request, student_id: int):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    current_trainer_id = (
        _get_current_trainer_id(request)
        if request._membership.role == ClubMembership.Role.TRAINER
        else None
    )
    return [
        _student_personal_booking_out(enrollment, current_trainer_id=current_trainer_id)
        for enrollment in get_student_upcoming_personal_bookings(
            club=request.club,
            student_id=student_id,
        )
    ]


@router.post("/{student_id}/personal-bookings/", response={200: PersonalBookingOut, 201: PersonalBookingOut})
@role_required("owner", "admin", "trainer")
def create_personal_booking_endpoint(request, student_id: int, payload: PersonalBookingIn):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    if is_unified_client_journey_enabled(club=request.club):
        raise BusinessLogicError(
            "Use the unified personal self-service command.",
            code="unified_personal_command_required",
        )
    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = _get_current_trainer_id(request)
    else:
        if payload.trainer_id is None:
            raise HttpError(400, "trainer_id is required")
        trainer_id = payload.trainer_id

    result = book_personal_session(
        club_id=request.club.id,
        student_id=student_id,
        trainer_id=trainer_id,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        subscription_id=payload.subscription_id,
        actor_user_id=request.user.id,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _personal_booking_out(result)


@router.get("/{student_id}/personal-drop-in-bookings/", response=list[PersonalDropInBookingOut])
@role_required("owner", "admin", "trainer")
def list_personal_drop_in_bookings_endpoint(request, student_id: int):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    bookings = (
        PersonalDropInBooking.objects.for_club(request.club)
        .select_related(
            "enrollment__student",
            "enrollment__schedule__trainer",
            "enrollment__schedule__location",
            "enrollment__schedule__training_type",
            "debt",
        )
        .filter(enrollment__student_id=student_id)
        .order_by("-enrollment__schedule__one_time_date", "-id")
    )
    return [_personal_drop_in_booking_out(booking, created=False) for booking in bookings]


@router.post(
    "/{student_id}/personal-drop-in-bookings/",
    response={200: PersonalDropInBookingOut, 201: PersonalDropInBookingOut},
)
@role_required("owner", "admin", "trainer")
def create_personal_drop_in_booking_endpoint(
    request,
    student_id: int,
    payload: PersonalDropInBookingIn,
):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = _get_current_trainer_id(request)
    else:
        if payload.trainer_id is None:
            raise HttpError(400, "trainer_id is required")
        trainer_id = payload.trainer_id
    result = book_personal_drop_in(
        club_id=request.club.id,
        student_id=student_id,
        trainer_id=trainer_id,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        tariff_id=payload.tariff_id,
        actor_user_id=request.user.id,
        availability_slot_id=payload.availability_slot_id,
        offer_digest=payload.offer_digest,
        idempotency_key=payload.idempotency_key,
    )
    return (201 if result.created else 200), _personal_drop_in_booking_out(
        result.booking,
        created=result.created,
    )


def _bank_payment_source_for_membership_role(role: str) -> str:
    if role == ClubMembership.Role.TRAINER:
        return "trainer"
    if role == ClubMembership.Role.ADMIN:
        return "admin"
    return "owner"


def _personal_payment_allowed_sources_for_role(role: str) -> set[str] | None:
    if role == ClubMembership.Role.TRAINER:
        return {_bank_payment_source_for_membership_role(role)}
    return None


@router.get(
    "/{student_id}/personal-booking-payment-reservations/",
    response=list[PersonalBookingPaymentReservationOut],
)
@role_required("owner", "admin", "trainer")
def list_personal_booking_payment_reservations_endpoint(
    request,
    student_id: int,
    status: str | None = None,
):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    current_trainer_id = (
        _get_current_trainer_id(request)
        if request._membership.role == ClubMembership.Role.TRAINER
        else None
    )
    return [
        _personal_booking_payment_reservation_out(reservation)
        for reservation in get_personal_booking_payment_reservations(
            club_id=request.club.id,
            student_id=student_id,
            status=status,
            allowed_sources=_personal_payment_allowed_sources_for_role(
                request._membership.role,
            ),
            allowed_trainer_id=current_trainer_id,
        )
    ]


@router.post(
    "/{student_id}/personal-booking-payment-reservations/",
    response={200: PersonalBookingPaymentReservationOut, 201: PersonalBookingPaymentReservationOut},
)
@role_required("owner", "admin", "trainer")
def create_personal_booking_payment_reservation_endpoint(
    request,
    student_id: int,
    payload: PersonalBookingPaymentReservationCreateIn,
):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = _get_current_trainer_id(request)
    else:
        if payload.trainer_id is None:
            raise HttpError(400, "trainer_id is required")
        trainer_id = payload.trainer_id
    reservation = create_personal_booking_payment_reservation(
        club_id=request.club.id,
        student_id=student_id,
        trainer_id=trainer_id,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location_id=payload.location_id,
        training_type_id=payload.training_type_id,
        tariff_id=payload.tariff_id,
        availability_slot_id=payload.availability_slot_id,
        offer_digest=payload.offer_digest,
        created_by_id=request.user.id,
        source=_bank_payment_source_for_membership_role(request._membership.role),
        idempotency_key=payload.idempotency_key,
        command_idempotency_key=payload.idempotency_key,
    )
    return 201, _personal_booking_payment_reservation_out(reservation)


@router.post(
    "/{student_id}/personal-booking-payment-reservations/{reservation_id}/cancel/",
    response=PersonalBookingPaymentReservationOut,
)
@role_required("owner", "admin", "trainer")
def cancel_personal_booking_payment_reservation_endpoint(request, student_id: int, reservation_id: int):
    _assert_personal_booking_actor_scope(request, student_id=student_id)
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .select_related("bank_payment_order")
        .filter(id=reservation_id, student_id=student_id)
        .first()
    )
    if reservation is None:
        raise HttpError(404, "Reservation not found")
    if (
        request._membership.role == ClubMembership.Role.TRAINER
        and reservation.trainer_id != _get_current_trainer_id(request)
    ):
        raise HttpError(403, "Reservation belongs to another trainer")
    if reservation.bank_payment_order_id is None:
        raise HttpError(400, "Reservation has no payment order")
    cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=reservation.bank_payment_order_id,
        actor_user_id=request.user.id,
        allowed_student_id=student_id,
        allowed_sources=_personal_payment_allowed_sources_for_role(
            request._membership.role,
        ),
    )
    reservation.refresh_from_db()
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
        .get(id=reservation.id)
    )
    return _personal_booking_payment_reservation_out(reservation)


@router.put("/{student_id}/", response=StudentOut)
@role_required("owner", "trainer")
def update_student_endpoint(request, student_id: int, payload: StudentUpdate):
    _assert_trainer_student_actor_scope(request, student_id=student_id)
    fields = {}
    for field_name in (
        "first_name",
        "last_name",
        "phone",
        "guardian_phone",
        "email",
        "date_of_birth",
        "is_child",
        "source",
        "contraindications",
    ):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    return update_student(
        student_id=student_id,
        club_id=request.club.id,
        **fields,
    )


@router.post("/{student_id}/transition/", response=StudentOut)
@role_required("owner", "admin")
def transition_status_endpoint(request, student_id: int, payload: StatusTransitionIn):
    return transition_status(
        student_id=student_id,
        club_id=request.club.id,
        new_status=payload.new_status,
        actor_user_id=request.user.id,
        source="students_api",
    )


@router.delete("/{student_id}/", response={204: None})
@role_required("owner")
def delete_student_endpoint(request, student_id: int):
    delete_student(student_id=student_id, club_id=request.club.id)
    return 204, None


@router.get("/{student_id}/notes/", response=list[StudentNoteOut])
@role_required("owner", "admin", "trainer")
def list_notes(request, student_id: int):
    _assert_trainer_student_actor_scope(request, student_id=student_id)
    get_student_by_id(club=request.club, student_id=student_id)
    return list(get_student_notes(club=request.club, student_id=student_id))


@router.post("/{student_id}/notes/", response={201: StudentNoteOut})
@role_required("owner", "admin", "trainer")
def add_note_endpoint(request, student_id: int, payload: StudentNoteIn):
    _assert_trainer_student_actor_scope(request, student_id=student_id)
    note = add_student_note(
        club_id=request.club.id,
        student_id=student_id,
        author_id=request.user.id,
        text=payload.text,
    )
    return 201, note


@router.get("/{student_id}/checkins/", response=list[StudentAttendanceOut])
@role_required("owner", "admin", "trainer")
def student_checkins(request, student_id: int, limit: int = 10, offset: int = 0):
    _assert_trainer_student_actor_scope(request, student_id=student_id)
    student = get_student_by_id(club=request.club, student_id=student_id)
    checkins = get_student_attendance(club=request.club, student_id=student.id)
    return [
        StudentAttendanceOut(
            id=c.id,
            date=c.date,
            group_name=c.schedule.group_name,
            trainer_name=f"{c.trainer.first_name} {c.trainer.last_name}",
            location_name=c.schedule.location.name,
            training_type_name=c.training_type.name,
            start_time=c.schedule.start_time.strftime("%H:%M"),
        )
        for c in checkins[offset : offset + limit]
    ]
