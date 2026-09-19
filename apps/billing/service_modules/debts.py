from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing.models import (
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    DebtWriteOffEvent,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
)
from apps.billing.service_modules.entitlements import (
    _deduct_subscription_component_for_checkin,
    _find_subscription_component_for_checkin,
)
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def debt_state(debt: Debt) -> str:
    if debt.resolved_at is not None:
        resolution = debt.resolution_type or "unknown"
        return f"resolved:{resolution}"
    if debt.settlement_payment_id is not None:
        return "reserved"
    return "open"


def record_debt_lifecycle_event(
    *,
    club_id: int,
    debt: Debt,
    event_type: str,
    previous_state: str,
    new_state: str,
    actor_user_id: int | None = None,
    reason: str = "",
    payment_id: int | None = None,
    subscription_id: int | None = None,
) -> None:
    event = DebtLifecycleEvent(
        club_id=club_id,
        debt=debt,
        payment_id=payment_id,
        subscription_id=subscription_id,
        actor_id=actor_user_id,
        event_type=event_type,
        reason=reason,
        previous_state=previous_state,
        new_state=new_state,
        amount_snapshot=debt.tariff_price,
        debt_id_snapshot=debt.id,
        student_id_snapshot=debt.student_id,
        student_name_snapshot=str(debt.student),
        checkin_id_snapshot=debt.checkin_id,
        debt_reason_snapshot=debt.reason,
    )
    event.full_clean()
    event.save()


def _assert_personal_drop_in_payment_path(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    personal_drop_in_booking_id: int | None = None,
) -> None:
    """Keep payment/subscription creation bound to the open drop-in that needs it."""
    from apps.attendance.models import PersonalDropInBooking, PersonalDropInPaymentLink
    from apps.billing.models import BankPaymentOrder

    open_drop_in_ids = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            enrollment__student_id=student_id,
            tariff_id=tariff_id,
        )
        .filter(
            Q(state=PersonalDropInBooking.State.SCHEDULED)
            | Q(
                state=PersonalDropInBooking.State.ATTENDED,
                debt__isnull=False,
                debt__resolved_at__isnull=True,
            )
        )
        .values("id")
    )
    # The attended branch needs the nullable Debt relation, so Django uses an
    # outer join for the OR above. PostgreSQL cannot lock an outer-joined
    # relation through that shape. Keep the relation-aware lookup in a
    # subquery and lock only the base booking rows in the outer query.
    matching_open_drop_ins = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(id__in=open_drop_in_ids)
        .select_for_update(of=("self",))
        .only("id")
        .order_by("id")
    )
    if personal_drop_in_booking_id is None:
        if matching_open_drop_ins.first() is not None:
            raise BusinessLogicError(
                "Используйте оплату из карточки разовой персоналки",
                code="personal_drop_in_use_booking_payment",
            )
        return
    if matching_open_drop_ins.filter(id=personal_drop_in_booking_id).first() is None:
        raise BusinessLogicError(
            "Разовая персоналка недоступна для этой оплаты",
            code="personal_drop_in_payment_not_actionable",
        )
    has_live_link = PersonalDropInPaymentLink.objects.for_club(club_id).filter(
        booking_id=personal_drop_in_booking_id,
        payment__status=Payment.Status.PENDING,
    ).exists()
    has_live_origin_order = BankPaymentOrder.objects.for_club(club_id).filter(
        personal_drop_in_booking_id_snapshot=personal_drop_in_booking_id,
    ).exclude(
        status__in=[
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.EXPIRED,
            BankPaymentOrder.Status.CANCELLED,
        ]
    ).exists()
    if has_live_link or has_live_origin_order:
        raise BusinessLogicError(
            "Разовая персоналка уже ожидает оплату",
            code="personal_drop_in_payment_pending",
        )


def _personal_drop_in_bookings_by_debt(*, club_id: int, debts: list[Debt]) -> dict[int, object]:
    if not debts:
        return {}
    from apps.attendance.models import PersonalDropInBooking

    return {
        booking.debt_id: booking
        for booking in PersonalDropInBooking.objects.for_club(club_id)
        .select_related("enrollment__schedule", "tariff")
        .filter(debt_id__in=[debt.id for debt in debts])
    }


def _validate_personal_drop_in_debt_tariff_contract(
    *, club_id: int, subscription: Subscription, debts: list[Debt]
) -> None:
    bookings_by_debt = _personal_drop_in_bookings_by_debt(club_id=club_id, debts=debts)
    for debt in debts:
        if debt.required_tariff_id is not None and debt.required_tariff_id != subscription.tariff_id:
            raise BusinessLogicError(
                "Выбранный долг за персоналку можно закрыть только его исходным тарифом",
                code="drop_in_debt_tariff_mismatch",
            )
        booking = bookings_by_debt.get(debt.id)
        if booking is None:
            continue
        schedule = booking.enrollment.schedule
        from apps.attendance.services import validate_personal_drop_in_tariff_contract

        validate_personal_drop_in_tariff_contract(
            club_id=club_id,
            trainer_id=schedule.trainer_id,
            location_id=schedule.location_id,
            training_type_id=schedule.training_type_id,
            tariff_id=subscription.tariff_id,
            lock=True,
        )
        if booking.price_snapshot != subscription.tariff.price:
            raise BusinessLogicError(
                "Тариф персоналки изменился после записи и не может закрыть этот долг",
                code="drop_in_tariff_contract_changed",
            )


def _complete_personal_terms_by_debt(*, club_id: int, debts: list[Debt]) -> dict[int, object]:
    if not debts:
        return {}
    from apps.attendance.models import PersonalServiceTermsSnapshot, complete_personal_terms_queryset

    return {
        terms.booking.debt_id: terms
        for terms in complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_related("booking")
            .filter(booking__debt_id__in=[debt.id for debt in debts])
        )
    }


def _validate_complete_personal_debt_terms(*, debts: list[Debt], terms_by_debt: dict[int, object]) -> None:
    for debt in debts:
        terms = terms_by_debt.get(debt.id)
        if terms is None:
            continue
        if debt.required_tariff_id != terms.tariff_id_snapshot or debt.tariff_price != terms.payable_amount:
            raise BusinessLogicError(
                "Personal booking debt does not match immutable terms",
                code="personal_terms_debt_mismatch",
            )


def _closed_period_personal_drop_in_debt_ids(*, club_id: int, debts: list[Debt]) -> set[int]:
    from apps.trainers.services import get_trainer_payroll_close_for_date

    bookings_by_debt = _personal_drop_in_bookings_by_debt(club_id=club_id, debts=debts)
    return {
        debt.id
        for debt in debts
        if debt.id in bookings_by_debt
        and get_trainer_payroll_close_for_date(club_id=club_id, target_date=debt.checkin.date) is not None
    }


def _attach_debts_to_subscription(
    *,
    subscription: Subscription,
    club_id: int,
    student_id: int,
    resolution_type: str,
    debt_ids: list[int] | None = None,
    attach_all_matching: bool = True,
    settlement_payment_id: int | None = None,
    require_existing_reservation: bool = False,
    created_at_gte=None,
    created_at_lte=None,
    actor_user_id: int | None = None,
    lifecycle_event_type: str = DebtLifecycleEvent.EventType.ATTACHED,
) -> list[int]:
    from apps.trainers.services import (
        assert_trainer_payroll_date_open,
        lock_trainer_payroll_mutation_scope,
    )

    requested_debt_ids = list(dict.fromkeys(debt_ids or []))
    subscription_components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(subscription=subscription, is_active=True)
        .select_related("training_type")
    )
    component_led_subscription = len(subscription_components) > 1
    component_training_type_ids = {component.training_type_id for component in subscription_components}
    if not component_training_type_ids:
        component_training_type_ids = {subscription.tariff.training_type_id}
    if not requested_debt_ids and not attach_all_matching:
        return []
    lock_trainer_payroll_mutation_scope(club_id=club_id)
    debts_qs = (
        Debt.objects.for_club(club_id)
        .select_for_update()
        .select_related("checkin")
        .filter(
            student_id=student_id,
            resolved_at__isnull=True,
            checkin__training_type_id__in=component_training_type_ids,
            checkin__subscription__isnull=True,
            checkin__deleted_at__isnull=True,
            checkin__cancelled_at__isnull=True,
        )
    )

    if settlement_payment_id is None:
        debts_qs = debts_qs.filter(settlement_payment__isnull=True)
    elif require_existing_reservation:
        # A confirmation reconciles only its own reservations. Accepting
        # unrelated open debt here would silently absorb history into a later
        # payment and break the reservation/audit ownership contract.
        debts_qs = debts_qs.filter(settlement_payment_id=settlement_payment_id)
    else:
        debts_qs = debts_qs.filter(
            Q(settlement_payment__isnull=True) | Q(settlement_payment_id=settlement_payment_id)
        )
    if requested_debt_ids:
        debts_qs = debts_qs.filter(id__in=requested_debt_ids)
    if created_at_gte is not None:
        debts_qs = debts_qs.filter(checkin__created_at__gte=created_at_gte)
    if created_at_lte is not None:
        debts_qs = debts_qs.filter(checkin__created_at__lte=created_at_lte)
    if subscription.scope == Tariff.Scope.LOCATION:
        debts_qs = debts_qs.filter(checkin__location_id=subscription.location_id)

    debts = list(debts_qs.order_by("checkin__date", "checkin__created_at", "id"))
    if requested_debt_ids and {debt.id for debt in debts} != set(requested_debt_ids):
        raise BusinessLogicError("Долг не найден", code="debt_not_found")
    if (
        requested_debt_ids
        and not component_led_subscription
        and subscription.trainings_left is not None
        and len(debts) > subscription.trainings_left
    ):
        raise BusinessLogicError(
            "Выбранных долгов больше, чем тренировок в абонементе",
            code="debt_exceeds_subscription_limit",
        )
    complete_terms_by_debt = _complete_personal_terms_by_debt(club_id=club_id, debts=debts)
    _validate_complete_personal_debt_terms(debts=debts, terms_by_debt=complete_terms_by_debt)
    _validate_personal_drop_in_debt_tariff_contract(
        club_id=club_id,
        subscription=subscription,
        debts=[debt for debt in debts if debt.id not in complete_terms_by_debt],
    )
    late_drop_in_debt_ids = _closed_period_personal_drop_in_debt_ids(
        club_id=club_id,
        debts=debts,
    )
    for debt_date in sorted({debt.checkin.date for debt in debts}):
        if any(
            debt.id not in late_drop_in_debt_ids and debt.checkin.date == debt_date
            for debt in debts
        ):
            assert_trainer_payroll_date_open(club_id=club_id, target_date=debt_date)

    salary_checkin_ids: list[int] = []

    for debt in debts:
        if (
            not component_led_subscription
            and subscription.trainings_left is not None
            and subscription.trainings_left <= 0
        ):
            break

        previous_state = debt_state(debt)
        checkin = debt.checkin
        component = _find_subscription_component_for_checkin(
            subscription=subscription,
            club_id=club_id,
            checkin=checkin,
        )
        if subscription_components and component is None:
            raise BusinessLogicError(
                "Нет подходящего компонента абонемента для закрытия долга",
                code="subscription_component_not_found",
            )
        checkin.subscription = subscription
        checkin.subscription_component = component
        checkin.is_debt = False
        checkin.save(update_fields=["subscription", "subscription_component", "is_debt", "updated_at"])

        _deduct_subscription_component_for_checkin(component=component)
        if component is not None:
            from apps.billing.service_modules.entitlements import refresh_subscription_counters

            refresh_subscription_counters(subscription=subscription)
        else:
            if subscription.trainings_left is not None:
                subscription.trainings_left = max(0, subscription.trainings_left - 1)
            subscription.trainings_used += 1
        if subscription.trainings_left == 0:
            subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["trainings_left", "trainings_used", "status", "updated_at"])

        debt.resolved_at = timezone.now()
        debt.resolution_type = resolution_type
        update_fields = ["resolved_at", "resolution_type", "updated_at"]
        if settlement_payment_id is not None:
            debt.settlement_payment_id = settlement_payment_id
            update_fields.append("settlement_payment")
        debt.save(update_fields=update_fields)
        record_debt_lifecycle_event(
            club_id=club_id,
            debt=debt,
            event_type=lifecycle_event_type,
            previous_state=previous_state,
            new_state=f"resolved:{resolution_type}",
            actor_user_id=actor_user_id,
            reason=resolution_type,
            payment_id=settlement_payment_id,
            subscription_id=subscription.id,
        )
        from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin

        if (
            component is None
            or component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN
        ):
            upsert_salary_snapshot_for_checkin(
                checkin=checkin,
                club_id=club_id,
                expect_salary=debt.id not in late_drop_in_debt_ids,
            )
            if debt.id in late_drop_in_debt_ids:
                if settlement_payment_id is None:
                    raise BusinessLogicError(
                        "Late drop-in settlement requires its payment", code="drop_in_payment_required")
                from apps.trainers.services import create_late_drop_in_settlement_credit

                payment = Payment.objects.for_club(club_id).get(id=settlement_payment_id)
                create_late_drop_in_settlement_credit(
                    club_id=club_id,
                    checkin_id=checkin.id,
                    payment_id=payment.id,
                    confirmed_at=payment.verified_at or timezone.now(),
                )
            else:
                salary_checkin_ids.append(checkin.id)

    return salary_checkin_ids


def assert_payment_reservation_capacity(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
    debts: list[Debt] | None = None,
    proposed_checkin=None,
) -> None:
    """Simulate deterministic component allocation before reserving a visit.

    Reservations do not have a component column, so their durable ownership is
    the payment link on ``Debt``. The allocator therefore uses the exact
    payment-owned debts (plus an optional prospective check-in), in stable
    visit order, and keeps finite-credit and weekly counters virtual until the
    caller commits its reservation.
    """
    from apps.attendance.models import Checkin

    if debts is None:
        debts = list(
            Debt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("checkin")
            .filter(
                settlement_payment_id=payment.id,
                resolved_at__isnull=True,
                checkin__deleted_at__isnull=True,
                checkin__cancelled_at__isnull=True,
            )
            .order_by("checkin__date", "checkin__created_at", "id")
        )

    visits = [
        (
            debt.checkin.date,
            debt.checkin.created_at,
            debt.id,
            debt.checkin.training_type_id,
            debt.checkin.location_id,
        )
        for debt in debts
    ]
    if proposed_checkin is not None:
        visits.append(
            (
                proposed_checkin.date,
                proposed_checkin.created_at,
                proposed_checkin.id,
                proposed_checkin.training_type_id,
                proposed_checkin.location_id,
            )
        )
    visits.sort(key=lambda visit: visit[:3])

    components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(subscription=subscription, is_active=True)
        .order_by("id")
    )
    if not components:
        if subscription.trainings_left is not None and len(visits) > subscription.trainings_left:
            raise BusinessLogicError(
                "Выбранных долгов больше, чем тренировок в абонементе",
                code="debt_exceeds_subscription_limit",
            )
        return

    virtual_credits = {component.id: component.credits_left for component in components}
    virtual_weekly_used: dict[tuple[int, date], int] = {}
    for component in components:
        if not component.weekly_limit:
            continue
        weeks = {visit[0] - timedelta(days=visit[0].weekday()) for visit in visits}
        for week_start in weeks:
            virtual_weekly_used[(component.id, week_start)] = (
                Checkin.objects.for_club(club_id)
                .filter(
                    subscription_component=component,
                    date__gte=week_start,
                    date__lte=week_start + timedelta(days=6),
                    deleted_at__isnull=True,
                    cancelled_at__isnull=True,
                )
                .count()
            )

    for visit_date, _created_at, _debt_id, training_type_id, location_id in visits:
        candidates = [
            component
            for component in components
            if component.training_type_id == training_type_id
            and (
                (component.scope == Tariff.Scope.LOCATION and component.location_id == location_id)
                or component.scope == Tariff.Scope.CLUB
            )
        ]
        candidates.sort(
            key=lambda component: (
                0 if component.scope == Tariff.Scope.LOCATION else 1,
                component.id,
            )
        )
        selected = None
        for component in candidates:
            credits_left = virtual_credits[component.id]
            if (
                component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
                and (credits_left is None or credits_left <= 0)
            ):
                continue
            week_start = visit_date - timedelta(days=visit_date.weekday())
            if (
                component.weekly_limit
                and virtual_weekly_used.get((component.id, week_start), 0) >= component.weekly_limit
            ):
                continue
            selected = component
            break
        if selected is None:
            raise BusinessLogicError(
                "Лимит компонента абонемента исчерпан",
                code="subscription_component_limit_exceeded",
            )
        if selected.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
            virtual_credits[selected.id] = (virtual_credits[selected.id] or 0) - 1
        if selected.weekly_limit:
            week_start = visit_date - timedelta(days=visit_date.weekday())
            key = (selected.id, week_start)
            virtual_weekly_used[key] = virtual_weekly_used.get(key, 0) + 1


def _reserve_debts_for_payment(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
    debt_ids: list[int] | None,
    personal_drop_in_booking_id: int | None = None,
) -> None:
    requested_debt_ids = list(dict.fromkeys(debt_ids or []))
    if not requested_debt_ids:
        return
    component_training_type_ids = set(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(subscription=subscription, is_active=True)
        .values_list("training_type_id", flat=True)
    )
    if not component_training_type_ids:
        component_training_type_ids = {subscription.tariff.training_type_id}

    debts_qs = (
        Debt.objects.for_club(club_id)
        .select_for_update()
        .select_related("checkin")
        .filter(
            id__in=requested_debt_ids,
            student_id=payment.student_id,
            resolved_at__isnull=True,
            settlement_payment__isnull=True,
            checkin__training_type_id__in=component_training_type_ids,
            checkin__subscription__isnull=True,
            checkin__deleted_at__isnull=True,
            checkin__cancelled_at__isnull=True,
        )
    )
    if subscription.scope == Tariff.Scope.LOCATION:
        debts_qs = debts_qs.filter(checkin__location_id=subscription.location_id)

    debts = list(debts_qs)
    if {debt.id for debt in debts} != set(requested_debt_ids):
        raise BusinessLogicError("Долг не найден", code="debt_not_found")
    from apps.attendance.models import PersonalDropInBooking

    linked_drop_in_booking_ids = set(
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(debt_id__in=requested_debt_ids)
        .values_list("id", flat=True)
    )
    if linked_drop_in_booking_ids and linked_drop_in_booking_ids != {personal_drop_in_booking_id}:
        raise BusinessLogicError(
            "Используйте оплату из карточки разовой персоналки",
            code="personal_drop_in_use_booking_payment",
        )
    complete_booking = None
    if personal_drop_in_booking_id is not None:
        from apps.attendance.models import PersonalServiceTermsSnapshot, complete_personal_terms_queryset

        complete_booking = complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
                booking_id=personal_drop_in_booking_id,
            )
        ).first()
    if complete_booking is not None:
        # A current catalog price is not authority for an accepted complete
        # personal intent.  The exact booking debt must instead match its
        # immutable price and required tariff snapshot.
        _validate_complete_personal_debt_terms(
            debts=debts,
            terms_by_debt={debt.id: complete_booking for debt in debts},
        )
    else:
        _validate_personal_drop_in_debt_tariff_contract(
            club_id=club_id,
            subscription=subscription,
            debts=debts,
        )
    if subscription.trainings_left is not None and len(debts) > subscription.trainings_left:
        raise BusinessLogicError(
            "Выбранных долгов больше, чем тренировок в абонементе",
            code="debt_exceeds_subscription_limit",
        )

    assert_payment_reservation_capacity(
        payment=payment,
        subscription=subscription,
        club_id=club_id,
        debts=debts,
    )

    previous_states = {debt.id: debt_state(debt) for debt in debts}
    Debt.objects.for_club(club_id).filter(id__in=requested_debt_ids).update(settlement_payment=payment)
    for debt in debts:
        debt.settlement_payment_id = payment.id
        record_debt_lifecycle_event(
            club_id=club_id,
            debt=debt,
            event_type=DebtLifecycleEvent.EventType.RESERVED,
            previous_state=previous_states[debt.id],
            new_state="reserved",
            actor_user_id=payment.recorded_by_id,
            reason="selected_payment",
            payment_id=payment.id,
            subscription_id=subscription.id,
        )
    record_debt_settlement_events(
        club_id=club_id,
        payment=payment,
        debt_ids=[debt.id for debt in debts],
        event_type=DebtSettlementEvent.EventType.RESERVED,
    )


def record_debt_settlement_events(
    *,
    club_id: int,
    payment: Payment,
    debt_ids: list[int],
    event_type: str,
) -> None:
    unique_debt_ids = list(dict.fromkeys(debt_ids))
    if not unique_debt_ids:
        return
    if payment.club_id != club_id:
        raise BusinessLogicError("Оплата не найдена", code="payment_not_found")

    known_debt_ids = set(
        Debt.objects.for_club(club_id)
        .filter(id__in=unique_debt_ids)
        .values_list("id", flat=True)
    )
    if known_debt_ids != set(unique_debt_ids):
        raise BusinessLogicError("Долг не найден", code="debt_not_found")

    DebtSettlementEvent.objects.for_club(club_id).bulk_create(
        [
            DebtSettlementEvent(
                club_id=club_id,
                debt_id=debt_id,
                payment_id=payment.id,
                event_type=event_type,
            )
            for debt_id in unique_debt_ids
        ]
    )


def _attach_subscription_to_pending_payment_debts(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
    actor_user_id: int | None,
) -> list[int]:
    return _attach_debts_to_subscription(
        subscription=subscription,
        club_id=club_id,
        student_id=payment.student_id,
        resolution_type="payment",
        settlement_payment_id=payment.id,
        require_existing_reservation=True,
        created_at_gte=payment.created_at,
        actor_user_id=actor_user_id,
        lifecycle_event_type=DebtLifecycleEvent.EventType.ATTACHED,
    )


def write_off_debt(
    *,
    debt_id: int,
    club_id: int,
    written_off_by_id: int,
    reason: str = "",
) -> Debt:
    reason = reason.strip()
    if not reason:
        raise BusinessLogicError(
            "Write-off reason is required",
            code="writeoff_reason_required",
        )

    from apps.trainers.services import (
        assert_trainer_payroll_date_open,
        lock_trainer_payroll_mutation_scope,
    )

    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        debt = (
            Debt.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("checkin", "settlement_payment", "student")
            .get(id=debt_id)
        )
        if debt.resolved_at is not None:
            raise BusinessLogicError(
                "Долг уже закрыт",
                code="debt_already_resolved",
            )
        if debt.settlement_payment_id and debt.settlement_payment.status == Payment.Status.PENDING:
            raise BusinessLogicError(
                "Долг уже привязан к ожидающей оплате",
                code="debt_payment_pending",
            )
        assert_trainer_payroll_date_open(club_id=club_id, target_date=debt.checkin.date)
        previous_state = debt_state(debt)
        decided_at = timezone.now()
        debt.resolved_at = decided_at
        debt.resolution_type = "writeoff"
        debt.save(update_fields=["resolved_at", "resolution_type", "updated_at"])
        event = DebtWriteOffEvent(
            club_id=club_id,
            debt=debt,
            written_off_by_id=written_off_by_id,
            reason=reason,
            decided_at=decided_at,
            amount_snapshot=debt.tariff_price,
            debt_id_snapshot=debt.id,
            student_id_snapshot=debt.student_id,
            student_name_snapshot=str(debt.student),
            checkin_id_snapshot=debt.checkin_id,
            debt_reason_snapshot=debt.reason,
        )
        event.full_clean()
        event.save()
        record_debt_lifecycle_event(
            club_id=club_id,
            debt=debt,
            event_type=DebtLifecycleEvent.EventType.WRITTEN_OFF,
            previous_state=previous_state,
            new_state="resolved:writeoff",
            actor_user_id=written_off_by_id,
            reason=reason,
        )
    logger.info("debt_written_off", extra={"id": debt_id, "club_id": club_id})
    return debt
