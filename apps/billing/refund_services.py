from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalBookingPaymentReservation,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    SubscriptionFreeze,
)
from apps.billing.service_modules.entitlements import refresh_subscription_counters
from apps.clubs.timezones import club_localdate_by_id
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import (
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerPackageAllocation,
)
from apps.trainers.services import (
    get_trainer_payroll_close_for_date,
    lock_and_assert_trainer_payroll_date_open,
)

MONEY_QUANTUM = Decimal("0.01")
LEGACY_ENROLLMENT_LEAVE_UNLINKED = "leave_unlinked"
LEGACY_ENROLLMENT_CANCEL_SELECTED = "cancel_selected"


def _money(value: Decimal | str | int) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise InvalidOperation
        return decimal_value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BusinessLogicError(
            "Некорректная сумма возврата",
            code="payment_refund_amount_invalid",
        ) from exc


def _clean_reason(reason: str) -> str:
    value = reason.strip()
    if not value:
        raise BusinessLogicError(
            "Укажите причину возврата",
            code="payment_refund_reason_required",
        )
    return value[:500]


def _validate_replay(
    *,
    existing: PaymentRefund,
    case_id: int,
    amount: Decimal,
    refund_kind: str,
) -> PaymentRefund:
    if existing.refund_case_id != case_id or existing.amount != amount or existing.refund_kind != refund_kind:
        raise BusinessLogicError(
            "Ключ повтора уже использован для другого возврата",
            code="payment_refund_idempotency_conflict",
        )
    return existing


def _settled_debt_snapshot(*, club_id: int, payment_id: int) -> list[dict]:
    rows = (
        Debt.objects.for_club(club_id)
        .filter(
            settlement_payment_id=payment_id,
            resolved_at__isnull=False,
        )
        .order_by("id")
    )
    return [
        {
            "debt_id": debt.id,
            "amount": format(_money(debt.tariff_price or Decimal("0.00")), ".2f"),
        }
        for debt in rows
    ]


def _entitlement_snapshot(*, club_id: int, subscription: Subscription) -> dict:
    components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(subscription_id=subscription.id)
        .order_by("id")
        .values("id", "is_active", "credits_total", "credits_left", "credits_used")
    )
    allocations = list(
        TrainerPackageAllocation.objects.for_club(club_id)
        .filter(subscription_id=subscription.id)
        .order_by("id")
        .values("id", "is_active", "sessions_total_snapshot", "sessions_remaining_snapshot")
    )
    return {
        "subscription_id": subscription.id,
        "status": subscription.status,
        "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
        "components": components,
        "trainer_allocations": allocations,
    }


def _cancel_payment_created_enrollment(
    *,
    club_id: int,
    payment: Payment,
    legacy_enrollment_action: str | None,
    legacy_enrollment_id: int | None,
) -> str:
    enrollment = None
    if payment.conversion_enrollment_id:
        enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=payment.conversion_enrollment_id)
            .first()
        )
        if (
            enrollment is None
            or enrollment.student_id != payment.student_id
            or enrollment.schedule_id != payment.target_schedule_id
            or enrollment.created_from != ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        ):
            raise BusinessLogicError(
                "Связанное зачисление не соответствует оплате",
                code="payment_refund_conversion_enrollment_mismatch",
            )
    elif payment.target_schedule_id:
        if legacy_enrollment_action == LEGACY_ENROLLMENT_LEAVE_UNLINKED:
            return PaymentRefund.EnrollmentDisposition.LEFT_UNLINKED
        if legacy_enrollment_action != LEGACY_ENROLLMENT_CANCEL_SELECTED or legacy_enrollment_id is None:
            raise BusinessLogicError(
                "Для старой оплаты выберите зачисление или явно оставьте его без изменений",
                code="payment_refund_legacy_enrollment_action_required",
            )
        enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                id=legacy_enrollment_id,
                student_id=payment.student_id,
                schedule_id=payment.target_schedule_id,
                created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            )
            .first()
        )
        if enrollment is None:
            raise BusinessLogicError(
                "Выбранное зачисление не принадлежит этой оплате",
                code="payment_refund_legacy_enrollment_mismatch",
            )

    if enrollment is None:
        return PaymentRefund.EnrollmentDisposition.NOT_APPLICABLE
    if enrollment.status in {
        ScheduleEnrollment.Status.ACTIVE,
        ScheduleEnrollment.Status.TRIAL,
        ScheduleEnrollment.Status.FROZEN,
    }:
        enrollment.status = ScheduleEnrollment.Status.CANCELLED
        enrollment.save(update_fields=["status", "updated_at"])
    return PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED


def _cancel_future_personal_booking(
    *,
    club_id: int,
    order_id: int | None,
    actor_user_id: int,
    reason: str,
) -> str:
    if order_id is None:
        return PaymentRefund.PersonalBookingDisposition.NOT_APPLICABLE
    reservation = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("enrollment", "schedule")
        .filter(bank_payment_order_id=order_id)
        .first()
    )
    if reservation is None:
        return PaymentRefund.PersonalBookingDisposition.NOT_APPLICABLE

    if reservation.status in {
        PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
        PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
    }:
        from apps.attendance.services import close_personal_booking_payment_reservation_for_order

        close_personal_booking_payment_reservation_for_order(
            club_id=club_id,
            order_id=order_id,
            status=PersonalBookingPaymentReservation.Status.CANCELLED,
            reason=reason,
            code="payment_refunded",
        )
        return PaymentRefund.PersonalBookingDisposition.CANCELLED_FUTURE

    if reservation.status != PersonalBookingPaymentReservation.Status.BOOKED or reservation.enrollment_id is None:
        return PaymentRefund.PersonalBookingDisposition.KEPT

    enrollment = reservation.enrollment
    target_date = enrollment.starts_on
    club_today = club_localdate_by_id(club_id)
    delivered = (
        target_date is None
        or target_date < club_today
        or Checkin.objects.for_club(club_id)
        .filter(
            student_id=enrollment.student_id,
            schedule_id=enrollment.schedule_id,
            date=target_date,
            deleted_at__isnull=True,
        )
        .exists()
        or GroupSession.objects.for_club(club_id)
        .filter(
            schedule_id=enrollment.schedule_id,
            date=target_date,
            closed_at__isnull=False,
        )
        .exists()
    )
    if delivered:
        return PaymentRefund.PersonalBookingDisposition.DELIVERED_HISTORY_KEPT

    from apps.attendance.services import cancel_personal_booking

    cancel_personal_booking(
        club_id=club_id,
        enrollment_id=enrollment.id,
        actor_user_id=actor_user_id,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        reason=reason,
    )
    reservation.status = PersonalBookingPaymentReservation.Status.CANCELLED
    reservation.last_error_code = "payment_refunded"
    reservation.last_error_message = reason
    reservation.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
    return PaymentRefund.PersonalBookingDisposition.CANCELLED_FUTURE


def _revoke_payment_owned_group_membership(
    *,
    club_id: int,
    payment: Payment,
    actor_user_id: int,
    reason: str,
) -> str | None:
    """Cancel a payment-owned group family only when no renewal still supports it."""
    membership_id = payment.conversion_group_membership_id or payment.target_group_membership_id
    if membership_id is None:
        return None

    from apps.attendance.models import TrainingGroupMembership, TrainingGroupMembershipEvent
    from apps.attendance.services.training_group_memberships import (
        cancel_payment_owned_training_group_membership,
    )

    membership = (
        TrainingGroupMembership.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=membership_id)
        .first()
    )
    owner_payment = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(conversion_group_membership_id=membership_id)
        .first()
    )
    if membership is None:
        raise BusinessLogicError(
            "Payment-owned group membership does not match its refund.",
            code="payment_refund_group_membership_mismatch",
        )
    if (
        membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
        or membership.status == TrainingGroupMembership.Status.TRANSFERRED
        or owner_payment is None
        or owner_payment.target_group_membership_id != membership_id
    ):
        return PaymentRefund.EnrollmentDisposition.KEPT

    owner_refund_requested = (
        payment.id == owner_payment.id
        or PaymentRefund.objects.for_club(club_id)
        .filter(
            payment_id=owner_payment.id,
            entitlement_disposition=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        )
        .exists()
    )
    if not owner_refund_requested:
        return PaymentRefund.EnrollmentDisposition.KEPT

    renewal_candidates = list(
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            target_group_membership_id=membership_id,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.RENEWAL,
            status=Payment.Status.CONFIRMED,
        )
        .exclude(id=payment.id)
        .order_by("id")
    )
    has_effective_renewal_support = any(
        (
            PaymentRefund.objects.for_club(club_id)
            .filter(payment_id=renewal.id)
            .aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        < renewal.amount
        or PaymentRefund.objects.for_club(club_id)
        .filter(payment_id=renewal.id)
        .order_by("-id")
        .values_list("entitlement_disposition", flat=True)
        .first() == PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS
        for renewal in renewal_candidates
    )
    if has_effective_renewal_support:
        TrainingGroupMembershipEvent.objects.get_or_create(
            club_id=club_id,
            membership=membership,
            idempotency_key=f"payment-refund-deferred:{owner_payment.id}",
            defaults={
                "action": "payment_refund_deferred",
                "effective_date": club_localdate_by_id(club_id),
                "previous_state_snapshot": {"status": membership.status},
                "new_state_snapshot": {"status": membership.status},
                "actor_id": actor_user_id,
                "rationale": reason,
                "source_payment_id": owner_payment.id,
            },
        )
        return PaymentRefund.EnrollmentDisposition.KEPT

    cancel_payment_owned_training_group_membership(
        club_id=club_id,
        membership_id=membership_id,
        payment_id=owner_payment.id,
        actor_user_id=actor_user_id,
        rationale=reason,
    )
    return PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED


def _revoke_remaining_entitlement(
    *,
    club_id: int,
    order_id: int | None,
    payment: Payment,
    subscription: Subscription,
    actor_user_id: int,
    reason: str,
    legacy_enrollment_action: str | None,
    legacy_enrollment_id: int | None,
) -> tuple[str, str]:
    now = timezone.now()
    subscription.status = Subscription.Status.CANCELLED
    subscription.save(update_fields=["status", "updated_at"])
    SubscriptionComponent.objects.for_club(club_id).filter(
        subscription_id=subscription.id,
        is_active=True,
    ).update(is_active=False, updated_at=now)
    refresh_subscription_counters(subscription=subscription)
    TrainerPackageAllocation.objects.for_club(club_id).filter(
        subscription_id=subscription.id,
        is_active=True,
    ).update(
        is_active=False,
        deactivated_at=now,
        deactivated_by_id=actor_user_id,
        note="Deactivated by full payment refund",
        updated_at=now,
    )
    SubscriptionFreeze.objects.for_club(club_id).filter(
        subscription_id=subscription.id,
        status=SubscriptionFreeze.FreezeStatus.PENDING,
    ).update(
        status=SubscriptionFreeze.FreezeStatus.REJECTED,
        approved_by_id=None,
        rejected_by_id=actor_user_id,
        decision_at=now,
        decision_reason="Закрыта из-за полного возврата оплаты",
        updated_at=now,
    )
    SubscriptionFreeze.objects.for_club(club_id).filter(
        subscription_id=subscription.id,
        status=SubscriptionFreeze.FreezeStatus.APPROVED,
        ends_at__isnull=True,
    ).update(ends_at=now, updated_at=now)

    enrollment_disposition = _revoke_payment_owned_group_membership(
        club_id=club_id,
        payment=payment,
        actor_user_id=actor_user_id,
        reason=reason,
    )
    if enrollment_disposition is None:
        enrollment_disposition = _cancel_payment_created_enrollment(
            club_id=club_id,
            payment=payment,
            legacy_enrollment_action=legacy_enrollment_action,
            legacy_enrollment_id=legacy_enrollment_id,
        )
    personal_booking_disposition = _cancel_future_personal_booking(
        club_id=club_id,
        order_id=order_id,
        actor_user_id=actor_user_id,
        reason=reason,
    )
    return enrollment_disposition, personal_booking_disposition


def _refund_delta_for_earning(
    *,
    earning_amount: Decimal,
    payment_amount: Decimal,
    cumulative_before: Decimal,
    cumulative_after: Decimal,
) -> Decimal:
    before = _money(earning_amount * cumulative_before / payment_amount)
    after = _money(earning_amount * cumulative_after / payment_amount)
    if cumulative_after == payment_amount:
        after = _money(earning_amount)
    return after - before


def _manual_correction_rows_for_earning(
    *,
    earning: TrainerEarning,
    payment_earnings: list[TrainerEarning],
) -> list[TrainerEarningAdjustment]:
    linked_rows = list(
        TrainerEarningAdjustment.objects.for_club(earning.club_id)
        .select_for_update(of=("self",))
        .filter(
            source_earning_id=earning.id,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            affects_payroll=True,
        )
        .order_by("id")
    )
    if linked_rows:
        return linked_rows

    legacy_debits = list(
        TrainerEarningAdjustment.objects.for_club(earning.club_id)
        .select_for_update(of=("self",))
        .filter(
            source_earning__isnull=True,
            source_payment_id=earning.payment_id,
            trainer_id=earning.trainer_id,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            affects_payroll=True,
        )
        .order_by("id")[:2]
    )
    if not legacy_debits:
        return []
    matching_earnings = [candidate for candidate in payment_earnings if candidate.trainer_id == earning.trainer_id]
    if len(legacy_debits) != 1 or len(matching_earnings) != 1:
        raise BusinessLogicError(
            "Ручная корректировка зарплаты требует сверки перед возвратом",
            code="payment_refund_manual_correction_reconciliation_required",
        )
    debit = legacy_debits[0]
    if debit.correction_group_id is None:
        raise BusinessLogicError(
            "Ручная корректировка зарплаты требует сверки перед возвратом",
            code="payment_refund_manual_correction_reconciliation_required",
        )
    legacy_rows = list(
        TrainerEarningAdjustment.objects.for_club(earning.club_id)
        .select_for_update(of=("self",))
        .filter(
            correction_group_id=debit.correction_group_id,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            affects_payroll=True,
        )
        .order_by("id")
    )
    if len(legacy_rows) != 2:
        raise BusinessLogicError(
            "Ручная корректировка зарплаты требует сверки перед возвратом",
            code="payment_refund_manual_correction_reconciliation_required",
        )
    TrainerEarningAdjustment.objects.for_club(earning.club_id).filter(
        id__in=[row.id for row in legacy_rows],
    ).update(source_earning_id=earning.id)
    for row in legacy_rows:
        row.source_earning_id = earning.id
    return legacy_rows


def _earning_payroll_shares(
    *,
    earning: TrainerEarning,
    payment_earnings: list[TrainerEarning],
) -> dict[int, Decimal]:
    shares = {earning.trainer_id: _money(earning.amount)}
    correction_rows = _manual_correction_rows_for_earning(
        earning=earning,
        payment_earnings=payment_earnings,
    )
    for row in correction_rows:
        shares[row.trainer_id] = shares.get(row.trainer_id, Decimal("0.00")) + _money(row.payable_amount_delta)
    if any(amount < 0 for amount in shares.values()) or _money(sum(shares.values())) != _money(earning.amount):
        raise BusinessLogicError(
            "Ручная корректировка зарплаты не согласована с начислением",
            code="payment_refund_manual_correction_reconciliation_required",
        )
    return {trainer_id: _money(amount) for trainer_id, amount in shares.items() if amount > 0}


def _refund_debits_by_trainer(
    *,
    refund: PaymentRefund,
    earning: TrainerEarning,
    shares: dict[int, Decimal],
    cumulative_after: Decimal,
) -> dict[int, Decimal]:
    prior_debits: dict[int, Decimal] = {}
    for row in (
        TrainerEarningAdjustment.objects.for_club(refund.club_id)
        .filter(
            source_earning_id=earning.id,
            kind=TrainerEarningAdjustment.Kind.REFUND,
            affects_payroll=True,
        )
        .values("trainer_id")
        .annotate(total=Sum("payable_amount_delta"))
    ):
        prior_debits[row["trainer_id"]] = -_money(row["total"])

    earning_target = _money(earning.amount * cumulative_after / refund.payment.amount)
    if cumulative_after == refund.payment.amount:
        earning_target = _money(earning.amount)
    already_debited = _money(sum(prior_debits.values(), Decimal("0.00")))
    required_now = earning_target - already_debited
    if required_now < 0:
        raise BusinessLogicError(
            "Возвратная корректировка превышает исходное начисление",
            code="payment_refund_payroll_reconciliation_required",
        )

    debits: dict[int, Decimal] = {}
    for trainer_id, share in shares.items():
        desired = _money(share * cumulative_after / refund.payment.amount)
        if cumulative_after == refund.payment.amount:
            desired = share
        prior = prior_debits.get(trainer_id, Decimal("0.00"))
        if prior > share:
            raise BusinessLogicError(
                "Возвратная корректировка превышает долю тренера",
                code="payment_refund_payroll_reconciliation_required",
            )
        debits[trainer_id] = max(desired - prior, Decimal("0.00"))

    difference = required_now - _money(sum(debits.values(), Decimal("0.00")))
    if difference > 0:
        for trainer_id in sorted(shares):
            capacity = shares[trainer_id] - prior_debits.get(trainer_id, Decimal("0.00")) - debits[trainer_id]
            addition = min(capacity, difference)
            if addition > 0:
                debits[trainer_id] += addition
                difference -= addition
            if difference == 0:
                break
    elif difference < 0:
        excess = -difference
        for trainer_id in sorted(shares, reverse=True):
            reduction = min(debits[trainer_id], excess)
            if reduction > 0:
                debits[trainer_id] -= reduction
                excess -= reduction
            if excess == 0:
                break
        difference = -excess
    if difference != 0:
        raise BusinessLogicError(
            "Возвратную корректировку зарплаты не удалось распределить",
            code="payment_refund_payroll_reconciliation_required",
        )
    return {trainer_id: _money(amount) for trainer_id, amount in debits.items() if amount > 0}


def _create_refund_payroll_adjustments(
    *,
    refund: PaymentRefund,
    actor_user_id: int,
    effective_date: date,
) -> None:
    prior_total = PaymentRefund.objects.for_club(refund.club_id).filter(
        payment_id=refund.payment_id, id__lt=refund.id
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cumulative_after = prior_total + refund.amount
    earnings = list(
        TrainerEarning.objects.for_club(refund.club_id)
        .select_for_update(of=("self",))
        .filter(
            payment_id=refund.payment_id,
            earning_source=TrainerEarning.Source.SALE,
            cancelled=False,
        )
        .order_by("id")
    )
    for earning in earnings:
        shares = _earning_payroll_shares(
            earning=earning,
            payment_earnings=earnings,
        )
        debits = _refund_debits_by_trainer(
            refund=refund,
            earning=earning,
            shares=shares,
            cumulative_after=cumulative_after,
        )
        for trainer_id, debit in debits.items():
            adjustment, created = TrainerEarningAdjustment.objects.get_or_create(
                club_id=refund.club_id,
                source_refund=refund,
                source_earning=earning,
                trainer_id=trainer_id,
                defaults={
                    "amount_basis_snapshot": refund.amount,
                    "payable_amount_delta": -debit,
                    "affects_payroll": True,
                    "direction": TrainerEarningAdjustment.Direction.DEBIT,
                    "kind": TrainerEarningAdjustment.Kind.REFUND,
                    "effective_date": effective_date,
                    "source_subscription_id": refund.subscription_id,
                    "source_payment_id": refund.payment_id,
                    "idempotency_key": (f"payment-refund-{refund.id}-earning-{earning.id}-trainer-{trainer_id}"),
                    "reason": refund.reason,
                    "created_by_id": actor_user_id,
                },
            )
            if created:
                from apps.trainers.settlement_services import note_adjustment_created

                note_adjustment_created(adjustment=adjustment)


def _post_payment_refund(
    *,
    club_id,
    payment,
    subscription,
    actor_user_id,
    refund_amount,
    refund_kind,
    accounting_date,
    clean_idempotency_key,
    clean_reason,
    entitlement_disposition,
    legacy_enrollment_action=None,
    legacy_enrollment_id=None,
    order=None,
    refund_case=None,
    command_snapshot=None,
):
    """Shared accounting, entitlement ownership and salary compensation under Club."""
    debt_snapshot = _settled_debt_snapshot(club_id=club_id, payment_id=payment.id)
    entitlement_snapshot = _entitlement_snapshot(
        club_id=club_id,
        subscription=subscription,
    )
    refund = PaymentRefund.objects.create(
        club_id=club_id,
        refund_case=refund_case,
        order=order,
        payment=payment,
        subscription=subscription,
        approved_by_id=actor_user_id,
        amount=refund_amount,
        currency=order.currency if order else "RUB",
        source=PaymentRefund.Source.PROVIDER if order else PaymentRefund.Source.MANUAL,
        command_snapshot=command_snapshot or {},
        refund_kind=refund_kind,
        provider_refunded_at=refund_case.provider_refunded_at if refund_case else None,
        accounting_date=accounting_date,
        idempotency_key=clean_idempotency_key,
        reason=clean_reason,
        entitlement_disposition=entitlement_disposition,
        settled_debt_disposition=(
            PaymentRefund.SettledDebtDisposition.ABSORBED
            if debt_snapshot
            else PaymentRefund.SettledDebtDisposition.NONE
        ),
        settled_debts_snapshot=debt_snapshot,
        entitlement_snapshot=entitlement_snapshot,
    )

    if entitlement_disposition == PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS:
        refund.enrollment_disposition = PaymentRefund.EnrollmentDisposition.KEPT
        refund.personal_booking_disposition = PaymentRefund.PersonalBookingDisposition.KEPT
    elif entitlement_disposition == PaymentRefund.EntitlementDisposition.REVOKE_REMAINING:
        (
            refund.enrollment_disposition,
            refund.personal_booking_disposition,
        ) = _revoke_remaining_entitlement(
            club_id=club_id,
            order_id=order.id if order else None,
            payment=payment,
            subscription=subscription,
            actor_user_id=actor_user_id,
            reason=clean_reason,
            legacy_enrollment_action=legacy_enrollment_action,
            legacy_enrollment_id=legacy_enrollment_id,
        )

    has_sale_earnings = (
        TrainerEarning.objects.for_club(club_id)
        .filter(
            payment_id=payment.id,
            earning_source=TrainerEarning.Source.SALE,
            cancelled=False,
        )
        .exists()
    )
    payroll_close = (
        get_trainer_payroll_close_for_date(
            club_id=club_id,
            target_date=accounting_date,
        )
        if has_sale_earnings
        else None
    )
    if payroll_close is not None:
        refund.status = PaymentRefund.Status.PAYROLL_ACTION_REQUIRED
    else:
        _create_refund_payroll_adjustments(
            refund=refund,
            actor_user_id=actor_user_id,
            effective_date=accounting_date,
        )
        refund.status = PaymentRefund.Status.COMPLETED
        refund.payroll_effective_date = accounting_date if has_sale_earnings else None

    refund.save(
        update_fields=[
            "enrollment_disposition",
            "personal_booking_disposition",
            "status",
            "payroll_effective_date",
            "updated_at",
        ]
    )
    return refund


def approve_payment_refund_case(
    *,
    club_id: int,
    case_id: int,
    actor_user_id: int,
    idempotency_key: str,
    amount: Decimal,
    refund_kind: str,
    reason: str,
    entitlement_action: str | None = None,
    legacy_enrollment_action: str | None = None,
    legacy_enrollment_id: int | None = None,
) -> PaymentRefund:
    clean_idempotency_key = idempotency_key.strip()[:120]
    if not clean_idempotency_key:
        raise BusinessLogicError(
            "Укажите ключ операции возврата",
            code="payment_refund_idempotency_required",
        )
    clean_reason = _clean_reason(reason)
    refund_amount = _money(amount)
    if refund_amount <= 0:
        raise BusinessLogicError(
            "Сумма возврата должна быть больше нуля",
            code="payment_refund_amount_invalid",
        )
    if refund_kind not in PaymentRefund.Kind.values:
        raise BusinessLogicError(
            "Некорректный вид возврата",
            code="payment_refund_kind_invalid",
        )

    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        existing = (
            PaymentRefund.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(idempotency_key=clean_idempotency_key)
            .first()
        )
        if existing is not None:
            return _validate_replay(
                existing=existing,
                case_id=case_id,
                amount=refund_amount,
                refund_kind=refund_kind,
            )

        refund_case = PaymentRefundCase.objects.for_club(club_id).select_for_update(of=("self",)).get(id=case_id)
        if refund_case.refund_kind == PaymentRefundCase.Kind.FULL and refund_kind != PaymentRefund.Kind.FULL:
            raise BusinessLogicError(
                "Полный возврат провайдера нельзя провести как частичный",
                code="payment_refund_case_kind_mismatch",
            )
        if refund_case.status == PaymentRefundCase.Status.RESOLVED:
            prior = PaymentRefund.objects.for_club(club_id).filter(refund_case=refund_case).first()
            if prior is not None:
                return _validate_replay(
                    existing=prior,
                    case_id=case_id,
                    amount=refund_amount,
                    refund_kind=refund_kind,
                )
            raise BusinessLogicError(
                "Кейс возврата уже закрыт",
                code="payment_refund_case_already_resolved",
            )

        order = BankPaymentOrder.objects.for_club(club_id).select_for_update(of=("self",)).get(id=refund_case.order_id)
        payment = (
            Payment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("conversion_enrollment")
            .get(id=order.payment_id)
        )
        subscription = (
            Subscription.objects.for_club(club_id).select_for_update(of=("self",)).get(id=order.subscription_id)
        )
        if payment.status != Payment.Status.CONFIRMED:
            raise BusinessLogicError(
                "Возврат возможен только по подтвержденной оплате",
                code="payment_refund_payment_not_confirmed",
            )
        if (
            PaymentRefund.objects.for_club(club_id)
            .filter(
                payment_id=payment.id,
                status=PaymentRefund.Status.PAYROLL_ACTION_REQUIRED,
            )
            .exists()
        ):
            raise BusinessLogicError(
                "Сначала завершите зарплатную корректировку предыдущего возврата",
                code="payment_refund_prior_payroll_action_required",
            )

        from apps.billing.tasks import create_sale_earning

        create_sale_earning(payment.id, club_id)

        prior_total = PaymentRefund.objects.for_club(club_id).filter(payment_id=payment.id).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        cumulative_total = prior_total + refund_amount
        if cumulative_total > payment.amount:
            raise BusinessLogicError(
                "Сумма возвратов превышает сумму оплаты",
                code="payment_refund_amount_exceeds_payment",
            )
        reaches_full = cumulative_total == payment.amount
        if reaches_full and refund_kind != PaymentRefund.Kind.FULL:
            raise BusinessLogicError(
                "Итоговый возврат требует решения по оставшимся правам посещения",
                code="payment_refund_full_action_required",
            )
        if refund_kind == PaymentRefund.Kind.FULL and not reaches_full:
            raise BusinessLogicError(
                "Полный возврат должен закрывать всю оставшуюся сумму оплаты",
                code="payment_refund_full_amount_required",
            )

        if refund_kind == PaymentRefund.Kind.PARTIAL:
            entitlement_disposition = PaymentRefund.EntitlementDisposition.KEPT_PARTIAL
        else:
            if entitlement_action not in {
                PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
                PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
            }:
                raise BusinessLogicError(
                    "Выберите, сохранить или отозвать оставшиеся права посещения",
                    code="payment_refund_entitlement_action_required",
                )
            entitlement_disposition = entitlement_action

        accounting_at = refund_case.provider_refunded_at or timezone.now()
        accounting_date = club_localdate_by_id(club_id, accounting_at)
        refund = _post_payment_refund(
            club_id=club_id,
            payment=payment,
            subscription=subscription,
            actor_user_id=actor_user_id,
            refund_amount=refund_amount,
            refund_kind=refund_kind,
            accounting_date=accounting_date,
            clean_idempotency_key=clean_idempotency_key,
            clean_reason=clean_reason,
            entitlement_disposition=entitlement_disposition,
            legacy_enrollment_action=legacy_enrollment_action,
            legacy_enrollment_id=legacy_enrollment_id,
            order=order,
            refund_case=refund_case,
        )

        order.status = BankPaymentOrder.Status.REFUNDED if reaches_full else BankPaymentOrder.Status.REFUNDED_PARTIALLY
        order.last_error_code = ""
        order.last_error_message = clean_reason
        order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
        refund_case.status = PaymentRefundCase.Status.RESOLVED
        refund_case.resolved_at = timezone.now()
        refund_case.resolved_by_id = actor_user_id
        refund_case.resolution_note = clean_reason
        refund_case.save(
            update_fields=[
                "status",
                "resolved_at",
                "resolved_by",
                "resolution_note",
                "updated_at",
            ]
        )
        refund.save(
            update_fields=[
                "enrollment_disposition",
                "personal_booking_disposition",
                "status",
                "payroll_effective_date",
                "updated_at",
            ]
        )
        return refund


def complete_payment_refund_payroll(
    *,
    club_id: int,
    refund_id: int,
    actor_user_id: int,
    effective_date: date,
) -> PaymentRefund:
    with transaction.atomic():
        from apps.trainers.services import lock_trainer_payroll_mutation_scope

        lock_trainer_payroll_mutation_scope(club_id=club_id)
        refund = (
            PaymentRefund.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "subscription")
            .get(id=refund_id)
        )
        if refund.status == PaymentRefund.Status.COMPLETED:
            return refund
        if refund.status != PaymentRefund.Status.PAYROLL_ACTION_REQUIRED:
            raise BusinessLogicError(
                "Возврат не ожидает решения по зарплате",
                code="payment_refund_payroll_action_not_required",
            )
        if effective_date < refund.accounting_date:
            raise BusinessLogicError(
                "Дата зарплатной компенсации не может быть раньше возврата",
                code="payment_refund_payroll_date_before_refund",
            )
        lock_and_assert_trainer_payroll_date_open(
            club_id=club_id,
            target_date=effective_date,
        )
        _create_refund_payroll_adjustments(
            refund=refund,
            actor_user_id=actor_user_id,
            effective_date=effective_date,
        )
        refund.status = PaymentRefund.Status.COMPLETED
        refund.payroll_effective_date = effective_date
        refund.save(update_fields=["status", "payroll_effective_date", "updated_at"])
        return refund


def record_manual_payment_refund(
    *,
    club_id: int,
    payment_id: int,
    subscription_id: int,
    actor_user_id: int,
    amount: Decimal,
    accounting_date: date,
    reason: str,
    idempotency_key: str,
    entitlement_action: str,
    legacy_enrollment_action: str | None = None,
    legacy_enrollment_id: int | None = None,
) -> PaymentRefund:
    """Record money actually returned for an exact manual/opening payment."""
    from django.conf import settings

    from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope
    from apps.billing.recognition import payment_recognition_date
    from apps.clubs.models import ClubMembership

    key = idempotency_key.strip()
    clean_reason = _clean_reason(reason)
    refund_amount = _money(amount)
    if not key or len(key) > 120:
        raise BusinessLogicError("Укажите постоянный ключ возврата", code="payment_refund_idempotency_required")
    if refund_amount <= 0 or Decimal(str(amount)) != refund_amount:
        raise BusinessLogicError(
            "Сумма должна быть положительной, с точностью до копеек", code="payment_refund_amount_invalid"
        )
    command = {
        "payment_id": payment_id,
        "subscription_id": subscription_id,
        "amount": str(refund_amount),
        "accounting_date": accounting_date.isoformat(),
        "reason": clean_reason,
        "entitlement_action": entitlement_action,
        "legacy_enrollment_action": legacy_enrollment_action,
        "legacy_enrollment_id": legacy_enrollment_id,
    }
    with transaction.atomic():
        lock_training_group_mutation_scope(club_id=club_id)
        if not ClubMembership.objects.filter(
            club_id=club_id,
            user_id=actor_user_id,
            is_active=True,
            user__is_active=True,
            role__in=["owner", "admin"],
        ).exists():
            raise BusinessLogicError("Действие доступно владельцу или администратору", code="actor_not_authorized")
        existing = PaymentRefund.objects.for_club(club_id).filter(idempotency_key=key).first()
        if existing:
            if existing.source != PaymentRefund.Source.MANUAL or existing.command_snapshot != command:
                raise BusinessLogicError(
                    "Ключ уже использован для другого возврата", code="payment_refund_idempotency_conflict"
                )
            return existing
        if not settings.STUDENT_ADMIN_CORRECTIONS_ENABLED:
            raise BusinessLogicError("Новые ручные возвраты выключены", code="student_corrections_disabled")
        payment = Payment.objects.for_club(club_id).select_for_update(of=("self",)).filter(id=payment_id).first()
        if payment is None or payment.subscription_id != subscription_id:
            raise BusinessLogicError(
                "Оплата и абонемент не соответствуют друг другу", code="payment_refund_subscription_mismatch"
            )
        if (
            payment.payment_method == Payment.Method.ONLINE
            or BankPaymentOrder.objects.for_club(club_id).filter(payment_id=payment.id).exists()
        ):
            raise BusinessLogicError(
                "Проведите возврат через кейс онлайн-оплаты", code="payment_refund_provider_required"
            )
        subscription = (
            Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=subscription_id, student_id=payment.student_id, student__club_id=club_id)
            .first()
        )
        if subscription is None:
            raise BusinessLogicError("Абонемент не соответствует оплате", code="payment_refund_subscription_mismatch")
        if payment.status != Payment.Status.CONFIRMED:
            raise BusinessLogicError(
                "Возврат возможен только по подтверждённой оплате", code="payment_refund_payment_not_confirmed"
            )
        recognized = payment_recognition_date(payment=payment)
        if recognized is None or accounting_date < recognized or accounting_date > club_localdate_by_id(club_id):
            raise BusinessLogicError(
                "Дата возврата должна быть между датой оплаты и сегодняшним днём",
                code="payment_refund_accounting_date_invalid",
            )
        prior = PaymentRefund.objects.for_club(club_id).filter(payment_id=payment.id)
        if prior.filter(status=PaymentRefund.Status.PAYROLL_ACTION_REQUIRED).exists():
            raise BusinessLogicError(
                "Сначала завершите зарплатное действие предыдущего возврата",
                code="payment_refund_prior_payroll_action_required",
            )
        if prior.filter(accounting_date__gt=accounting_date).exists():
            raise BusinessLogicError(
                "Более ранний возврат требует отдельной сверки", code="payment_refund_date_before_prior"
            )
        cumulative = (prior.aggregate(total=Sum("amount"))["total"] or Decimal("0")) + refund_amount
        if cumulative > payment.amount:
            raise BusinessLogicError("Сумма возвратов превышает оплату", code="payment_refund_amount_exceeds_payment")
        full = cumulative == payment.amount
        if full:
            allowed = {
                PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
                PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
            }
        else:
            allowed = {PaymentRefund.EntitlementDisposition.KEPT_PARTIAL}
        if entitlement_action not in allowed:
            raise BusinessLogicError(
                "Частичный возврат сохраняет право; для полного выберите сохранить или отозвать",
                code="payment_refund_entitlement_action_required",
            )
        from apps.billing.tasks import create_sale_earning

        create_sale_earning(payment.id, club_id)
        return _post_payment_refund(
            club_id=club_id,
            payment=payment,
            subscription=subscription,
            actor_user_id=actor_user_id,
            refund_amount=refund_amount,
            refund_kind=PaymentRefund.Kind.FULL if full else PaymentRefund.Kind.PARTIAL,
            accounting_date=accounting_date,
            clean_idempotency_key=key,
            clean_reason=clean_reason,
            entitlement_disposition=entitlement_action,
            legacy_enrollment_action=legacy_enrollment_action,
            legacy_enrollment_id=legacy_enrollment_id,
            command_snapshot=command,
        )
