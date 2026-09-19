"""Ordered locks for immutable, complete personal payment intents.

The personal command crosses attendance and billing owners.  Every mutating
entry point therefore previews its origin without a lock and then calls the
helpers here while already inside its transaction.  The helpers intentionally
do not decide lifecycle policy; they only provide the D12 lock boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    Schedule,
    ScheduleEnrollment,
    complete_personal_terms_queryset,
)
from apps.billing.models import BankPaymentOrder, Payment, Subscription, SubscriptionComponent
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import Trainer


@dataclass(frozen=True)
class CompletePersonalOrigin:
    booking_id: int | None
    reservation_id: int | None
    trainer_id: int
    student_id: int
    slot_id: int | None
    enrollment_id: int | None
    payment_ids: tuple[int, ...]
    subscription_ids: tuple[int, ...]
    order_ids: tuple[int, ...]


@dataclass
class LockedCompletePersonalScope:
    origins: tuple[CompletePersonalOrigin, ...]
    bookings_by_id: dict[int, PersonalDropInBooking]
    reservations_by_id: dict[int, PersonalBookingPaymentReservation]
    payment_links_by_id: dict[int, PersonalDropInPaymentLink]
    payments_by_id: dict[int, Payment]
    subscriptions_by_id: dict[int, Subscription]
    components_by_id: dict[int, SubscriptionComponent]
    orders_by_id: dict[int, BankPaymentOrder]


def _complete_terms_for_targets(
    *, club_id: int, booking_ids: set[int], reservation_ids: set[int]
) -> tuple[set[int], set[int]]:
    rows = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(club_id)
    )
    target_filter = Q()
    if booking_ids:
        target_filter |= Q(booking_id__in=booking_ids)
    if reservation_ids:
        target_filter |= Q(reservation_id__in=reservation_ids)
    if not target_filter:
        return set(), set()
    return (
        set(rows.filter(target_filter).values_list("booking_id", flat=True)) - {None},
        set(rows.filter(target_filter).values_list("reservation_id", flat=True)) - {None},
    )


def preview_complete_personal_scopes(
    *,
    club_id: int,
    booking_ids: list[int] | tuple[int, ...] = (),
    reservation_ids: list[int] | tuple[int, ...] = (),
    payment_ids: list[int] | tuple[int, ...] = (),
    order_ids: list[int] | tuple[int, ...] = (),
) -> tuple[CompletePersonalOrigin, ...]:
    """Return complete-term personal origins without consulting today's flag.

    Creation uses the journey capability, but persisted complete terms
    contract remains the lifecycle, settlement, and locking authority after a
    rollout is switched off.
    """
    requested_booking_ids = {value for value in booking_ids if value}
    requested_reservation_ids = {value for value in reservation_ids if value}
    requested_payment_ids = {value for value in payment_ids if value}
    requested_order_ids = {value for value in order_ids if value}

    if requested_payment_ids:
        requested_booking_ids.update(
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .filter(payment_id__in=requested_payment_ids)
            .values_list("booking_id", flat=True)
        )
        requested_reservation_ids.update(
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(payment_id__in=requested_payment_ids)
            .values_list("id", flat=True)
        )
    if requested_order_ids:
        order_rows = BankPaymentOrder.objects.for_club(club_id).filter(id__in=requested_order_ids)
        requested_payment_ids.update(order_rows.values_list("payment_id", flat=True))
        requested_booking_ids.update(
            order_rows.exclude(personal_drop_in_booking_id_snapshot__isnull=True).values_list(
                "personal_drop_in_booking_id_snapshot", flat=True
            )
        )
        requested_reservation_ids.update(
            order_rows.exclude(personal_booking_reservation_id_snapshot__isnull=True).values_list(
                "personal_booking_reservation_id_snapshot", flat=True
            )
        )
        requested_reservation_ids.update(
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .filter(bank_payment_order_id__in=requested_order_ids)
            .values_list("id", flat=True)
        )

    complete_booking_ids, complete_reservation_ids = _complete_terms_for_targets(
        club_id=club_id,
        booking_ids=requested_booking_ids,
        reservation_ids=requested_reservation_ids,
    )
    if not complete_booking_ids and not complete_reservation_ids:
        return ()

    origins: list[CompletePersonalOrigin] = []
    for booking in (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related("enrollment__schedule")
        .filter(id__in=complete_booking_ids)
        .order_by("id")
    ):
        links = list(
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .filter(booking_id=booking.id)
            .order_by("id")
            .values("payment_id", "bank_payment_order_id")
        )
        origin_payment_ids = {row["payment_id"] for row in links}
        origin_order_ids = {row["bank_payment_order_id"] for row in links if row["bank_payment_order_id"]}
        origin_order_ids.update(
            BankPaymentOrder.objects.for_club(club_id)
            .filter(personal_drop_in_booking_id_snapshot=booking.id)
            .values_list("id", flat=True)
        )
        origin_payment_ids.update(
            BankPaymentOrder.objects.for_club(club_id)
            .filter(id__in=origin_order_ids)
            .values_list("payment_id", flat=True)
        )
        origin_subscription_ids = set(
            Payment.objects.for_club(club_id)
            .filter(id__in=origin_payment_ids)
            .exclude(subscription_id__isnull=True)
            .values_list("subscription_id", flat=True)
        )
        origins.append(
            CompletePersonalOrigin(
                booking_id=booking.id,
                reservation_id=None,
                trainer_id=booking.enrollment.schedule.trainer_id,
                student_id=booking.enrollment.student_id,
                slot_id=(
                    PersonalAvailabilitySlot.objects.for_club(club_id)
                    .filter(booked_enrollment_id=booking.enrollment_id)
                    .order_by("id")
                    .values_list("id", flat=True)
                    .first()
                ),
                enrollment_id=booking.enrollment_id,
                payment_ids=tuple(sorted(origin_payment_ids)),
                subscription_ids=tuple(sorted(origin_subscription_ids)),
                order_ids=tuple(sorted(origin_order_ids)),
            )
        )
    for reservation in (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(id__in=complete_reservation_ids)
        .order_by("id")
    ):
        origin_order_ids = set(
            BankPaymentOrder.objects.for_club(club_id)
            .filter(
                Q(id=reservation.bank_payment_order_id)
                | Q(personal_booking_reservation_id_snapshot=reservation.id)
            )
            .values_list("id", flat=True)
        )
        origin_payment_ids = {reservation.payment_id} if reservation.payment_id else set()
        origin_payment_ids.update(
            BankPaymentOrder.objects.for_club(club_id)
            .filter(id__in=origin_order_ids)
            .values_list("payment_id", flat=True)
        )
        origin_subscription_ids = {reservation.subscription_id} if reservation.subscription_id else set()
        origin_subscription_ids.update(
            Payment.objects.for_club(club_id)
            .filter(id__in=origin_payment_ids)
            .exclude(subscription_id__isnull=True)
            .values_list("subscription_id", flat=True)
        )
        origins.append(
            CompletePersonalOrigin(
                booking_id=None,
                reservation_id=reservation.id,
                trainer_id=reservation.trainer_id,
                student_id=reservation.student_id,
                slot_id=reservation.availability_slot_id,
                enrollment_id=reservation.enrollment_id,
                payment_ids=tuple(sorted(origin_payment_ids)),
                subscription_ids=tuple(sorted(origin_subscription_ids)),
                order_ids=tuple(sorted(origin_order_ids)),
            )
        )
    return tuple(origins)


def lock_complete_personal_scopes(
    *,
    club_id: int,
    booking_ids: list[int] | tuple[int, ...] = (),
    reservation_ids: list[int] | tuple[int, ...] = (),
    payment_ids: list[int] | tuple[int, ...] = (),
    order_ids: list[int] | tuple[int, ...] = (),
    extra_student_ids: list[int] | tuple[int, ...] = (),
    extra_slot_ids: list[int] | tuple[int, ...] = (),
    extra_schedule_ids: list[int] | tuple[int, ...] = (),
    extra_reservation_ids: list[int] | tuple[int, ...] = (),
    lock_financial: bool = True,
) -> LockedCompletePersonalScope | None:
    """Lock flag-on complete origins in the D12 order, or return ``None``.

    Callers are responsible for their own outer transaction.  IDs are previewed
    before locking, then every level is locked in ascending ID order.  The
    second preview detects an origin that changed while the early levels were
    being acquired instead of discovering and locking it late.
    """

    origins = preview_complete_personal_scopes(
        club_id=club_id,
        booking_ids=booking_ids,
        reservation_ids=reservation_ids,
        payment_ids=payment_ids,
        order_ids=order_ids,
    )
    if not origins:
        return None

    trainer_ids = sorted({origin.trainer_id for origin in origins})
    student_ids = sorted({origin.student_id for origin in origins} | {value for value in extra_student_ids if value})
    slot_ids = sorted(
        {origin.slot_id for origin in origins if origin.slot_id is not None}
        | {value for value in extra_slot_ids if value}
    )
    origin_reservation_ids = sorted(
        {origin.reservation_id for origin in origins if origin.reservation_id is not None}
    )
    reservation_ids_to_lock = sorted(
        set(origin_reservation_ids) | {value for value in extra_reservation_ids if value}
    )
    booking_ids_to_lock = sorted({origin.booking_id for origin in origins if origin.booking_id is not None})
    enrollment_ids = sorted({origin.enrollment_id for origin in origins if origin.enrollment_id is not None})
    payment_ids_to_lock = sorted({value for origin in origins for value in origin.payment_ids})
    subscription_ids = sorted({value for origin in origins for value in origin.subscription_ids})
    order_ids_to_lock = sorted({value for origin in origins for value in origin.order_ids})

    list(
        Trainer.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=trainer_ids)
        .order_by("id")
    )
    list(
        Student.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=student_ids)
        .order_by("id")
    )
    if slot_ids:
        list(
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=slot_ids)
            .order_by("id")
        )
    schedule_ids = sorted(set(
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id__in=enrollment_ids)
        .order_by("schedule_id")
        .values_list("schedule_id", flat=True)
        .distinct()
    ) | {value for value in extra_schedule_ids if value})
    if schedule_ids:
        list(
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=schedule_ids)
            .order_by("id")
        )
    reservations = list(
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=reservation_ids_to_lock)
        .order_by("id")
    )
    bookings = list(
        PersonalDropInBooking.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=booking_ids_to_lock)
        .order_by("id")
    )
    payment_links = list(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(booking_id__in=booking_ids_to_lock)
        .order_by("id")
    )
    if enrollment_ids:
        list(
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=enrollment_ids)
            .order_by("id")
        )
    payments: list[Payment] = []
    subscriptions: list[Subscription] = []
    components: list[SubscriptionComponent] = []
    orders: list[BankPaymentOrder] = []
    if lock_financial:
        payments = list(
            Payment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=payment_ids_to_lock)
            .order_by("id")
        )
        subscriptions = list(
            Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=subscription_ids)
            .order_by("id")
        )
        components = list(
            SubscriptionComponent.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(subscription_id__in=subscription_ids)
            .order_by("id")
        )
        orders = list(
            BankPaymentOrder.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=order_ids_to_lock)
            .order_by("id")
        )

    current = preview_complete_personal_scopes(
        club_id=club_id,
        booking_ids=booking_ids_to_lock,
        reservation_ids=origin_reservation_ids,
        payment_ids=payment_ids_to_lock,
        order_ids=order_ids_to_lock,
    )
    if current != origins:
        raise BusinessLogicError(
            "Personal payment ownership changed while acquiring ordered locks.",
            code="personal_lock_scope_changed",
        )
    return LockedCompletePersonalScope(
        origins=origins,
        bookings_by_id={booking.id: booking for booking in bookings},
        reservations_by_id={reservation.id: reservation for reservation in reservations},
        payment_links_by_id={link.id: link for link in payment_links},
        payments_by_id={payment.id: payment for payment in payments},
        subscriptions_by_id={subscription.id: subscription for subscription in subscriptions},
        components_by_id={component.id: component for component in components},
        orders_by_id={order.id: order for order in orders},
    )


def lock_personal_booking_enrollment_scope(
    *,
    club_id: int,
    enrollment_id: int,
    extra_slot_ids: list[int] | tuple[int, ...] = (),
) -> None:
    """Lock an entitlement or ordinary personal booking before cancellation.

    Unlike a complete payment origin, an entitlement booking has no reservation
    snapshot to route through ``lock_complete_personal_scopes``.  Preview its
    immutable identity first, then acquire the shared D12 prefix so cancellation
    never starts at ``ScheduleEnrollment`` while exact check-in holds Schedule.
    """

    preview = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=enrollment_id)
        .values("id", "student_id", "schedule_id", "schedule__trainer_id")
        .first()
    )
    if preview is None:
        return
    source_slot_ids = list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .filter(booked_enrollment_id=enrollment_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    slot_ids = sorted(set(source_slot_ids) | {value for value in extra_slot_ids if value})
    if preview["schedule__trainer_id"] is not None:
        list(
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=preview["schedule__trainer_id"])
            .order_by("id")
        )
    list(
        Student.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=preview["student_id"])
        .order_by("id")
    )
    if slot_ids:
        list(
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=slot_ids)
            .order_by("id")
        )
    list(
        Schedule.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=preview["schedule_id"])
        .order_by("id")
    )
    locked = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=enrollment_id)
        .values("id", "student_id", "schedule_id", "schedule__trainer_id")
        .first()
    )
    current_source_slot_ids = list(
        PersonalAvailabilitySlot.objects.for_club(club_id)
        .filter(booked_enrollment_id=enrollment_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if locked != preview or current_source_slot_ids != source_slot_ids:
        raise BusinessLogicError(
            "Personal booking identity changed while acquiring ordered locks.",
            code="personal_lock_scope_changed",
        )


def lock_personal_booking_reservation_scope(*, club_id: int, reservation_id: int) -> None:
    """Lock a pre-confirmation reservation before releasing its held slot."""

    preview = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(id=reservation_id)
        .values("id", "trainer_id", "student_id", "availability_slot_id", "enrollment_id")
        .first()
    )
    if preview is None:
        return
    schedule_id = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=preview["enrollment_id"])
        .values_list("schedule_id", flat=True)
        .first()
        if preview["enrollment_id"] is not None
        else None
    )
    list(
        Trainer.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=preview["trainer_id"])
        .order_by("id")
    )
    list(
        Student.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=preview["student_id"])
        .order_by("id")
    )
    if preview["availability_slot_id"] is not None:
        list(
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=preview["availability_slot_id"])
            .order_by("id")
        )
    if schedule_id is not None:
        list(
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=schedule_id)
            .order_by("id")
        )
    locked = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=reservation_id)
        .values("id", "trainer_id", "student_id", "availability_slot_id", "enrollment_id")
        .first()
    )
    current_schedule_id = (
        ScheduleEnrollment.objects.for_club(club_id)
        .filter(id=locked["enrollment_id"])
        .values_list("schedule_id", flat=True)
        .first()
        if locked is not None and locked["enrollment_id"] is not None
        else None
    )
    if locked != preview or current_schedule_id != schedule_id:
        raise BusinessLogicError(
            "Personal payment reservation changed while acquiring ordered locks.",
            code="personal_lock_scope_changed",
        )
