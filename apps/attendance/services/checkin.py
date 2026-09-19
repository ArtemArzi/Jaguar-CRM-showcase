from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.attendance.models import (
    COMPLETE_PERSONAL_TERMS_VERSION_VALUES,
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMembership,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.clubs.models import Club
from apps.clubs.timezones import club_local_day_start_by_id, club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import Trainer

logger = logging.getLogger(__name__)
_SUBSCRIPTION_LOOKUP_MISSING = object()


def _active_through_date_q(*, field_name: str, target_date: date, club_id: int) -> Q:
    return Q(**{f"{field_name}__isnull": True}) | Q(
        **{f"{field_name}__gt": club_local_day_start_by_id(club_id, target_date)}
    )


def _selected_personal_booking_entitlement_evidence(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    checkin_date: date,
) -> tuple[int | None, int | None]:
    """Return immutable subscription/component evidence for one booked visit."""
    event = (
        ScheduleBookingEvent.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            schedule_id=schedule_id,
            effective_date=checkin_date,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            enrollment__created_from__in=[
                ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
                ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
            ],
            enrollment__starts_on=checkin_date,
            enrollment__ends_on=checkin_date,
        )
        .select_related("schedule")
        .order_by("-created_at", "-id")
        .first()
    )
    metadata = event.metadata if event is not None else {}
    raw_subscription_id = (metadata or {}).get("subscription_id")
    raw_component_id = (metadata or {}).get("subscription_component_id")
    subscription_id = raw_subscription_id if isinstance(raw_subscription_id, int) and raw_subscription_id > 0 else None
    component_id = raw_component_id if isinstance(raw_component_id, int) and raw_component_id > 0 else None
    if subscription_id is not None and component_id is None and event is not None:
        from apps.attendance.services.enrollment import personal_booking_event_component_id

        component_id = personal_booking_event_component_id(
            event=event,
            subscription_id=subscription_id,
        )
    return subscription_id, component_id


def _selected_personal_booking_subscription_id(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    checkin_date: date,
) -> int | None:
    return _selected_personal_booking_entitlement_evidence(
        club_id=club_id,
        student_id=student_id,
        schedule_id=schedule_id,
        checkin_date=checkin_date,
    )[0]


def async_task(*args, **kwargs):
    # Resolve at call time from the services package namespace so that
    # `@patch("apps.attendance.services.async_task")` in tests takes effect.
    from apps.attendance import services as _services

    return _services.async_task(*args, **kwargs)


def _existing_checkin_result(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    checkin_date: date,
) -> dict | None:
    existing = (
        Checkin.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            schedule_id=schedule_id,
            date=checkin_date,
            deleted_at__isnull=True,
        )
        .first()
    )
    if existing is None:
        return None

    return {
        "checkin_id": existing.id,
        "is_debt": existing.is_debt,
        "subscription_id": existing.subscription_id,
        "created": False,
    }


def _preview_pending_manual_admission_payment_ids(
    *,
    club_id: int,
    student_ids: list[int],
    schedule_id: int,
) -> dict[int, list[int]]:
    """Read candidate payment ids before an attendance writer locks a schedule.

    Legacy payments stay exact to their target schedule.  A payment-owned
    group admission intentionally applies to every current slot in its group.
    """
    candidates: dict[int, list[int]] = {student_id: [] for student_id in student_ids}
    if not candidates:
        return candidates
    target_group_id = (
        Schedule.objects.for_club(club_id).filter(id=schedule_id).values_list("training_group_id", flat=True).first()
    )
    admission_target = Q(
        target_schedule_id=schedule_id,
        conversion_enrollment__isnull=False,
        conversion_enrollment__schedule_id=schedule_id,
    )
    if target_group_id is not None:
        admission_target |= Q(
            target_training_group_id=target_group_id,
            conversion_group_membership__training_group_id=target_group_id,
            conversion_enrollment__isnull=False,
        )
    for student_id, payment_id in (
        Payment.objects.for_club(club_id)
        .filter(
            student_id__in=candidates,
            payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            status=Payment.Status.PENDING,
        )
        .filter(admission_target)
        .order_by("student_id", "id")
        .values_list("student_id", "id")
    ):
        candidates[student_id].append(payment_id)
    return candidates


def _lock_pending_manual_admission_payments(
    *,
    club_id: int,
    payment_ids: list[int],
) -> dict[str, set[int]]:
    """Lock Payment then Subscription; defer enrollment to identity scope."""
    unique_payment_ids = sorted(set(payment_ids))
    if not unique_payment_ids:
        return {"payment_ids": set(), "enrollment_ids": set()}

    # Payment is the financial root.  Derive its current dependents only after
    # its ascending lock has been acquired; never lock an enrollment here.
    list(
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=unique_payment_ids)
        .order_by("id")
    )
    payment_roots = list(
        Payment.objects.for_club(club_id)
        .filter(id__in=unique_payment_ids)
        .order_by("id")
        .values("id", "subscription_id", "conversion_enrollment_id")
    )
    subscription_ids = sorted({row["subscription_id"] for row in payment_roots if row["subscription_id"]})
    enrollment_ids = sorted(
        {row["conversion_enrollment_id"] for row in payment_roots if row["conversion_enrollment_id"]}
    )
    if subscription_ids:
        list(
            Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=subscription_ids)
            .order_by("id")
        )
    return {
        "payment_ids": {row["id"] for row in payment_roots},
        "enrollment_ids": set(enrollment_ids),
    }


def _load_prelocked_pending_manual_admission_payments(
    *,
    club_id: int,
    payment_ids: set[int],
) -> dict[int, list[Payment]]:
    """Read roots after the identity scope locked their conversion enrollments."""
    payments_by_student: dict[int, list[Payment]] = {}
    for payment in (
        Payment.objects.for_club(club_id)
        .select_related(
            "subscription__tariff",
            "conversion_enrollment",
            "conversion_group_membership",
            "target_schedule",
        )
        .filter(id__in=payment_ids)
        .order_by("id")
    ):
        payments_by_student.setdefault(payment.student_id, []).append(payment)
    return payments_by_student


def _preview_personal_drop_in_financial_scope(
    *, club_id: int, student_id: int, schedule_id: int, target_date: date
) -> dict[str, object]:
    """Read the exact scheduled drop-in dependency graph without locking it.

    This preview deliberately starts with the immutable booking occurrence.  It
    does not inspect a student's unrelated payment history.
    """
    booking_id = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            enrollment__student_id=student_id,
            enrollment__schedule_id=schedule_id,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            state=PersonalDropInBooking.State.SCHEDULED,
        )
        .values_list("id", flat=True)
        .first()
    )
    if booking_id is None:
        # A confirmed SBP personal reservation creates a personal booking
        # enrollment without a ``PersonalDropInBooking`` row.  It is still a
        # complete immutable personal origin, so check-in must discover it
        # before it locks generic Payment/Subscription candidates.
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(
                enrollment__student_id=student_id,
                enrollment__schedule_id=schedule_id,
                enrollment__starts_on=target_date,
                enrollment__ends_on=target_date,
                status=PersonalBookingPaymentReservation.Status.BOOKED,
                terms_snapshot__terms_version__in=COMPLETE_PERSONAL_TERMS_VERSION_VALUES,
            )
            .values("id", "payment_id", "bank_payment_order_id")
            .first()
        )
        if reservation is not None:
            payment_ids = [reservation["payment_id"]] if reservation["payment_id"] else []
            order_ids = [reservation["bank_payment_order_id"]] if reservation["bank_payment_order_id"] else []
            return {
                "booking_id": None,
                "reservation_ids": [reservation["id"]],
                "link_ids": [],
                "payment_ids": payment_ids,
                "order_ids": order_ids,
                "refund_case_ids": list(
                    PaymentRefundCase.objects.for_club(club_id)
                    .filter(order_id__in=order_ids)
                    .order_by("id")
                    .values_list("id", flat=True)
                ),
            }
        return {
            "booking_id": None,
            "reservation_ids": [],
            "link_ids": [],
            "payment_ids": [],
            "order_ids": [],
            "refund_case_ids": [],
        }
    link_rows = list(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .filter(booking_id=booking_id)
        .order_by("id")
        .values("id", "payment_id", "bank_payment_order_id")
    )
    # The bank-order origin is committed before the attendance link.  Include
    # that small claim-to-link window in the complete check-in scope so an
    # arrival cannot create an unreserved debt beside a live payment family.
    snapshot_rows = list(
        BankPaymentOrder.objects.for_club(club_id)
        .filter(personal_drop_in_booking_id_snapshot=booking_id)
        .order_by("id")
        .values("id", "payment_id")
    )
    payment_ids = sorted(
        {row["payment_id"] for row in link_rows}
        | {row["payment_id"] for row in snapshot_rows}
    )
    order_ids = sorted(
        {
            *[row["bank_payment_order_id"] for row in link_rows if row["bank_payment_order_id"]],
            *[row["id"] for row in snapshot_rows],
            *BankPaymentOrder.objects.for_club(club_id).filter(payment_id__in=payment_ids).values_list("id", flat=True),
        }
    )
    refund_case_ids = list(
        PaymentRefundCase.objects.for_club(club_id)
        .filter(order_id__in=order_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    return {
        "booking_id": booking_id,
        "reservation_ids": [],
        "link_ids": [row["id"] for row in link_rows],
        "payment_ids": payment_ids,
        "order_ids": order_ids,
        "refund_case_ids": refund_case_ids,
    }


def _selected_personal_booking_subscription_ids(
    *, club_id: int, student_ids: list[int], schedule_id: int, checkin_date: date
) -> dict[int, int | None]:
    """Freeze the exact self-booking entitlement choice before identity locks."""
    return {
        student_id: _selected_personal_booking_subscription_id(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            checkin_date=checkin_date,
        )
        for student_id in student_ids
    }


def _preview_checkin_subscription_candidates(
    *,
    club_id: int,
    student_ids: list[int],
    training_type_id: int,
    location_id: int,
    checkin_date: date,
) -> dict[str, object]:
    """Read every financial candidate the create path can later select.

    The broad component set also preserves the distinct
    ``subscription_component_limit_exceeded`` branch without a late financial
    read below the schedule lock.
    """
    normalized_student_ids = sorted(set(student_ids))
    base_components = (
        SubscriptionComponent.objects.for_club(club_id)
        .filter(
            subscription__student_id__in=normalized_student_ids,
            subscription__status=Subscription.Status.ACTIVE,
            subscription__deleted_at__isnull=True,
            training_type_id=training_type_id,
            is_active=True,
        )
        .filter(
            _active_through_date_q(
                field_name="subscription__expires_at",
                target_date=checkin_date,
                club_id=club_id,
            )
        )
    )
    matching_component_rows = list(base_components.order_by("subscription_id", "id").values("id", "subscription_id"))
    eligible_component_rows = list(
        base_components.filter(Q(credits_left__isnull=True) | Q(credits_left__gt=0))
        .filter(Q(scope=Tariff.Scope.LOCATION, location_id=location_id) | Q(scope=Tariff.Scope.CLUB))
        .order_by("subscription_id", "id")
        .values("id", "subscription_id")
    )
    legacy_subscription_ids = list(
        Subscription.objects.for_club(club_id)
        .filter(
            student_id__in=normalized_student_ids,
            status=Subscription.Status.ACTIVE,
            tariff__training_type_id=training_type_id,
            deleted_at__isnull=True,
            components__isnull=True,
        )
        .filter(
            _active_through_date_q(
                field_name="expires_at",
                target_date=checkin_date,
                club_id=club_id,
            )
        )
        .filter(Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
        .filter(Q(scope=Tariff.Scope.LOCATION, location_id=location_id) | Q(scope=Tariff.Scope.CLUB))
        .order_by(F("expires_at").asc(nulls_last=True), "id")
        .values_list("id", flat=True)
    )
    return {
        "matching_component_ids": [row["id"] for row in matching_component_rows],
        "eligible_component_ids": [row["id"] for row in eligible_component_rows],
        "legacy_subscription_ids": legacy_subscription_ids,
        "subscription_ids": sorted(
            {row["subscription_id"] for row in matching_component_rows} | set(legacy_subscription_ids)
        ),
    }


def _assert_same_ids(*, expected: list[int], locked: list[int], code: str, message: str) -> None:
    if expected != locked:
        raise BusinessLogicError(message, code=code)


def _preview_checkin_create_financial_scope(
    *,
    club_id: int,
    student_ids: list[int],
    schedule_id: int,
    training_type_id: int,
    checkin_date: date,
) -> dict[str, object]:
    """Capture all roots a check-in create may use before any identity lock."""
    normalized_student_ids = sorted(set(student_ids))
    location_id = (
        Schedule.objects.for_club(club_id).filter(id=schedule_id).values_list("location_id", flat=True).first()
    )
    if location_id is None:
        raise Schedule.DoesNotExist
    personal_scopes = {
        student_id: _preview_personal_drop_in_financial_scope(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            target_date=checkin_date,
        )
        for student_id in normalized_student_ids
    }
    selected_subscription_ids = _selected_personal_booking_subscription_ids(
        club_id=club_id,
        student_ids=[
            student_id
            for student_id in normalized_student_ids
            if personal_scopes[student_id]["booking_id"] is None
            and not personal_scopes[student_id]["reservation_ids"]
        ],
        schedule_id=schedule_id,
        checkin_date=checkin_date,
    )
    selected_subscription_ids.update(
        {student_id: None for student_id in normalized_student_ids if student_id not in selected_subscription_ids}
    )
    return {
        "student_ids": normalized_student_ids,
        "schedule_id": schedule_id,
        "training_type_id": training_type_id,
        "checkin_date": checkin_date,
        "pending_payment_ids_by_student": _preview_pending_manual_admission_payment_ids(
            club_id=club_id,
            student_ids=normalized_student_ids,
            schedule_id=schedule_id,
        ),
        "personal_scopes": personal_scopes,
        "selected_subscription_ids": selected_subscription_ids,
        "subscription_candidates": _preview_checkin_subscription_candidates(
            club_id=club_id,
            student_ids=normalized_student_ids,
            training_type_id=training_type_id,
            location_id=location_id,
            checkin_date=checkin_date,
        ),
    }


def _lock_checkin_create_financial_scope(*, club_id: int, scope: dict[str, object]) -> dict[str, object]:
    """Lock the financial tail after a complete personal scope, in D12 order."""
    personal_scopes = scope["personal_scopes"]
    pending_by_student = scope["pending_payment_ids_by_student"]
    refund_case_ids = sorted(
        {
            refund_case_id
            for personal_scope in personal_scopes.values()
            for refund_case_id in personal_scope["refund_case_ids"]
        }
    )
    order_ids = sorted(
        {order_id for personal_scope in personal_scopes.values() for order_id in personal_scope["order_ids"]}
    )
    personal_payment_ids = {
        payment_id for personal_scope in personal_scopes.values() for payment_id in personal_scope["payment_ids"]
    }
    pending_payment_ids = {payment_id for payment_ids in pending_by_student.values() for payment_id in payment_ids}
    payment_ids = sorted(personal_payment_ids | pending_payment_ids)

    # The owner scope locks Trainer/Student/slot/booking/enrollment first.
    # Generic check-ins still reach this helper unchanged, but its internal
    # financial tail is always Payment -> Subscription -> BankOrder -> audit.
    locked_payment_ids = list(
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=payment_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    _assert_same_ids(
        expected=payment_ids,
        locked=locked_payment_ids,
        code="checkin_financial_scope_changed",
        message="Check-in payment roots changed before attendance could be locked.",
    )

    payment_roots = list(
        Payment.objects.for_club(club_id)
        .filter(id__in=payment_ids)
        .order_by("id")
        .values(
            "id",
            "student_id",
            "subscription_id",
            "conversion_enrollment_id",
            "target_training_group_id",
        )
    )
    order_subscription_ids = {
        subscription_id
        for subscription_id in BankPaymentOrder.objects.for_club(club_id)
        .filter(id__in=order_ids)
        .values_list("subscription_id", flat=True)
        if subscription_id is not None
    }
    candidates = scope["subscription_candidates"]
    subscription_ids = sorted(
        set(candidates["subscription_ids"])
        | {row["subscription_id"] for row in payment_roots if row["subscription_id"]}
        | order_subscription_ids
    )
    locked_subscription_ids = list(
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=subscription_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    _assert_same_ids(
        expected=subscription_ids,
        locked=locked_subscription_ids,
        code="checkin_financial_scope_changed",
        message="Check-in subscription roots changed before attendance could be locked.",
    )

    extra_component_ids = list(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(
            subscription_id__in=subscription_ids,
            is_active=True,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    component_ids = sorted(set(candidates["matching_component_ids"]) | set(extra_component_ids))
    locked_component_ids = list(
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=component_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    _assert_same_ids(
        expected=component_ids,
        locked=locked_component_ids,
        code="checkin_financial_scope_changed",
        message="Check-in subscription components changed before attendance could be locked.",
    )

    for model, ids, label in (
        (BankPaymentOrder, order_ids, "bank order"),
        (PaymentRefundCase, refund_case_ids, "financial audit"),
    ):
        locked_ids = list(
            model.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=ids)
            .order_by("id")
            .values_list("id", flat=True)
        )
        _assert_same_ids(
            expected=ids,
            locked=locked_ids,
            code="checkin_financial_scope_changed",
            message=f"Check-in {label} roots changed before attendance could be locked.",
        )

    current_candidates = _preview_checkin_subscription_candidates(
        club_id=club_id,
        student_ids=scope["student_ids"],
        training_type_id=scope["training_type_id"],
        location_id=(
            Schedule.objects.for_club(club_id)
            .filter(id=scope["schedule_id"])
            .values_list("location_id", flat=True)
            .get()
        ),
        checkin_date=scope["checkin_date"],
    )
    if current_candidates != candidates:
        raise BusinessLogicError(
            "Check-in subscription candidates changed before attendance could be locked.",
            code="checkin_subscription_scope_changed",
        )

    scope["payment_rows_by_id"] = {
        payment.id: payment
        for payment in Payment.objects.for_club(club_id)
        .select_related("subscription__tariff")
        .filter(id__in=payment_ids)
        .order_by("id")
    }
    scope["subscription_rows_by_id"] = {
        subscription.id: subscription
        for subscription in Subscription.objects.for_club(club_id)
        .select_related("tariff")
        .filter(id__in=subscription_ids)
        .order_by("id")
    }
    scope["subscription_components_by_id"] = {
        component.id: component
        for component in SubscriptionComponent.objects.for_club(club_id)
        .select_related("subscription", "subscription__tariff", "training_type", "location")
        .filter(id__in=component_ids)
        .order_by("id")
    }
    scope["pending_enrollment_ids"] = {
        row["conversion_enrollment_id"]
        for row in payment_roots
        if (
            row["id"] in pending_payment_ids
            and row["conversion_enrollment_id"]
            # A canonical group payment's immutable anchor enrollment can be
            # a different slot.  The requested slot's generated projection is
            # already discovered and locked by the attendance identity scope.
            and row["target_training_group_id"] is None
        )
    }
    scope["pending_payments_by_student"] = {}
    for row in payment_roots:
        if row["id"] in pending_payment_ids:
            scope["pending_payments_by_student"].setdefault(row["student_id"], []).append(
                scope["payment_rows_by_id"][row["id"]]
            )
    for personal_scope in personal_scopes.values():
        personal_scope["payment_rows_by_id"] = scope["payment_rows_by_id"]
    return scope


def _assert_checkin_create_financial_scope_unchanged(*, club_id: int, scope: dict[str, object]) -> None:
    """Fail closed if a read-only post-identity re-preview finds a new root."""
    current = _preview_checkin_create_financial_scope(
        club_id=club_id,
        student_ids=scope["student_ids"],
        schedule_id=scope["schedule_id"],
        training_type_id=scope["training_type_id"],
        checkin_date=scope["checkin_date"],
    )
    for key in (
        "pending_payment_ids_by_student",
        "selected_subscription_ids",
        "subscription_candidates",
    ):
        if current[key] != scope[key]:
            raise BusinessLogicError(
                "Check-in financial scope changed while attendance was starting.",
                code="checkin_financial_scope_changed",
            )
    expected_personal_scopes = {
        student_id: {
            key: personal_scope[key]
            for key in (
                "booking_id",
                "reservation_ids",
                "link_ids",
                "payment_ids",
                "order_ids",
                "refund_case_ids",
            )
        }
        for student_id, personal_scope in scope["personal_scopes"].items()
    }
    if current["personal_scopes"] != expected_personal_scopes:
        raise BusinessLogicError(
            "Personal drop-in financial scope changed while attendance was starting.",
            code="checkin_financial_scope_changed",
        )


def _lock_personal_drop_in_dependencies(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    target_date: date,
    scope: dict[str, object],
    lock_bridges: bool = True,
) -> dict[str, object]:
    """Lock exact booking/link evidence before the financial tail.

    The second, bridge-attaching phase may run after financial rows only
    because the current transaction already owns the booking/link evidence.
    """
    booking = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("enrollment", "enrollment__schedule", "tariff")
        .filter(
            enrollment__student_id=student_id,
            enrollment__schedule_id=schedule_id,
            enrollment__starts_on=target_date,
            enrollment__ends_on=target_date,
            state=PersonalDropInBooking.State.SCHEDULED,
        )
        .first()
    )
    expected_booking_id = scope["booking_id"]
    if (booking.id if booking is not None else None) != expected_booking_id:
        raise BusinessLogicError(
            "Personal drop-in booking changed while attendance was starting.",
            code="personal_drop_in_scope_changed",
        )
    if booking is None:
        return {"booking": None, "links": []}
    links = list(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(booking_id=booking.id)
        .order_by("id")
    )
    if [link.id for link in links] != scope["link_ids"]:
        raise BusinessLogicError(
            "Personal drop-in payment links changed while attendance was starting.",
            code="personal_drop_in_scope_changed",
        )
    if not lock_bridges:
        return {"booking": booking, "links": links}
    payments_by_id = scope["payment_rows_by_id"]
    linked_order_ids = {link.bank_payment_order_id for link in links if link.bank_payment_order_id}
    live_order_statuses = {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.APPROVED,
        BankPaymentOrder.Status.AUTHORIZED,
        BankPaymentOrder.Status.MANUAL_REVIEW,
    }
    # The financial tail has already locked these order/payment rows.  Bridge
    # an unattached live snapshot now; the original creator will discover the
    # same bridge by order ID and return it without provider I/O.
    for order in (
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            id__in=scope["order_ids"],
            personal_drop_in_booking_id_snapshot=booking.id,
            status__in=live_order_statuses,
        )
        .order_by("id")
    ):
        if order.id in linked_order_ids:
            continue
        payment = payments_by_id.get(order.payment_id)
        if payment is None or payment.status not in {Payment.Status.PENDING, Payment.Status.CONFIRMED}:
            continue
        bridge = PersonalDropInPaymentLink(
            club_id=club_id,
            booking=booking,
            payment_id=order.payment_id,
            bank_payment_order=order,
            created_by_id=order.created_by_id,
            idempotency_key=f"personal-bank-order-claim-{order.id}",
        )
        bridge.full_clean()
        bridge.save()
        bridge._state.fields_cache["payment"] = payment
        links.append(bridge)
        linked_order_ids.add(order.id)
    for link in links:
        payment = payments_by_id.get(link.payment_id)
        if payment is None:
            raise BusinessLogicError(
                "Personal drop-in payment scope changed while attendance was starting.",
                code="personal_drop_in_scope_changed",
            )
        link._state.fields_cache["payment"] = payment
    return {"booking": booking, "links": links}


def _resolve_prelocked_subscription(
    *,
    student: Student,
    schedule: Schedule,
    checkin_date: date,
    selected_subscription_id: int | None,
    scope: dict[str, object],
) -> tuple[Subscription | None, SubscriptionComponent | None, bool]:
    """Resolve the preserved priority from rows locked before identity."""
    components = scope["subscription_components_by_id"].values()
    matching_component_ids = set(scope["subscription_candidates"]["matching_component_ids"])
    # Rebuild the original candidate predicate explicitly.  The split avoids a
    # new Subscription/Component query below the schedule lock.
    eligible_components = [
        component
        for component in components
        if component.id in set(scope["subscription_candidates"]["eligible_component_ids"])
        and component.subscription.student_id == student.id
        and (selected_subscription_id is None or component.subscription_id == selected_subscription_id)
    ]
    for component_scope in (Tariff.Scope.LOCATION, Tariff.Scope.CLUB):
        for component in sorted(
            (
                component
                for component in eligible_components
                if component.scope == component_scope
                and (component_scope != Tariff.Scope.LOCATION or component.location_id == schedule.location_id)
            ),
            key=lambda component: (
                component.subscription.expires_at is None,
                component.subscription.expires_at,
                component.subscription_id,
                component.id,
            ),
        ):
            if _component_has_weekly_capacity(component=component, checkin_date=checkin_date):
                return component.subscription, component, True
    subscription_by_id = scope["subscription_rows_by_id"]
    legacy_subscriptions = [
        subscription_by_id[subscription_id]
        for subscription_id in scope["subscription_candidates"]["legacy_subscription_ids"]
        if subscription_id in subscription_by_id
        and subscription_by_id[subscription_id].student_id == student.id
        and (selected_subscription_id is None or subscription_id == selected_subscription_id)
    ]
    for subscription_scope in (Tariff.Scope.LOCATION, Tariff.Scope.CLUB):
        for subscription in sorted(
            (
                subscription
                for subscription in legacy_subscriptions
                if subscription.scope == subscription_scope
                and (subscription_scope != Tariff.Scope.LOCATION or subscription.location_id == schedule.location_id)
            ),
            key=lambda subscription: (
                subscription.expires_at is None,
                subscription.expires_at,
                subscription.id,
            ),
        ):
            return subscription, None, True
    has_matching_component = any(
        component.id in matching_component_ids and component.subscription.student_id == student.id
        for component in components
    )
    return None, None, has_matching_component


def _prelocked_component_for_subscription(
    *, subscription_id: int, training_type_id: int, scope: dict[str, object]
) -> SubscriptionComponent | None:
    """Match the explicit-subscription deduction branch without a late query."""
    return next(
        (
            component
            for component in scope["subscription_components_by_id"].values()
            if component.subscription_id == subscription_id
            and component.training_type_id == training_type_id
            and component.is_active
            and (component.credits_left is None or component.credits_left > 0)
        ),
        None,
    )


def _preview_attendance_identity_scope(
    *,
    club_id: int,
    schedule_id: int,
    student_ids: list[int],
) -> dict[str, object]:
    """Read the exact identity rows that must be locked before attendance."""
    normalized_student_ids = sorted(set(student_ids))
    schedule = (
        Schedule.objects.for_club(club_id)
        .filter(id=schedule_id)
        .values("id", "trainer_id", "training_group_id")
        .first()
    )
    if schedule is None:
        raise Schedule.DoesNotExist
    existing_student_ids = list(
        Student.objects.for_club(club_id)
        .filter(id__in=normalized_student_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    group_id = schedule["training_group_id"]
    membership_ids: list[int] = []
    if group_id is not None:
        membership_ids = list(
            TrainingGroupMembership.objects.for_club(club_id)
            .filter(student_id__in=normalized_student_ids, training_group_id=group_id)
            .order_by("id")
            .values_list("id", flat=True)
        )
    enrollment_ids = list(
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(student_id__in=normalized_student_ids, schedule_id=schedule_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    return {
        "schedule_id": schedule["id"],
        "trainer_id": schedule["trainer_id"],
        "training_group_id": group_id,
        "requested_student_ids": normalized_student_ids,
        "student_ids": existing_student_ids,
        "membership_ids": membership_ids,
        "enrollment_ids": enrollment_ids,
    }


def _lock_attendance_identity_scope(
    *,
    club_id: int,
    schedule_id: int,
    student_ids: list[int],
    required_enrollment_ids: set[int] | None = None,
    missing_student_code: str | None = None,
) -> tuple[Schedule, dict[int, Student]]:
    """Lock Trainer -> sorted Student -> attendance roots in global D12 order."""
    preview = _preview_attendance_identity_scope(
        club_id=club_id,
        schedule_id=schedule_id,
        student_ids=student_ids,
    )
    required_enrollment_ids = required_enrollment_ids or set()
    if not required_enrollment_ids.issubset(set(preview["enrollment_ids"])):
        raise BusinessLogicError(
            "Pending admission identity scope changed before it could be locked.",
            code="pending_manual_admission_scope_changed",
        )
    if preview["trainer_id"] is not None:
        locked_trainers = list(
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=preview["trainer_id"])
            .order_by("id")
            .values_list("id", flat=True)
        )
        if locked_trainers != [preview["trainer_id"]]:
            raise BusinessLogicError(
                "Trainer identity changed before attendance could be locked.",
                code="attendance_identity_scope_changed",
            )
    locked_students = list(
        Student.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=preview["requested_student_ids"])
        .order_by("id")
    )
    if [student.id for student in locked_students] != preview["requested_student_ids"]:
        if missing_student_code is None:
            raise Student.DoesNotExist
        raise BusinessLogicError("Student is unavailable for attendance.", code=missing_student_code)
    group_id = preview["training_group_id"]
    if group_id is not None:
        locked_groups = list(
            TrainingGroup.objects.for_club(club_id).select_for_update(of=("self",)).filter(id=group_id).order_by("id")
        )
        if len(locked_groups) != 1:
            raise BusinessLogicError(
                "Training group identity changed before attendance could be locked.",
                code="attendance_identity_scope_changed",
            )
    schedule = (
        Schedule.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("club", "trainer", "location", "training_type")
        .get(id=schedule_id)
    )
    if preview["membership_ids"]:
        list(
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=preview["membership_ids"])
            .order_by("id")
        )
    if preview["enrollment_ids"]:
        list(
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=preview["enrollment_ids"])
            .order_by("id")
        )
    if (
        _preview_attendance_identity_scope(
            club_id=club_id,
            schedule_id=schedule_id,
            student_ids=student_ids,
        )
        != preview
    ):
        raise BusinessLogicError(
            "Attendance identity scope changed while it was being locked.",
            code="attendance_identity_scope_changed",
        )
    return schedule, {student.id: student for student in locked_students}


def _assert_pending_manual_admission_scope_unchanged(
    *,
    club_id: int,
    student_ids: list[int],
    schedule_id: int,
    expected_payment_ids_by_student: dict[int, list[int]],
) -> None:
    if (
        _preview_pending_manual_admission_payment_ids(
            club_id=club_id,
            student_ids=student_ids,
            schedule_id=schedule_id,
        )
        != expected_payment_ids_by_student
    ):
        raise BusinessLogicError(
            "Pending admission scope changed while attendance was starting.",
            code="pending_manual_admission_scope_changed",
        )


def _get_locked_pending_manual_admission_payment(
    *,
    payments: list[Payment],
    student_id: int,
    schedule: Schedule,
    target_date: date,
    club: Club,
) -> Payment | None:
    """Choose an exact eligible payment from roots locked before the schedule."""
    valid_payments: list[Payment] = []
    for payment in payments:
        subscription = payment.subscription
        enrollment = payment.conversion_enrollment
        if (
            payment.student_id != student_id
            or subscription is None
            or enrollment is None
            or payment.target_start_date is None
            or payment.target_start_date > target_date
            or payment.target_training_type_id_snapshot != schedule.training_type_id
            or payment.target_location_id_snapshot != schedule.location_id
            or subscription.status != Subscription.Status.PENDING
            or subscription.deleted_at is not None
            or enrollment.student_id != student_id
            or enrollment.status != ScheduleEnrollment.Status.ACTIVE
            or enrollment.starts_on != payment.target_start_date
        ):
            continue
        if payment.conversion_group_membership_id is not None:
            membership = payment.conversion_group_membership
            if (
                schedule.training_group_id is None
                or payment.target_training_group_id != schedule.training_group_id
                or membership is None
                or membership.training_group_id != schedule.training_group_id
                or enrollment.training_group_membership_id != membership.id
                or enrollment.created_from != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
            ):
                continue
        elif payment.target_schedule_id != schedule.id or enrollment.schedule_id != schedule.id:
            continue
        expires_at = club_local_day_start_by_id(
            club.id,
            payment.target_start_date + timedelta(days=subscription.tariff.duration_days),
        )
        if club_local_day_start_by_id(club.id, target_date) < expires_at:
            valid_payments.append(payment)
    # Preserve the existing fail-closed ambiguity rule.
    return valid_payments[0] if len(valid_payments) == 1 else None


def create_checkin(
    *,
    club_id: int,
    student_id: int,
    schedule_id: int,
    training_type_id: int,
    source: str,
    checkin_date: date | None = None,
    _skip_group_analytics: bool = False,
    _defer_async_until_commit: bool = False,
    _prelocked_attendance_identity: tuple[Schedule, dict[int, Student]] | None = None,
    _prelocked_create_financial_scope: dict[str, object] | None = None,
    _prelocked_personal_drop_in_dependencies: dict[str, object] | None = None,
) -> dict:
    actual_date: date | None = checkin_date
    try:
        with transaction.atomic():
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            # All attendance writers serialize on the payroll scope and rollout
            # row before they touch a schedule.  For a pending admission, lock
            # financial roots before the later schedule/student validation.
            lock_training_group_mutation_scope(club_id=club_id)
            club = Club.objects.only("id", "timezone").get(id=club_id)
            actual_date = actual_date or club_localdate(club)
            financial_scope = _prelocked_create_financial_scope
            if _prelocked_attendance_identity is None:
                financial_scope = _preview_checkin_create_financial_scope(
                    club_id=club_id,
                    student_ids=[student_id],
                    schedule_id=schedule_id,
                    training_type_id=training_type_id,
                    checkin_date=actual_date,
                )
                from apps.attendance.services.personal_locking import lock_complete_personal_scopes

                lock_complete_personal_scopes(
                    club_id=club_id,
                    booking_ids=sorted(
                        scope["booking_id"]
                        for scope in financial_scope["personal_scopes"].values()
                        if scope["booking_id"] is not None
                    ),
                    reservation_ids=sorted(
                        reservation_id
                        for scope in financial_scope["personal_scopes"].values()
                        for reservation_id in scope["reservation_ids"]
                    ),
                    extra_student_ids=[student_id],
                    lock_financial=False,
                )
                # Every generic/exact check-in takes its attendance roots
                # before the Payment -> Subscription -> Component tail.  Do
                # this even without an existing personal booking: otherwise a
                # generic check-in can invert a self-service booking's
                # Trainer -> Student -> slot -> Subscription order.
                prelocked_identity = _lock_attendance_identity_scope(
                    club_id=club_id,
                    schedule_id=schedule_id,
                    student_ids=[student_id],
                )
                _lock_personal_drop_in_dependencies(
                    club_id=club_id,
                    student_id=student_id,
                    schedule_id=schedule_id,
                    target_date=actual_date,
                    scope=financial_scope["personal_scopes"][student_id],
                    lock_bridges=False,
                )
                financial_scope = _lock_checkin_create_financial_scope(
                    club_id=club_id,
                    scope=financial_scope,
                )
                schedule, students = prelocked_identity
                _assert_pending_manual_admission_scope_unchanged(
                    club_id=club_id,
                    student_ids=[student_id],
                    schedule_id=schedule_id,
                    expected_payment_ids_by_student=financial_scope["pending_payment_ids_by_student"],
                )
                _assert_checkin_create_financial_scope_unchanged(
                    club_id=club_id,
                    scope=financial_scope,
                )
                personal_dependencies = _lock_personal_drop_in_dependencies(
                    club_id=club_id,
                    student_id=student_id,
                    schedule_id=schedule_id,
                    target_date=actual_date,
                    scope=financial_scope["personal_scopes"][student_id],
                )
            else:
                # ``batch_checkin`` already locked the complete financial,
                # identity, and exact booking/link scopes in hierarchy order.
                schedule, students = _prelocked_attendance_identity
                if financial_scope is None or _prelocked_personal_drop_in_dependencies is None:
                    raise BusinessLogicError(
                        "Prelocked batch financial scope is incomplete.",
                        code="checkin_financial_scope_changed",
                    )
                personal_dependencies = _prelocked_personal_drop_in_dependencies
            if financial_scope is None:
                raise BusinessLogicError(
                    "Check-in financial scope is unavailable.",
                    code="checkin_financial_scope_changed",
                )
            pending_manual_admission_payments = list(financial_scope["pending_payments_by_student"].get(student_id, []))
            if schedule.id != schedule_id or student_id not in students:
                raise BusinessLogicError(
                    "Prelocked attendance identity does not match the requested check-in.",
                    code="attendance_identity_scope_changed",
                )
            student = students[student_id]
            _validate_training_type_matches_schedule(schedule=schedule, training_type_id=training_type_id)

            # Idempotency: same (student, schedule, date) -> return existing checkin.
            # Keep this before terminal session checks so double-click/retry stays a
            # no-op even after batch close created the GroupSession.
            existing_result = _existing_checkin_result(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                checkin_date=actual_date,
            )
            if existing_result is not None:
                from apps.leads.services import convert_lead_after_group_admission_checkin

                # A retry also repairs an older committed check-in whose
                # process ended before the exact debt evidence converted the
                # lead.  The conversion owner is idempotent.
                convert_lead_after_group_admission_checkin(
                    club_id=club_id,
                    student_id=student_id,
                    schedule_id=schedule_id,
                    checkin_id=existing_result["checkin_id"],
                    actor_user_id=None,
                )
                return existing_result

            # A flag-on group admission keeps a lead in the lead workspace
            # until either review confirmation or this exact check-in writes
            # the payment-owned debt reservation.  The financial roots are
            # already locked above, so this is a narrow eligibility exception
            # rather than a general lead check-in path.
            exact_pending_manual_admission = _get_locked_pending_manual_admission_payment(
                payments=pending_manual_admission_payments,
                student_id=student.id,
                schedule=schedule,
                target_date=actual_date,
                club=club,
            )
            if source == Checkin.Source.KIOSK:
                _validate_scheduled_checkin(
                    student=student,
                    schedule=schedule,
                    checkin_date=actual_date,
                    source_label="kiosk",
                    has_exact_pending_manual_admission=exact_pending_manual_admission is not None,
                )
            elif source == Checkin.Source.BATCH:
                _validate_scheduled_checkin(
                    student=student,
                    schedule=schedule,
                    checkin_date=actual_date,
                    source_label="batch",
                    has_exact_pending_manual_admission=exact_pending_manual_admission is not None,
                )

            # Validate one-time schedule date matches checkin date.
            if schedule.one_time_date is not None and schedule.one_time_date != actual_date:
                raise BusinessLogicError(
                    "Дата чек-ина не совпадает с датой разового занятия",
                    code="one_time_date_mismatch",
                )

            trainer = _resolve_trainer(schedule=schedule, checkin_date=actual_date)
            salary_subscription = _SUBSCRIPTION_LOOKUP_MISSING
            salary_subscription_component = _SUBSCRIPTION_LOOKUP_MISSING
            drop_in_booking = personal_dependencies["booking"]
            selected_personal_subscription_id = financial_scope["selected_subscription_ids"][student.id]
            (
                prelocked_subscription,
                prelocked_subscription_component,
                has_matching_subscription_component,
            ) = _resolve_prelocked_subscription(
                student=student,
                schedule=schedule,
                checkin_date=actual_date,
                selected_subscription_id=selected_personal_subscription_id,
                scope=financial_scope,
            )
            # Every entitled personal booking, including accepted Slice4 staff
            # intent, stores this exact evidence in its owner event.  Generic
            # selection is not allowed to substitute another component at
            # check-in after capacity/catalog changes.
            evidence_subscription_id, evidence_component_id = _selected_personal_booking_entitlement_evidence(
                club_id=club_id,
                student_id=student.id,
                schedule_id=schedule.id,
                checkin_date=actual_date,
            )
            if evidence_subscription_id is not None:
                reserved_subscription = financial_scope["subscription_rows_by_id"].get(evidence_subscription_id)
                if reserved_subscription is None:
                    raise BusinessLogicError(
                        "The selected subscription is no longer available for this check-in.",
                        code="selected_subscription_not_available",
                    )
                if evidence_component_id is None:
                    if any(
                        component.subscription_id == reserved_subscription.id
                        for component in financial_scope["subscription_components_by_id"].values()
                    ):
                        raise BusinessLogicError(
                            "The legacy personal booking has ambiguous component evidence.",
                            code="personal_booking_component_evidence_ambiguous",
                        )
                    prelocked_subscription = reserved_subscription
                    prelocked_subscription_component = None
                    has_matching_subscription_component = False
                else:
                    reserved_component = financial_scope["subscription_components_by_id"].get(
                        evidence_component_id
                    )
                    if (
                        reserved_component is None
                        or reserved_component.subscription_id != reserved_subscription.id
                    ):
                        raise BusinessLogicError(
                            "Reserved personal entitlement changed before check-in.",
                            code="personal_self_service_entitlement_scope_changed",
                        )
                    prelocked_subscription = reserved_subscription
                    prelocked_subscription_component = reserved_component
                    has_matching_subscription_component = True
            from apps.trainers.services import (
                assert_trainer_payroll_date_open,
                create_late_drop_in_settlement_credit,
                get_trainer_payroll_close_for_date,
            )

            cancelled_drop_in_checkins = []
            if drop_in_booking is not None:
                # Cancellation soft-deletes the check-in but preserves it as
                # the durable audit record that authorizes exactly one replay.
                cancelled_drop_in_checkins = list(
                    Checkin.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(
                        student_id=student.id,
                        schedule_id=schedule.id,
                        date=actual_date,
                        cancelled_at__isnull=False,
                    )
                    .only("id")
                )
                if len(cancelled_drop_in_checkins) >= 2:
                    raise BusinessLogicError(
                        "Personal drop-in replacement limit has been reached",
                        code="personal_drop_in_replacement_limit_reached",
                    )

            linked_payment = None
            if drop_in_booking is not None:
                linked_payment = max(
                    personal_dependencies["links"],
                    key=lambda link: (link.created_at, link.id),
                    default=None,
                )
            subscription_match = (
                (prelocked_subscription, prelocked_subscription_component)
                if prelocked_subscription is not None
                else None
            )
            if (
                source == Checkin.Source.MANUAL
                and student.crm_entry_kind == Student.CrmEntryKind.EXISTING_STUDENT
                and subscription_match is None
                and drop_in_booking is None
                and not pending_manual_admission_payments
            ):
                _validate_scheduled_checkin(
                    student=student,
                    schedule=schedule,
                    checkin_date=actual_date,
                    source_label="manual",
                )
            confirmed_subscription_component = None
            has_pending_drop_in_payment = bool(
                linked_payment is not None and linked_payment.payment.status == "pending"
            )
            has_confirmed_drop_in_payment = bool(
                linked_payment is not None
                and linked_payment.payment.status == "confirmed"
                and linked_payment.payment.subscription_id
            )
            closed_personal_drop_in_replacement = bool(
                drop_in_booking is not None
                and len(cancelled_drop_in_checkins) == 1
                and get_trainer_payroll_close_for_date(
                    club_id=club_id,
                    target_date=actual_date,
                )
                is not None
            )
            if linked_payment is None or not has_pending_drop_in_payment:
                if (
                    linked_payment is not None
                    and linked_payment.payment.status == "confirmed"
                    and linked_payment.payment.subscription_id
                ):
                    salary_subscription = linked_payment.payment.subscription
                    confirmed_subscription_component = _prelocked_component_for_subscription(
                        subscription_id=salary_subscription.id,
                        training_type_id=training_type_id,
                        scope=financial_scope,
                    )
            if salary_subscription is not _SUBSCRIPTION_LOOKUP_MISSING:
                if schedule.training_type.kind != TrainingType.Kind.GROUP and not closed_personal_drop_in_replacement:
                    assert_trainer_payroll_date_open(club_id=club_id, target_date=actual_date)
            elif subscription_match is not None:
                salary_subscription, salary_subscription_component = subscription_match
                if salary_subscription_component is None:
                    if (
                        schedule.training_type.kind != TrainingType.Kind.GROUP
                        and not closed_personal_drop_in_replacement
                    ):
                        assert_trainer_payroll_date_open(club_id=club_id, target_date=actual_date)
                elif (
                    salary_subscription_component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN
                    and not closed_personal_drop_in_replacement
                ):
                    assert_trainer_payroll_date_open(club_id=club_id, target_date=actual_date)
            elif not has_pending_drop_in_payment:
                salary_subscription = None
                salary_subscription_component = None
                if selected_personal_subscription_id is not None:
                    raise BusinessLogicError(
                        "Выбранный при записи абонемент больше недоступен",
                        code="selected_subscription_not_available",
                    )

            from apps.billing.service_modules.opening_attendance import assert_opening_attendance_not_covered

            assert_opening_attendance_not_covered(
                club_id=club_id,
                student_id=student.id,
                schedule=schedule,
                checkin_date=actual_date,
                subscription=(
                    salary_subscription if salary_subscription is not _SUBSCRIPTION_LOOKUP_MISSING else None
                ),
            )

            checkin = Checkin.objects.create(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                trainer=trainer,
                location=schedule.location,
                date=actual_date,
                source=source,
                notification_policy=(
                    Checkin.NotificationPolicy.SILENT_CORRECTION
                    if actual_date < club_localdate(club) else Checkin.NotificationPolicy.LIVE
                ),
            )

            if drop_in_booking is not None:
                from apps.attendance.services.drop_in import apply_personal_drop_in_checkin

                apply_personal_drop_in_checkin(
                    booking=drop_in_booking,
                    checkin=checkin,
                    student=student,
                    club=club,
                    prelocked_links=personal_dependencies["links"],
                    prelocked_subscription_component=confirmed_subscription_component,
                    prelocked_subscription_match=subscription_match,
                    prelocked_has_matching_subscription_component=has_matching_subscription_component,
                )
            else:
                _deduct_subscription(
                    student=student,
                    club_id=club_id,
                    schedule=schedule,
                    club=club,
                    training_type_id=training_type_id,
                    location=schedule.location,
                    checkin=checkin,
                    checkin_date=actual_date,
                    subscription=salary_subscription,
                    subscription_component=salary_subscription_component,
                    pending_manual_admission_payments=pending_manual_admission_payments,
                    has_matching_subscription_component=has_matching_subscription_component,
                )

            # The exact pending-admission Debt created above is durable
            # conversion evidence.  Keep its lead transition in this same
            # transaction so a process crash cannot strand the check-in.
            from apps.leads.services import convert_lead_after_group_admission_checkin

            convert_lead_after_group_admission_checkin(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                checkin_id=checkin.id,
                actor_user_id=None,
            )

            Student.objects.for_club(club_id).filter(id=student_id).filter(
                Q(last_visit_date__isnull=True) | Q(last_visit_date__lt=actual_date),
            ).update(last_visit_date=actual_date)

            # Ordinary scheduled check-in can reactivate recoverable inactive students.
            if actual_date >= club_localdate(club) and student.status in (
                Student.Status.AT_RISK,
                Student.Status.CHURNED,
            ):
                from apps.students.services import reactivate_student

                reactivate_student(student_id=student_id, club_id=club_id)

            cascade_events = _record_checkin_cascade_events(
                checkin=checkin,
                student=student,
                training_type=schedule.training_type,
                club_id=club_id,
                trainer_id=checkin.trainer_id,
                skip_group_analytics=_skip_group_analytics,
            )
            salary_expected = any(
                event.effect == CheckinCascadeEvent.Effect.SALARY and event.expected for event in cascade_events
            )
            if (
                closed_personal_drop_in_replacement
                and salary_subscription is not _SUBSCRIPTION_LOOKUP_MISSING
                and salary_subscription is not None
                and salary_expected
            ):
                upsert_salary_snapshot_for_checkin(
                    checkin=checkin,
                    club_id=club_id,
                    expect_salary=False,
                )
                create_late_drop_in_settlement_credit(
                    club_id=club_id,
                    checkin_id=checkin.id,
                    payment_id=(linked_payment.payment_id if has_confirmed_drop_in_payment else None),
                    confirmed_at=(
                        linked_payment.payment.verified_at or timezone.now()
                        if has_confirmed_drop_in_payment
                        else timezone.now()
                    ),
                )
                for event in cascade_events:
                    if event.effect == CheckinCascadeEvent.Effect.SALARY:
                        event.expected = False
    except IntegrityError:
        existing_result = _existing_checkin_result(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            checkin_date=actual_date,
        )
        if existing_result is not None:
            with transaction.atomic():
                from apps.leads.services import convert_lead_after_group_admission_checkin

                convert_lead_after_group_admission_checkin(
                    club_id=club_id,
                    student_id=student_id,
                    schedule_id=schedule_id,
                    checkin_id=existing_result["checkin_id"],
                    actor_user_id=None,
                )
            logger.info(
                "checkin_duplicate_race_skipped",
                extra={"student_id": student_id, "schedule_id": schedule_id, "club_id": club_id},
            )
            return existing_result
        raise

    from apps.leads.services import (
        complete_booked_trial_after_checkin,
    )

    complete_booked_trial_after_checkin(club_id=club_id, student_id=student_id, checkin=checkin)
    _enqueue_checkin_tasks(
        cascade_events=cascade_events,
        defer_until_commit=_defer_async_until_commit,
    )

    logger.info(
        "checkin_created",
        extra={"checkin_id": checkin.id, "student_id": student_id, "club_id": club_id},
    )
    return {
        "checkin_id": checkin.id,
        "is_debt": checkin.is_debt,
        "subscription_id": checkin.subscription_id,
        "created": True,
    }


def _validate_scheduled_checkin(
    *,
    student: Student,
    schedule: Schedule,
    checkin_date: date,
    source_label: str,
    has_exact_pending_manual_admission: bool = False,
) -> None:
    if student.deleted_at is not None:
        raise BusinessLogicError(
            f"Ученик недоступен для {source_label} check-in",
            code="student_ineligible",
        )

    is_exact_drop_in_lead = False
    if student.status == Student.Status.LEAD:
        from apps.attendance.services.drop_in import get_active_personal_drop_in_booking

        is_exact_drop_in_lead = (
            get_active_personal_drop_in_booking(
                club_id=schedule.club_id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=checkin_date,
            )
            is not None
        )
    is_exact_group_admission_lead = (
        student.status == Student.Status.LEAD
        and schedule.training_group_id is not None
        and has_exact_pending_manual_admission
    )
    if (
        student.status
        not in {
            Student.Status.ACTIVE,
            Student.Status.TRIAL,
            Student.Status.AT_RISK,
            Student.Status.CHURNED,
        }
        and not is_exact_drop_in_lead
        and not is_exact_group_admission_lead
    ):
        raise BusinessLogicError(
            f"Ученик недоступен для {source_label} check-in",
            code="student_ineligible",
        )

    from apps.attendance.training_group_roster import resolve_expected_roster_for_schedule_date

    _assert_schedule_occurrence_exists(schedule=schedule, checkin_date=checkin_date)
    if _is_group_session_closed(schedule=schedule, checkin_date=checkin_date):
        raise BusinessLogicError(
            "Тренировка уже закрыта тренером",
            code="group_session_closed",
        )

    expected_roster = resolve_expected_roster_for_schedule_date(
        club=schedule.club,
        schedule_id=schedule.id,
        target_date=checkin_date,
    )
    roster_entry = expected_roster.get(student.id)
    if roster_entry is None:
        raise BusinessLogicError(
            "Ученик не записан на выбранную тренировку",
            code="student_schedule_ineligible",
        )

    if roster_entry.blocked_reason or _has_frozen_schedule_enrollment(
        student=student,
        schedule=schedule,
        checkin_date=checkin_date,
    ):
        raise BusinessLogicError(
            "Запись ученика на эту тренировку заморожена",
            code="enrollment_frozen",
        )


def _get_schedule_occurrence(*, schedule: Schedule, checkin_date: date):
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    occurrences = [
        occurrence
        for occurrence in get_schedule_occurrences_for_date(
            club=schedule.club,
            target_date=checkin_date,
        )
        if occurrence.schedule_id == schedule.id
    ]
    if not occurrences:
        raise BusinessLogicError(
            "Нет подходящей тренировки на выбранную дату",
            code="schedule_occurrence_not_found",
        )
    if len(occurrences) > 1:
        raise BusinessLogicError(
            "На выбранную дату найдено несколько занятий одного расписания",
            code="schedule_occurrence_conflict",
        )
    return occurrences[0]


def _assert_schedule_occurrence_exists(*, schedule: Schedule, checkin_date: date) -> None:
    _get_schedule_occurrence(schedule=schedule, checkin_date=checkin_date)


def _assert_session_close_allowed(*, schedule: Schedule, checkin_date: date):
    from apps.attendance.selectors import get_session_close_state

    occurrence = _get_schedule_occurrence(
        schedule=schedule,
        checkin_date=checkin_date,
    )
    close_state = get_session_close_state(
        club=schedule.club,
        session_date=occurrence.effective_date,
        effective_end_time=occurrence.effective_end_time,
    )
    if not close_state["can_close"]:
        raise BusinessLogicError(
            "Занятие можно закрыть только после времени окончания",
            code="session_close_not_allowed_yet",
        )
    return occurrence


def _close_group_session(
    *,
    club_id: int,
    schedule: Schedule,
    checkin_date: date,
    trainer,
    attendee_count: int,
    actor_user_id: int,
    close_source: str,
    topic_tags: list[str] | None,
    notes: str,
) -> GroupSession:
    session = (
        GroupSession.objects.for_club(club_id)
        .select_for_update()
        .filter(
            schedule_id=schedule.id,
            date=checkin_date,
        )
        .first()
    )
    if session is None:
        return GroupSession.objects.create(
            club_id=club_id,
            schedule_id=schedule.id,
            date=checkin_date,
            trainer=trainer,
            attendee_count=attendee_count,
            topic_tags=topic_tags or [],
            notes=notes,
            closed_at=timezone.now(),
            closed_by_id=actor_user_id,
            close_source=close_source,
        )

    session.trainer = trainer
    session.attendee_count = attendee_count
    session.topic_tags = topic_tags or []
    session.notes = notes
    update_fields = [
        "trainer",
        "attendee_count",
        "topic_tags",
        "notes",
    ]
    if session.closed_at is None:
        session.closed_at = timezone.now()
        session.closed_by_id = actor_user_id
        session.close_source = close_source
        update_fields.extend(["closed_at", "closed_by", "close_source"])
    session.save(update_fields=[*update_fields, "updated_at"])
    return session


def _is_group_session_closed(*, schedule: Schedule, checkin_date: date) -> bool:
    return (
        GroupSession.objects.for_club(schedule.club_id)
        .filter(
            schedule_id=schedule.id,
            date=checkin_date,
            closed_at__isnull=False,
        )
        .exists()
    )


def _has_frozen_schedule_enrollment(
    *,
    student: Student,
    schedule: Schedule,
    checkin_date: date,
) -> bool:
    return (
        ScheduleEnrollment.objects.for_club(schedule.club_id)
        .filter(
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.FROZEN,
        )
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=checkin_date))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=checkin_date))
        .exists()
    )


def _enqueue_task_after_commit(task_name: str, *args, defer_until_commit: bool = False, **kwargs) -> None:
    def enqueue() -> None:
        async_task(task_name, *args, **kwargs)

    if defer_until_commit:
        transaction.on_commit(enqueue)
        return

    enqueue()


def _enqueue_checkin_tasks(
    *,
    cascade_events: list[CheckinCascadeEvent],
    defer_until_commit: bool,
) -> None:
    drop_in_checkin_ids = (
        set(
            PersonalDropInBooking.objects.for_club(cascade_events[0].club_id)
            .filter(checkin_id__in={event.checkin_id for event in cascade_events})
            .values_list("checkin_id", flat=True)
        )
        if cascade_events
        else set()
    )
    for event in cascade_events:
        if event.effect == CheckinCascadeEvent.Effect.POST_TRIAL_TASK and not event.expected:
            continue
        if (
            event.checkin_id in drop_in_checkin_ids
            and not event.expected
            and event.effect
            in {
                CheckinCascadeEvent.Effect.SALARY,
                CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
            }
        ):
            continue
        if event.effect == CheckinCascadeEvent.Effect.POST_TRIAL_TASK:
            _enqueue_task_after_commit(
                event.task_name,
                event.payload["student_id"],
                event.payload["club_id"],
                event.payload["trainer_id"],
                checkin_id=event.payload["checkin_id"],
                defer_until_commit=defer_until_commit,
            )
            continue

        _enqueue_task_after_commit(
            event.task_name,
            event.payload["checkin_id"],
            club_id=event.payload["club_id"],
            defer_until_commit=defer_until_commit,
        )


def _record_checkin_cascade_events(
    *,
    checkin: Checkin,
    student: Student,
    training_type: TrainingType,
    club_id: int,
    trainer_id: int,
    skip_group_analytics: bool,
) -> list[CheckinCascadeEvent]:
    drop_in_booking = PersonalDropInBooking.objects.for_club(club_id).filter(checkin_id=checkin.id).first()
    expected = _expected_checkin_cascade_effects(
        checkin=checkin,
        student=student,
        training_type=training_type,
        club_id=club_id,
        skip_group_analytics=skip_group_analytics,
    )
    # A paid personal drop-in is never a trial, even when its lead has not
    # converted yet. Preserve the effect ledger but suppress the workflow.
    if drop_in_booking is not None:
        expected[CheckinCascadeEvent.Effect.POST_TRIAL_TASK] = False
    specs = [
        (
            CheckinCascadeEvent.Effect.SALARY,
            "apps.attendance.tasks.calculate_salary",
            {"checkin_id": checkin.id, "club_id": club_id},
        ),
        (
            CheckinCascadeEvent.Effect.GRADE_PROGRESS,
            "apps.attendance.tasks.update_grade_progress",
            {"checkin_id": checkin.id, "club_id": club_id},
        ),
    ]
    if not skip_group_analytics:
        specs.append(
            (
                CheckinCascadeEvent.Effect.GROUP_ANALYTICS,
                "apps.attendance.tasks.update_group_analytics",
                {"checkin_id": checkin.id, "club_id": club_id},
            )
        )
    specs.extend(
        [
            (
                CheckinCascadeEvent.Effect.PARENT_NOTIFICATION,
                "apps.attendance.tasks.log_parent_event",
                {"checkin_id": checkin.id, "club_id": club_id},
            ),
            (
                CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE,
                "apps.retention.tasks.auto_close_retention_on_checkin",
                {"checkin_id": checkin.id, "club_id": club_id},
            ),
            (
                CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
                "apps.retention.tasks.create_post_trial_task",
                {
                    "checkin_id": checkin.id,
                    "student_id": student.id,
                    "club_id": club_id,
                    "trainer_id": trainer_id,
                },
            ),
            (
                CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH,
                "apps.notifications.tasks.check_trainings_left_push",
                {"checkin_id": checkin.id, "club_id": club_id},
            ),
        ]
    )

    events: list[CheckinCascadeEvent] = []
    for effect, task_name, payload in specs:
        if effect == CheckinCascadeEvent.Effect.SALARY and (expected.get(effect, False) or drop_in_booking is not None):
            payload = _salary_snapshot_payload_for_checkin(
                checkin=checkin,
                training_type=training_type,
                club_id=club_id,
                price_snapshot=(drop_in_booking.price_snapshot if drop_in_booking else None),
            )
        event = CheckinCascadeEvent(
            club_id=club_id,
            checkin=checkin,
            effect=effect,
            status=CheckinCascadeEvent.Status.QUEUED,
            expected=expected.get(effect, False),
            task_name=task_name,
            payload=payload,
        )
        event.full_clean()
        event.save()
        events.append(event)
    return events


def _salary_snapshot_payload_for_checkin(
    *,
    checkin: Checkin,
    training_type: TrainingType,
    club_id: int,
    price_snapshot=None,
) -> dict:
    from apps.trainers.selectors import resolve_trainer_rate

    rate = resolve_trainer_rate(
        club_id=club_id,
        trainer_id=checkin.trainer_id,
        location_id=checkin.location_id,
        training_type_id=checkin.training_type_id,
    )
    subscription_price = None
    component_id = None
    component_paid_amount_basis = None
    payout_policy = ""
    if checkin.subscription_component_id:
        component = checkin.subscription_component
        component_id = component.id
        component_paid_amount_basis = str(component.paid_amount_basis_snapshot)
        payout_policy = component.trainer_payout_policy_snapshot
        subscription_price = str(component.unit_amount_basis_snapshot or component.paid_amount_basis_snapshot)
    elif checkin.subscription_id:
        subscription_price = str(checkin.subscription.tariff.price)
        payout_policy = checkin.subscription.trainer_payout_policy_snapshot
    if price_snapshot is not None:
        subscription_price = str(price_snapshot)
        payout_policy = Tariff.PayoutPolicy.ON_CHECKIN

    return {
        "checkin_id": checkin.id,
        "club_id": club_id,
        "trainer_id_snapshot": checkin.trainer_id,
        "training_type_id_snapshot": checkin.training_type_id,
        "training_type_kind_snapshot": training_type.kind,
        "subscription_price_snapshot": subscription_price,
        "payout_policy_snapshot": payout_policy,
        "component_id_snapshot": component_id,
        "component_paid_amount_basis_snapshot": component_paid_amount_basis,
        "rate_percent_snapshot": str(rate) if rate is not None else None,
        "calculation_basis": "checkin_salary_snapshot",
        "snapshot_provenance": "checkin_queue",
    }


def upsert_salary_snapshot_for_checkin(
    *,
    checkin: Checkin,
    club_id: int,
    expect_salary: bool = True,
) -> None:
    event = (
        CheckinCascadeEvent.objects.for_club(club_id)
        .filter(
            checkin=checkin,
            effect=CheckinCascadeEvent.Effect.SALARY,
        )
        .first()
    )
    if event is not None:
        # A debt check-in initially carries the immutable booking price so the
        # deferred salary can be audited. Once it is settled, salary must use
        # the actual paid subscription/component basis (including discounts),
        # while the trainer/rate/type captured at attendance stay immutable.
        settled_payload = _salary_snapshot_payload_for_checkin(
            checkin=checkin,
            training_type=checkin.training_type,
            club_id=club_id,
        )
        payload = dict(event.payload or {})
        if "trainer_id_snapshot" not in payload:
            payload = settled_payload
        else:
            for key in (
                "subscription_price_snapshot",
                "payout_policy_snapshot",
                "component_id_snapshot",
                "component_paid_amount_basis_snapshot",
            ):
                payload[key] = settled_payload[key]
        payload["settlement_basis_provenance"] = "settled_subscription_component"
        event.expected = expect_salary
        event.status = CheckinCascadeEvent.Status.QUEUED
        event.task_name = "apps.attendance.tasks.calculate_salary"
        event.payload = payload
        event.save(update_fields=["expected", "status", "task_name", "payload", "updated_at"])
        return
    drop_in_booking = PersonalDropInBooking.objects.for_club(club_id).filter(checkin_id=checkin.id).first()
    payload = _salary_snapshot_payload_for_checkin(
        checkin=checkin,
        training_type=checkin.training_type,
        club_id=club_id,
        price_snapshot=(
            drop_in_booking.price_snapshot if drop_in_booking and checkin.subscription_id is None else None
        ),
    )
    CheckinCascadeEvent.objects.create(
        club_id=club_id,
        checkin=checkin,
        effect=CheckinCascadeEvent.Effect.SALARY,
        status=CheckinCascadeEvent.Status.QUEUED,
        expected=expect_salary,
        task_name="apps.attendance.tasks.calculate_salary",
        payload=payload,
    )


def _expected_checkin_cascade_effects(
    *,
    checkin: Checkin,
    student: Student,
    training_type: TrainingType,
    club_id: int,
    skip_group_analytics: bool,
) -> dict[str, bool]:
    from apps.grades.models import StudentGrade
    from apps.retention.models import RetentionTask

    has_grade_progress = bool(
        training_type.grade_system_id
        and StudentGrade.objects.for_club(club_id)
        .filter(student_id=student.id, grade_system_id=training_type.grade_system_id)
        .exists()
    )
    has_retention_task = (
        RetentionTask.objects.for_club(club_id)
        .filter(
            student_id=student.id,
            resolved_at__isnull=True,
            task_type__in=[
                RetentionTask.TaskType.RETENTION,
                RetentionTask.TaskType.NEW_LEAD,
            ],
        )
        .exists()
    )
    has_active_subscription = (
        Subscription.objects.for_club(club_id)
        .filter(
            student_id=student.id,
            status=Subscription.Status.ACTIVE,
            deleted_at__isnull=True,
        )
        .exists()
    )
    from apps.leads.services import is_exact_booked_trial_checkin

    is_exact_trial = is_exact_booked_trial_checkin(
        club_id=club_id,
        student=student,
        checkin=checkin,
    )
    trainings_left_push = False
    if checkin.subscription_id:
        subscription = Subscription.objects.for_club(club_id).get(id=checkin.subscription_id)
        if subscription.trainings_left is not None:
            trainings_left_push = subscription.trainings_left == 2 or subscription.trainings_left <= 0

    return {
        CheckinCascadeEvent.Effect.SALARY: bool(
            not checkin.is_debt
            and checkin.subscription_id
            and (
                (
                    checkin.subscription_component_id
                    and checkin.subscription_component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN
                )
                or (not checkin.subscription_component_id and training_type.kind != TrainingType.Kind.GROUP)
            )
        ),
        CheckinCascadeEvent.Effect.GRADE_PROGRESS: has_grade_progress,
        CheckinCascadeEvent.Effect.GROUP_ANALYTICS: not skip_group_analytics,
        CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: bool(
            student.is_child and student.parent_user_id
            and checkin.notification_policy != Checkin.NotificationPolicy.SILENT_CORRECTION
        ),
        CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: bool(
            has_retention_task and checkin.date >= club_localdate(checkin.club, checkin.created_at)
        ),
        CheckinCascadeEvent.Effect.POST_TRIAL_TASK: bool(is_exact_trial and not has_active_subscription),
        CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: trainings_left_push,
    }


def _validate_training_type_matches_schedule(*, schedule: Schedule, training_type_id: int) -> None:
    if schedule.training_type_id is None:
        raise BusinessLogicError(
            "У занятия не указан тип тренировки",
            code="schedule_training_type_required",
        )

    if schedule.training_type_id != training_type_id:
        raise BusinessLogicError(
            "Тип тренировки не совпадает с расписанием",
            code="training_type_mismatch",
        )


def _resolve_trainer(*, schedule: Schedule, checkin_date: date):
    exception = (
        ScheduleException.objects.for_club(schedule.club_id)
        .filter(
            schedule=schedule,
            date=checkin_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
        )
        .select_related("substitute_trainer")
        .first()
    )
    if exception and exception.substitute_trainer:
        return exception.substitute_trainer
    return schedule.trainer


def _week_bounds(target_date: date) -> tuple[date, date]:
    week_start = target_date - timedelta(days=target_date.weekday())
    return week_start, week_start + timedelta(days=6)


def _component_has_weekly_capacity(*, component: SubscriptionComponent, checkin_date: date) -> bool:
    if not component.weekly_limit:
        return True
    week_start, week_end = _week_bounds(checkin_date)
    used_this_week = (
        Checkin.objects.for_club(component.club_id)
        .filter(
            subscription_component=component,
            date__gte=week_start,
            date__lte=week_end,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .count()
    )
    return used_this_week < component.weekly_limit


def _find_checkin_subscription_component(
    *,
    student: Student,
    club_id: int,
    training_type_id: int,
    location,
    checkin_date: date,
    subscription_id: int | None = None,
) -> tuple[Subscription, SubscriptionComponent] | None:
    eligible_components = (
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update(of=("self", "subscription"))
        .select_related("subscription", "subscription__tariff", "training_type", "location")
        .filter(
            subscription__student=student,
            subscription__status=Subscription.Status.ACTIVE,
            subscription__deleted_at__isnull=True,
            training_type_id=training_type_id,
            is_active=True,
        )
        .filter(
            _active_through_date_q(
                field_name="subscription__expires_at",
                target_date=checkin_date,
                club_id=club_id,
            )
        )
        .filter(Q(credits_left__isnull=True) | Q(credits_left__gt=0))
        .order_by(F("subscription__expires_at").asc(nulls_last=True), "subscription_id", "id")
    )
    if subscription_id is not None:
        eligible_components = eligible_components.filter(subscription_id=subscription_id)

    for scoped_components in (
        eligible_components.filter(scope=Tariff.Scope.LOCATION, location=location),
        eligible_components.filter(scope=Tariff.Scope.CLUB),
    ):
        for component in scoped_components:
            if _component_has_weekly_capacity(component=component, checkin_date=checkin_date):
                return component.subscription, component

    return None


def _find_legacy_checkin_subscription(
    *,
    student: Student,
    club_id: int,
    training_type_id: int,
    location,
    checkin_date: date,
    subscription_id: int | None = None,
) -> Subscription | None:
    eligible_subscriptions = (
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student=student,
            status=Subscription.Status.ACTIVE,
            tariff__training_type_id=training_type_id,
            deleted_at__isnull=True,
            components__isnull=True,
        )
        .filter(
            _active_through_date_q(
                field_name="expires_at",
                target_date=checkin_date,
                club_id=club_id,
            )
        )
        .filter(Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
        .order_by(F("expires_at").asc(nulls_last=True), "id")
    )
    if subscription_id is not None:
        eligible_subscriptions = eligible_subscriptions.filter(id=subscription_id)

    subscription = eligible_subscriptions.filter(
        scope=Tariff.Scope.LOCATION,
        location=location,
    ).first()
    if subscription is None:
        subscription = eligible_subscriptions.filter(scope=Tariff.Scope.CLUB).first()
    return subscription


def _find_checkin_subscription(
    *,
    student: Student,
    club_id: int,
    training_type_id: int,
    location,
    checkin_date: date,
) -> Subscription | None:
    match = _find_checkin_subscription_component(
        student=student,
        club_id=club_id,
        training_type_id=training_type_id,
        location=location,
        checkin_date=checkin_date,
    )
    if match is not None:
        return match[0]

    return _find_legacy_checkin_subscription(
        student=student,
        club_id=club_id,
        training_type_id=training_type_id,
        location=location,
        checkin_date=checkin_date,
    )


def _deduct_subscription(
    *,
    student: Student,
    club_id: int,
    schedule: Schedule,
    club: Club,
    training_type_id: int,
    location,
    checkin: Checkin,
    checkin_date: date,
    subscription=_SUBSCRIPTION_LOOKUP_MISSING,
    subscription_component=_SUBSCRIPTION_LOOKUP_MISSING,
    pending_manual_admission_payments: list[Payment] | None = None,
    has_matching_subscription_component: bool | None = None,
) -> Subscription | None:
    # T5 tenant fix: route through TenantManager.for_club() instead of raw club_id=.
    # Check-in can be recorded offline/backdated, so subscription validity is
    # evaluated against the training date rather than the wall clock.
    pending_manual_admission_payments = pending_manual_admission_payments or []
    if subscription is _SUBSCRIPTION_LOOKUP_MISSING:
        match = _find_checkin_subscription_component(
            student=student,
            club_id=club_id,
            training_type_id=training_type_id,
            location=location,
            checkin_date=checkin_date,
        )
        if match is None:
            subscription = _find_legacy_checkin_subscription(
                student=student,
                club_id=club_id,
                training_type_id=training_type_id,
                location=location,
                checkin_date=checkin_date,
            )
            subscription_component = None
        else:
            subscription, subscription_component = match
    elif subscription_component is _SUBSCRIPTION_LOOKUP_MISSING and subscription is not None:
        subscription_component = (
            SubscriptionComponent.objects.for_club(club_id)
            .select_for_update()
            .filter(
                subscription=subscription,
                training_type_id=training_type_id,
                is_active=True,
            )
            .filter(Q(credits_left__isnull=True) | Q(credits_left__gt=0))
            .order_by("id")
            .first()
        )

    from apps.billing.service_modules.opening_attendance import assert_opening_attendance_not_covered

    assert_opening_attendance_not_covered(
        club_id=club_id,
        student_id=student.id,
        schedule=schedule,
        checkin_date=checkin_date,
        subscription=subscription,
    )

    if not subscription:
        if has_matching_subscription_component is None:
            has_matching_subscription_component = (
                SubscriptionComponent.objects.for_club(club_id)
                .filter(
                    subscription__student=student,
                    subscription__status=Subscription.Status.ACTIVE,
                    subscription__deleted_at__isnull=True,
                    training_type_id=training_type_id,
                    is_active=True,
                )
                .filter(
                    _active_through_date_q(
                        field_name="subscription__expires_at",
                        target_date=checkin_date,
                        club_id=club_id,
                    )
                )
                .exists()
            )
        if has_matching_subscription_component:
            raise BusinessLogicError(
                "Лимит компонента абонемента исчерпан",
                code="subscription_component_limit_exceeded",
            )

        # The training type was validated against the schedule upstream.  It is
        # needed by both the pending-admission debt and ordinary drop-in path.
        training_type = TrainingType.objects.for_club(club_id).get(id=training_type_id)

        pending_payment = _get_locked_pending_manual_admission_payment(
            payments=pending_manual_admission_payments,
            student_id=student.id,
            schedule=schedule,
            target_date=checkin_date,
            club=club,
        )
        if pending_payment is not None:
            from apps.billing.models import DebtLifecycleEvent, DebtSettlementEvent
            from apps.billing.service_modules.debts import (
                assert_payment_reservation_capacity,
                record_debt_lifecycle_event,
                record_debt_settlement_events,
            )

            assert_payment_reservation_capacity(
                payment=pending_payment,
                subscription=pending_payment.subscription,
                club_id=club_id,
                proposed_checkin=checkin,
            )
            debt = Debt(
                club_id=club_id,
                student=student,
                checkin=checkin,
                tariff_price=training_type.drop_in_price,
                reason="pending_manual_admission",
                required_tariff=pending_payment.tariff,
                settlement_payment=pending_payment,
            )
            # Nullable tariff_price is intentional for a payment-owned visit;
            # Django's form-level blank validation otherwise rejects the model
            # field despite its database-level nullable contract.
            debt.full_clean(exclude={"tariff_price"})
            debt.save()
            checkin.is_debt = True
            checkin.save(update_fields=["is_debt"])
            record_debt_lifecycle_event(
                club_id=club_id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.RESERVED,
                previous_state="open",
                new_state="reserved",
                actor_user_id=pending_payment.recorded_by_id,
                reason="pending_manual_admission",
                payment_id=pending_payment.id,
                subscription_id=pending_payment.subscription_id,
            )
            record_debt_settlement_events(
                club_id=club_id,
                payment=pending_payment,
                debt_ids=[debt.id],
                event_type=DebtSettlementEvent.EventType.RESERVED,
            )
            return None

        # A pending payment-owned admission is intentionally limited to its
        # captured target and validity window. Do not turn an invalid attempt
        # into a generic drop-in debt while it owns the operational enrollment.
        if pending_manual_admission_payments:
            raise BusinessLogicError(
                "Ожидающая оплата не покрывает этот чек-ин",
                code="pending_manual_admission_not_eligible",
            )

        is_trial = student.status == Student.Status.TRIAL
        if is_trial and training_type.trial_free:
            # Free trial — no debt, no is_debt flag
            return None

        if training_type.drop_in_price is None:
            raise BusinessLogicError(
                "Drop-in price is required for no-subscription check-in",
                code="drop_in_price_required",
            )

        Debt.objects.create(
            club_id=club_id,
            student=student,
            checkin=checkin,
            tariff_price=training_type.drop_in_price,
            reason="no_subscription",
        )
        checkin.is_debt = True
        checkin.save(update_fields=["is_debt"])
        return None

    checkin.subscription = subscription
    checkin.subscription_component = subscription_component
    checkin.save(update_fields=["subscription", "subscription_component"])

    # This is the same durable booking-event ledger used when a future
    # personal slot is reserved.  A generic/backdated check-in must not spend
    # a credit already promised to another active personal booking; its own
    # exact booking event is exchanged from reserved to consumed once.
    from apps.attendance.services.enrollment import assert_personal_entitlement_checkin_capacity

    assert_personal_entitlement_checkin_capacity(
        subscription=subscription,
        component=subscription_component,
        checkin_at=club_local_day_start_by_id(club_id, checkin_date),
        student_id=student.id,
        schedule_id=schedule.id,
        checkin_id=checkin.id,
    )

    if subscription_component is not None:
        if subscription_component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
            if subscription_component.credits_left is None or subscription_component.credits_left <= 0:
                raise BusinessLogicError(
                    "В компоненте абонемента закончились посещения",
                    code="subscription_component_credits_exhausted",
                )
            subscription_component.credits_left -= 1
        subscription_component.credits_used += 1
        subscription_component.save(update_fields=["credits_left", "credits_used", "updated_at"])

    active_components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(subscription=subscription, is_active=True)
        .only("entitlement_kind", "credits_left")
    )
    if active_components:
        # The subscription stays live while any component can still provide a
        # visit.  Do not close a package simply because the component consumed
        # by this check-in is finite and has just reached zero.
        has_usable_entitlement = any(
            component.entitlement_kind
            in {
                TariffComponent.EntitlementKind.WEEKLY_LIMIT,
                TariffComponent.EntitlementKind.UNLIMITED,
            }
            or (
                component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
                and component.credits_left is not None
                and component.credits_left > 0
            )
            for component in active_components
        )
    else:
        # A legacy aggregate is evaluated after this check-in decrements it.
        has_usable_entitlement = None

    if subscription_component is not None:
        from apps.billing.service_modules.entitlements import refresh_subscription_counters

        refresh_subscription_counters(subscription=subscription)
        if not has_usable_entitlement:
            subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["status", "updated_at"])
    elif subscription.trainings_left is not None:
        subscription.trainings_left -= 1
        subscription.trainings_used += 1
        if subscription.trainings_left <= 0 and not has_usable_entitlement:
            subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["trainings_left", "trainings_used", "status"])
    else:
        subscription.trainings_used += 1
        if has_usable_entitlement is None:
            has_usable_entitlement = subscription.trainings_left is None or subscription.trainings_left > 0
        if not has_usable_entitlement:
            subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["trainings_used", "status"])

    return subscription


def _preview_checkin_financial_lock_scope(*, club_id: int, checkin_id: int) -> dict[str, object]:
    """Read the cancellation financial roots before locking the check-in row."""
    from apps.billing.models import BankPaymentOrder, PaymentRefundCase

    checkin_preview = (
        Checkin.objects.for_club(club_id)
        .filter(id=checkin_id)
        .values("id", "student_id", "schedule_id", "subscription_id", "subscription_component_id")
        .first()
    )
    if checkin_preview is None:
        raise Checkin.DoesNotExist
    debt_payment_ids = list(
        Debt.objects.for_club(club_id)
        .filter(checkin_id=checkin_id)
        .exclude(settlement_payment_id__isnull=True)
        .order_by("settlement_payment_id")
        .values_list("settlement_payment_id", flat=True)
    )
    booking_ids = list(
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(checkin_id=checkin_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    linked_payment_ids = list(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .filter(booking_id__in=booking_ids)
        .order_by("payment_id")
        .values_list("payment_id", flat=True)
    )
    subscription_payment_ids = list(
        Payment.objects.for_club(club_id).filter(subscription_id=checkin_preview["subscription_id"])
        .order_by("id").values_list("id", flat=True)
    ) if checkin_preview["subscription_id"] else []
    payment_ids = sorted(set(debt_payment_ids) | set(linked_payment_ids) | set(subscription_payment_ids))
    payment_roots = list(
        Payment.objects.for_club(club_id).filter(id__in=payment_ids).order_by("id").values("id", "subscription_id")
    )
    subscription_ids = sorted(
        ({checkin_preview["subscription_id"]} if checkin_preview["subscription_id"] else set())
        | {row["subscription_id"] for row in payment_roots if row["subscription_id"]}
    )
    component_ids = (
        [checkin_preview["subscription_component_id"]]
        if checkin_preview["subscription_component_id"]
        else []
    )
    order_ids = list(
        BankPaymentOrder.objects.for_club(club_id)
        .filter(payment_id__in=payment_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    refund_case_ids = list(
        PaymentRefundCase.objects.for_club(club_id)
        .filter(order_id__in=order_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    return {
        "student_id": checkin_preview["student_id"],
        "schedule_id": checkin_preview["schedule_id"],
        "subscription_id": checkin_preview["subscription_id"],
        "booking_ids": booking_ids,
        "debt_payment_ids": debt_payment_ids,
        "linked_payment_ids": linked_payment_ids,
        "subscription_payment_ids": subscription_payment_ids,
        "payment_ids": payment_ids,
        "subscription_ids": subscription_ids,
        "component_ids": component_ids,
        "order_ids": order_ids,
        "refund_case_ids": refund_case_ids,
    }


def _lock_checkin_financial_scope(*, club_id: int, scope: dict[str, object]) -> None:
    """Lock cancellation financial roots after attendance identity in D12 order."""
    from apps.billing.models import BankPaymentOrder, PaymentRefundCase

    for model, ids in (
        (Payment, scope["payment_ids"]),
        (Subscription, scope["subscription_ids"]),
        (SubscriptionComponent, scope["component_ids"]),
        (BankPaymentOrder, scope["order_ids"]),
        (PaymentRefundCase, scope["refund_case_ids"]),
    ):
        if ids:
            list(model.objects.for_club(club_id).select_for_update(of=("self",)).filter(id__in=ids).order_by("id"))


def _assert_checkin_financial_scope_unchanged(
    *,
    club_id: int,
    checkin: Checkin,
    scope: dict[str, object],
) -> None:
    """Fail closed rather than taking a newly discovered financial lock late."""
    current_debt_payment_ids = list(
        Debt.objects.for_club(club_id)
        .filter(checkin_id=checkin.id)
        .exclude(settlement_payment_id__isnull=True)
        .order_by("settlement_payment_id")
        .values_list("settlement_payment_id", flat=True)
    )
    booking_ids = list(
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(checkin_id=checkin.id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    current_linked_payment_ids = list(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .filter(booking_id__in=booking_ids)
        .order_by("payment_id")
        .values_list("payment_id", flat=True)
    )
    current_subscription_payment_ids = list(
        Payment.objects.for_club(club_id).filter(subscription_id=checkin.subscription_id)
        .order_by("id").values_list("id", flat=True)
    ) if checkin.subscription_id else []
    if (
        checkin.student_id != scope["student_id"]
        or checkin.schedule_id != scope["schedule_id"]
        or checkin.subscription_id != scope["subscription_id"]
        or ([checkin.subscription_component_id] if checkin.subscription_component_id else []) != scope["component_ids"]
        or current_subscription_payment_ids != scope["subscription_payment_ids"]
        or booking_ids != scope["booking_ids"]
        or current_debt_payment_ids != scope["debt_payment_ids"]
        or current_linked_payment_ids != scope["linked_payment_ids"]
    ):
        raise BusinessLogicError(
            "Check-in financial scope changed while cancellation was starting.",
            code="checkin_financial_scope_changed",
        )


def cancel_checkin(
    *,
    checkin_id: int,
    club_id: int,
    cancelled_by_user_id: int,
    user_role: str = "",
    reason: str = "Отмена посещения",
    channel: str = "existing",
    command_key: str | None = None,
    payload_fingerprint: str | None = None,
    _defer_async_until_commit: bool = False,
) -> None:
    from apps.billing.models import DebtLifecycleEvent, Payment
    from apps.billing.service_modules.debts import debt_state, record_debt_lifecycle_event
    from apps.clubs.models import ClubMembership

    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope
        from apps.trainers.services import (
            assert_trainer_payroll_date_open,
            create_closed_period_drop_in_cancellation_debits,
            get_trainer_payroll_close_for_date,
            reverse_checkin_package_transfer,
        )

        lock_training_group_mutation_scope(club_id=club_id)
        financial_scope = _preview_checkin_financial_lock_scope(
            club_id=club_id,
            checkin_id=checkin_id,
        )
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        # A complete personal admission has a booking/enrollment owner that
        # must be acquired before any family Payment.  Keep the financial tail
        # deferred so unrelated, lower-id roots join one global ascending
        # Payment/Subscription/BankOrder lock sequence below.
        lock_complete_personal_scopes(
            club_id=club_id,
            booking_ids=financial_scope["booking_ids"],
            payment_ids=financial_scope["payment_ids"],
            extra_student_ids=[financial_scope["student_id"]],
            lock_financial=False,
        )
        # Cancellation releases the same entitlement ledger, so its generic
        # path must share the Trainer -> Student -> attendance-root prefix
        # rather than taking financial rows ahead of an active booking owner.
        _lock_attendance_identity_scope(
            club_id=club_id,
            schedule_id=financial_scope["schedule_id"],
            student_ids=[financial_scope["student_id"]],
        )
        _lock_checkin_financial_scope(club_id=club_id, scope=financial_scope)
        checkin = (
            Checkin.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("subscription")
            .get(id=checkin_id)
        )
        _assert_checkin_financial_scope_unchanged(
            club_id=club_id,
            checkin=checkin,
            scope=financial_scope,
        )

        is_owner = user_role in (ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN)
        if not is_owner:
            cutoff = timezone.now() - timedelta(hours=24)
            if checkin.created_at < cutoff:
                raise BusinessLogicError(
                    "Cannot cancel checkin older than 24 hours",
                    code="cancel_time_limit",
                )

        if checkin.cancelled_at is not None or checkin.deleted_at is not None:
            return

        from apps.attendance.models import StudentAttendanceCorrection
        from apps.billing.service_modules.subscription_corrections import _digest, _json, _snapshot

        before_correction = _json({
            "checkin_id": checkin.id, "date": checkin.date,
            "subscription_id": checkin.subscription_id,
            "component_id": checkin.subscription_component_id,
            "cancelled_at": checkin.cancelled_at,
            "notification_policy": checkin.notification_policy,
        })
        if checkin.subscription_id:
            before_correction["entitlement"] = _snapshot(checkin.subscription, list(
                SubscriptionComponent.objects.for_club(club_id).filter(
                    subscription_id=checkin.subscription_id,
                ).order_by("id"),
            ))

        drop_in_booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment")
            .filter(checkin_id=checkin.id)
            .first()
        )
        closed_period = get_trainer_payroll_close_for_date(
            club_id=club_id,
            target_date=checkin.date,
        )
        closed_personal_drop_in = drop_in_booking is not None and closed_period is not None
        if closed_personal_drop_in:
            create_closed_period_drop_in_cancellation_debits(
                club_id=club_id,
                checkin_id=checkin.id,
                cancelled_at=timezone.now(),
                cancelled_by_id=cancelled_by_user_id,
            )
        else:
            assert_trainer_payroll_date_open(club_id=club_id, target_date=checkin.date)

        pending_settlement_debt = (
            Debt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                checkin=checkin,
                resolved_at__isnull=True,
                settlement_payment__status=Payment.Status.PENDING,
            )
            .only("id")
            .first()
        )
        if pending_settlement_debt is not None:
            raise BusinessLogicError(
                "Долг уже привязан к ожидающей оплате",
                code="debt_payment_pending",
            )

        cancelled_at = timezone.now()

        if checkin.subscription:
            from apps.attendance.services.subscription_lifecycle import (
                assert_checkin_cancellation_entitlement,
                refresh_subscription_status_after_cancellation,
            )

            sub = Subscription.objects.for_club(club_id).select_for_update().get(id=checkin.subscription_id)
            component = None
            if checkin.subscription_component_id:
                component = (
                    SubscriptionComponent.objects.for_club(club_id)
                    .select_for_update()
                    .get(id=checkin.subscription_component_id)
                )
            assert_checkin_cancellation_entitlement(subscription=sub, component=component)
            if component is not None:
                if component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
                    component.credits_left = (component.credits_left or 0) + 1
                component.credits_used = max(0, component.credits_used - 1)
                component.save(update_fields=["credits_left", "credits_used", "updated_at"])
            if checkin.subscription_component_id:
                from apps.billing.service_modules.entitlements import refresh_subscription_counters

                refresh_subscription_counters(subscription=sub)
            else:
                if sub.trainings_left is not None:
                    sub.trainings_left += 1
                sub.trainings_used = max(0, sub.trainings_used - 1)
            refresh_subscription_status_after_cancellation(subscription=sub)
            sub.save(update_fields=["trainings_left", "trainings_used", "status"])

        debts_to_cancel = list(
            Debt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("student")
            .filter(checkin=checkin)
        )
        for debt in debts_to_cancel:
            previous_state = debt_state(debt)
            payment_id = debt.settlement_payment_id
            subscription_id = checkin.subscription_id
            debt.resolved_at = cancelled_at
            debt.resolution_type = "cancelled"
            debt.save(update_fields=["resolved_at", "resolution_type", "updated_at"])
            record_debt_lifecycle_event(
                club_id=club_id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.CANCELLED,
                previous_state=previous_state,
                new_state="resolved:cancelled",
                actor_user_id=cancelled_by_user_id,
                reason="checkin_cancelled",
                payment_id=payment_id,
                subscription_id=subscription_id,
            )

        if drop_in_booking is not None:
            drop_in_booking.checkin = None
            drop_in_booking.debt = None
            drop_in_booking.state = PersonalDropInBooking.State.SCHEDULED
            drop_in_booking.full_clean()
            drop_in_booking.save(update_fields=["checkin", "debt", "state", "updated_at"])

        checkin.cancelled_at = cancelled_at
        checkin.cancelled_by_id = cancelled_by_user_id
        checkin.save(update_fields=["cancelled_at", "cancelled_by"])
        checkin.soft_delete()
        after_correction = _json({**before_correction, "cancelled_at": cancelled_at})
        if checkin.subscription_id:
            after_correction["entitlement"] = _snapshot(sub, list(
                SubscriptionComponent.objects.for_club(club_id).filter(
                    subscription_id=checkin.subscription_id,
                ).order_by("id"),
            ))
        StudentAttendanceCorrection.objects.create(
            club_id=club_id, checkin=checkin, actor_id=cancelled_by_user_id,
            action=StudentAttendanceCorrection.Action.CANCEL, channel=channel,
            reason=reason, command_key=command_key or f"cancel-checkin:{checkin.id}",
            payload_fingerprint=payload_fingerprint or _digest({
                "checkin_id": checkin.id, "actor": cancelled_by_user_id,
                "reason": reason, "channel": channel,
            }),
            before=before_correction,
            after=after_correction,
        )
        previous_visit = (
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=checkin.student_id,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("-date", "-created_at", "-id")
            .values_list("date", flat=True)
            .first()
        )
        Student.objects.for_club(club_id).filter(id=checkin.student_id).update(last_visit_date=previous_visit)

        if closed_personal_drop_in:
            reverse_checkin_package_transfer(
                checkin_id=checkin.id,
                club_id=club_id,
                actor_id=cancelled_by_user_id,
            )
        else:
            from apps.attendance.tasks import reverse_salary

            reverse_salary(checkin.id, club_id=club_id)

    # Reverse async tasks
    if not closed_personal_drop_in:
        _enqueue_task_after_commit("apps.attendance.tasks.reverse_salary", checkin.id,
                                  club_id=club_id, defer_until_commit=_defer_async_until_commit)
    _enqueue_task_after_commit("apps.attendance.tasks.reverse_grade_progress", checkin.id,
                              club_id=club_id, defer_until_commit=_defer_async_until_commit)
    _enqueue_task_after_commit("apps.attendance.tasks.reverse_group_analytics", checkin.id,
                              club_id=club_id, defer_until_commit=_defer_async_until_commit)
    # T4: reverse retention auto-close + compensating parent push
    _enqueue_task_after_commit("apps.retention.tasks.reverse_auto_close_retention", checkin.id,
                              club_id=club_id, defer_until_commit=_defer_async_until_commit)
    _enqueue_task_after_commit("apps.attendance.tasks.reverse_parent_checkin_push", checkin.id,
                              club_id=club_id, defer_until_commit=_defer_async_until_commit)

    logger.info(
        "checkin_cancelled",
        extra={"checkin_id": checkin.id, "club_id": club_id},
    )


def close_session_from_existing_checkins(
    *,
    club_id: int,
    schedule_id: int,
    checkin_date: date,
    actor_user_id: int,
    topic_tags: list[str] | None = None,
    notes: str = "",
) -> GroupSession:
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        schedule = (
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("club", "trainer")
            .get(id=schedule_id)
        )
        _assert_session_close_allowed(
            schedule=schedule,
            checkin_date=checkin_date,
        )

        trainer = _resolve_trainer(schedule=schedule, checkin_date=checkin_date)
        attendee_count = (
            Checkin.objects.for_club(club_id)
            .filter(
                schedule_id=schedule_id,
                date=checkin_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .count()
        )
        session = _close_group_session(
            club_id=club_id,
            schedule=schedule,
            checkin_date=checkin_date,
            trainer=trainer,
            attendee_count=attendee_count,
            actor_user_id=actor_user_id,
            close_source=GroupSession.CloseSource.TRAINER_REVIEW,
            topic_tags=topic_tags,
            notes=notes,
        )

    logger.info(
        "group_session_closed",
        extra={
            "schedule_id": schedule_id,
            "date": str(checkin_date),
            "attendee_count": attendee_count,
            "club_id": club_id,
        },
    )
    return session


def batch_checkin(
    *,
    club_id: int,
    schedule_id: int,
    checkin_date: date,
    present_student_ids: list[int],
    training_type_id: int,
    actor_user_id: int,
    topic_tags: list[str] | None = None,
    notes: str = "",
    _preserve_closed_session_on_idempotent_retry: bool = False,
) -> dict:
    # T5: hoist O(1) lookups out of the per-student loop. Schedule + trainer
    # + group session resolution happens once instead of len(students)+1 times.
    # Full O(1) bulk-deduction across subscriptions is deferred (TODO) — it's
    # perf-only, the per-student create_checkin path is correct today.
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        unique_student_ids = list(dict.fromkeys(present_student_ids))
        # Identify then lock every candidate financial root before the schedule.
        # ``create_checkin`` receives this same scope and discovers no root late.
        financial_scope = _preview_checkin_create_financial_scope(
            club_id=club_id,
            student_ids=unique_student_ids,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
            checkin_date=checkin_date,
        )
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        lock_complete_personal_scopes(
            club_id=club_id,
            booking_ids=sorted(
                scope["booking_id"]
                for scope in financial_scope["personal_scopes"].values()
                if scope["booking_id"] is not None
            ),
            reservation_ids=sorted(
                reservation_id
                for scope in financial_scope["personal_scopes"].values()
                for reservation_id in scope["reservation_ids"]
            ),
            extra_student_ids=sorted(unique_student_ids),
            lock_financial=False,
        )
        # Batch uses the exact same D12 prefix as ``create_checkin``.  It may
        # not put a generic financial candidate ahead of a self-service slot
        # simply because this batch currently has no personal booking.
        prelocked_identity = _lock_attendance_identity_scope(
            club_id=club_id,
            schedule_id=schedule_id,
            student_ids=unique_student_ids,
            missing_student_code="student_ineligible",
        )
        for student_id in unique_student_ids:
            _lock_personal_drop_in_dependencies(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                target_date=checkin_date,
                scope=financial_scope["personal_scopes"][student_id],
                lock_bridges=False,
            )
        financial_scope = _lock_checkin_create_financial_scope(
            club_id=club_id,
            scope=financial_scope,
        )
        locked_schedule, students = prelocked_identity
        _assert_pending_manual_admission_scope_unchanged(
            club_id=club_id,
            student_ids=unique_student_ids,
            schedule_id=schedule_id,
            expected_payment_ids_by_student=financial_scope["pending_payment_ids_by_student"],
        )
        _assert_checkin_create_financial_scope_unchanged(
            club_id=club_id,
            scope=financial_scope,
        )
        personal_dependencies_by_student = {
            student_id: _lock_personal_drop_in_dependencies(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                target_date=checkin_date,
                scope=financial_scope["personal_scopes"][student_id],
            )
            for student_id in unique_student_ids
        }
        _validate_training_type_matches_schedule(schedule=locked_schedule, training_type_id=training_type_id)
        _assert_session_close_allowed(
            schedule=locked_schedule,
            checkin_date=checkin_date,
        )
        for student_id in unique_student_ids:
            student = students.get(student_id)
            if student is None:
                raise BusinessLogicError(
                    "Ученик недоступен для batch check-in",
                    code="student_ineligible",
                )
            if (
                _existing_checkin_result(
                    club_id=club_id,
                    student_id=student_id,
                    schedule_id=schedule_id,
                    checkin_date=checkin_date,
                )
                is not None
            ):
                continue
            _validate_scheduled_checkin(
                student=student,
                schedule=locked_schedule,
                checkin_date=checkin_date,
                source_label="batch",
            )
        trainer = _resolve_trainer(schedule=locked_schedule, checkin_date=checkin_date)

        results = []
        for student_id in unique_student_ids:
            result = create_checkin(
                club_id=club_id,
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                source="batch",
                checkin_date=checkin_date,
                _skip_group_analytics=True,
                _defer_async_until_commit=True,
                _prelocked_attendance_identity=(locked_schedule, students),
                _prelocked_create_financial_scope=financial_scope,
                _prelocked_personal_drop_in_dependencies=personal_dependencies_by_student[student_id],
            )
            results.append(result)

        attendee_count = (
            Checkin.objects.for_club(club_id)
            .filter(
                schedule_id=schedule_id,
                date=checkin_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .count()
        )
        session = None
        if (
            _preserve_closed_session_on_idempotent_retry
            and unique_student_ids
            and all(not result["created"] for result in results)
        ):
            session = (
                GroupSession.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    schedule_id=schedule_id,
                    date=checkin_date,
                    closed_at__isnull=False,
                )
                .first()
            )
        if session is None:
            session = _close_group_session(
                club_id=club_id,
                schedule=locked_schedule,
                checkin_date=checkin_date,
                trainer=trainer,
                attendee_count=attendee_count,
                actor_user_id=actor_user_id,
                close_source=GroupSession.CloseSource.BATCH,
                topic_tags=topic_tags,
                notes=notes,
            )

    logger.info(
        "batch_checkin_completed",
        extra={
            "schedule_id": schedule_id,
            "count": len(results),
            "club_id": club_id,
        },
    )
    return {"checkins": results, "group_session_id": session.id}
