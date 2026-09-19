from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date as date_type
from datetime import datetime as datetime_type
from datetime import time as time_type
from datetime import timedelta
from datetime import tzinfo as tzinfo_type

from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, QuerySet
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    Schedule,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    is_complete_personal_terms,
)
from apps.attendance.personal_offers import personal_offer_payload
from apps.billing.models import Debt, Payment, Subscription, Tariff, TrainingType
from apps.billing.selectors import current_active_subscription_q
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import Club
from apps.clubs.timezones import club_local_day_start, club_localdate, club_localtime, club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.grades.models import StudentGrade
from apps.students.models import Student
from apps.trainers.models import TrainerPayrollPeriodClose

# ──────────────────────────────────────────────
# Student self-service selectors
# ──────────────────────────────────────────────

OPEN_SCHEDULE_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.ACTIVE,
    ScheduleEnrollment.Status.TRIAL,
    ScheduleEnrollment.Status.FROZEN,
)
TERMINAL_SCHEDULE_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.TRANSFERRED,
    ScheduleEnrollment.Status.CANCELLED,
)
KIOSK_ELIGIBLE_STUDENT_STATUSES = (
    Student.Status.ACTIVE,
    Student.Status.TRIAL,
    Student.Status.AT_RISK,
    Student.Status.CHURNED,
)
KIOSK_GUEST_BOOKING_STUDENT_STATUSES = (
    Student.Status.ACTIVE,
    Student.Status.TRIAL,
)
KIOSK_CHECKIN_OPENS_BEFORE = timedelta(minutes=30)

TRAINER_AVAILABILITY_VISIBLE_STATUSES = (
    PersonalAvailabilitySlot.Status.PUBLISHED,
    PersonalAvailabilitySlot.Status.HELD,
    PersonalAvailabilitySlot.Status.BOOKED,
    PersonalAvailabilitySlot.Status.BLOCKED,
    PersonalAvailabilitySlot.Status.CANCELLED,
)
DATED_BOOKING_CREATED_FROM = (
    ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
    ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
    ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
    ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
)


def _kiosk_checkin_window(
    *,
    club,
    effective_date: date_type,
    effective_start_time: time_type,
    effective_end_time: time_type,
    current_time: datetime_type,
) -> dict:
    club_tz = club_zoneinfo(club)
    starts_at = timezone.make_aware(
        datetime_type.combine(effective_date, effective_start_time),
        club_tz,
    )
    closes_at = timezone.make_aware(
        datetime_type.combine(effective_date, effective_end_time),
        club_tz,
    )
    if closes_at <= starts_at:
        closes_at += timedelta(days=1)

    opens_at = starts_at - KIOSK_CHECKIN_OPENS_BEFORE
    local_current_time = club_localtime(club, current_time)
    if local_current_time < opens_at:
        status = "too_early"
    elif local_current_time <= closes_at:
        status = "open"
    else:
        status = "closed"

    return {
        "checkin_window_status": status,
        "checkin_opens_at": opens_at,
        "checkin_closes_at": closes_at,
    }


def _local_day_bounds(target_date: date_type, *, club_tz: tzinfo_type | None = None):
    tz = club_tz or timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime_type.combine(target_date, time_type.min),
        tz,
    )
    return day_start, day_start + timedelta(days=1)


def _cancelled_dated_booking_q() -> Q:
    return Q(
        status=ScheduleEnrollment.Status.CANCELLED,
        starts_on__isnull=False,
        ends_on__isnull=False,
        starts_on=F("ends_on"),
        created_from__in=DATED_BOOKING_CREATED_FROM,
    )


def _is_cancelled_dated_booking(enrollment: ScheduleEnrollment) -> bool:
    return (
        enrollment.status == ScheduleEnrollment.Status.CANCELLED
        and enrollment.starts_on is not None
        and enrollment.starts_on == enrollment.ends_on
        and enrollment.created_from in DATED_BOOKING_CREATED_FROM
    )


def schedule_enrollment_active_on_date_q(target_date: date_type) -> Q:
    """Return the canonical effective-membership predicate for one local date."""
    return (
        (
            Q(status__in=OPEN_SCHEDULE_ENROLLMENT_STATUSES)
            | Q(
                status__in=TERMINAL_SCHEDULE_ENROLLMENT_STATUSES,
                ends_on__isnull=False,
                ends_on__gte=target_date,
            )
        )
        & (Q(starts_on__isnull=True) | Q(starts_on__lte=target_date))
        & (Q(ends_on__isnull=True) | Q(ends_on__gte=target_date))
        & ~_cancelled_dated_booking_q()
        # A rejected payment keeps this terminal row only as financial and
        # attendance provenance. It must never restore roster eligibility.
        & ~Q(
            status__in=TERMINAL_SCHEDULE_ENROLLMENT_STATUSES,
            created_from__in=[
                ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
                ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            ],
        )
    )


def _open_or_upcoming_enrollment_q(target_date: date_type) -> Q:
    return (
        (
            Q(status__in=OPEN_SCHEDULE_ENROLLMENT_STATUSES)
            | Q(
                status__in=TERMINAL_SCHEDULE_ENROLLMENT_STATUSES,
                ends_on__isnull=False,
                ends_on__gte=target_date,
            )
        )
        & (Q(ends_on__isnull=True) | Q(ends_on__gte=target_date))
        & ~_cancelled_dated_booking_q()
        & ~Q(
            status__in=TERMINAL_SCHEDULE_ENROLLMENT_STATUSES,
            created_from__in=[
                ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
                ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            ],
        )
    )


def _enrollment_matches_date(*, enrollment: ScheduleEnrollment, target_date: date_type) -> bool:
    if _is_cancelled_dated_booking(enrollment):
        return False
    if (
        enrollment.status in TERMINAL_SCHEDULE_ENROLLMENT_STATUSES
        and enrollment.created_from
        in {
            ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        }
    ):
        return False
    if enrollment.status not in (
        *OPEN_SCHEDULE_ENROLLMENT_STATUSES,
        *TERMINAL_SCHEDULE_ENROLLMENT_STATUSES,
    ):
        return False
    if enrollment.status in TERMINAL_SCHEDULE_ENROLLMENT_STATUSES and not enrollment.ends_on:
        return False
    if enrollment.starts_on and enrollment.starts_on > target_date:
        return False
    if enrollment.ends_on and enrollment.ends_on < target_date:
        return False
    return True


def _enrollment_can_cancel_booking(*, enrollment: ScheduleEnrollment, target_date: date_type) -> bool:
    if target_date < date_type.today():
        return False
    if enrollment.status not in OPEN_SCHEDULE_ENROLLMENT_STATUSES:
        return False
    if enrollment.starts_on is None or enrollment.ends_on is None:
        return False
    if enrollment.starts_on != enrollment.ends_on:
        return False
    return enrollment.created_from in (
        ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    )


def _subscription_active_on_date_q(
    target_date: date_type,
    *,
    club_tz: tzinfo_type | None = None,
) -> Q:
    day_start, _ = _local_day_bounds(target_date, club_tz=club_tz)
    return Q(expires_at__isnull=True) | Q(expires_at__gt=day_start)


def get_locked_pending_manual_admission_payment(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    target_date: date_type,
) -> Payment | None:
    """Return the single payment-owned pending admission valid for this visit.

    The payment, subscription, and owned enrollment are locked together because
    the caller may create an auditable reservation immediately afterwards.
    """
    target_group_id = (
        Schedule.objects.for_club(club_id)
        .filter(id=schedule_id)
        .values_list("training_group_id", flat=True)
        .first()
    )
    exact_target = Q(
        conversion_enrollment__student_id=student_id,
        conversion_enrollment__schedule_id=schedule_id,
        target_schedule_id=schedule_id,
    )
    if target_group_id is not None:
        exact_target |= Q(
            conversion_group_membership__isnull=False,
            conversion_group_membership__training_group_id=target_group_id,
            target_training_group_id=target_group_id,
        )
    candidates = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self", "subscription", "conversion_enrollment"))
        .select_related(
            "subscription__tariff",
            "conversion_enrollment",
            "target_schedule",
            "conversion_group_membership__training_group",
        )
        .filter(
            student_id=student_id,
            payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            status=Payment.Status.PENDING,
            subscription__status=Subscription.Status.PENDING,
            subscription__deleted_at__isnull=True,
            conversion_enrollment__student_id=student_id,
            conversion_enrollment__status=ScheduleEnrollment.Status.ACTIVE,
            conversion_enrollment__starts_on=F("target_start_date"),
            target_start_date__lte=target_date,
            target_training_type_id_snapshot=F("target_schedule__training_type_id"),
            target_location_id_snapshot=F("target_schedule__location_id"),
        )
        .filter(exact_target)
        .order_by("id")
    )
    club = Club.objects.only("id", "timezone").get(id=club_id)
    valid_payments: list[Payment] = []
    for payment in candidates:
        if payment.target_start_date is None:
            continue
        expires_at = club_local_day_start(
            club,
            payment.target_start_date + timedelta(days=payment.subscription.tariff.duration_days),
        )
        if club_local_day_start(club, target_date) < expires_at:
            valid_payments.append(payment)
    # An ambiguous admission must never silently choose an arbitrary payment:
    # the check-in caller will fail closed instead of creating a generic debt.
    return valid_payments[0] if len(valid_payments) == 1 else None


def _active_subscription_by_training_location(
    *,
    club,
    student_id: int,
    training_type_ids: set[int],
    location_ids: set[int],
    target_date: date_type,
) -> dict[tuple[int, int | None], Subscription]:
    if not training_type_ids:
        return {}
    club_tz = club_zoneinfo(club)

    scope_filter = Q(scope=Tariff.Scope.CLUB)
    if location_ids:
        scope_filter |= Q(scope=Tariff.Scope.LOCATION, location_id__in=location_ids)

    subscriptions_by_key: dict[tuple[int, int | None], Subscription] = {}
    for subscription in (
        Subscription.objects.for_club(club)
        .filter(
            student_id=student_id,
            status=Subscription.Status.ACTIVE,
            tariff__training_type_id__in=training_type_ids,
            deleted_at__isnull=True,
        )
        .filter(_subscription_active_on_date_q(target_date, club_tz=club_tz))
        .filter(Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
        .filter(scope_filter)
        .select_related("tariff")
        .order_by("expires_at", "id")
    ):
        location_key = subscription.location_id if subscription.scope == Tariff.Scope.LOCATION else None
        subscriptions_by_key.setdefault(
            (subscription.tariff.training_type_id, location_key),
            subscription,
        )
    return subscriptions_by_key


def _scoped_subscription(
    subscriptions_by_key: dict[tuple[int, int | None], Subscription],
    *,
    training_type_id: int,
    location_id: int,
) -> Subscription | None:
    return subscriptions_by_key.get((training_type_id, location_id)) or subscriptions_by_key.get(
        (training_type_id, None)
    )


def _guest_financial_payload(
    *,
    student: Student,
    training_type: TrainingType,
    subscription: Subscription | None,
) -> dict:
    if subscription is not None:
        return {
            "financial_status": "subscription",
            "subscription_id": subscription.id,
            "drop_in_price": None,
            "reason_code": "",
        }
    if student.status == Student.Status.TRIAL and training_type.trial_free:
        return {
            "financial_status": "trial_free",
            "subscription_id": None,
            "drop_in_price": None,
            "reason_code": "",
        }
    if training_type.drop_in_price is not None:
        return {
            "financial_status": "drop_in_debt",
            "subscription_id": None,
            "drop_in_price": str(training_type.drop_in_price),
            "reason_code": "",
        }
    return {
        "financial_status": "blocked",
        "subscription_id": None,
        "drop_in_price": None,
        "reason_code": "drop_in_price_required",
    }


def _guest_booking_financial_eligibilities(
    *,
    club,
    student: Student,
    pairs: list[tuple[int, int]],
    target_date: date_type,
) -> dict[tuple[int, int], dict]:
    unique_pairs = list(dict.fromkeys(pairs))
    training_type_ids = {training_type_id for training_type_id, _ in unique_pairs}
    location_ids = {location_id for _, location_id in unique_pairs}
    training_types_by_id = {
        training_type.id: training_type
        for training_type in TrainingType.objects.for_club(club).filter(id__in=training_type_ids)
    }
    subscriptions_by_key = _active_subscription_by_training_location(
        club=club,
        student_id=student.id,
        training_type_ids=training_type_ids,
        location_ids=location_ids,
        target_date=target_date,
    )

    result: dict[tuple[int, int], dict] = {}
    for training_type_id, location_id in unique_pairs:
        training_type = training_types_by_id.get(training_type_id)
        if training_type is None:
            continue
        subscription = _scoped_subscription(
            subscriptions_by_key,
            training_type_id=training_type_id,
            location_id=location_id,
        )
        result[(training_type_id, location_id)] = _guest_financial_payload(
            student=student,
            training_type=training_type,
            subscription=subscription,
        )
    return result


def get_expected_student_ids_by_schedule_date(
    *,
    club,
    schedule_ids: list[int],
    target_date: date_type,
    legacy_window_days: int = 30,
    legacy_min_checkins: int = 1,
) -> dict[int, set[int]]:
    """Return the canonical expected roster IDs for concrete occurrences."""
    from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date

    roster = resolve_expected_roster_by_schedule_date(
        club=club,
        schedule_ids=schedule_ids,
        target_date=target_date,
        legacy_window_days=legacy_window_days,
        legacy_min_checkins=legacy_min_checkins,
    )
    return {schedule_id: set(entries) for schedule_id, entries in roster.items()}


def get_expected_student_ids_for_schedule_date(
    *,
    club,
    schedule_id: int,
    target_date: date_type,
    legacy_window_days: int = 30,
    legacy_min_checkins: int = 1,
) -> set[int]:
    expected_by_schedule = get_expected_student_ids_by_schedule_date(
        club=club,
        schedule_ids=[schedule_id],
        target_date=target_date,
        legacy_window_days=legacy_window_days,
        legacy_min_checkins=legacy_min_checkins,
    )
    return expected_by_schedule.get(schedule_id, set())


def get_guest_booking_financial_eligibility(
    *,
    club,
    student_id: int,
    training_type_id: int,
    location_id: int,
    target_date: date_type,
) -> dict:
    student = Student.objects.for_club(club).get(id=student_id, deleted_at__isnull=True)
    eligibility = _guest_booking_financial_eligibilities(
        club=club,
        student=student,
        pairs=[(training_type_id, location_id)],
        target_date=target_date,
    )
    if (training_type_id, location_id) not in eligibility:
        TrainingType.objects.for_club(club).get(id=training_type_id)
    return eligibility[(training_type_id, location_id)]


def get_kiosk_checkin_options(
    *,
    club,
    student_id: int,
    target_date: date_type,
    current_time: datetime_type | None = None,
) -> dict:
    from apps.common.exceptions import BusinessLogicError

    student = Student.objects.for_club(club).filter(id=student_id, deleted_at__isnull=True).first()
    exact_drop_ins_by_schedule: dict[int, PersonalDropInBooking] = {}
    if student is not None and student.status == Student.Status.LEAD:
        exact_drop_ins = list(
            PersonalDropInBooking.objects.for_club(club)
            .select_related("enrollment", "enrollment__schedule", "tariff")
            .filter(
                enrollment__student_id=student.id,
                enrollment__starts_on=target_date,
                enrollment__ends_on=target_date,
                state=PersonalDropInBooking.State.SCHEDULED,
            )
            .order_by("id")
        )
        exact_drop_ins_by_schedule = {booking.enrollment.schedule_id: booking for booking in exact_drop_ins}
    if student is None or (student.status not in KIOSK_ELIGIBLE_STUDENT_STATUSES and not exact_drop_ins_by_schedule):
        raise BusinessLogicError(
            "Ученик недоступен для kiosk check-in",
            code="student_ineligible",
        )

    occurrences = [
        occurrence
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=target_date,
        )
        if occurrence.training_type_id is not None
    ]
    if exact_drop_ins_by_schedule:
        occurrences = [occurrence for occurrence in occurrences if occurrence.schedule_id in exact_drop_ins_by_schedule]
    schedule_ids = [occurrence.schedule_id for occurrence in occurrences]
    expected_by_schedule = get_expected_student_ids_by_schedule_date(
        club=club,
        schedule_ids=schedule_ids,
        target_date=target_date,
    )
    frozen_schedule_ids = set(
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            student_id=student.id,
            schedule_id__in=schedule_ids,
            status=ScheduleEnrollment.Status.FROZEN,
        )
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=target_date))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=target_date))
        .values_list("schedule_id", flat=True)
    )
    closed_schedule_ids = set(
        GroupSession.objects.for_club(club)
        .filter(schedule_id__in=schedule_ids, date=target_date, closed_at__isnull=False)
        .values_list("schedule_id", flat=True)
    )
    existing_checkin_by_schedule = {
        row["schedule_id"]: row["id"]
        for row in (
            Checkin.objects.for_club(club)
            .filter(
                student_id=student.id,
                schedule_id__in=schedule_ids,
                date=target_date,
                deleted_at__isnull=True,
            )
            .values("schedule_id", "id")
        )
    }
    financial_by_pair = _guest_booking_financial_eligibilities(
        club=club,
        student=student,
        pairs=[
            (occurrence.training_type_id, occurrence.location_id)
            for occurrence in occurrences
            if occurrence.training_type_id is not None
        ],
        target_date=target_date,
    )

    resolved_current_time = current_time or timezone.now()
    options = []
    for occurrence in occurrences:
        financial = financial_by_pair[(occurrence.training_type_id, occurrence.location_id)]
        exact_drop_in = exact_drop_ins_by_schedule.get(occurrence.schedule_id)
        if exact_drop_in is not None:
            financial = {
                "financial_status": "drop_in_debt",
                "subscription_id": None,
                "drop_in_price": str(exact_drop_in.price_snapshot),
                "reason_code": "",
            }
        reason_code = financial["reason_code"]
        existing_checkin_id = existing_checkin_by_schedule.get(occurrence.schedule_id)
        if existing_checkin_id is not None:
            status = "blocked"
            reason_code = "already_checked_in"
        elif occurrence.schedule_id in frozen_schedule_ids:
            status = "blocked"
            reason_code = "enrollment_frozen"
        elif occurrence.schedule_id in closed_schedule_ids:
            status = "blocked"
            reason_code = "group_session_closed"
        elif student.id in expected_by_schedule.get(occurrence.schedule_id, set()):
            status = "can_checkin"
            if financial["financial_status"] == "blocked":
                status = "blocked"
        elif occurrence.training_type_kind != TrainingType.Kind.GROUP:
            status = "blocked"
            reason_code = "guest_visit_requires_group_schedule"
        elif occurrence.one_time_date is not None:
            status = "blocked"
            reason_code = "guest_visit_requires_recurring_schedule"
        elif student.status not in KIOSK_GUEST_BOOKING_STUDENT_STATUSES:
            status = "blocked"
            reason_code = "student_ineligible"
        elif financial["financial_status"] == "blocked":
            status = "blocked"
        else:
            status = "can_book_guest_visit"

        checkin_window = _kiosk_checkin_window(
            club=club,
            effective_date=occurrence.effective_date,
            effective_start_time=occurrence.effective_start_time,
            effective_end_time=occurrence.effective_end_time,
            current_time=resolved_current_time,
        )
        options.append(
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
                "self_checkin_status": status,
                "reason_code": reason_code,
                "financial_status": financial["financial_status"],
                "subscription_id": financial["subscription_id"],
                "drop_in_price": financial["drop_in_price"],
                "existing_checkin_id": existing_checkin_id,
                **checkin_window,
            }
        )

    return {
        "student_id": student.id,
        "date": target_date,
        "options": options,
    }


def get_self_service_guest_booking_options(
    *,
    club,
    student_id: int,
    target_date: date_type,
) -> list[dict]:
    from apps.common.exceptions import BusinessLogicError

    student = Student.objects.for_club(club).filter(id=student_id, deleted_at__isnull=True).first()
    if student is None:
        raise BusinessLogicError(
            "Student does not belong to this club",
            code="student_club_mismatch",
        )

    occurrences = [
        occurrence
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=target_date,
        )
        if occurrence.training_type_id is not None
        and occurrence.training_type_kind == TrainingType.Kind.GROUP
        and occurrence.one_time_date is None
    ]
    schedule_ids = [occurrence.schedule_id for occurrence in occurrences]
    expected_by_schedule = get_expected_student_ids_by_schedule_date(
        club=club,
        schedule_ids=schedule_ids,
        target_date=target_date,
    )
    closed_schedule_ids = set(
        GroupSession.objects.for_club(club)
        .filter(schedule_id__in=schedule_ids, date=target_date, closed_at__isnull=False)
        .values_list("schedule_id", flat=True)
    )
    existing_checkin_by_schedule = {
        row["schedule_id"]: row["id"]
        for row in (
            Checkin.objects.for_club(club)
            .filter(
                student_id=student.id,
                schedule_id__in=schedule_ids,
                date=target_date,
                deleted_at__isnull=True,
            )
            .values("schedule_id", "id")
        )
    }
    financial_by_pair = _guest_booking_financial_eligibilities(
        club=club,
        student=student,
        pairs=[
            (occurrence.training_type_id, occurrence.location_id)
            for occurrence in occurrences
            if occurrence.training_type_id is not None
        ],
        target_date=target_date,
    )

    club_tz = club_zoneinfo(club)
    now = timezone.now()
    options = []
    for occurrence in occurrences:
        financial = financial_by_pair[(occurrence.training_type_id, occurrence.location_id)]
        reason_code = financial["reason_code"]
        starts_at = timezone.make_aware(
            datetime_type.combine(occurrence.effective_date, occurrence.effective_start_time),
            club_tz,
        )
        if occurrence.schedule_id in existing_checkin_by_schedule:
            booking_status = "already_booked"
            reason_code = "already_checked_in"
        elif student.id in expected_by_schedule.get(occurrence.schedule_id, set()):
            booking_status = "already_booked"
            reason_code = "already_booked"
        elif occurrence.schedule_id in closed_schedule_ids:
            booking_status = "blocked"
            reason_code = "group_session_closed"
        elif starts_at <= now:
            booking_status = "blocked"
            reason_code = "self_booking_past_session"
        elif financial["financial_status"] == "blocked":
            booking_status = "blocked"
        else:
            booking_status = "can_book"

        options.append(
            {
                "schedule_id": occurrence.schedule_id,
                "date": occurrence.effective_date,
                "start_time": occurrence.effective_start_time,
                "end_time": occurrence.effective_end_time,
                "group_name": occurrence.group_name,
                "trainer_name": occurrence.trainer_name,
                "location_name": occurrence.location_name,
                "training_type_id": occurrence.training_type_id,
                "training_type_name": occurrence.training_type_name,
                "booking_status": booking_status,
                "reason_code": reason_code,
                "financial_status": financial["financial_status"],
                "subscription_id": financial["subscription_id"],
                "drop_in_price": financial["drop_in_price"],
            }
        )

    return options


def _get_personal_availability_subscription_id(
    *,
    club,
    student_id: int,
    training_type_id: int,
    location_id: int,
    target_date: date_type,
) -> int | None:
    club_tz = club_zoneinfo(club)
    qs = (
        Subscription.objects.for_club(club)
        .filter(
            student_id=student_id,
            status=Subscription.Status.ACTIVE,
            tariff__training_type_id=training_type_id,
            deleted_at__isnull=True,
        )
        .filter(_subscription_active_on_date_q(target_date, club_tz=club_tz))
        .filter(Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
        .filter(Q(scope=Tariff.Scope.CLUB) | Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
        .order_by("expires_at", "id")
    )
    subscription_id = (
        qs.filter(scope=Tariff.Scope.LOCATION, location_id=location_id).values_list("id", flat=True).first()
    )
    if subscription_id is not None:
        return subscription_id
    return qs.filter(scope=Tariff.Scope.CLUB).values_list("id", flat=True).first()


def _get_personal_availability_payment_tariff(
    *,
    club,
    training_type_id: int,
    location_id: int,
) -> Tariff | None:
    qs = (
        Tariff.objects.for_club(club)
        .filter(
            is_active=True,
            training_type_id=training_type_id,
        )
        .filter(Q(scope=Tariff.Scope.CLUB) | Q(scope=Tariff.Scope.LOCATION, location_id=location_id))
        .order_by(F("trainings_limit").asc(nulls_last=True), "duration_days", "price", "id")
    )
    location_tariff = qs.filter(scope=Tariff.Scope.LOCATION, location_id=location_id).first()
    if location_tariff is not None:
        return location_tariff
    return qs.filter(scope=Tariff.Scope.CLUB).first()


def _payment_tariff_by_training_location(
    *,
    club,
    training_type_ids: set[int],
    location_ids: set[int],
) -> dict[tuple[int, int | None], Tariff]:
    if not training_type_ids:
        return {}

    scope_filter = Q(scope=Tariff.Scope.CLUB)
    if location_ids:
        scope_filter |= Q(scope=Tariff.Scope.LOCATION, location_id__in=location_ids)

    tariffs_by_key: dict[tuple[int, int | None], Tariff] = {}
    for tariff in (
        Tariff.objects.for_club(club)
        .filter(is_active=True, training_type_id__in=training_type_ids)
        .filter(scope_filter)
        .select_related("training_type", "location")
        .order_by(F("trainings_limit").asc(nulls_last=True), "duration_days", "price", "id")
    ):
        location_key = tariff.location_id if tariff.scope == Tariff.Scope.LOCATION else None
        tariffs_by_key.setdefault((tariff.training_type_id, location_key), tariff)
    return tariffs_by_key


def _scoped_payment_tariff(
    tariffs_by_key: dict[tuple[int, int | None], Tariff],
    *,
    training_type_id: int,
    location_id: int,
) -> Tariff | None:
    return tariffs_by_key.get((training_type_id, location_id)) or tariffs_by_key.get((training_type_id, None))


def _slot_overlaps_schedule_occurrences(
    *,
    slot: PersonalAvailabilitySlot,
    occurrences: list[ScheduleOccurrence],
    club_tz: tzinfo_type,
) -> bool:
    local_starts_at = timezone.localtime(slot.starts_at, club_tz)
    local_ends_at = timezone.localtime(slot.ends_at, club_tz)
    return any(
        occurrence.effective_start_time < local_ends_at.time()
        and occurrence.effective_end_time > local_starts_at.time()
        for occurrence in occurrences
    )


def get_self_service_personal_availability_options(
    *,
    club,
    student_id: int,
    target_date: date_type,
) -> list[dict]:
    from apps.common.exceptions import BusinessLogicError

    student = Student.objects.for_club(club).filter(id=student_id, deleted_at__isnull=True).first()
    if student is None:
        raise BusinessLogicError(
            "Student does not belong to this club",
            code="student_club_mismatch",
        )

    club_tz = club_zoneinfo(club)
    day_start, day_end = _local_day_bounds(target_date, club_tz=club_tz)
    now = timezone.now()
    slots = list(
        PersonalAvailabilitySlot.objects.for_club(club)
        .filter(
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
            starts_at__gte=max(day_start, now),
            starts_at__lt=day_end,
            trainer__is_active=True,
            training_type__is_active=True,
            training_type__kind__in=[
                TrainingType.Kind.PERSONAL,
                TrainingType.Kind.MINI_GROUP,
            ],
        )
        .select_related("trainer", "location", "training_type")
        .order_by("starts_at", "trainer_id", "id")
    )
    reserved_windows = list(
        PersonalBookingPaymentReservation.objects.for_club(club)
        .filter(
            status__in=[
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.BOOKED,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            ],
            starts_at__gte=day_start,
            starts_at__lt=day_end,
        )
        .filter(
            Q(status=PersonalBookingPaymentReservation.Status.BOOKED)
            | Q(status=PersonalBookingPaymentReservation.Status.MANUAL_REVIEW)
            | Q(expires_at__gt=now)
        )
        .values("trainer_id", "starts_at", "ends_at")
    )
    occurrences_by_trainer: dict[int, list[ScheduleOccurrence]] = {}
    for occurrence in get_schedule_occurrences_for_date(club=club, target_date=target_date):
        occurrences_by_trainer.setdefault(occurrence.trainer_id, []).append(occurrence)

    training_type_ids = {slot.training_type_id for slot in slots}
    location_ids = {slot.location_id for slot in slots}
    subscriptions_by_key: dict[tuple[int, int | None], Subscription] = {}
    payment_tariffs_by_key: dict[tuple[int, int | None], Tariff] = {}
    if student.status == Student.Status.ACTIVE:
        subscriptions_by_key = _active_subscription_by_training_location(
            club=club,
            student_id=student.id,
            training_type_ids=training_type_ids,
            location_ids=location_ids,
            target_date=target_date,
        )
        payment_tariffs_by_key = _payment_tariff_by_training_location(
            club=club,
            training_type_ids=training_type_ids,
            location_ids=location_ids,
        )

    options = []
    unified_personal_policy = is_unified_client_journey_enabled(club=club)
    for slot in slots:
        if _slot_overlaps_schedule_occurrences(
            slot=slot,
            occurrences=occurrences_by_trainer.get(slot.trainer_id, []),
            club_tz=club_tz,
        ):
            continue
        if any(
            window["trainer_id"] == slot.trainer_id
            and window["starts_at"] < slot.ends_at
            and window["ends_at"] > slot.starts_at
            for window in reserved_windows
        ):
            continue
        subscription_id = None
        payment_tariff = None
        reason_code = ""
        booking_status = "can_book"
        offer_fields = {
            "offer_tariff_id": None,
            "offer_tariff_name": "",
            "offer_price": "",
            "offer_duration_days": None,
            "offer_scope": "",
            "offer_location_id": None,
            "offer_digest": "",
            "offer_error_code": "",
        }
        offer = None
        if unified_personal_policy and slot.training_type.kind == TrainingType.Kind.PERSONAL:
            try:
                offer = resolve_personal_booking_offer(
                    club_id=slot.club_id,
                    trainer_id=slot.trainer_id,
                    training_type_id=slot.training_type_id,
                    location_id=slot.location_id,
                )
                offer_fields.update(personal_offer_payload(slot=slot, offer=offer))
            except BusinessLogicError as exc:
                offer_fields["offer_error_code"] = exc.code
        if student.status != Student.Status.ACTIVE:
            booking_status = "blocked"
            reason_code = "student_ineligible"
        else:
            subscription = _scoped_subscription(
                subscriptions_by_key,
                training_type_id=slot.training_type_id,
                location_id=slot.location_id,
            )
            subscription_id = subscription.id if subscription else None
            if subscription is None:
                if unified_personal_policy and slot.training_type.kind == TrainingType.Kind.PERSONAL:
                    payment_tariff = offer.tariff if offer is not None else None
                else:
                    payment_tariff = _scoped_payment_tariff(
                        payment_tariffs_by_key,
                        training_type_id=slot.training_type_id,
                        location_id=slot.location_id,
                    )
                if payment_tariff is not None:
                    booking_status = "can_pay"
                    reason_code = "payment_required"
                else:
                    booking_status = "blocked"
                    reason_code = offer_fields["offer_error_code"] or "subscription_not_available"

        options.append(
            {
                "slot_id": slot.id,
                "date": target_date,
                "starts_at": slot.starts_at,
                "ends_at": slot.ends_at,
                "trainer_id": slot.trainer_id,
                "trainer_name": f"{slot.trainer.first_name} {slot.trainer.last_name}".strip(),
                "location_id": slot.location_id,
                "location_name": slot.location.name,
                "training_type_id": slot.training_type_id,
                "training_type_name": slot.training_type.name,
                "booking_status": booking_status,
                "reason_code": reason_code,
                "subscription_id": subscription_id,
                "payment_tariff_id": payment_tariff.id if payment_tariff else None,
                "payment_tariff_name": payment_tariff.name if payment_tariff else "",
                "payment_amount": str(payment_tariff.price) if payment_tariff else "",
                **offer_fields,
            }
        )

    return options


def get_unified_self_service_personal_options(
    *,
    club,
    student_id: int,
    target_date: date_type,
) -> list[dict]:
    """Return the capability-on, personal-only self-service read model.

    The legacy selector remains the compatibility authority while rollout is
    off.  This deliberately narrow projection removes its staff/legacy fields
    and hides every slot for which the current actor has no safe next action.
    """

    legacy_options = get_self_service_personal_availability_options(
        club=club,
        student_id=student_id,
        target_date=target_date,
    )
    candidate_ids = [option["slot_id"] for option in legacy_options]
    personal_slot_ids = set(
        PersonalAvailabilitySlot.objects.for_club(club)
        .filter(
            id__in=candidate_ids,
            training_type__kind=TrainingType.Kind.PERSONAL,
        )
        .values_list("id", flat=True)
    )

    from apps.attendance.services.enrollment import (
        get_personal_booking_entitlement,
        has_pending_personal_booking_subscription,
    )

    options: list[dict] = []
    for option in legacy_options:
        if option["slot_id"] not in personal_slot_ids:
            continue
        if option["reason_code"] == "student_ineligible":
            continue
        entitlement = get_personal_booking_entitlement(
            club_id=club.id,
            student_id=student_id,
            location_id=option["location_id"],
            training_type_id=option["training_type_id"],
            target_date=target_date,
            lock=False,
            starts_at=option["starts_at"],
        )
        if entitlement is not None:
            # Coverage is independently valid.  A missing paid default must
            # never suppress a usable entitlement or force a fake digest.
            options.append(
                {
                    "slot_id": option["slot_id"],
                    "date": option["date"],
                    "starts_at": option["starts_at"],
                    "ends_at": option["ends_at"],
                    "trainer_id": option["trainer_id"],
                    "trainer_name": option["trainer_name"],
                    "location_id": option["location_id"],
                    "location_name": option["location_name"],
                    "training_type_id": option["training_type_id"],
                    "training_type_name": option["training_type_name"],
                    "capability": "can_book",
                    "offer_tariff_name": "",
                    "offer_price": "",
                    "offer_digest": "",
                }
            )
        else:
            if has_pending_personal_booking_subscription(
                club_id=club.id,
                student_id=student_id,
                location_id=option["location_id"],
                training_type_id=option["training_type_id"],
            ):
                # The pending subscription attempt is the durable next-step
                # authority.  Do not mint a second command/order while it
                # drains or is resolved by the billing lifecycle.
                continue
            # Paid options are actionable only while the existing billing
            # readiness authority can safely create and reconcile the order.
            # Do not hand the client a can_pay action that the owner will
            # reject after a command claim has already been persisted.
            from apps.billing.service_modules.payment_readiness import get_online_payment_capability

            if not get_online_payment_capability().enabled:
                continue
            try:
                offer = resolve_personal_booking_offer(
                    club_id=club.id,
                    trainer_id=option["trainer_id"],
                    training_type_id=option["training_type_id"],
                    location_id=option["location_id"],
                )
                slot = PersonalAvailabilitySlot.objects.for_club(club).get(id=option["slot_id"])
                offer_fields = personal_offer_payload(slot=slot, offer=offer)
            except BusinessLogicError:
                continue
            options.append(
                {
                    "slot_id": option["slot_id"],
                    "date": option["date"],
                    "starts_at": option["starts_at"],
                    "ends_at": option["ends_at"],
                    "trainer_id": option["trainer_id"],
                    "trainer_name": option["trainer_name"],
                    "location_id": option["location_id"],
                    "location_name": option["location_name"],
                    "training_type_id": option["training_type_id"],
                    "training_type_name": option["training_type_name"],
                    "capability": "can_pay",
                    "offer_tariff_name": offer_fields["offer_tariff_name"],
                    "offer_price": offer_fields["offer_price"],
                    "offer_digest": offer_fields["offer_digest"],
                }
            )
    return options


def _trainer_schedule_overlap_exists(
    *,
    club,
    trainer_id: int,
    starts_at,
    ends_at,
) -> bool:
    club_tz = club_zoneinfo(club)
    local_starts_at = timezone.localtime(starts_at, club_tz)
    local_ends_at = timezone.localtime(ends_at, club_tz)
    target_date = local_starts_at.date()
    return any(
        occurrence.effective_start_time < local_ends_at.time()
        and occurrence.effective_end_time > local_starts_at.time()
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=target_date,
            trainer_id=trainer_id,
        )
    )


def _trainer_availability_slot_payload(
    slot: PersonalAvailabilitySlot,
    *,
    club_tz: tzinfo_type,
) -> dict:
    now = timezone.now()
    trainer_name = f"{slot.trainer.first_name} {slot.trainer.last_name}".strip()
    mutable = slot.starts_at > now
    payload = {
        "id": slot.id,
        "date": timezone.localtime(slot.starts_at, club_tz).date(),
        "starts_at": slot.starts_at,
        "ends_at": slot.ends_at,
        "trainer_id": slot.trainer_id,
        "trainer_name": trainer_name,
        "location_id": slot.location_id,
        "location_name": slot.location.name,
        "training_type_id": slot.training_type_id,
        "training_type_name": slot.training_type.name,
        "training_type_kind": slot.training_type.kind,
        "status": slot.status,
        "block_reason": slot.block_reason,
        "booked_enrollment_id": slot.booked_enrollment_id,
        "can_block": mutable and slot.status == PersonalAvailabilitySlot.Status.PUBLISHED,
        "can_unblock": mutable and slot.status == PersonalAvailabilitySlot.Status.BLOCKED,
        "can_cancel": mutable
        and slot.status
        in {
            PersonalAvailabilitySlot.Status.PUBLISHED,
            PersonalAvailabilitySlot.Status.BLOCKED,
        },
    }
    if (
        slot.training_type.kind != TrainingType.Kind.PERSONAL
        or not is_unified_client_journey_enabled(club=slot.club_id)
    ):
        return {
            **payload,
            "offer_tariff_id": None,
            "offer_tariff_name": "",
            "offer_price": "",
            "offer_duration_days": None,
            "offer_scope": "",
            "offer_location_id": None,
            "offer_digest": "",
            "offer_error_code": "",
        }
    try:
        return {
            **payload,
            **personal_offer_payload(
                slot=slot,
                offer=resolve_personal_booking_offer(
                    club_id=slot.club_id,
                    trainer_id=slot.trainer_id,
                    training_type_id=slot.training_type_id,
                    location_id=slot.location_id,
                ),
            ),
            "offer_error_code": "",
        }
    except BusinessLogicError as exc:
        return {
            **payload,
            "offer_tariff_id": None,
            "offer_tariff_name": "",
            "offer_price": "",
            "offer_duration_days": None,
            "offer_scope": "",
            "offer_location_id": None,
            "offer_digest": "",
            "offer_error_code": exc.code,
        }


def get_trainer_personal_availability_calendar(
    *,
    club,
    trainer_id: int,
    date_from: date_type,
    date_to: date_type,
) -> list[dict]:
    if date_to < date_from:
        return []
    club_tz = club_zoneinfo(club)
    day_start, _ = _local_day_bounds(date_from, club_tz=club_tz)
    _, day_after_end = _local_day_bounds(date_to, club_tz=club_tz)
    slots = (
        PersonalAvailabilitySlot.objects.for_club(club)
        .filter(
            trainer_id=trainer_id,
            starts_at__gte=day_start,
            starts_at__lt=day_after_end,
            status__in=TRAINER_AVAILABILITY_VISIBLE_STATUSES,
        )
        .select_related("trainer", "location", "training_type", "booked_enrollment")
        .order_by("starts_at", "id")
    )
    return [_trainer_availability_slot_payload(slot, club_tz=club_tz) for slot in slots]


def personal_availability_slot_payload(slot: PersonalAvailabilitySlot) -> dict:
    slot = (
        PersonalAvailabilitySlot.objects.for_club(slot.club_id)
        .select_related(
            "club",
            "trainer",
            "location",
            "training_type",
            "booked_enrollment",
        )
        .get(id=slot.id)
    )
    return _trainer_availability_slot_payload(slot, club_tz=club_zoneinfo(slot.club))


def get_student_attendance(*, club, student_id: int, month: date_type | None = None) -> QuerySet[Checkin]:
    qs = (
        Checkin.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .select_related("schedule", "schedule__location", "trainer", "training_type")
        .order_by("-date")
    )
    if month:
        qs = qs.filter(date__year=month.year, date__month=month.month)
    return qs


@dataclass(frozen=True)
class StudentAttendanceSummary:
    attended_count: int
    missed_count: int
    decided_count: int
    attendance_rate: int | None


_MEMBERSHIP_ROOT_ACTIONS = {"created", "backfilled"}
_MEMBERSHIP_STATE_ACTIONS = {"frozen", "unfrozen"}


def _round_half_up_percentage(*, attended_count: int, decided_count: int) -> int | None:
    if decided_count <= 0:
        return None
    return (200 * attended_count + decided_count) // (2 * decided_count)


def _membership_event_effective_at(*, event: dict, club_tz: tzinfo_type) -> datetime_type:
    effective_day_start = timezone.make_aware(
        datetime_type.combine(event["effective_date"], time_type.min),
        club_tz,
    )
    created_at = timezone.localtime(event["created_at"], club_tz)
    return max(effective_day_start, created_at)


def _membership_expected_at_occurrence(
    *,
    membership: dict,
    events: list[dict],
    occurrence_starts_at: datetime_type,
    club_tz: tzinfo_type,
) -> bool:
    occurrence_date = occurrence_starts_at.date()
    if occurrence_date < membership["starts_on"]:
        return False
    if membership["ends_on"] is not None and occurrence_date > membership["ends_on"]:
        return False

    root_event = next(
        (event for event in events if event["action"] in _MEMBERSHIP_ROOT_ACTIONS),
        None,
    )
    if root_event is None:
        return False

    coverage_starts_at = _membership_event_effective_at(
        event=root_event,
        club_tz=club_tz,
    )
    if occurrence_starts_at < coverage_starts_at:
        return False

    state = root_event["new_state_snapshot"].get("status")
    for event in events:
        if event["id"] == root_event["id"] or event["action"] not in _MEMBERSHIP_STATE_ACTIONS:
            continue
        if occurrence_starts_at < _membership_event_effective_at(
            event=event,
            club_tz=club_tz,
        ):
            continue
        state = event["new_state_snapshot"].get("status", state)
    return state == TrainingGroupMembership.Status.ACTIVE


def get_student_attendance_summary(*, club, student_id: int) -> StudentAttendanceSummary:
    """Return conservative all-time attendance based on persisted outcomes.

    Attendance is a live check-in. A miss requires either an explicit personal
    no-show or a closed canonical Training Group session whose membership
    ledger proves that the student was expected when the occurrence started.
    """

    valid_checkin_keys = set(
        Checkin.objects.for_club(club)
        .filter(
            student_id=student_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .values_list("schedule_id", "date")
    )
    explicit_no_show_keys = {
        (schedule_id, booking_date)
        for schedule_id, booking_date in (
            PersonalDropInBooking.objects.for_club(club)
            .filter(
                enrollment__student_id=student_id,
                state=PersonalDropInBooking.State.NO_SHOW,
                enrollment__starts_on__isnull=False,
            )
            .values_list("enrollment__schedule_id", "enrollment__starts_on")
        )
        if booking_date is not None
    }

    memberships = list(
        TrainingGroupMembership.objects.for_club(club)
        .filter(student_id=student_id)
        .values("id", "training_group_id", "starts_on", "ends_on")
        .order_by("id")
    )
    membership_ids = [membership["id"] for membership in memberships]
    events_by_membership: dict[int, list[dict]] = defaultdict(list)
    if membership_ids:
        events = (
            TrainingGroupMembershipEvent.objects.for_club(club)
            .filter(membership_id__in=membership_ids)
            .values(
                "id",
                "membership_id",
                "action",
                "effective_date",
                "new_state_snapshot",
                "created_at",
            )
            .order_by("effective_date", "created_at", "id")
        )
        for event in events:
            events_by_membership[event["membership_id"]].append(event)

    group_ids = {membership["training_group_id"] for membership in memberships}
    schedules = list(
        Schedule.objects.for_club(club)
        .filter(training_group_id__in=group_ids)
        .values("id", "training_group_id", "start_time")
        .order_by("id")
    )
    schedules_by_id = {schedule["id"]: schedule for schedule in schedules}
    memberships_by_group: dict[int, list[dict]] = defaultdict(list)
    for membership in memberships:
        memberships_by_group[membership["training_group_id"]].append(membership)

    closed_sessions = list(
        GroupSession.objects.for_club(club)
        .filter(
            schedule_id__in=schedules_by_id,
            date__lte=club_localdate(club),
            closed_at__isnull=False,
        )
        .values("schedule_id", "date")
        .order_by("date", "schedule_id")
    )
    relevant_dates = {
        *explicit_no_show_keys,
        *((session["schedule_id"], session["date"]) for session in closed_sessions),
    }
    schedule_ids = {schedule_id for schedule_id, _ in relevant_dates}
    dates = {target_date for _, target_date in relevant_dates}
    exceptions = list(
        ScheduleException.objects.for_club(club)
        .filter(schedule_id__in=schedule_ids)
        .filter(Q(date__in=dates) | Q(new_date__in=dates))
        .values(
            "schedule_id",
            "date",
            "exception_type",
            "new_date",
            "new_start_time",
        )
    )

    excluded_occurrence_keys = {
        (exception["schedule_id"], exception["date"])
        for exception in exceptions
        if exception["exception_type"]
        in {
            ScheduleException.ExceptionType.CANCELLED,
            ScheduleException.ExceptionType.RESCHEDULED,
        }
    }
    rescheduled_start_times = {
        (exception["schedule_id"], exception["new_date"]): exception["new_start_time"]
        for exception in exceptions
        if exception["exception_type"] == ScheduleException.ExceptionType.RESCHEDULED
        and exception["new_date"] is not None
        and exception["new_start_time"] is not None
    }

    missed_keys = {
        key
        for key in explicit_no_show_keys
        if key not in excluded_occurrence_keys and key not in valid_checkin_keys
    }
    club_tz = club_zoneinfo(club)
    for session in closed_sessions:
        key = (session["schedule_id"], session["date"])
        if key in excluded_occurrence_keys or key in valid_checkin_keys or key in missed_keys:
            continue
        schedule = schedules_by_id[session["schedule_id"]]
        start_time = rescheduled_start_times.get(key, schedule["start_time"])
        occurrence_starts_at = timezone.make_aware(
            datetime_type.combine(session["date"], start_time),
            club_tz,
        )
        if any(
            _membership_expected_at_occurrence(
                membership=membership,
                events=events_by_membership[membership["id"]],
                occurrence_starts_at=occurrence_starts_at,
                club_tz=club_tz,
            )
            for membership in memberships_by_group[schedule["training_group_id"]]
        ):
            missed_keys.add(key)

    attended_count = len(valid_checkin_keys)
    missed_count = len(missed_keys)
    decided_count = attended_count + missed_count
    return StudentAttendanceSummary(
        attended_count=attended_count,
        missed_count=missed_count,
        decided_count=decided_count,
        attendance_rate=_round_half_up_percentage(
            attended_count=attended_count,
            decided_count=decided_count,
        ),
    )


def get_student_schedule(*, club, student_id: int) -> QuerySet[Schedule]:
    """Return active schedules assigned to the student, with legacy check-in fallback."""
    today = club_localdate(club)
    enrollments = ScheduleEnrollment.objects.for_club(club).filter(student_id=student_id)
    enrolled_schedule_ids = set(
        enrollments.filter(_open_or_upcoming_enrollment_q(today)).values_list("schedule_id", flat=True).distinct()
    )
    schedule_ids_with_enrollment = enrollments.values_list("schedule_id", flat=True).distinct()

    legacy_attended_schedule_ids = (
        Checkin.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .exclude(schedule_id__in=schedule_ids_with_enrollment)
        .values_list("schedule_id", flat=True)
        .distinct()
    )
    schedule_ids = enrolled_schedule_ids | set(legacy_attended_schedule_ids)
    return (
        Schedule.objects.for_club(club)
        .filter(id__in=schedule_ids, is_active=True)
        .select_related("trainer", "location", "training_group")
        .order_by("day_of_week", "start_time")
    )


def get_student_upcoming_personal_bookings(
    *,
    club,
    student_id: int,
    start_date: date_type | None = None,
) -> QuerySet[ScheduleEnrollment]:
    target_date = start_date or club_localdate(club)
    regular_booking_q = Q(
        status__in=OPEN_SCHEDULE_ENROLLMENT_STATUSES,
        created_from__in=[
            ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        ],
        schedule__is_active=True,
        schedule__one_time_date__gte=target_date,
        schedule__training_type__kind__in=[
            TrainingType.Kind.PERSONAL,
            TrainingType.Kind.MINI_GROUP,
        ],
    )
    drop_in_booking_q = Q(
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        personal_drop_in_booking__isnull=False,
    )
    live_reschedule_checkin_q = Checkin.objects.for_club(club).filter(
        schedule_id=OuterRef("schedule_id"),
        date=OuterRef("schedule__one_time_date"),
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    )
    reschedule_group_session_q = GroupSession.objects.for_club(club).filter(
        schedule_id=OuterRef("schedule_id"),
        date=OuterRef("schedule__one_time_date"),
    )
    return (
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            student_id=student_id,
        )
        .filter(regular_booking_q | drop_in_booking_q)
        .select_related(
            "schedule",
            "schedule__club",
            "schedule__trainer",
            "schedule__location",
            "schedule__training_type",
            "personal_payment_reservation",
            "personal_payment_reservation__terms_snapshot",
            "personal_drop_in_booking",
            "personal_drop_in_booking__debt",
            "personal_drop_in_booking__tariff",
            "personal_drop_in_booking__terms_snapshot",
        )
        .annotate(
            has_personal_reschedule_checkin=Exists(live_reschedule_checkin_q),
            has_personal_reschedule_group_session=Exists(reschedule_group_session_q),
        )
        .prefetch_related(
            Prefetch(
                "personal_availability_slots",
                queryset=PersonalAvailabilitySlot.objects.for_club(club).only(
                    "id",
                    "booked_enrollment_id",
                    "status",
                ),
                to_attr="reschedule_source_slots",
            ),
            Prefetch(
                "personal_drop_in_booking__payment_links",
                queryset=PersonalDropInPaymentLink.objects.for_club(club)
                .select_related("payment__subscription", "bank_payment_order__subscription")
                .order_by(
                    "-created_at",
                    "-id",
                ),
                to_attr="prefetched_payment_links",
            )
        )
        .order_by("schedule__one_time_date", "schedule__start_time", "id")
    )


def can_reschedule_student_personal_booking(*, enrollment, can_manage: bool) -> bool:
    """Evaluate exact-reschedule capability from ``get_student_upcoming_personal_bookings`` evidence.

    The selector deliberately loads every relation and evidence marker this
    needs.  The API may therefore project this boolean without any per-booking
    queries; the mutation service remains the lock-held authority.
    """

    schedule = enrollment.schedule
    if not can_manage:
        return False
    if (
        schedule.one_time_date is None
        or enrollment.starts_on is None
        or enrollment.starts_on != enrollment.ends_on
        or enrollment.starts_on != schedule.one_time_date
        or not schedule.is_active
        or schedule.training_type.kind != TrainingType.Kind.PERSONAL
        or enrollment.status not in OPEN_SCHEDULE_ENROLLMENT_STATUSES
    ):
        return False

    starts_at = timezone.make_aware(
        datetime_type.combine(schedule.one_time_date, schedule.start_time),
        club_zoneinfo(schedule.club),
    )
    if starts_at <= timezone.now():
        return False
    if (
        enrollment.has_personal_reschedule_checkin
        or enrollment.has_personal_reschedule_group_session
    ):
        return False

    source_slots = enrollment.reschedule_source_slots
    if len(source_slots) > 1 or any(
        slot.status != PersonalAvailabilitySlot.Status.BOOKED for slot in source_slots
    ):
        return False

    drop_in = getattr(enrollment, "personal_drop_in_booking", None)
    if drop_in is not None:
        return (
            drop_in.state == PersonalDropInBooking.State.SCHEDULED
            and enrollment.created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN
            and is_complete_personal_terms(getattr(drop_in, "terms_snapshot", None))
        )

    payment_reservation = getattr(enrollment, "personal_payment_reservation", None)
    if payment_reservation is not None:
        return (
            payment_reservation.status == PersonalBookingPaymentReservation.Status.BOOKED
            and payment_reservation.schedule_id == schedule.id
            and is_complete_personal_terms(getattr(payment_reservation, "terms_snapshot", None))
        )
    return enrollment.created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING


def get_student_schedule_occurrences_for_range(
    *, club, student_id: int, date_from: date_type, date_to: date_type
) -> list[ScheduleOccurrence]:
    enrollments = list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(student_id=student_id)
        .select_related("schedule")
        .order_by("schedule_id", "id")
    )
    enrollments_by_schedule: dict[int, list[ScheduleEnrollment]] = {}
    for enrollment in enrollments:
        enrollments_by_schedule.setdefault(enrollment.schedule_id, []).append(enrollment)

    schedule_ids_with_enrollment = set(enrollments_by_schedule)

    legacy_attended_schedule_ids = set(
        Checkin.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .exclude(schedule_id__in=schedule_ids_with_enrollment)
        .values_list("schedule_id", flat=True)
        .distinct()
    )
    if not enrollments_by_schedule and not legacy_attended_schedule_ids:
        return []

    occurrences_by_date = get_schedule_occurrences_for_range(
        club=club,
        date_from=date_from,
        date_to=date_to,
    )
    result: list[ScheduleOccurrence] = []
    for target_date in sorted(occurrences_by_date.keys()):
        for occurrence in occurrences_by_date[target_date]:
            schedule_enrollments = enrollments_by_schedule.get(occurrence.schedule_id, [])
            matched_enrollment = next(
                (
                    enrollment
                    for enrollment in schedule_enrollments
                    if _enrollment_matches_date(
                        enrollment=enrollment,
                        target_date=occurrence.effective_date,
                    )
                ),
                None,
            )
            if matched_enrollment is not None:
                result.append(
                    replace(
                        occurrence,
                        enrollment_id=matched_enrollment.id,
                        created_from=matched_enrollment.created_from,
                        can_cancel=_enrollment_can_cancel_booking(
                            enrollment=matched_enrollment,
                            target_date=occurrence.effective_date,
                        ),
                    )
                )
            elif occurrence.schedule_id in legacy_attended_schedule_ids:
                result.append(occurrence)
    return result


def get_schedules(*, club) -> QuerySet[Schedule]:
    return (
        Schedule.objects.for_club(club)
        .filter(is_active=True)
        .select_related("trainer", "location", "training_group")
        .order_by("day_of_week", "start_time")
    )


def list_schedule_enrollments(
    *,
    club,
    student_id: int | None = None,
    schedule_id: int | None = None,
    status: str | None = None,
) -> QuerySet[ScheduleEnrollment]:
    qs = ScheduleEnrollment.objects.for_club(club).select_related(
        "student", "schedule", "schedule__training_group", "training_group_membership"
    ).order_by("-id")
    if student_id is not None:
        qs = qs.filter(student_id=student_id)
    if schedule_id is not None:
        qs = qs.filter(schedule_id=schedule_id)
    if status is not None:
        qs = qs.filter(status=status)
    return qs


def get_schedule_by_id(*, club, schedule_id: int) -> Schedule:
    return (
        Schedule.objects.for_club(club)
        .select_related("trainer", "location", "training_group", "training_type")
        .get(id=schedule_id)
    )


def get_today_sessions(*, club, today: date_type | None = None) -> QuerySet[Schedule]:
    if today is None:
        today = date_type.today()

    return (
        Schedule.objects.for_club(club)
        .filter(
            Q(one_time_date__isnull=True, day_of_week=today.weekday()) | Q(one_time_date=today),
            is_active=True,
        )
        .exclude(exceptions__date=today, exceptions__exception_type="cancelled")
        .select_related("trainer", "location", "training_group")
        .order_by("start_time")
    )


@dataclass(frozen=True)
class ScheduleOccurrence:
    schedule_id: int
    group_name: str
    occurrence_date: date_type
    effective_date: date_type
    effective_start_time: time_type
    effective_end_time: time_type
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    one_time_date: date_type | None = None
    is_rescheduled: bool = False
    is_substitute: bool = False
    training_type_id: int | None = None
    training_type_name: str = ""
    training_type_kind: str = ""
    enrollment_id: int | None = None
    created_from: str = ""
    can_cancel: bool = False


def _trainer_full_name(trainer) -> str:
    return f"{trainer.first_name} {trainer.last_name}"


def _schedule_occurs_on_date(*, schedule: Schedule, target_date: date_type) -> bool:
    if schedule.one_time_date is not None:
        return schedule.one_time_date == target_date
    return schedule.day_of_week == target_date.weekday()


def _build_occurrence(
    *,
    schedule: Schedule,
    occurrence_date: date_type,
    effective_date: date_type,
    trainer,
    start_time: time_type,
    end_time: time_type,
    is_rescheduled: bool = False,
    is_substitute: bool = False,
) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        schedule_id=schedule.id,
        group_name=schedule.training_group.name if schedule.training_group_id else schedule.group_name,
        occurrence_date=occurrence_date,
        effective_date=effective_date,
        effective_start_time=start_time,
        effective_end_time=end_time,
        trainer_id=trainer.id,
        trainer_name=_trainer_full_name(trainer),
        location_id=schedule.location_id,
        location_name=schedule.location.name,
        one_time_date=schedule.one_time_date,
        is_rescheduled=is_rescheduled,
        is_substitute=is_substitute,
        training_type_id=schedule.training_type_id,
        training_type_name=schedule.training_type.name if schedule.training_type else "",
        training_type_kind=schedule.training_type.kind if schedule.training_type else "",
    )


def _get_schedule_occurrences_for_date(*, club, target_date: date_type) -> list[ScheduleOccurrence]:
    exceptions = list(
        ScheduleException.objects.for_club(club)
        .filter(Q(date=target_date) | Q(new_date=target_date), schedule__is_active=True)
        .select_related(
            "substitute_trainer",
        )
    )
    exception_schedule_ids = {exc.schedule_id for exc in exceptions}
    schedule_filter = Q(one_time_date=target_date) | Q(
        one_time_date__isnull=True,
        day_of_week=target_date.weekday(),
    )
    if exception_schedule_ids:
        schedule_filter |= Q(id__in=exception_schedule_ids)
    schedules = list(
        Schedule.objects.for_club(club)
        .filter(is_active=True)
        .filter(schedule_filter)
        .select_related("trainer", "location", "training_type", "training_group")
    )

    exception_by_schedule_date = {(exc.schedule_id, exc.date): exc for exc in exceptions}
    reschedules_into_date: dict[int, list[ScheduleException]] = {}
    for exc in exceptions:
        if exc.exception_type == ScheduleException.ExceptionType.RESCHEDULED and exc.new_date == target_date:
            reschedules_into_date.setdefault(exc.schedule_id, []).append(exc)

    occurrences: list[ScheduleOccurrence] = []

    for schedule in schedules:
        same_day_exception = exception_by_schedule_date.get((schedule.id, target_date))
        base_occurs_today = _schedule_occurs_on_date(schedule=schedule, target_date=target_date)

        if base_occurs_today:
            if same_day_exception and same_day_exception.exception_type in (
                ScheduleException.ExceptionType.CANCELLED,
                ScheduleException.ExceptionType.RESCHEDULED,
            ):
                pass
            else:
                trainer = schedule.trainer
                is_substitute = False
                if (
                    same_day_exception
                    and same_day_exception.exception_type == ScheduleException.ExceptionType.SUBSTITUTE
                ):
                    trainer = same_day_exception.substitute_trainer or schedule.trainer
                    is_substitute = same_day_exception.substitute_trainer_id is not None

                occurrences.append(
                    _build_occurrence(
                        schedule=schedule,
                        occurrence_date=target_date,
                        effective_date=target_date,
                        trainer=trainer,
                        start_time=schedule.start_time,
                        end_time=schedule.end_time,
                        is_substitute=is_substitute,
                    )
                )

        for reschedule in reschedules_into_date.get(schedule.id, []):
            if same_day_exception and same_day_exception.exception_type == ScheduleException.ExceptionType.CANCELLED:
                continue

            trainer = schedule.trainer
            is_substitute = False
            if same_day_exception and same_day_exception.exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
                trainer = same_day_exception.substitute_trainer or schedule.trainer
                is_substitute = same_day_exception.substitute_trainer_id is not None

            occurrences.append(
                _build_occurrence(
                    schedule=schedule,
                    occurrence_date=reschedule.date,
                    effective_date=target_date,
                    trainer=trainer,
                    start_time=reschedule.new_start_time or schedule.start_time,
                    end_time=reschedule.new_end_time or schedule.end_time,
                    is_rescheduled=True,
                    is_substitute=is_substitute,
                )
            )

    occurrences.sort(
        key=lambda item: (
            item.effective_date,
            item.effective_start_time,
            item.group_name,
            item.schedule_id,
        )
    )
    return occurrences


def get_schedule_occurrences_for_date(
    *, club, target_date: date_type, trainer_id: int | None = None
) -> list[ScheduleOccurrence]:
    occurrences = _get_schedule_occurrences_for_date(club=club, target_date=target_date)
    if trainer_id is None:
        return occurrences
    return [occurrence for occurrence in occurrences if occurrence.trainer_id == trainer_id]


def get_schedule_occurrence(
    *,
    club,
    schedule_id: int,
    occurrence_date: date_type,
) -> ScheduleOccurrence | None:
    """Resolve one schedule occurrence by its user-visible effective date."""
    matches = [
        occurrence
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=occurrence_date,
        )
        if occurrence.schedule_id == schedule_id and occurrence.effective_date == occurrence_date
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def get_schedule_occurrences_for_range(
    *, club, date_from: date_type, date_to: date_type, trainer_id: int | None = None
) -> dict[date_type, list[ScheduleOccurrence]]:
    if date_to < date_from:
        return {}

    occurrences_by_date: dict[date_type, list[ScheduleOccurrence]] = {}
    current_date = date_from
    while current_date <= date_to:
        occurrences_by_date[current_date] = get_schedule_occurrences_for_date(
            club=club,
            target_date=current_date,
            trainer_id=trainer_id,
        )
        current_date += timedelta(days=1)
    return occurrences_by_date


def get_unclosed_sessions(*, club, session_date: date_type) -> list[Schedule]:
    """Return schedules that need trainer confirmation.

    Closed = explicit session review with GroupSession.closed_at.
    Expected students = enrollment roster with legacy check-in fallback.
    """
    all_sessions = list(get_today_sessions(club=club, today=session_date))
    if not all_sessions:
        return []

    schedule_ids = [s.id for s in all_sessions]

    # 1. Has GroupSession → closed (trainer confirmed)
    closed_ids = set(
        GroupSession.objects.for_club(club)
        .filter(
            date=session_date,
            schedule_id__in=schedule_ids,
            closed_at__isnull=False,
        )
        .values_list("schedule_id", flat=True)
    )

    # Checked-in roster alone does not close a session; trainer review sets closed_at.
    return [sched for sched in all_sessions if sched.id not in closed_ids]


def get_today_checkins(*, club, today: date_type | None = None) -> QuerySet[Checkin]:
    if today is None:
        today = date_type.today()
    return (
        Checkin.objects.for_club(club)
        .filter(date=today, deleted_at__isnull=True)
        .select_related("student")
        .order_by("-created_at")
    )


def get_schedule_exceptions(*, club, schedule_id: int) -> QuerySet[ScheduleException]:
    return (
        ScheduleException.objects.for_club(club)
        .filter(schedule_id=schedule_id)
        .select_related("substitute_trainer")
        .order_by("-date")
    )


# ──────────────────────────────────────────────
# Schedule students with alerts
# ──────────────────────────────────────────────

# Canonical alert types -- frontend AlertType enum MUST match these values exactly.
# See frontend/src/features/trainer/types.ts AlertType.
ALERT_TYPES = {
    "newcomer": {"icon": "star", "message_template": "Новичок"},
    "debtor": {"icon": "alert-triangle", "message_template": "Долг"},
    "contraindications": {"icon": "heart-pulse", "message_template": None},
    "returned": {"icon": "undo", "message_template": "Вернулся после {days} дн."},
    "last_training": {"icon": "clock", "message_template": "Осталось {count} тр."},
    "expiring": {"icon": "clock", "message_template": "Абонемент истекает"},
    "child": {"icon": "baby", "message_template": "Ребёнок"},
}


GUEST_VISIT_CANDIDATE_STATUSES = (
    Student.Status.ACTIVE,
    Student.Status.LEAD,
    Student.Status.TRIAL,
    Student.Status.AT_RISK,
    Student.Status.CHURNED,
)


def get_guest_visit_candidates(
    *,
    club,
    schedule_id: int,
    target_date: date_type,
    query: str,
    trainer_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Privacy-safe candidate search for adding a one-day guest to a roster."""
    normalized_query = query.strip()
    if len(normalized_query) < 2:
        return []

    expected_ids = get_expected_student_ids_by_schedule_date(
        club=club,
        schedule_ids=[schedule_id],
        target_date=target_date,
    ).get(schedule_id, set())
    digits = "".join(ch for ch in normalized_query if ch.isdigit())
    terms = normalized_query.split()
    search_filter = Q(first_name__icontains=normalized_query) | Q(last_name__icontains=normalized_query)
    if len(terms) >= 2:
        first, second = terms[0], terms[1]
        search_filter |= Q(first_name__icontains=first, last_name__icontains=second) | Q(
            first_name__icontains=second, last_name__icontains=first
        )
    if digits and len(digits) >= 4:
        search_filter |= Q(phone__endswith=digits[-4:]) | Q(guardian_phone__endswith=digits[-4:])

    qs = (
        Student.objects.for_club(club)
        .filter(deleted_at__isnull=True, status__in=GUEST_VISIT_CANDIDATE_STATUSES)
        .exclude(id__in=expected_ids)
        .filter(search_filter)
    )
    if trainer_id is not None:
        qs = qs.filter(Q(status=Student.Status.LEAD, assigned_trainer_id=trainer_id) | ~Q(status=Student.Status.LEAD))

    rows = qs.order_by("first_name", "last_name", "id").values(
        "id",
        "first_name",
        "last_name",
        "phone",
        "guardian_phone",
        "status",
    )[:limit]

    return [
        {
            "id": row["id"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "masked_phone": _mask_phone(
                _contact_phone(row),
                suffix=_lookup_suffix(_contact_phone(row)),
            ),
            "kind": "lead" if row["status"] == Student.Status.LEAD else "student",
            "status": row["status"],
        }
        for row in rows
    ]


def get_students_for_schedule(*, club, schedule_id: int, reference_date: date_type | None = None) -> list[dict]:
    """Students for one occurrence from the shared expected-roster resolver."""
    from apps.attendance.training_group_roster import resolve_expected_roster_for_schedule_date

    Schedule.objects.for_club(club).get(id=schedule_id)
    today = reference_date or date_type.today()
    roster_by_student_id = resolve_expected_roster_for_schedule_date(
        club=club,
        schedule_id=schedule_id,
        target_date=today,
        legacy_include_target_date=True,
    )
    student_ids = set(roster_by_student_id)
    membership_projection_ids = {
        membership_id: enrollment_id
        for membership_id, enrollment_id in ScheduleEnrollment.objects.for_club(club)
        .filter(
            schedule_id=schedule_id,
            training_group_membership_id__in={
                entry.membership_id
                for entry in roster_by_student_id.values()
                if entry.membership_id is not None
            },
        )
        .values_list("training_group_membership_id", "id")
    }
    students = list(
        Student.objects.for_club(club)
        .filter(id__in=student_ids, deleted_at__isnull=True)
        .exclude(status="lost")
        .select_related("club")
        .order_by("first_name", "last_name")
    )
    alerts_by_student_id = _compute_alerts_for_students(
        students=students,
        club=club,
        today=today,
    )

    result = []
    for s in students:
        alerts = alerts_by_student_id.get(s.id, [])
        roster_entry = roster_by_student_id[s.id]
        enrollment_status = roster_entry.enrollment_status
        created_from = roster_entry.created_from
        starts_on = roster_entry.starts_on
        ends_on = roster_entry.ends_on
        result.append(
            {
                "id": s.id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "enrollment_id": roster_entry.enrollment_id or membership_projection_ids.get(
                    roster_entry.membership_id
                ),
                "training_group_membership_id": roster_entry.membership_id,
                "created_from": created_from,
                "starts_on": starts_on,
                "ends_on": ends_on,
                "is_guest_visit": bool(
                    created_from
                    in {
                        ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
                        ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
                    }
                    and starts_on == today
                    and ends_on == today
                ),
                "enrollment_status": enrollment_status,
                "checkin_blocked_reason": roster_entry.blocked_reason,
                "alerts": alerts,
            }
        )
    return result


def _compute_alerts_for_students(
    *,
    students: list[Student],
    club,
    today: date_type | None = None,
) -> dict[int, list[dict]]:
    """Bulk version of roster pre-checkin alerts."""
    today = today or date_type.today()
    student_ids = [student.id for student in students]
    alerts_by_student_id: dict[int, list[dict]] = {student.id: [] for student in students}
    if not student_ids:
        return alerts_by_student_id

    checkin_counts = {
        row["student_id"]: row["total"]
        for row in (
            Checkin.objects.for_club(club)
            .filter(student_id__in=student_ids, deleted_at__isnull=True)
            .values("student_id")
            .annotate(total=Count("id"))
        )
    }
    debtor_student_ids = set(
        Debt.objects.for_club(club)
        .filter(
            student_id__in=student_ids,
            resolved_at__isnull=True,
            settlement_payment__isnull=True,
        )
        .values_list("student_id", flat=True)
        .distinct()
    )
    active_subscription_by_student_id: dict[int, Subscription] = {}
    for subscription in (
        Subscription.objects.for_club(club)
        .filter(student_id__in=student_ids)
        .filter(current_active_subscription_q())
        .order_by("student_id", "expires_at", "id")
    ):
        active_subscription_by_student_id.setdefault(subscription.student_id, subscription)

    for student in students:
        alerts = alerts_by_student_id[student.id]
        if checkin_counts.get(student.id, 0) <= 3:
            alerts.append(
                {
                    "type": "newcomer",
                    "icon": ALERT_TYPES["newcomer"]["icon"],
                    "message": ALERT_TYPES["newcomer"]["message_template"],
                }
            )

        if student.id in debtor_student_ids:
            alerts.append(
                {
                    "type": "debtor",
                    "icon": ALERT_TYPES["debtor"]["icon"],
                    "message": ALERT_TYPES["debtor"]["message_template"],
                }
            )

        if student.contraindications:
            alerts.append(
                {
                    "type": "contraindications",
                    "icon": ALERT_TYPES["contraindications"]["icon"],
                    "message": student.contraindications,
                }
            )

        if student.last_visit_date:
            days_away = (today - student.last_visit_date).days
            if days_away > 14:
                alerts.append(
                    {
                        "type": "returned",
                        "icon": ALERT_TYPES["returned"]["icon"],
                        "message": f"Вернулся после {days_away} дн.",
                    }
                )

        active_sub = active_subscription_by_student_id.get(student.id)
        if active_sub and active_sub.trainings_left is not None and active_sub.trainings_left <= 2:
            alerts.append(
                {
                    "type": "last_training",
                    "icon": ALERT_TYPES["last_training"]["icon"],
                    "message": f"Осталось {active_sub.trainings_left} тр.",
                }
            )

    return alerts_by_student_id


def _compute_student_alerts(*, student: Student, club) -> list[dict]:
    """Pre-checkin alerts. Uses ALERT_TYPES constant for type consistency."""
    alerts: list[dict] = []
    today = date_type.today()

    total = Checkin.objects.for_club(club).filter(student=student, deleted_at__isnull=True).count()
    if total <= 3:
        alerts.append(
            {
                "type": "newcomer",
                "icon": ALERT_TYPES["newcomer"]["icon"],
                "message": ALERT_TYPES["newcomer"]["message_template"],
            }
        )

    from apps.billing.models import Debt

    has_debt = (
        Debt.objects.for_club(club)
        .filter(student=student, resolved_at__isnull=True, settlement_payment__isnull=True)
        .exists()
    )
    if has_debt:
        alerts.append(
            {
                "type": "debtor",
                "icon": ALERT_TYPES["debtor"]["icon"],
                "message": ALERT_TYPES["debtor"]["message_template"],
            }
        )

    if student.contraindications:
        alerts.append(
            {
                "type": "contraindications",
                "icon": ALERT_TYPES["contraindications"]["icon"],
                "message": student.contraindications,
            }
        )

    if student.last_visit_date:
        days_away = (today - student.last_visit_date).days
        if days_away > 14:
            alerts.append(
                {
                    "type": "returned",
                    "icon": ALERT_TYPES["returned"]["icon"],
                    "message": f"Вернулся после {days_away} дн.",
                }
            )

    from apps.billing.models import Subscription

    active_sub = (
        Subscription.objects.for_club(club)
        .filter(student=student)
        .filter(current_active_subscription_q())
        .order_by("expires_at", "id")
        .first()
    )
    if active_sub and active_sub.trainings_left is not None and active_sub.trainings_left <= 2:
        alerts.append(
            {
                "type": "last_training",
                "icon": ALERT_TYPES["last_training"]["icon"],
                "message": f"Осталось {active_sub.trainings_left} тр.",
            }
        )

    return alerts


# ──────────────────────────────────────────────
# Kiosk phone lookup
# ──────────────────────────────────────────────


def lookup_by_phone_suffix(*, club_id: int, phone_suffix: str) -> list[dict]:
    target_date = club_localdate(Club.objects.only("id", "timezone").get(id=club_id))
    drop_in_lead_ids = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            state=PersonalDropInBooking.State.SCHEDULED,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            enrollment__student__status=Student.Status.LEAD,
            enrollment__student__deleted_at__isnull=True,
        )
        .values("enrollment__student_id")
    )
    students = list(
        Student.objects.for_club(club_id)
        .filter(
            Q(phone__endswith=phone_suffix) | Q(guardian_phone__endswith=phone_suffix),
            deleted_at__isnull=True,
        )
        .filter(
            Q(
                status__in=[
                    Student.Status.ACTIVE,
                    Student.Status.TRIAL,
                    Student.Status.AT_RISK,
                    Student.Status.CHURNED,
                ]
            )
            | Q(id__in=drop_in_lead_ids)
        )
        .order_by("first_name", "last_name")
        .values("id", "first_name", "last_name", "phone", "guardian_phone")[:10]
    )
    return _safe_kiosk_students(club_id=club_id, rows=students, matched_suffix=phone_suffix)


def get_kiosk_roster(*, club_id: int) -> list[dict]:
    target_date = club_localdate(Club.objects.only("id", "timezone").get(id=club_id))
    drop_in_lead_ids = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            state=PersonalDropInBooking.State.SCHEDULED,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            enrollment__student__status=Student.Status.LEAD,
            enrollment__student__deleted_at__isnull=True,
        )
        .values("enrollment__student_id")
    )
    students = list(
        Student.objects.for_club(club_id)
        .filter(
            Q(
                status__in=[
                    Student.Status.ACTIVE,
                    Student.Status.TRIAL,
                    Student.Status.AT_RISK,
                    Student.Status.CHURNED,
                ]
            )
            | Q(id__in=drop_in_lead_ids),
            deleted_at__isnull=True,
        )
        .order_by("first_name", "last_name")
        .values("id", "first_name", "last_name", "phone", "guardian_phone")
    )
    return _safe_kiosk_students(club_id=club_id, rows=students)


def _safe_kiosk_students(*, club_id: int, rows: list[dict], matched_suffix: str | None = None) -> list[dict]:
    student_ids = [row["id"] for row in rows]
    subscriptions_by_student: dict[int, Subscription] = {}
    grades_by_student: dict[int, StudentGrade] = {}
    group_names_by_student: dict[int, str] = {}

    if student_ids:
        for subscription in (
            Subscription.objects.for_club(club_id)
            .filter(student_id__in=student_ids)
            .filter(current_active_subscription_q())
            .select_related("tariff")
            .order_by("student_id", "expires_at", "id")
        ):
            subscriptions_by_student.setdefault(subscription.student_id, subscription)

        for student_grade in (
            StudentGrade.objects.for_club(club_id)
            .filter(student_id__in=student_ids)
            .select_related("current_grade")
            .order_by("student_id", "grade_system_id", "id")
        ):
            grades_by_student.setdefault(student_grade.student_id, student_grade)

        target_date = club_localdate(Club.objects.only("id", "timezone").get(id=club_id))
        for membership in (
            TrainingGroupMembership.objects.for_club(club_id)
            .filter(
                student_id__in=student_ids,
                status__in=(
                    TrainingGroupMembership.Status.ACTIVE,
                    TrainingGroupMembership.Status.FROZEN,
                ),
                starts_on__lte=target_date,
                training_group__status="active",
            )
            .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=target_date))
            .select_related("training_group")
            .order_by("student_id", "starts_on", "id")
        ):
            group_names_by_student.setdefault(
                membership.student_id,
                membership.training_group.name,
            )

    return [
        _safe_kiosk_student(
            row,
            subscription=subscriptions_by_student.get(row["id"]),
            student_grade=grades_by_student.get(row["id"]),
            group_name=group_names_by_student.get(row["id"], ""),
            matched_suffix=matched_suffix,
        )
        for row in rows
    ]


def _safe_kiosk_student(
    row: dict,
    *,
    subscription: Subscription | None,
    student_grade: StudentGrade | None,
    group_name: str,
    matched_suffix: str | None = None,
) -> dict:
    suffixes = _lookup_suffixes(row)
    suffix = matched_suffix if matched_suffix in suffixes else (suffixes[0] if suffixes else "")
    contact_phone = _phone_for_suffix(row, suffix) or _contact_phone(row)
    return {
        "id": row["id"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "lookup_suffix": suffix,
        "lookup_suffixes": suffixes,
        "masked_phone": _mask_phone(contact_phone, suffix=suffix),
        "group_name": group_name,
        "grade_name": student_grade.current_grade.name if student_grade and student_grade.current_grade else "",
        "subscription_name": subscription.tariff.name if subscription else "",
        "subscription_status": subscription.status if subscription else "",
        "trainings_left": subscription.trainings_left if subscription else None,
    }


def _contact_phone(row: dict) -> str:
    return row.get("phone") or row.get("guardian_phone") or ""


def _phone_for_suffix(row: dict, suffix: str) -> str:
    for phone in (row.get("phone") or "", row.get("guardian_phone") or ""):
        if suffix and _lookup_suffix(phone) == suffix:
            return phone
    return ""


def _lookup_suffixes(row: dict) -> list[str]:
    suffixes: list[str] = []
    for phone in (row.get("phone") or "", row.get("guardian_phone") or ""):
        suffix = _lookup_suffix(phone)
        if len(suffix) == 4 and suffix not in suffixes:
            suffixes.append(suffix)
    return suffixes


def _lookup_suffix(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:]


def _mask_phone(phone: str, *, suffix: str) -> str:
    if not suffix:
        return ""
    prefix = "+" if phone.startswith("+") else ""
    return f"{prefix}***{suffix}"


# ──────────────────────────────────────────────
# Alert computation
# ──────────────────────────────────────────────


def compute_checkin_alerts(*, checkin, student, subscription) -> list[dict]:
    alerts: list[dict] = []
    today = date_type.today()

    # 1. Newcomer: <= 3 total checkins
    total = Checkin.objects.for_club(checkin.club_id).filter(student=student, deleted_at__isnull=True).count()
    if total <= 3:
        alerts.append({"type": "newcomer", "icon": "star", "message": "Newcomer"})

    # 2. Debtor
    if checkin.is_debt:
        alerts.append({"type": "debtor", "icon": "alert-triangle", "message": "No active subscription"})

    # 3. Contraindications
    if student.contraindications:
        alerts.append(
            {
                "type": "contraindications",
                "icon": "heart-pulse",
                "message": student.contraindications,
            }
        )

    # 4. Returned after pause (> 14 days since last visit)
    if student.last_visit_date:
        days_away = (today - student.last_visit_date).days
        if days_away > 14:
            alerts.append(
                {
                    "type": "returned",
                    "icon": "undo",
                    "message": f"Returned after {days_away} days",
                }
            )

    # 5. Expiring subscription
    if subscription and subscription.trainings_left is not None and subscription.trainings_left <= 2:
        alerts.append(
            {
                "type": "expiring",
                "icon": "clock",
                "message": f"{subscription.trainings_left} trainings left",
            }
        )

    # 6. Birthday within 7 days
    if student.date_of_birth:
        bday_this_year = student.date_of_birth.replace(year=today.year)
        # Handle year wrap (e.g., today is Dec 28, birthday is Jan 2)
        delta = (bday_this_year - today).days
        if delta < 0:
            bday_next_year = student.date_of_birth.replace(year=today.year + 1)
            delta = (bday_next_year - today).days
        if 0 <= delta <= 7:
            alerts.append({"type": "birthday", "icon": "cake", "message": "Birthday soon!"})

    # 7. First after grade promotion (promoted within 30 days, first checkin since)
    from django.utils import timezone

    from apps.grades.models import StudentGrade

    recent_promotion = StudentGrade.objects.filter(
        club_id=checkin.club_id,
        student=student,
        promoted_at__isnull=False,
        promoted_at__gte=timezone.now() - timedelta(days=30),
    ).first()
    if recent_promotion:
        checkins_since = (
            Checkin.objects.for_club(checkin.club_id)
            .filter(
                student=student,
                deleted_at__isnull=True,
                date__gte=recent_promotion.promoted_at.date(),
            )
            .exclude(id=checkin.id)
            .exists()
        )
        if not checkins_since:
            alerts.append(
                {
                    "type": "first_after_grade",
                    "icon": "trophy",
                    "message": "First session after promotion!",
                }
            )

    return alerts


def get_has_group_session(
    *,
    club,
    schedule_id: int,
    session_date: date_type,
) -> bool:
    """Check if trainer/owner explicitly closed this schedule occurrence."""

    return (
        GroupSession.objects.for_club(club)
        .filter(
            schedule_id=schedule_id,
            date=session_date,
            closed_at__isnull=False,
        )
        .exists()
    )


def get_session_close_state(
    *,
    club,
    session_date: date_type,
    effective_end_time: time_type,
    is_closed: bool = False,
    now=None,
) -> dict:
    club_tz = club_zoneinfo(club)
    close_allowed_at = timezone.make_aware(
        datetime_type.combine(session_date, effective_end_time),
        club_tz,
    )
    current_time = now or timezone.now()
    if is_closed:
        return {
            "can_close": False,
            "close_allowed_at": close_allowed_at,
            "close_block_reason": "session_closed",
        }
    if current_time < close_allowed_at:
        return {
            "can_close": False,
            "close_allowed_at": close_allowed_at,
            "close_block_reason": "session_not_finished",
        }
    return {
        "can_close": True,
        "close_allowed_at": close_allowed_at,
        "close_block_reason": "",
    }


def get_personal_drop_in_attendance_correction_preview(
    *,
    club,
    schedule_id: int,
    session_date: date_type,
    effective_end_time: time_type,
    has_exception: bool,
    checkin_blocked_reason: str = "",
) -> dict | None:
    """Build advisory UI state for one exact personal drop-in correction."""
    booking = (
        PersonalDropInBooking.objects.for_club(club)
        .select_related(
            "checkin",
            "enrollment",
            "enrollment__student",
            "enrollment__schedule",
            "enrollment__schedule__training_type",
        )
        .filter(
            enrollment__schedule_id=schedule_id,
            enrollment__starts_on=session_date,
            enrollment__ends_on=session_date,
            enrollment__created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        .first()
    )
    if booking is None:
        return None

    live_checkin = (
        Checkin.objects.for_club(club)
        .filter(
            id=booking.checkin_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
        if booking.checkin_id
        else None
    )
    session_is_closed = (
        GroupSession.objects.for_club(club)
        .filter(
            schedule_id=schedule_id,
            date=session_date,
            closed_at__isnull=False,
        )
        .exists()
    )
    close_state = get_session_close_state(
        club=club,
        session_date=session_date,
        effective_end_time=effective_end_time,
        is_closed=session_is_closed,
    )
    payment_links = list(
        PersonalDropInPaymentLink.objects.for_club(club)
        .select_related("payment", "payment__subscription")
        .filter(
            booking=booking,
            payment__status__in=[Payment.Status.PENDING, Payment.Status.CONFIRMED],
        )
        .order_by("-created_at", "-id")
    )
    payment_link = next(
        (
            link
            for link in payment_links
            if link.payment.status == Payment.Status.CONFIRMED
        ),
        None,
    ) or next(
        (
            link
            for link in payment_links
            if link.payment.status == Payment.Status.PENDING
        ),
        None,
    )

    financial_message = (
        "Система применит подходящий абонемент; если его нет — создаст долг "
        f"на {booking.price_snapshot} ₽."
    )
    financial_block_reason = ""
    creates_payroll_effect = False
    if payment_link is not None and payment_link.payment.status == Payment.Status.PENDING:
        financial_message = (
            "Посещение будет зафиксировано, а задолженность — привязана "
            "к ожидающей оплате."
        )
    elif payment_link is not None and payment_link.payment.status == Payment.Status.CONFIRMED:
        subscription = payment_link.payment.subscription
        if subscription is None or subscription.status != Subscription.Status.ACTIVE:
            financial_block_reason = (
                "Подтверждённая оплата требует проверки: активный абонемент не найден."
            )
        else:
            creates_payroll_effect = True
            if subscription.trainings_left is not None:
                remaining = max(subscription.trainings_left - 1, 0)
                financial_message = (
                    "Будет списана 1 тренировка из оплаченного абонемента. "
                    f"Останется: {remaining}."
                )
            else:
                financial_message = "Будет списана 1 тренировка из оплаченного абонемента."

    payroll_is_closed = creates_payroll_effect and (
        TrainerPayrollPeriodClose.objects.for_club(club)
        .filter(period_start__lte=session_date, period_end__gte=session_date)
        .exists()
    )

    can_record = False
    block_reason = ""
    if live_checkin is not None:
        block_reason = "Посещение уже отмечено."
    elif booking.state == PersonalDropInBooking.State.CANCELLED:
        block_reason = "Бронь отменена. Зачесть её как посещённую нельзя."
    elif booking.state == PersonalDropInBooking.State.NO_SHOW:
        block_reason = "Бронь отмечена как пропуск. Для изменения нужна отдельная корректировка."
    elif booking.state != PersonalDropInBooking.State.SCHEDULED:
        block_reason = "Состояние брони требует проверки."
    elif not booking.enrollment.schedule.is_active:
        block_reason = "Занятие неактивно. Посещение нельзя зачесть."
    elif has_exception:
        block_reason = "Изменённое или отменённое занятие нельзя зачесть этим действием."
    elif checkin_blocked_reason:
        block_reason = "Запись ученика заморожена. Посещение нельзя зачесть."
    elif session_is_closed:
        block_reason = "Занятие уже закрыто. Для исправления нужна отдельная корректировка."
    elif not close_state["can_close"]:
        block_reason = (
            "Посещение можно зачесть после окончания занятия — "
            f"после {effective_end_time:%H:%M}."
        )
    elif financial_block_reason:
        block_reason = financial_block_reason
    elif payroll_is_closed:
        block_reason = (
            "Период выплат уже закрыт. Нужна отдельная корректировка зарплаты."
        )
    else:
        can_record = True

    return {
        "booking": booking,
        "student": booking.enrollment.student,
        "is_checked_in": live_checkin is not None,
        "can_record": can_record,
        "block_reason": block_reason,
        "financial_message": financial_message,
        "default_reason": "Тренер не отметил",
    }


def get_already_checked_in_ids(
    *,
    club,
    schedule_id: int,
    checkin_date: date_type,
) -> list[int]:
    """Return student IDs that already have a checkin for this schedule+date."""
    return list(
        Checkin.objects.for_club(club)
        .filter(
            schedule_id=schedule_id,
            date=checkin_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .values_list("student_id", flat=True)
    )


def get_session_detail(*, club, schedule_id: int, session_date: date_type) -> dict:
    occurrence = next(
        (
            item
            for item in get_schedule_occurrences_for_date(
                club=club,
                target_date=session_date,
            )
            if item.schedule_id == schedule_id
        ),
        None,
    )
    if occurrence is None:
        from apps.common.exceptions import BusinessLogicError

        raise BusinessLogicError(
            "Schedule occurrence not found",
            code="schedule_occurrence_not_found",
        )

    roster = get_students_for_schedule(
        club=club,
        schedule_id=schedule_id,
        reference_date=session_date,
    )
    checkins_by_student_id = {
        row["student_id"]: row
        for row in (
            Checkin.objects.for_club(club)
            .filter(
                schedule_id=schedule_id,
                date=session_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("created_at", "id")
            .values("id", "student_id", "source", "created_at")
        )
    }
    session = (
        GroupSession.objects.for_club(club)
        .filter(schedule_id=schedule_id, date=session_date)
        .select_related("closed_by")
        .first()
    )
    is_closed = bool(session and session.closed_at)
    close_state = get_session_close_state(
        club=club,
        session_date=session_date,
        effective_end_time=occurrence.effective_end_time,
        is_closed=is_closed,
    )

    detail_roster = []
    checked_in_count = 0
    waiting_count = 0
    blocked_count = 0
    for student in roster:
        checkin = checkins_by_student_id.get(student["id"])
        is_blocked = bool(student.get("checkin_blocked_reason")) or (
            student.get("enrollment_status") == ScheduleEnrollment.Status.FROZEN
        )
        if checkin is not None:
            checkin_status = "checked_in"
            checked_in_count += 1
        elif is_blocked:
            checkin_status = "blocked"
            blocked_count += 1
        else:
            checkin_status = "waiting"
            waiting_count += 1

        detail_roster.append(
            {
                **student,
                "checkin_status": checkin_status,
                "checkin_id": checkin["id"] if checkin else None,
                "checkin_source": checkin["source"] if checkin else "",
                "checked_in_at": checkin["created_at"] if checkin else None,
            }
        )

    return {
        "schedule_id": schedule_id,
        "date": session_date,
        "occurrence": occurrence,
        "is_closed": is_closed,
        "group_session_id": session.id if session else None,
        "closed_at": session.closed_at if session else None,
        "closed_by_id": session.closed_by_id if session else None,
        "close_source": session.close_source if session else "",
        **close_state,
        "summary": {
            "expected_count": len(roster),
            "checked_in_count": checked_in_count,
            "waiting_count": waiting_count,
            "blocked_count": blocked_count,
        },
        "roster": detail_roster,
    }
