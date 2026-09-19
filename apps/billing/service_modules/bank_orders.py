from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentReconciliationAttempt,
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    Payment,
    Subscription,
    Tariff,
    TrainingType,
)
from apps.billing.payment_providers.base import (
    SBP_ONLY_PAYMENT_MODES,
    validate_sbp_only_payment_modes,
)
from apps.billing.service_modules._shared import _money
from apps.billing.service_modules.debts import (
    debt_state as _debt_state,
)
from apps.billing.service_modules.debts import (
    record_debt_lifecycle_event as _record_debt_lifecycle_event,
)
from apps.billing.service_modules.debts import (
    record_debt_settlement_events as _record_debt_settlement_events,
)
from apps.billing.service_modules.entitlements import (
    _resolve_package_owner_trainer_id_for_components,
)
from apps.billing.service_modules.group_contracts import (
    validate_trainer_group_payment_contract as _validate_trainer_group_payment_contract,
)
from apps.billing.service_modules.group_payments import (
    _resolve_canonical_group_payment_target,
    _validate_group_conversion_target,
)
from apps.billing.service_modules.payment_creation import _apply_discounts, create_payment
from apps.billing.service_modules.tariff_components import (
    _payment_preflight_tariff_components,
)
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student

logger = logging.getLogger(__name__)

LIVE_BANK_PAYMENT_ORDER_STATUSES = (
    BankPaymentOrder.Status.CREATED,
    BankPaymentOrder.Status.PENDING,
    BankPaymentOrder.Status.AUTHORIZED,
)

CLOSABLE_BANK_PAYMENT_ORDER_STATUSES = LIVE_BANK_PAYMENT_ORDER_STATUSES

_RECOVERY_LEASE_SECONDS = 120
_RECOVERABLE_ORDER_STATUSES = frozenset(
    {
        BankPaymentOrder.Status.CREATED,
        BankPaymentOrder.Status.PENDING,
        BankPaymentOrder.Status.AUTHORIZED,
    }
)


def _assert_no_live_non_sbp_bank_payment_order(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int | None,
    now,
) -> None:
    legacy_order = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student_id=student_id,
            payment__student_id=student_id,
            payment__tariff_id=tariff_id,
            payment__status=Payment.Status.PENDING,
            subscription__student_id=student_id,
            subscription__tariff_id=tariff_id,
            subscription__status=Subscription.Status.PENDING,
            subscription__deleted_at__isnull=True,
        )
        .filter(
            Q(status=BankPaymentOrder.Status.MANUAL_REVIEW)
            | Q(status__in=LIVE_BANK_PAYMENT_ORDER_STATUSES, expires_at__gt=now)
        )
        .exclude(provider_payment_modes=list(SBP_ONLY_PAYMENT_MODES))
        .order_by("id")
        .first()
    )
    if legacy_order is not None:
        raise BusinessLogicError(
            "Активная ссылка использует недоступный способ оплаты. Отмените её и создайте новую",
            code="bank_payment_order_legacy_payment_mode",
        )





def _active_renewed_from_subscription(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int | None,
    lock: bool = True,
) -> Subscription | None:
    subscriptions = (
        Subscription.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            tariff_id=tariff_id,
            status=Subscription.Status.ACTIVE,
            deleted_at__isnull=True,
        )
        .order_by("-expires_at", "-id")
    )
    if lock:
        subscriptions = subscriptions.select_for_update()
    return subscriptions.first()


def _legacy_renewal_source_preview(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    source: str,
) -> Subscription | None:
    """Recover the historical source for the pre-context student/parent call.

    Legacy self-service callers submit the source tariff and expected current
    offer, but cannot submit ``renewed_from_subscription_id``.  Preserve the
    old active-source choice first.  Once an accepted provider family retires
    that source, use only an unambiguous existing bank-order family; this
    bounded fallback lets an accepted receipt replay without guessing among
    unrelated historical subscriptions.
    """

    active_source = _active_renewed_from_subscription(
        club_id=club_id,
        student_id=student_id,
        tariff_id=tariff_id,
        lock=False,
    )
    if active_source is not None:
        return active_source

    family_source_ids = list(
        BankPaymentOrder.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            source=source,
            renewed_from_subscription__student_id=student_id,
            renewed_from_subscription__tariff_id=tariff_id,
            renewed_from_subscription__deleted_at__isnull=True,
            payment__student_id=student_id,
            payment__status__in=[Payment.Status.PENDING, Payment.Status.CONFIRMED],
            status__in=[
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.APPROVED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            ],
        )
        .order_by("-id")
        .values_list("renewed_from_subscription_id", flat=True)
        .distinct()[:2]
    )
    if len(family_source_ids) > 1:
        raise BusinessLogicError(
            "Источник продления требует ручного выбора",
            code="renewal_source_ambiguous",
        )
    if not family_source_ids:
        # A natural expiry has no accepted order family to identify it, but a
        # legacy client still names the historical source tariff.  Recover one
        # exact historical leaf only when there is no competing source.  Keep
        # administratively closed states in the candidate set so the exact
        # renewal validator can preserve its frozen/pending/closed guard
        # instead of silently starting a fresh purchase on the new leaf.
        historical_source_ids = list(
            Subscription.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                tariff_id=tariff_id,
                deleted_at__isnull=True,
                status__in=[
                    Subscription.Status.EXPIRED,
                    Subscription.Status.FROZEN,
                    Subscription.Status.PENDING,
                    Subscription.Status.CANCELLED,
                ],
            )
            .order_by("-expires_at", "-id")
            .values_list("id", flat=True)[:2]
        )
        if len(historical_source_ids) > 1:
            raise BusinessLogicError(
                "Источник продления требует ручного выбора",
                code="renewal_source_ambiguous",
            )
        if not historical_source_ids:
            return None
        source_id = historical_source_ids[0]
    else:
        source_id = family_source_ids[0]
    return (
        Subscription.objects.for_club(club_id)
        .select_related("tariff")
        .filter(
            id=source_id,
            student_id=student_id,
            tariff_id=tariff_id,
            deleted_at__isnull=True,
        )
        .first()
    )


def _assert_locked_exact_group_renewal_target(
    *,
    club_id: int,
    student: Student,
    tariff: Tariff,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
    rollout_state,
) -> None:
    """Require a live matching membership before treating a group command as renewal.

    The caller holds the club, rollout, student, and exact source-subscription
    locks.  This prevents a caller from naming a valid source subscription to
    route a new group admission around the v2 group-sale command.
    """

    target_schedule = _validate_group_conversion_target(
        club_id=club_id,
        tariff=tariff,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        seller_trainer_id=None,
        lock_schedule=True,
    )
    canonical_group, membership, action = _resolve_canonical_group_payment_target(
        club_id=club_id,
        student_id=student.id,
        schedule=target_schedule,
        target_start_date=target_start_date,
        requested_training_group_id=target_training_group_id,
        rollout_state=rollout_state,
        lock=True,
    )
    if (
        canonical_group is None
        or membership is None
        or action != Payment.GroupMembershipActionSnapshot.RENEWAL
    ):
        raise BusinessLogicError(
            "Точное групповое продление требует действующее участие в выбранной группе",
            code="renewal_group_membership_required",
        )


def _generate_provider_payment_link_id(order_id: int) -> str:
    return f"jgr-{order_id}-{uuid4().hex[:8]}"


def _payment_order_ttl_minutes(source: str) -> int:
    if source in {BankPaymentOrder.Source.STUDENT, BankPaymentOrder.Source.PARENT}:
        return settings.TOCHKA_SELF_SERVICE_PAYMENT_LINK_TTL_MINUTES
    return settings.TOCHKA_STAFF_PAYMENT_LINK_TTL_MINUTES


def _payment_order_purpose(*, tariff: Tariff, student: Student) -> str:
    purpose = f"Оплата абонемента {tariff.name}, ученик #{student.id}"
    return purpose[:255]


def _receipt_mode() -> str:
    mode = settings.TOCHKA_RECEIPT_MODE or BankPaymentOrder.ReceiptMode.NONE
    if mode not in BankPaymentOrder.ReceiptMode.values:
        raise BusinessLogicError("Некорректный режим чеков", code="invalid_receipt_mode")
    return mode


def _receipt_status_for_mode(receipt_mode: str) -> str:
    if receipt_mode == BankPaymentOrder.ReceiptMode.NONE:
        return BankPaymentOrder.ReceiptStatus.NOT_REQUIRED
    return BankPaymentOrder.ReceiptStatus.PENDING


def _fiscal_item_snapshot(*, tariff: Tariff, amount: Decimal) -> dict:
    return {
        "name": tariff.name,
        "amount": str(_money(amount)),
        "quantity": 1,
        "vatType": settings.TOCHKA_RECEIPT_VAT_TYPE,
        "paymentMethod": settings.TOCHKA_RECEIPT_PAYMENT_METHOD,
        "paymentObject": "service",
    }


def _find_reusable_bank_payment_order(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    source: str,
    student: Student,
    discount_ids: list[int] | None,
    debt_ids: list[int] | None,
    seller_trainer_id: int | None,
    package_owner_trainer_id: int | None,
    target_schedule_id: int | None,
    target_training_group_id: int | None,
    target_start_date: date | None,
    personal_booking_reservation_id: int | None,
    personal_drop_in_booking_id: int | None,
    now,
    tariff: Tariff,
    rollout_state,
    preflight_training_group_id: int | None,
    enforce_trainer_group_contract: bool,
) -> BankPaymentOrder | None:
    requested_debt_ids = set(dict.fromkeys(debt_ids or []))
    requested_discount_ids = set(dict.fromkeys(discount_ids or []))
    if personal_booking_reservation_id is not None or personal_drop_in_booking_id is not None:
        return None
    order = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student_id=student_id,
            payment__student_id=student_id,
            payment__tariff_id=tariff_id,
            payment__status=Payment.Status.PENDING,
            subscription__student_id=student_id,
            subscription__tariff_id=tariff_id,
            subscription__status=Subscription.Status.PENDING,
            subscription__deleted_at__isnull=True,
        )
        .filter(
            Q(status=BankPaymentOrder.Status.MANUAL_REVIEW)
            | Q(status__in=LIVE_BANK_PAYMENT_ORDER_STATUSES, expires_at__gt=now)
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if order is None:
        return None

    # The caller already holds the student serialization lock. Lock the whole
    # existing financial family before any target group/slot row. A reuse
    # candidate is an existing mutation root even though no payment is created.
    payment = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("tariff")
        .get(id=order.payment_id)
    )
    subscription = (
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .get(id=order.subscription_id)
    )
    if (
        payment.student_id != student_id
        or payment.tariff_id != tariff_id
        or payment.status != Payment.Status.PENDING
        or subscription.student_id != student_id
        or subscription.tariff_id != tariff_id
        or subscription.status != Subscription.Status.PENDING
        or subscription.deleted_at is not None
    ):
        raise BusinessLogicError(
            "Активная ссылка на оплату изменилась и требует ручной проверки",
            code="bank_payment_order_pending_family_changed",
        )

    effective_seller_trainer_id = seller_trainer_id
    effective_target_training_group_id = target_training_group_id
    effective_target_group_membership_id = None
    effective_target_start_date = target_start_date
    if target_schedule_id is not None:
        # The group ID comes from the preflight read.  It is locked before the
        # exact schedule, then the schedule association is revalidated.
        if enforce_trainer_group_contract:
            _validate_trainer_group_payment_contract(
                club_id=club_id,
                student=student,
                tariff=tariff,
                target_schedule_id=target_schedule_id,
                target_start_date=target_start_date,
                lock_enrollments=True,
            )
        if preflight_training_group_id is not None:
            from apps.attendance.models import TrainingGroup

            TrainingGroup.objects.for_club(club_id).select_for_update(of=("self",)).get(
                id=preflight_training_group_id
            )
        target_schedule = _validate_group_conversion_target(
            club_id=club_id,
            tariff=tariff,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            seller_trainer_id=None,
            lock_schedule=True,
        )
        if target_schedule.training_group_id != preflight_training_group_id:
            raise BusinessLogicError(
                "Целевая группа изменилась, выберите группу заново",
                code="bank_payment_order_target_changed",
            )
        if target_schedule.training_group_id is not None and rollout_state is None:
            raise BusinessLogicError(
                "Целевая группа изменилась, выберите группу заново",
                code="bank_payment_order_target_changed",
            )
        canonical_group, target_group_membership, _ = _resolve_canonical_group_payment_target(
            club_id=club_id,
            student_id=student.id,
            schedule=target_schedule,
            target_start_date=target_start_date,
            requested_training_group_id=target_training_group_id,
            rollout_state=rollout_state,
            lock=True,
        )
        effective_seller_trainer_id = (
            canonical_group.responsible_trainer_id
            if canonical_group is not None
            else target_schedule.trainer_id
        )
        effective_target_training_group_id = (
            canonical_group.id if canonical_group is not None else None
        )
        effective_target_group_membership_id = (
            target_group_membership.id if target_group_membership is not None else None
        )
        effective_target_start_date = target_start_date

    components = _payment_preflight_tariff_components(tariff, club_id=club_id)
    effective_package_owner_trainer_id = _resolve_package_owner_trainer_id_for_components(
        components=components,
        seller_trainer_id=effective_seller_trainer_id,
        package_owner_trainer_id=package_owner_trainer_id,
    )

    reserved_debt_ids = set(
        Debt.objects.for_club(club_id)
        .filter(settlement_payment_id=payment.id, resolved_at__isnull=True)
        .values_list("id", flat=True)
    )
    applied_discount_ids = set(payment.applied_discounts.values_list("id", flat=True))
    expected_amount, _ = _apply_discounts(
        base_price=tariff.price,
        club_id=club_id,
        discount_ids=list(requested_discount_ids),
    )
    expected_renewed_from = _active_renewed_from_subscription(
        club_id=club_id,
        student_id=student_id,
        tariff_id=tariff_id,
    )
    same_snapshot = (
        reserved_debt_ids == requested_debt_ids
        and applied_discount_ids == requested_discount_ids
        and payment.seller_trainer_id == effective_seller_trainer_id
        and payment.package_owner_trainer_id == effective_package_owner_trainer_id
        and payment.target_schedule_id == target_schedule_id
        and payment.target_training_group_id == effective_target_training_group_id
        and payment.target_group_membership_id == effective_target_group_membership_id
        and payment.target_start_date == effective_target_start_date
        and order.amount_snapshot == expected_amount
        and order.currency == "RUB"
        and order.purpose_snapshot == _payment_order_purpose(tariff=tariff, student=student)
        and order.renewed_from_subscription_id
        == (expected_renewed_from.id if expected_renewed_from is not None else None)
    )
    if same_snapshot:
        if (
            source == BankPaymentOrder.Source.TRAINER
            and order.source in {BankPaymentOrder.Source.STUDENT, BankPaymentOrder.Source.PARENT}
        ):
            # The canonical order blocks a duplicate, but its private self-
            # service link is not disclosed to a trainer who did not create it.
            raise BusinessLogicError(
                "У ученика уже есть самостоятельная ссылка на эту оплату",
                code="bank_payment_order_private_intent_exists",
            )
        return order

    raise BusinessLogicError(
        "У ученика уже есть активная ссылка на оплату с другим составом",
        code="bank_payment_order_pending_exists",
    )



def _self_service_bank_order_source(source: str) -> bool:
    return source in {
        BankPaymentOrder.Source.STUDENT,
        BankPaymentOrder.Source.PARENT,
    }


def _assert_self_service_bank_order_allowed(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    source: str,
    discount_ids: list[int] | None,
    debt_ids: list[int] | None,
    allow_new_self_service_subscription: bool = False,
) -> None:
    if not _self_service_bank_order_source(source):
        return

    if discount_ids:
        raise BusinessLogicError(
            "Самостоятельная оплата со скидкой пока недоступна",
            code="self_service_discount_not_supported",
        )
    if debt_ids:
        raise BusinessLogicError(
            "Самостоятельная оплата долгов пока недоступна",
            code="self_service_debt_payment_not_supported",
        )

    if Subscription.objects.for_club(club_id).filter(
        student_id=student_id,
        tariff_id=tariff_id,
        deleted_at__isnull=True,
    ).exists():
        return
    if allow_new_self_service_subscription:
        return
    raise BusinessLogicError(
        "Тариф недоступен для самостоятельного продления",
        code="self_service_tariff_not_allowed",
    )


def _cancel_pending_bank_order_artifacts(
    *,
    order: BankPaymentOrder,
    reason: str,
    actor_user_id: int | None = None,
) -> None:
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=order.club_id)
        payment = (
            Payment.objects.for_club(order.club_id)
            .select_for_update(of=("self",))
            .select_related("subscription")
            .get(id=order.payment_id)
        )
        if payment.status != Payment.Status.PENDING:
            return

        payment.status = Payment.Status.REJECTED
        payment.rejection_reason = reason
        payment.save(update_fields=["status", "rejection_reason", "updated_at"])

        if payment.subscription and payment.subscription.status == Subscription.Status.PENDING:
            payment.subscription.soft_delete()

        reserved_debts = list(
            Debt.objects.for_club(order.club_id)
            .select_for_update(of=("self",))
            .select_related("student")
            .filter(
                settlement_payment_id=payment.id,
                resolved_at__isnull=True,
            )
        )
        _record_debt_settlement_events(
            club_id=order.club_id,
            payment=payment,
            debt_ids=[debt.id for debt in reserved_debts],
            event_type=DebtSettlementEvent.EventType.REJECTED,
        )
        for debt in reserved_debts:
            _record_debt_lifecycle_event(
                club_id=order.club_id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.REJECTED,
                previous_state=_debt_state(debt),
                new_state="open",
                actor_user_id=actor_user_id,
                reason=reason,
                payment_id=payment.id,
                subscription_id=payment.subscription_id,
            )
        Debt.objects.for_club(order.club_id).filter(
            settlement_payment_id=payment.id,
            resolved_at__isnull=True,
        ).update(settlement_payment=None)

        from apps.attendance.services import close_personal_booking_payment_reservation_for_order

        close_personal_booking_payment_reservation_for_order(
            club_id=order.club_id,
            order_id=order.id,
            status=(
                "expired"
                if order.status == BankPaymentOrder.Status.EXPIRED
                else "cancelled"
            ),
            reason=reason,
        )
        if (
            payment.group_membership_action_snapshot
            == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
            and payment.target_training_group_id is not None
        ):
            from apps.leads.services import restore_lead_after_terminal_group_payment

            restore_lead_after_terminal_group_payment(
                club_id=order.club_id,
                student_id=payment.student_id,
                payment_id=payment.id,
                actor_user_id=actor_user_id,
                outcome=order.status,
            )


def close_pending_personal_drop_in_financial_family(
    *,
    club_id: int,
    booking_id: int,
    actor_user_id: int,
    reason: str,
) -> bool:
    """Dispose a complete personal drop-in's pending financial family.

    This is deliberately owned by billing rather than the attendance terminal
    action.  A pre-check-in cancellation has to release the exact booking, but
    it may only reject financial rows that were created from that immutable
    personal terms snapshot.  The preliminary reads give us the stable lock
    identities; the mutation itself then follows the personal-command order:
    trainer, student, availability slot, booking, enrollment, payment,
    subscription and bank order.

    Returning ``False`` keeps legacy/flag-off bookings on their existing
    payment-review path.  Returning ``True`` means at least one pending payment
    belonging to the complete terms was terminally disposed.
    """

    from apps.attendance.models import (
        PersonalAvailabilitySlot,
        PersonalDropInBooking,
        PersonalDropInPaymentLink,
        PersonalServiceTermsSnapshot,
        ScheduleEnrollment,
    )
    from apps.trainers.models import Trainer

    preview = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_related("enrollment__schedule")
        .filter(id=booking_id)
        .values(
            "enrollment_id",
            "enrollment__student_id",
            "enrollment__schedule__trainer_id",
        )
        .first()
    )
    if preview is None:
        return False
    from apps.attendance.models import complete_personal_terms_queryset

    if not complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking_id)
    ).exists():
        return False

    # These are read before mutable rows are locked so that the canonical lock
    # order never has to take a finance lock just to discover its owner.
    preview_payment_ids = set(
        PersonalDropInPaymentLink.objects.for_club(club_id)
        .filter(booking_id=booking_id)
        .values_list("payment_id", flat=True)
    )
    preview_payment_ids.update(
        BankPaymentOrder.objects.for_club(club_id)
        .filter(personal_drop_in_booking_id_snapshot=booking_id)
        .values_list("payment_id", flat=True)
    )

    with transaction.atomic():
        Trainer.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=preview["enrollment__schedule__trainer_id"]
        )
        Student.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=preview["enrollment__student_id"]
        )
        _slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booked_enrollment_id=preview["enrollment_id"])
            .order_by("id")
            .first()
        )
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment__schedule")
            .get(id=booking_id)
        )
        if (
            booking.enrollment_id != preview["enrollment_id"]
            or booking.enrollment.student_id != preview["enrollment__student_id"]
            or booking.enrollment.schedule.trainer_id
            != preview["enrollment__schedule__trainer_id"]
        ):
            raise BusinessLogicError(
                "Personal booking ownership changed while closing pending payment",
                code="personal_drop_in_lock_scope_changed",
            )
        if booking.state != PersonalDropInBooking.State.SCHEDULED:
            # A concurrent check-in has already become the lifecycle owner;
            # never terminally dispose its pending order after attendance.
            return False
        ScheduleEnrollment.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=booking.enrollment_id
        )

        links = list(
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booking_id=booking.id)
            .order_by("id")
        )
        payment_ids = preview_payment_ids | {link.payment_id for link in links}
        payments = {
            payment.id: payment
            for payment in Payment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=payment_ids)
            .order_by("id")
        }
        subscription_ids = {
            payment.subscription_id
            for payment in payments.values()
            if payment.subscription_id is not None
        }
        subscriptions = {
            subscription.id: subscription
            for subscription in Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=subscription_ids)
            .order_by("id")
        }
        orders = list(
            BankPaymentOrder.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                Q(personal_drop_in_booking_id_snapshot=booking.id)
                | Q(payment_id__in=payment_ids)
            )
            .order_by("id")
        )
        linked_order_ids = {link.bank_payment_order_id for link in links if link.bank_payment_order_id}
        live_order_statuses = {
            BankPaymentOrder.Status.CREATED,
            BankPaymentOrder.Status.PENDING,
            BankPaymentOrder.Status.APPROVED,
            BankPaymentOrder.Status.AUTHORIZED,
            BankPaymentOrder.Status.MANUAL_REVIEW,
        }
        # A committed origin snapshot can briefly precede its attendance link.
        # Treat it as part of this exact family rather than letting the
        # terminal action ignore it (or merely reject the cancellation later).
        for order in orders:
            payment = payments.get(order.payment_id)
            if (
                order.personal_drop_in_booking_id_snapshot != booking.id
                or order.id in linked_order_ids
                or order.status not in live_order_statuses
                or payment is None
                or payment.status != Payment.Status.PENDING
            ):
                continue
            bridge = PersonalDropInPaymentLink(
                club_id=club_id,
                booking=booking,
                payment=payment,
                bank_payment_order=order,
                created_by_id=order.created_by_id,
                idempotency_key=f"personal-bank-order-claim-{order.id}",
            )
            bridge.full_clean()
            bridge.save()
            links.append(bridge)
            linked_order_ids.add(order.id)
        pending_links = [
            link
            for link in links
            if payments.get(link.payment_id) is not None
            and payments[link.payment_id].status == Payment.Status.PENDING
        ]
        if not pending_links:
            return False

        orders_by_id = {order.id: order for order in orders}
        for link in pending_links:
            if link.bank_payment_order_id and (
                (order := orders_by_id.get(link.bank_payment_order_id)) is None
                or order.payment_id != link.payment_id
            ):
                raise BusinessLogicError(
                    "Personal payment order does not match its payment link",
                    code="personal_drop_in_bank_order_mismatch",
                )

        normalized_reason = reason.strip()[:500] or "personal_booking_cancelled"
        for order in orders:
            payment = payments.get(order.payment_id)
            if payment is None or payment.status != Payment.Status.PENDING:
                continue
            if order.status in {
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.APPROVED,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            }:
                order.status = BankPaymentOrder.Status.CANCELLED
                order.last_error_code = "personal_booking_cancelled"
                order.last_error_message = normalized_reason
                order.save(
                    update_fields=["status", "last_error_code", "last_error_message", "updated_at"]
                )

        for link in pending_links:
            payment = payments[link.payment_id]
            payment.status = Payment.Status.REJECTED
            payment.rejection_reason = normalized_reason
            payment.save(update_fields=["status", "rejection_reason", "updated_at"])
            subscription = subscriptions.get(payment.subscription_id)
            if subscription is not None and subscription.status == Subscription.Status.PENDING:
                subscription.soft_delete()

            from apps.leads.services import restore_lead_after_terminal_personal_payment

            restore_lead_after_terminal_personal_payment(
                club_id=club_id,
                student_id=booking.enrollment.student_id,
                payment_id=payment.id,
                actor_user_id=actor_user_id,
                outcome="cancelled_before_checkin",
            )

        return True


def _fail_bank_payment_order_and_cleanup(
    *,
    club_id: int,
    order_id: int,
    error_code: str,
    error_message: str,
    actor_user_id: int | None,
    link_creation_state: str | None = None,
    cleanup_reason: str | None = None,
) -> BankPaymentOrder:
    """Commit a deterministic provider failure and every linked disposition together."""
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update(of=("self",)).get(id=order_id)
        )
        order.status = BankPaymentOrder.Status.FAILED
        order.last_error_code = error_code
        order.last_error_message = error_message
        update_fields = ["status", "last_error_code", "last_error_message", "updated_at"]
        if link_creation_state is not None:
            order.link_creation_state = link_creation_state
            update_fields.append("link_creation_state")
        order.save(update_fields=update_fields)
        _cancel_pending_bank_order_artifacts(
            order=order,
            reason=cleanup_reason or error_message,
            actor_user_id=actor_user_id,
        )
    return order


def _find_existing_renewal_bank_order_family(
    *,
    club_id: int,
    source_subscription_id: int,
    source: str | None = None,
    discount_ids: list[int] | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date: date | None = None,
) -> BankPaymentOrder | None:
    """Find a matching online renewal family before consulting current pricing."""

    orders = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("payment", "payment__tariff", "subscription")
        .filter(
            renewed_from_subscription_id=source_subscription_id,
            payment__payment_method=Payment.Method.ONLINE,
            payment__status__in=[Payment.Status.PENDING, Payment.Status.CONFIRMED],
            status__in=[
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.APPROVED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            ],
        )
        .order_by("id")
    )
    if target_schedule_id is not None:
        orders = orders.filter(payment__target_schedule_id=target_schedule_id)
    else:
        orders = orders.filter(payment__target_schedule__isnull=True)
    if target_training_group_id is not None:
        orders = orders.filter(payment__target_training_group_id=target_training_group_id)
    else:
        orders = orders.filter(payment__target_training_group__isnull=True)
    if target_start_date is not None:
        orders = orders.filter(payment__target_start_date=target_start_date)
    elif target_schedule_id is None:
        orders = orders.filter(payment__target_start_date__isnull=True)
    if source is not None:
        orders = orders.filter(source=source)
    order = orders.first()
    if order is None:
        return None
    requested_discount_ids = set(dict.fromkeys(discount_ids or []))
    existing_discount_ids = set(order.payment.applied_discounts.values_list("id", flat=True))
    if existing_discount_ids != requested_discount_ids:
        return None
    return order


def _find_existing_ordinary_bank_order(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    source: str,
    discount_ids: list[int] | None = None,
    debt_ids: list[int] | None = None,
    seller_trainer_id: int | None = None,
    package_owner_trainer_id: int | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date: date | None = None,
) -> BankPaymentOrder | None:
    """Return an exact unkeyed ordinary order before a retired tariff lookup."""

    orders = (
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("payment", "payment__tariff", "subscription")
        .filter(
            student_id=student_id,
            source=source,
            renewed_from_subscription_id__isnull=True,
            payment__student_id=student_id,
            payment__tariff_id=tariff_id,
            payment__payment_method=Payment.Method.ONLINE,
            payment__status=Payment.Status.PENDING,
            status__in=[
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            ],
        )
        .order_by("id")
    )
    if target_schedule_id is not None:
        orders = orders.filter(payment__target_schedule_id=target_schedule_id)
    else:
        orders = orders.filter(payment__target_schedule__isnull=True)
    if target_training_group_id is not None:
        orders = orders.filter(payment__target_training_group_id=target_training_group_id)
    else:
        orders = orders.filter(payment__target_training_group__isnull=True)
    if target_start_date is not None:
        orders = orders.filter(payment__target_start_date=target_start_date)
    elif target_schedule_id is None:
        orders = orders.filter(payment__target_start_date__isnull=True)
    requested_discount_ids = set(dict.fromkeys(discount_ids or []))
    requested_debt_ids = set(dict.fromkeys(debt_ids or []))
    for order in orders:
        payment = order.payment
        if payment.seller_trainer_id != seller_trainer_id:
            continue
        if payment.package_owner_trainer_id != package_owner_trainer_id:
            continue
        if set(payment.applied_discounts.values_list("id", flat=True)) != requested_discount_ids:
            continue
        if set(
            payment.settled_debts.filter(resolved_at__isnull=True).values_list("id", flat=True)
        ) != requested_debt_ids:
            continue
        return order
    return None


def _sync_personal_reservation_disposition(*, order: BankPaymentOrder) -> None:
    if order.status not in {
        BankPaymentOrder.Status.MANUAL_REVIEW,
        BankPaymentOrder.Status.FAILED,
        BankPaymentOrder.Status.EXPIRED,
        BankPaymentOrder.Status.CANCELLED,
    }:
        return
    from apps.attendance.services import close_personal_booking_payment_reservation_for_order

    if order.status == BankPaymentOrder.Status.MANUAL_REVIEW:
        reservation_status = "manual_review"
    elif order.status == BankPaymentOrder.Status.EXPIRED:
        reservation_status = "expired"
    else:
        reservation_status = "cancelled"
    close_personal_booking_payment_reservation_for_order(
        club_id=order.club_id,
        order_id=order.id,
        status=reservation_status,
        reason=order.last_error_message,
        code=order.last_error_code,
    )

def _operation_id_conflicts(*, order: BankPaymentOrder, operation_id: str) -> bool:
    if not operation_id:
        return False
    return (
        BankPaymentOrder.objects.unscoped()
        .select_for_update(of=("self",))
        .filter(provider=order.provider, provider_operation_id=operation_id)
        .exclude(id=order.id)
        .exists()
    )

def build_payment_intent_key(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    amount: Decimal,
    currency: str,
    purpose_snapshot: str,
    debt_ids: list[int] | None,
    target_schedule_id: int | None,
    target_training_group_id: int | None,
    target_start_date: date | None,
    renewal_chain_id,
    personal_booking_reservation_id: int | None,
    personal_drop_in_booking_id: int | None,
) -> str:
    material = "|".join(
        [
            str(club_id),
            str(student_id),
            str(tariff_id),
            str(amount.quantize(Decimal("0.01"))),
            currency,
            purpose_snapshot,
            ",".join(str(value) for value in sorted(set(debt_ids or []))),
            str(target_schedule_id or ""),
            str(target_training_group_id or ""),
            target_start_date.isoformat() if target_start_date else "",
            str(renewal_chain_id or ""),
            str(personal_booking_reservation_id or ""),
            str(personal_drop_in_booking_id or ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

def apply_provider_creation_result(
    *,
    club_id: int,
    order_id: int,
    link,
    provider_payment_modes: list[str],
) -> BankPaymentOrder:
    """Apply a late provider response without regressing authenticated state."""

    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        # A complete personal order has attendance identity before its
        # financial family in the global D12 order.  Provider I/O returned
        # after the original command committed, so re-lock that origin before
        # touching its BankPaymentOrder rather than beginning with the order.
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order_queryset = BankPaymentOrder.objects.for_club(club_id).select_related("payment", "subscription")
        order = (
            order_queryset.get(id=order_id)
            if ordered_scope is not None
            else order_queryset.select_for_update(of=("self",)).get(id=order_id)
        )
        if order.status in {
            BankPaymentOrder.Status.APPROVED,
            BankPaymentOrder.Status.FAILED,
            BankPaymentOrder.Status.EXPIRED,
            BankPaymentOrder.Status.CANCELLED,
            BankPaymentOrder.Status.REFUNDED,
            BankPaymentOrder.Status.REFUNDED_PARTIALLY,
        }:
            return order
        if order.status == BankPaymentOrder.Status.MANUAL_REVIEW:
            return order
        error_code = ""
        if _operation_id_conflicts(order=order, operation_id=link.operation_id):
            error_code = "bank_payment_operation_already_bound"
        elif link.payment_link_id != order.provider_payment_link_id:
            error_code = "bank_payment_link_mismatch"
        elif order.provider_operation_id and link.operation_id and order.provider_operation_id != link.operation_id:
            error_code = "bank_payment_operation_mismatch"
        elif order.payment.status != "pending" or order.subscription.status != "pending":
            error_code = "bank_payment_creation_family_changed"
        elif order.link_creation_state not in {
            BankPaymentOrder.LinkCreationState.CLAIMED,
            BankPaymentOrder.LinkCreationState.DISPATCHED,
            BankPaymentOrder.LinkCreationState.UNKNOWN,
        }:
            error_code = "bank_payment_creation_claim_changed"
        if error_code:
            order.status = BankPaymentOrder.Status.MANUAL_REVIEW
            order.last_error_code = error_code
            order.last_error_message = "Поздний ответ банка требует защищённой сверки"
            order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
            _sync_personal_reservation_disposition(order=order)
            return order

        order.provider_payment_url = link.payment_url
        order.provider_payment_link_id = link.payment_link_id
        order.provider_operation_id = link.operation_id or order.provider_operation_id
        order.provider_status = link.provider_status
        order.provider_customer_code = link.customer_code
        order.provider_merchant_id = link.merchant_id
        order.provider_payment_modes = provider_payment_modes
        order.status = BankPaymentOrder.Status.PENDING
        order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
        order.last_error_code = ""
        order.last_error_message = ""
        order.full_clean()
        try:
            with transaction.atomic():
                order.save(
                    update_fields=[
                        "provider_payment_url",
                        "provider_payment_link_id",
                        "provider_operation_id",
                        "provider_status",
                        "provider_customer_code",
                        "provider_merchant_id",
                        "provider_payment_modes",
                        "status",
                        "link_creation_state",
                        "last_error_code",
                        "last_error_message",
                        "updated_at",
                    ]
                )
        except IntegrityError:
            order.refresh_from_db()
            order.status = BankPaymentOrder.Status.MANUAL_REVIEW
            order.last_error_code = "bank_payment_provider_identifier_conflict"
            order.last_error_message = "Идентификатор ответа банка уже принадлежит другой оплате"
            order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
            _sync_personal_reservation_disposition(order=order)
            return order
        if order.provider == BankPaymentOrder.Provider.TOCHKA:
            BankPaymentReconciliationAttempt.objects.for_club(club_id).get_or_create(
                order=order,
                defaults={
                    "club_id": club_id,
                    "status": BankPaymentReconciliationAttempt.Status.PENDING,
                    "retry_at": timezone.now(),
                },
            )
        return order

def record_provider_creation_manual_review(
    *,
    club_id: int,
    order_id: int,
    link,
    error_code: str,
) -> BankPaymentOrder:
    """Retain a dispatched provider result that cannot be auto-admitted."""

    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        # Keep the late manual-review branch on the same origin-first lock
        # plan as a successful provider response.  It can race cancellation
        # or expiry after the provider request was dispatched.
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order_queryset = BankPaymentOrder.objects.for_club(club_id)
        order = (
            order_queryset.get(id=order_id)
            if ordered_scope is not None
            else order_queryset.select_for_update(of=("self",)).get(id=order_id)
        )
        operation_conflict = _operation_id_conflicts(order=order, operation_id=link.operation_id)
        order.provider_payment_url = link.payment_url
        if not operation_conflict:
            order.provider_operation_id = link.operation_id or order.provider_operation_id
        order.provider_status = link.provider_status
        order.provider_customer_code = link.customer_code or order.provider_customer_code
        order.provider_merchant_id = link.merchant_id or order.provider_merchant_id
        order.provider_payment_modes = list(link.payment_modes or [])
        order.status = BankPaymentOrder.Status.MANUAL_REVIEW
        order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
        order.last_error_code = (
            "bank_payment_operation_already_bound"
            if operation_conflict
            else error_code[:120]
        )
        order.last_error_message = "Ответ банка требует защищённой сверки"
        order.save(
            update_fields=[
                "provider_payment_url",
                "provider_operation_id",
                "provider_status",
                "provider_customer_code",
                "provider_merchant_id",
                "provider_payment_modes",
                "status",
                "link_creation_state",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        _sync_personal_reservation_disposition(order=order)
        if order.provider_operation_id:
            BankPaymentReconciliationAttempt.objects.for_club(club_id).get_or_create(
                order=order,
                defaults={
                    "club_id": club_id,
                    "status": BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW,
                    "last_error_code": error_code[:120],
                },
            )
        return order

def mark_provider_dispatch(order: BankPaymentOrder) -> None:
    order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
    order.link_creation_dispatched_at = timezone.now()
    order.save(update_fields=["link_creation_state", "link_creation_dispatched_at", "updated_at"])

def mark_provider_creation_unknown(order: BankPaymentOrder, *, error_code: str) -> None:
    """Mark only a still-unresolved dispatch; never regress webhook evidence."""

    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=order.club_id)
        current = (
            BankPaymentOrder.objects.for_club(order.club_id)
            .select_for_update(of=("self",))
            .select_related("payment", "subscription")
            .get(id=order.id)
        )
        if (
            current.status not in _RECOVERABLE_ORDER_STATUSES
            or current.provider_operation_id
            or current.payment.status != "pending"
            or current.subscription.status != "pending"
        ):
            return
        current.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
        current.last_error_code = error_code[:120]
        current.last_error_message = "Статус создания ссылки требует сверки с банком"
        current.save(
            update_fields=["link_creation_state", "last_error_code", "last_error_message", "updated_at"]
        )

def provider_dispatch_blocks_cancellation(order: BankPaymentOrder) -> bool:
    return order.provider == BankPaymentOrder.Provider.TOCHKA and order.link_creation_state in {
        BankPaymentOrder.LinkCreationState.CLAIMED,
        BankPaymentOrder.LinkCreationState.DISPATCHED,
        BankPaymentOrder.LinkCreationState.UNKNOWN,
    }

def _is_recoverable_lost_creation(order: BankPaymentOrder) -> bool:
    return bool(
        order.provider == BankPaymentOrder.Provider.TOCHKA
        and order.status in _RECOVERABLE_ORDER_STATUSES
        and order.link_creation_state in {
            BankPaymentOrder.LinkCreationState.UNKNOWN,
            BankPaymentOrder.LinkCreationState.DISPATCHED,
        }
        and not order.provider_operation_id
        and order.provider_payment_link_id
        and order.link_creation_dispatched_at
    )

def _release_recovery_claim(*, club_id: int, order_id: int, claim_token: str, error_code: str = "") -> None:
    """Release only our lease, leaving another worker's claim untouched."""
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update().get(id=order_id)
        )
        if order.creation_recovery_claim_token != claim_token or not _is_recoverable_lost_creation(order):
            return
        order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
        order.creation_recovery_claim_token = ""
        order.creation_recovery_claimed_at = None
        if error_code:
            order.last_error_code = error_code
        order.save(
            update_fields=[
                "link_creation_state",
                "creation_recovery_claim_token",
                "creation_recovery_claimed_at",
                "last_error_code",
                "updated_at",
            ]
        )

def _record_creation_recovery_failure(
    *,
    club_id: int,
    order_id: int,
    claim_token: str,
    error_code: str,
) -> str:
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update().get(id=order_id)
        )
        if order.creation_recovery_claim_token != claim_token or not _is_recoverable_lost_creation(order):
            return "stale"
        order.creation_recovery_failure_count += 1
        order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
        order.creation_recovery_claim_token = ""
        order.creation_recovery_claimed_at = None
        if order.creation_recovery_failure_count >= _max_creation_recovery_failures():
            order.status = BankPaymentOrder.Status.MANUAL_REVIEW
            order.creation_recovery_retry_at = None
            order.last_error_code = "tochka_creation_recovery_exhausted"
            order.last_error_message = "Требуется подтверждённая сверка создания ссылки с банком"
            attempt = (
                BankPaymentReconciliationAttempt.objects.for_club(club_id)
                .select_for_update()
                .filter(order=order)
                .first()
            )
            if attempt is not None:
                attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
                attempt.retry_at = None
                attempt.lease_token = ""
                attempt.lease_expires_at = None
                attempt.last_error_code = "tochka_creation_recovery_exhausted"
                attempt.save(
                    update_fields=[
                        "status",
                        "retry_at",
                        "lease_token",
                        "lease_expires_at",
                        "last_error_code",
                        "updated_at",
                    ]
                )
            outcome = "manual_review"
        else:
            delay_seconds = min(3600, 30 * (2 ** min(order.creation_recovery_failure_count, 6)))
            order.creation_recovery_retry_at = timezone.now() + timedelta(seconds=delay_seconds)
            order.last_error_code = error_code[:120]
            outcome = "retry"
        order.save(
            update_fields=[
                "status",
                "link_creation_state",
                "creation_recovery_claim_token",
                "creation_recovery_claimed_at",
                "creation_recovery_failure_count",
                "creation_recovery_retry_at",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        if outcome == "manual_review":
            _sync_personal_reservation_disposition(order=order)
        return outcome

def _max_creation_recovery_failures() -> int:
    return max(1, min(int(getattr(settings, "TOCHKA_RECONCILIATION_MAX_ATTEMPTS", 8)), 20))

def _recovered_operation_error(*, order: BankPaymentOrder, operation) -> str:
    """Validate the list item before it can affect local financial state."""
    try:
        validate_sbp_only_payment_modes(operation.payment_modes)
    except BusinessLogicError:
        return "tochka_recovery_payment_mode_mismatch"
    if not operation.operation_id:
        return "tochka_recovery_operation_missing"
    if _operation_id_conflicts(order=order, operation_id=operation.operation_id):
        return "bank_payment_operation_already_bound"
    if operation.payment_link_id != order.provider_payment_link_id:
        return "bank_payment_link_mismatch"
    if operation.amount != order.amount_snapshot:
        return "bank_payment_amount_mismatch"
    if not operation.customer_code or operation.customer_code != order.provider_customer_code:
        return "bank_payment_customer_mismatch"
    if not operation.merchant_id or operation.merchant_id != order.provider_merchant_id:
        return "bank_payment_merchant_mismatch"
    return ""

def _provider_recovery_unavailable_outcome() -> str:
    if not settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED:
        return "disabled"
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    if not get_online_payment_capability().reconciliation_available:
        return "not_ready"
    return ""

def recover_unknown_bank_payment_order(*, club_id: int, order_id: int) -> str:
    """Recover a lost Tochka create response without issuing a replacement.

    The claim is committed before the bounded list call.  It is deliberately a
    separate transaction: provider I/O is never performed while holding a
    database lock, while a stale worker can still be reclaimed after the lease.
    """
    unavailable_outcome = _provider_recovery_unavailable_outcome()
    if unavailable_outcome:
        return unavailable_outcome
    now = timezone.now()
    claim_token = secrets.token_urlsafe(32)
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update().get(id=order_id)
        )
        if order.provider != BankPaymentOrder.Provider.TOCHKA:
            return "not_tochka"
        if not _is_recoverable_lost_creation(order):
            return "not_recoverable"
        if order.creation_recovery_retry_at is not None and order.creation_recovery_retry_at > now:
            return "cooldown"
        if (
            order.creation_recovery_claim_token
            and order.creation_recovery_claimed_at
            and now - order.creation_recovery_claimed_at < timedelta(seconds=_RECOVERY_LEASE_SECONDS)
        ):
            return "in_progress"
        order.creation_recovery_claim_token = claim_token
        order.creation_recovery_claimed_at = now
        order.save(update_fields=["creation_recovery_claim_token", "creation_recovery_claimed_at", "updated_at"])

    unavailable_outcome = _provider_recovery_unavailable_outcome()
    if unavailable_outcome:
        _release_recovery_claim(
            club_id=club_id,
            order_id=order_id,
            claim_token=claim_token,
        )
        return unavailable_outcome

    try:
        from apps.billing.payment_providers import get_payment_provider

        match = get_payment_provider(BankPaymentOrder.Provider.TOCHKA).find_payment_operation_by_link(order=order)
    except BusinessLogicError as exc:
        return _record_creation_recovery_failure(
            club_id=club_id,
            order_id=order_id,
            claim_token=claim_token,
            error_code=exc.code,
        )
    except Exception:
        return _record_creation_recovery_failure(
            club_id=club_id,
            order_id=order_id,
            claim_token=claim_token,
            error_code="tochka_recovery_unexpected_error",
        )

    now = timezone.now()
    with transaction.atomic():
        # Keep the existing cancellation lock order.  Expiry may close pending
        # membership/debt roots below, so its group scope lock must precede the
        # bank-order row lock.
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order = (
            BankPaymentOrder.objects.for_club(club_id).get(id=order_id)
            if ordered_scope is not None
            else BankPaymentOrder.objects.for_club(club_id).select_for_update().get(id=order_id)
        )
        # Revalidate after I/O: a webhook/manual action or another worker may
        # have changed the family while this worker was awaiting the provider.
        if (
            order.creation_recovery_claim_token != claim_token
            or not _is_recoverable_lost_creation(order)
        ):
            return "not_recoverable"
        unavailable_outcome = _provider_recovery_unavailable_outcome()
        if unavailable_outcome:
            order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
            order.creation_recovery_claim_token = ""
            order.creation_recovery_claimed_at = None
            order.save(
                update_fields=[
                    "link_creation_state",
                    "creation_recovery_claim_token",
                    "creation_recovery_claimed_at",
                    "updated_at",
                ]
            )
            return unavailable_outcome
        if match is not None:
            match_error = _recovered_operation_error(order=order, operation=match)
            if match_error:
                # Authenticated but conflicting evidence is permanent until an
                # operator resolves it. Never poll forever or release the
                # financial family for a replacement bearer link.
                order.status = BankPaymentOrder.Status.MANUAL_REVIEW
                order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
                order.creation_recovery_claim_token = ""
                order.creation_recovery_claimed_at = None
                order.creation_recovery_retry_at = None
                order.last_error_code = match_error
                order.save(
                    update_fields=[
                        "status",
                        "link_creation_state",
                        "creation_recovery_claim_token",
                        "creation_recovery_claimed_at",
                        "creation_recovery_retry_at",
                        "last_error_code",
                        "updated_at",
                    ]
                )
                attempt = (
                    BankPaymentReconciliationAttempt.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(order=order)
                    .first()
                )
                if attempt is not None:
                    attempt.status = BankPaymentReconciliationAttempt.Status.MANUAL_REVIEW
                    attempt.retry_at = None
                    attempt.lease_token = ""
                    attempt.lease_expires_at = None
                    attempt.last_error_code = match_error
                    attempt.save(
                        update_fields=[
                            "status",
                            "retry_at",
                            "lease_token",
                            "lease_expires_at",
                            "last_error_code",
                            "updated_at",
                        ]
                    )
                _sync_personal_reservation_disposition(order=order)
                return "manual_review"
        if order.creation_recovery_failure_count or order.creation_recovery_retry_at is not None:
            order.creation_recovery_failure_count = 0
            order.creation_recovery_retry_at = None
            order.save(
                update_fields=[
                    "creation_recovery_failure_count",
                    "creation_recovery_retry_at",
                    "updated_at",
                ]
            )
        if match is None:
            cutoff = order.expires_at + timedelta(hours=24)
            if now < cutoff:
                order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
                order.last_error_code = "provider_creation_unknown"
                order.creation_recovery_claim_token = ""
                order.creation_recovery_claimed_at = None
                order.creation_recovery_retry_at = now + timedelta(seconds=60)
                order.save(
                    update_fields=[
                        "link_creation_state",
                        "last_error_code",
                        "creation_recovery_claim_token",
                        "creation_recovery_claimed_at",
                        "creation_recovery_retry_at",
                        "updated_at",
                    ]
                )
                return "unknown"
            if (
                order.creation_last_absence_at
                and order.creation_last_absence_at >= cutoff
                and now - order.creation_last_absence_at >= timedelta(seconds=60)
            ):
                order.creation_absence_count += 1
            else:
                order.creation_absence_count = 1
            order.creation_last_absence_at = now
            order.creation_recovery_claim_token = ""
            order.creation_recovery_claimed_at = None
            if order.creation_absence_count >= 2:
                order.status = BankPaymentOrder.Status.EXPIRED
                # Do not reopen the creation state: expiry is authoritative
                # after two distinct bounded scans and must never dispatch a
                # replacement from this recovery path.
                order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
            order.save(
                update_fields=[
                    "creation_absence_count",
                    "creation_last_absence_at",
                    "creation_recovery_claim_token",
                    "creation_recovery_claimed_at",
                    "status",
                    "link_creation_state",
                    "updated_at",
                ]
            )
            if order.status == BankPaymentOrder.Status.EXPIRED:
                # This nested helper joins the current outer transaction, so
                # expiry and every pending financial root commit or roll back
                # together.
                _cancel_pending_bank_order_artifacts(
                    order=order,
                    reason="tochka_creation_authoritatively_absent",
                )
                return "expired"
            return "unknown"
        elif match.status.strip().lower() == "expired":
            order.status = BankPaymentOrder.Status.EXPIRED
            order.link_creation_state = BankPaymentOrder.LinkCreationState.UNKNOWN
            order.creation_recovery_claim_token = ""
            order.creation_recovery_claimed_at = None
            order.save(
                update_fields=[
                    "status",
                    "link_creation_state",
                    "creation_recovery_claim_token",
                    "creation_recovery_claimed_at",
                    "updated_at",
                ]
            )
            _cancel_pending_bank_order_artifacts(order=order, reason="tochka_creation_provider_expired")
            return "expired"
        else:
            order.provider_operation_id = match.operation_id
            order.provider_payment_link_id = match.payment_link_id
            if match.payment_url:
                order.provider_payment_url = match.payment_url
            order.provider_status = match.status
            order.provider_customer_code = match.customer_code
            order.provider_merchant_id = match.merchant_id
            order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
            order.creation_recovery_claim_token = ""
            order.creation_recovery_claimed_at = None
            order.save(
                update_fields=[
                    "provider_operation_id",
                    "provider_payment_link_id",
                    "provider_payment_url",
                    "provider_status",
                    "provider_customer_code",
                    "provider_merchant_id",
                    "link_creation_state",
                    "creation_recovery_claim_token",
                    "creation_recovery_claimed_at",
                    "updated_at",
                ]
            )
    provider_events = import_module("apps.billing.service_modules.provider_events")
    provider_events.request_provider_reconciliation(
        club_id=club_id,
        order_id=order_id,
        provider_event_id=None,
    )
    return provider_events.reconcile_provider_payment_order(
        club_id=club_id,
        order_id=order_id,
    )

def create_bank_payment_order(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int | None,
    source: str,
    created_by_id: int,
    seller_trainer_id: int | None = None,
    package_owner_trainer_id: int | None = None,
    discount_ids: list[int] | None = None,
    debt_ids: list[int] | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date: date | None = None,
    buyer_email: str | None = None,
    buyer_phone: str | None = None,
    enforce_trainer_group_contract: bool = False,
    allow_new_self_service_subscription: bool = False,
    allow_reuse: bool = True,
    personal_booking_reservation_id: int | None = None,
    personal_drop_in_booking_id: int | None = None,
    expires_at_cap: datetime | None = None,
    command_idempotency_key: str | None = None,
    command_fingerprint: str | None = None,
    renewed_from_subscription_id: int | None = None,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
    reject_reusable_order_for_distinct_key: bool = False,
    _locked_pre_reuse_validator=None,
    _locked_pre_create_validator=None,
) -> BankPaymentOrder:
    from apps.billing.payment_providers import get_payment_provider

    command_key = (command_idempotency_key or "").strip()
    if command_key and (len(command_key) > 120 or (command_fingerprint is not None and len(command_fingerprint) != 64)):
        raise BusinessLogicError("Некорректная идентичность команды", code="invalid_command_identity")

    if source not in BankPaymentOrder.Source.values:
        raise BusinessLogicError("Некорректный источник оплаты", code="invalid_payment_source")
    # A keyed retry is an immutable bank-order receipt.  It must work even
    # after the original catalog, source, provider or target has changed, and
    # before any provider readiness check can issue a second dispatch.
    if command_key:
        with transaction.atomic():
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
            existing_command_payment = (
                Payment.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("tariff", "subscription", "subscription__renewed_from")
                .filter(command_idempotency_key=command_key)
                .first()
            )
            if existing_command_payment is not None:
                from apps.billing.service_modules.renewals import build_subscription_command_fingerprint

                stored_source_subscription_id = getattr(
                    existing_command_payment.subscription,
                    "renewed_from_id",
                    None,
                )
                stored_source_tariff_id = getattr(
                    getattr(existing_command_payment.subscription, "renewed_from", None),
                    "tariff_id",
                    existing_command_payment.tariff_id,
                )
                stored_is_revision = (
                    stored_source_subscription_id is not None
                    and stored_source_tariff_id != existing_command_payment.tariff_id
                )
                replay_source_subscription_id = (
                    renewed_from_subscription_id
                    if renewed_from_subscription_id is not None
                    else stored_source_subscription_id
                )
                replay_tariff_id = (
                    (
                        expected_target_tariff_id
                        or existing_command_payment.tariff_id
                    )
                    if stored_is_revision
                    else (tariff_id or existing_command_payment.tariff_id)
                )
                replay_fingerprint = command_fingerprint or build_subscription_command_fingerprint(
                    student_id=student_id,
                    tariff_id=replay_tariff_id,
                    payment_method=Payment.Method.ONLINE,
                    discount_ids=discount_ids,
                    debt_ids=debt_ids,
                    target_schedule_id=target_schedule_id,
                    target_training_group_id=target_training_group_id,
                    target_start_date=target_start_date,
                    renewed_from_subscription_id=replay_source_subscription_id,
                    seller_trainer_id=(
                        None if target_schedule_id is not None else seller_trainer_id
                    ),
                    package_owner_trainer_id=package_owner_trainer_id,
                    expected_target_tariff_id=(
                        (
                            expected_target_tariff_id
                            if expected_target_tariff_id is not None
                            else existing_command_payment.tariff_id
                        )
                        if stored_is_revision
                        else None
                    ),
                    expected_target_price=(
                        (
                            expected_target_price
                            if expected_target_price is not None
                            else existing_command_payment.tariff.price
                        )
                        if stored_is_revision
                        else None
                    ),
                )
                if existing_command_payment.command_fingerprint != replay_fingerprint:
                    raise BusinessLogicError(
                        "Idempotency key was already used for another command",
                        code="idempotency_conflict",
                    )
                replay_order = (
                    BankPaymentOrder.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(payment_id=existing_command_payment.id)
                    .order_by("-id")
                    .first()
                )
                if replay_order is None:
                    raise BusinessLogicError(
                        "Команда оплаты требует ручной проверки",
                        code="command_replay_order_missing",
                    )
                replay_order._command_replayed = True
                return replay_order

    # A legacy retry without a key still has a durable ordinary order identity.
    # Resolve that receipt before inferring a historical renewal source; a
    # pending ordinary child must never be reinterpreted as a closed source.
    if (
        not command_key
        and renewed_from_subscription_id is None
        and personal_booking_reservation_id is None
        and personal_drop_in_booking_id is None
        and tariff_id is not None
        and not Tariff.objects.for_club(club_id).filter(id=tariff_id, is_active=True).exists()
    ):
        with transaction.atomic():
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
            existing_ordinary_order = _find_existing_ordinary_bank_order(
                club_id=club_id,
                student_id=student_id,
                tariff_id=tariff_id,
                source=source,
                discount_ids=discount_ids,
                debt_ids=debt_ids,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
                target_schedule_id=target_schedule_id,
                target_training_group_id=target_training_group_id,
                target_start_date=target_start_date,
            )
            if existing_ordinary_order is not None:
                existing_ordinary_order._command_replayed = True
                return existing_ordinary_order

    # Student/parent clients from the pre-context contract submit the source
    # tariff plus the displayed target offer, but cannot submit the exact
    # source subscription id. Infer that source only for a retired tariff,
    # before provider readiness or mutable catalog lookup. Active tariffs keep
    # the historical ordinary-sale path below; the endpoint-level contextual
    # gate remains intact.
    legacy_source_preview = None
    if (
        renewed_from_subscription_id is None
        and personal_booking_reservation_id is None
        and personal_drop_in_booking_id is None
        and tariff_id is not None
        and source in BankPaymentOrder.Source.values
        and not Tariff.objects.for_club(club_id).filter(id=tariff_id, is_active=True).exists()
    ):
        from apps.clubs.capabilities import is_unified_client_journey_enabled

        if not is_unified_client_journey_enabled(club=club_id):
            legacy_source_preview = _legacy_renewal_source_preview(
                club_id=club_id,
                student_id=student_id,
                tariff_id=tariff_id,
                source=source,
            )
            if legacy_source_preview is not None:
                renewed_from_subscription_id = legacy_source_preview.id
    if renewed_from_subscription_id is not None:
        from apps.billing.service_modules.renewals import _normalise_expected_renewal_offer

        expected_target_tariff_id, expected_target_price = _normalise_expected_renewal_offer(
            expected_target_tariff_id=expected_target_tariff_id,
            expected_target_price=expected_target_price,
        )

    renewed_from_subscription = None
    renewal_chain_id = None
    renewal_source_preview = legacy_source_preview
    if renewed_from_subscription_id is not None:
        if personal_booking_reservation_id is not None or personal_drop_in_booking_id is not None:
            raise BusinessLogicError(
                "Продление нельзя привязать к персональной записи",
                code="renewal_source_not_allowed",
            )
        if debt_ids:
            raise BusinessLogicError(
                "Продление не может принять произвольный долг",
                code="renewal_debt_not_allowed",
            )
        renewal_source_preview = (
            Subscription.objects.for_club(club_id)
            .select_related("tariff")
            .filter(
                id=renewed_from_subscription_id,
                student_id=student_id,
                deleted_at__isnull=True,
            )
            .first()
        )
        if renewal_source_preview is None:
            raise BusinessLogicError("Абонемент для продления не найден", code="renewal_source_not_found")
        if tariff_id is not None and tariff_id != renewal_source_preview.tariff_id:
            raise BusinessLogicError("Тариф продления не совпадает с источником", code="renewal_tariff_mismatch")
        tariff_id = renewal_source_preview.tariff_id

        # An accepted online renewal is an immutable family receipt. Return it
        # before provider readiness or mutable current-offer checks, including
        # an unkeyed retry after the source has advanced to a newer leaf.
        if not command_key:
            with transaction.atomic():
                existing_family_order = _find_existing_renewal_bank_order_family(
                    club_id=club_id,
                    source_subscription_id=renewal_source_preview.id,
                    source=source,
                    discount_ids=discount_ids,
                    target_schedule_id=target_schedule_id,
                    target_training_group_id=target_training_group_id,
                    target_start_date=target_start_date,
                )
                if existing_family_order is not None:
                    existing_family_order._command_replayed = True
                    return existing_family_order

    if not settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED:
        raise BusinessLogicError(
            "Создание онлайн-оплаты временно отключено",
            code="online_payment_order_creation_disabled",
        )
    if source not in BankPaymentOrder.Source.values:
        raise BusinessLogicError("Некорректный источник оплаты", code="invalid_payment_source")

    provider_name = settings.PAYMENT_PROVIDER
    if provider_name == BankPaymentOrder.Provider.MOCK and (
        not settings.DEBUG or not settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
    ):
        raise BusinessLogicError(
            "Mock-провайдер не может создавать платёжные ссылки в этом окружении",
            code="mock_payment_order_creation_disabled",
        )
    if provider_name == BankPaymentOrder.Provider.TOCHKA:
        from apps.billing.payment_providers.base import online_payments_enabled

        if not online_payments_enabled():
            raise BusinessLogicError(
                "Онлайн-оплата Точки пока не готова к созданию ссылок",
                code="tochka_payment_creation_not_ready",
            )
    provider = get_payment_provider(provider_name)
    now = timezone.now()
    ttl_minutes = _payment_order_ttl_minutes(source)
    if expires_at_cap is not None:
        if timezone.is_naive(expires_at_cap):
            expires_at_cap = timezone.make_aware(expires_at_cap, timezone.get_current_timezone())
        if expires_at_cap <= now:
            raise BusinessLogicError(
                "Срок этой оплаты уже истёк",
                code="bank_payment_order_expiry_elapsed",
            )
        if (
            provider_name == BankPaymentOrder.Provider.TOCHKA
            and expires_at_cap < now + timedelta(minutes=2)
        ):
            raise BusinessLogicError(
                "До окончания записи недостаточно времени для безопасной ссылки СБП",
                code="bank_payment_order_ttl_too_short",
            )
    order_expires_at = min(
        now + timedelta(minutes=ttl_minutes),
        expires_at_cap or now + timedelta(minutes=ttl_minutes),
    )
    receipt_mode = _receipt_mode()
    if (
        provider_name == BankPaymentOrder.Provider.TOCHKA
        and receipt_mode == BankPaymentOrder.ReceiptMode.TOCHKA_RECEIPT
        and not (buyer_email or "").strip()
    ):
        raise BusinessLogicError(
            "Для фискального чека нужен email покупателя",
            code="receipt_buyer_email_required",
        )
    if tariff_id is None:
        raise BusinessLogicError("Укажите источник продления", code="renewal_source_required")

    # This is only a preflight read that determines whether the leading
    # payroll/rollout scope is required.  The locked reuse path revalidates the
    # exact group association after it has locked its financial candidate.
    preflight_training_group_id = None
    if target_schedule_id is not None:
        from apps.attendance.models import Schedule

        preflight_training_group_id = (
            Schedule.objects.for_club(club_id)
            .filter(id=target_schedule_id)
            .values_list("training_group_id", flat=True)
            .first()
        )
    with transaction.atomic():
        # Command arbitration precedes all student/subscription locks.  This
        # serializes an identical key even when two callers name different
        # students, avoiding a unique-key IntegrityError after child creation.
        if command_idempotency_key or target_schedule_id is not None or target_training_group_id is not None:
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
        # Recheck K1 after acquiring the club arbitration lock.  It may have
        # committed after the optimistic replay lookup above; accepted replay
        # must precede every mutable protocol gate.
        if command_key:
            from apps.billing.service_modules.renewals import build_subscription_command_fingerprint

            existing_command_payment = (
                Payment.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("tariff", "subscription", "subscription__renewed_from")
                .filter(command_idempotency_key=command_key)
                .first()
            )
            if existing_command_payment is not None:
                stored_source_subscription_id = getattr(
                    existing_command_payment.subscription,
                    "renewed_from_id",
                    None,
                )
                stored_source_tariff_id = getattr(
                    getattr(existing_command_payment.subscription, "renewed_from", None),
                    "tariff_id",
                    existing_command_payment.tariff_id,
                )
                stored_is_revision = (
                    stored_source_subscription_id is not None
                    and stored_source_tariff_id != existing_command_payment.tariff_id
                )
                replay_source_subscription_id = (
                    renewed_from_subscription_id
                    if renewed_from_subscription_id is not None
                    else stored_source_subscription_id
                )
                locked_replay_fingerprint = command_fingerprint or build_subscription_command_fingerprint(
                    student_id=student_id,
                    tariff_id=(
                        expected_target_tariff_id or existing_command_payment.tariff_id
                        if stored_is_revision
                        else existing_command_payment.tariff_id
                    ),
                    payment_method=Payment.Method.ONLINE,
                    discount_ids=discount_ids,
                    debt_ids=debt_ids,
                    target_schedule_id=target_schedule_id,
                    target_training_group_id=target_training_group_id,
                    target_start_date=target_start_date,
                    renewed_from_subscription_id=replay_source_subscription_id,
                    seller_trainer_id=(
                        None if target_schedule_id is not None else seller_trainer_id
                    ),
                    package_owner_trainer_id=package_owner_trainer_id,
                    expected_target_tariff_id=(
                        (
                            expected_target_tariff_id
                            if expected_target_tariff_id is not None
                            else existing_command_payment.tariff_id
                        )
                        if stored_is_revision
                        else None
                    ),
                    expected_target_price=(
                        (
                            expected_target_price
                            if expected_target_price is not None
                            else existing_command_payment.tariff.price
                        )
                        if stored_is_revision
                        else None
                    ),
                )
                if existing_command_payment.command_fingerprint != locked_replay_fingerprint:
                    raise BusinessLogicError(
                        "Idempotency key was already used for another command",
                        code="idempotency_conflict",
                    )
                replay_order = (
                    BankPaymentOrder.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(payment_id=existing_command_payment.id)
                    .order_by("-id")
                    .first()
                )
                if replay_order is None:
                    raise BusinessLogicError(
                        "Команда оплаты требует ручной проверки",
                        code="command_replay_order_missing",
                    )
                replay_order._command_replayed = True
                return replay_order
        if renewal_source_preview is not None:
            from apps.billing.service_modules.renewals import _lock_renewal_catalog_scope

            _lock_renewal_catalog_scope(
                club_id=club_id,
                source_tariff=renewal_source_preview.tariff,
            )
        # Distinct commands arbitrate protocol immediately after Club and
        # before any catalog, rollout, student, reservation, or finance lock.
        if _locked_pre_reuse_validator is not None:
            _locked_pre_reuse_validator()
        # A complete accepted personal offer is settlement authority.  The
        # current catalog remains the authority for legacy and generic orders.
        # The attendance owner supplies the D12 lock boundary before this
        # command can inspect an existing payment/order family or lock Student.
        if personal_booking_reservation_id is not None or personal_drop_in_booking_id is not None:
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes

            ordered_personal_scope = lock_complete_personal_scopes(
                club_id=club_id,
                booking_ids=(
                    [personal_drop_in_booking_id]
                    if personal_drop_in_booking_id is not None
                    else []
                ),
                reservation_ids=(
                    [personal_booking_reservation_id]
                    if personal_booking_reservation_id is not None
                    else []
                ),
            )
        from apps.attendance.models import is_complete_personal_terms

        personal_terms = None
        if personal_booking_reservation_id is not None or personal_drop_in_booking_id is not None:
            from apps.attendance.models import PersonalServiceTermsSnapshot

            personal_terms = (
                PersonalServiceTermsSnapshot.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(
                    reservation_id=personal_booking_reservation_id,
                    booking_id=personal_drop_in_booking_id,
                )
                .first()
            )
        uses_personal_terms = is_complete_personal_terms(personal_terms)
        if uses_personal_terms:
            if personal_terms.tariff_id_snapshot != tariff_id:
                raise BusinessLogicError(
                    "Personal payment terms do not match the requested tariff",
                    code="personal_terms_tariff_mismatch",
                )
            # Complete terms freeze the only catalog fields this order needs
            # for its safe human receipt.  Do not lock or read a mutable tariff
            # after the ordered trainer/reservation scope has been acquired.
            tariff = Tariff(id=tariff_id, name=personal_terms.tariff_name_snapshot)
            if personal_drop_in_booking_id is not None:
                from apps.attendance.models import PersonalDropInBooking

                booking = ordered_personal_scope.bookings_by_id.get(personal_drop_in_booking_id)
                if booking is None:
                    raise BusinessLogicError(
                        "Personal booking was not found",
                        code="personal_drop_in_booking_not_found",
                    )
                if booking.state in {
                    PersonalDropInBooking.State.CANCELLED,
                    PersonalDropInBooking.State.NO_SHOW,
                }:
                    raise BusinessLogicError(
                        "Personal booking is no longer payable",
                        code="personal_drop_in_payment_not_actionable",
                    )
                exact_debt_ids: list[int] = []
                if booking.debt_id is not None:
                    debt = (
                        Debt.objects.for_club(club_id)
                        .select_for_update(of=("self",))
                        .filter(id=booking.debt_id, resolved_at__isnull=True)
                        .first()
                    )
                    if debt is not None:
                        exact_debt_ids = [debt.id]
                if debt_ids and set(debt_ids) != set(exact_debt_ids):
                    raise BusinessLogicError(
                        "Personal booking debt changed before payment creation",
                        code="personal_drop_in_debt_changed",
                    )
                debt_ids = exact_debt_ids
        else:
            if tariff_id is None:
                raise BusinessLogicError("Укажите источник продления", code="renewal_source_required")
            if renewal_source_preview is not None:
                # The source may already be archived.  The current target is
                # resolved after the exact source lock below.
                tariff = renewal_source_preview.tariff
            else:
                tariff = Tariff.objects.for_club(club_id).select_related("training_type", "location").get(
                    id=tariff_id,
                    is_active=True,
                )
        # A group-tariff target can become mapped after the preflight read.
        # Take the leading scope for every such intent, not merely for an
        # already-mapped slot, so the later locked revalidation cannot acquire
        # payroll/rollout after financial or identity rows.
        locked_rollout_state = None
        if not uses_personal_terms and tariff.training_type.kind == TrainingType.Kind.GROUP and (
            target_schedule_id is not None or target_training_group_id is not None
        ):
            from apps.attendance.models import Schedule
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            locked_rollout_state = lock_training_group_mutation_scope(club_id=club_id)
            mapped_target_group_id = (
                Schedule.objects.for_club(club_id)
                .filter(id=target_schedule_id)
                .values_list("training_group_id", flat=True)
                .first()
                if target_schedule_id is not None
                else None
            )
            if target_training_group_id is not None or mapped_target_group_id is not None:
                from apps.attendance.services.training_group_memberships import (
                    assert_training_group_new_writes_enabled,
                )

                assert_training_group_new_writes_enabled()
        _assert_self_service_bank_order_allowed(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            source=source,
            discount_ids=discount_ids,
            debt_ids=debt_ids,
            allow_new_self_service_subscription=allow_new_self_service_subscription,
        )
        if (
            enforce_trainer_group_contract
            and tariff.training_type.kind == TrainingType.Kind.GROUP
            and (target_schedule_id is None or target_start_date is None)
        ):
            # Reject an incomplete trainer group request before a legacy
            # pending order is considered.  Full student/enrollment contract
            # validation follows the financial candidate locks below.
            raise BusinessLogicError(
                "Для групповой оплаты укажите группу и дату старта",
                code="target_schedule_required",
            )
        # Serialize every canonical decision for one student before locking an
        # existing order or creating a new financial family.
        student = Student.objects.for_club(club_id).select_for_update().get(id=student_id)
        if renewed_from_subscription_id is not None:
            from apps.billing.service_modules.renewals import (
                get_renewal_offer,
                lock_and_validate_exact_renewal_source,
                validate_expected_renewal_offer,
                validate_locked_renewal_source,
            )

            renewed_from_subscription = lock_and_validate_exact_renewal_source(
                club_id=club_id,
                student_id=student.id,
                renewed_from_subscription_id=renewed_from_subscription_id,
                validate=False,
            )
            if (
                renewal_source_preview is not None
                and renewed_from_subscription.tariff_id != renewal_source_preview.tariff_id
            ):
                raise BusinessLogicError(
                    "Тариф продления изменился",
                    code="renewal_tariff_mismatch",
                )
            # Validate immutable group terms before family replay arbitration.
            # A request that changes the selected group is a new invalid
            # command, even when an older renewal family already exists.  This
            # check uses the source contract and does not resolve mutable
            # current pricing.
            if target_training_group_id is not None:
                if target_schedule_id is None or target_start_date is None:
                    raise BusinessLogicError(
                        "Групповое продление требует точную группу, слот и дату старта",
                        code="renewal_group_target_required",
                    )
                _assert_locked_exact_group_renewal_target(
                    club_id=club_id,
                    student=student,
                    tariff=tariff,
                    target_training_group_id=target_training_group_id,
                    target_schedule_id=target_schedule_id,
                    target_start_date=target_start_date,
                    rollout_state=locked_rollout_state,
                )
            existing_family_order = _find_existing_renewal_bank_order_family(
                club_id=club_id,
                source_subscription_id=renewed_from_subscription.id,
                source=source,
                discount_ids=discount_ids,
                target_schedule_id=target_schedule_id,
                target_training_group_id=target_training_group_id,
                target_start_date=target_start_date,
            )
            if existing_family_order is not None:
                if command_key and existing_family_order.payment.command_idempotency_key != command_key:
                    raise BusinessLogicError(
                        "У этого абонемента уже есть ожидающее или подтверждённое продление",
                        code="renewal_source_finalized_successor",
                    )
                if command_key:
                    existing_family_order._command_replayed = True
                return existing_family_order
            offer = get_renewal_offer(
                club_id=club_id,
                source_tariff=renewed_from_subscription.tariff,
                lock=True,
            )
            validate_expected_renewal_offer(
                offer=offer,
                expected_target_tariff_id=expected_target_tariff_id,
                expected_target_price=expected_target_price,
            )
            if not offer.target_tariff.is_active:
                raise BusinessLogicError(
                    "Тариф продления больше недоступен",
                    code="renewal_offer_stale",
                )
            tariff_id = offer.target_tariff_id
            tariff = offer.target_tariff
            validate_locked_renewal_source(source=renewed_from_subscription)
            renewal_chain_id = renewed_from_subscription.renewal_chain_id or uuid4()

        if renewed_from_subscription is not None and target_training_group_id is not None:
            if (
                target_schedule_id is None
                or target_start_date is None
            ):
                raise BusinessLogicError(
                    "Групповое продление требует точную группу, слот и дату старта",
                    code="renewal_group_target_required",
                )
            _assert_locked_exact_group_renewal_target(
                club_id=club_id,
                student=student,
                tariff=tariff,
                target_training_group_id=target_training_group_id,
                target_schedule_id=target_schedule_id,
                target_start_date=target_start_date,
                rollout_state=locked_rollout_state,
            )

        _assert_no_live_non_sbp_bank_payment_order(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            now=now,
        )
        if allow_reuse:
            reusable_order = _find_reusable_bank_payment_order(
                club_id=club_id,
                student_id=student_id,
                tariff_id=tariff_id,
                source=source,
                student=student,
                discount_ids=discount_ids,
                debt_ids=debt_ids,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
                target_schedule_id=target_schedule_id,
                target_training_group_id=target_training_group_id,
                target_start_date=target_start_date,
                personal_booking_reservation_id=personal_booking_reservation_id,
                personal_drop_in_booking_id=personal_drop_in_booking_id,
                now=now,
                tariff=tariff,
                rollout_state=locked_rollout_state,
                preflight_training_group_id=preflight_training_group_id,
                enforce_trainer_group_contract=enforce_trainer_group_contract,
            )
            if reusable_order is not None:
                if reject_reusable_order_for_distinct_key:
                    raise BusinessLogicError(
                        "Для этой оплаты уже существует активная ссылка с другим ключом команды",
                        code="bank_payment_order_live_command_conflict",
                    )
                return reusable_order

        # Exact-key replay and live-family arbitration own precedence over a
        # mutable offer revalidation. A strict distinct key must receive the
        # durable-family conflict even when accepting K1 changed the current
        # offer projection; a genuinely new family is validated while all
        # command, rollout and student locks are still held.
        if _locked_pre_create_validator is not None:
            _locked_pre_create_validator()

        # Booking-specific payments are settlement for their exact reservation
        # or drop-in booking, never a generic renewal.
        if (
            renewed_from_subscription is None
            and personal_booking_reservation_id is None
            and personal_drop_in_booking_id is None
        ):
            from apps.clubs.capabilities import is_unified_client_journey_enabled

            if not is_unified_client_journey_enabled(club=club_id):
                renewed_from_subscription = _active_renewed_from_subscription(
                    club_id=club_id,
                    student_id=student_id,
                    tariff_id=tariff_id,
                )
        if renewed_from_subscription is not None:
            renewal_chain_id = renewed_from_subscription.renewal_chain_id or uuid4()

        if enforce_trainer_group_contract:
            _validate_trainer_group_payment_contract(
                club_id=club_id,
                student=student,
                tariff=tariff,
                target_schedule_id=target_schedule_id,
                target_start_date=target_start_date,
                lock_enrollments=True,
            )

        if uses_personal_terms:
            from apps.billing.service_modules.payment_creation import (
                build_personal_command_fingerprint,
                create_personal_terms_payment,
            )

            payment = create_personal_terms_payment(
                club_id=club_id,
                booking_id=personal_drop_in_booking_id,
                reservation_id=personal_booking_reservation_id,
                payment_method=Payment.Method.ONLINE,
                recorded_by_id=created_by_id,
                command_idempotency_key=command_idempotency_key,
                command_fingerprint=build_personal_command_fingerprint(
                    student_id=student_id,
                    terms_id=personal_terms.id,
                    payment_method=Payment.Method.ONLINE,
                    booking_id=personal_drop_in_booking_id,
                    reservation_id=personal_booking_reservation_id,
                    debt_ids=debt_ids,
                ),
                debt_ids=debt_ids,
            )
        else:
            from apps.billing.service_modules.renewals import build_subscription_command_fingerprint

            resolved_command_fingerprint = command_fingerprint
            if command_idempotency_key:
                is_revision = (
                    renewed_from_subscription is not None
                    and renewed_from_subscription.tariff_id != tariff_id
                )
                resolved_command_fingerprint = command_fingerprint or build_subscription_command_fingerprint(
                    student_id=student_id,
                    tariff_id=tariff_id,
                    payment_method=Payment.Method.ONLINE,
                    discount_ids=discount_ids,
                    debt_ids=debt_ids,
                    target_schedule_id=target_schedule_id,
                    target_training_group_id=target_training_group_id,
                    target_start_date=target_start_date,
                    renewed_from_subscription_id=(
                        renewed_from_subscription.id if renewed_from_subscription is not None else None
                    ),
                    seller_trainer_id=(
                        None if target_schedule_id is not None else seller_trainer_id
                    ),
                    package_owner_trainer_id=package_owner_trainer_id,
                    expected_target_tariff_id=(
                        expected_target_tariff_id if is_revision else None
                    ),
                    expected_target_price=(
                        expected_target_price if is_revision else None
                    ),
                )
            payment = create_payment(
                club_id=club_id,
                student_id=student_id,
                tariff_id=tariff_id,
                payment_method=Payment.Method.ONLINE,
                discount_ids=discount_ids,
                debt_ids=debt_ids,
                recorded_by_id=created_by_id,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
                target_schedule_id=target_schedule_id,
                target_training_group_id=target_training_group_id,
                target_start_date=target_start_date,
                enforce_trainer_group_contract=enforce_trainer_group_contract,
                allow_renewal=renewed_from_subscription is not None,
                renewed_from_subscription_id=(
                    renewed_from_subscription.id if renewed_from_subscription is not None else None
                ),
                renewal_source_tariff_id=(
                    renewed_from_subscription.tariff_id
                    if renewed_from_subscription is not None
                    else None
                ),
                expected_target_tariff_id=(
                    expected_target_tariff_id
                    if renewed_from_subscription is not None
                    else None
                ),
                expected_target_price=(
                    expected_target_price
                    if renewed_from_subscription is not None
                    else None
                ),
                renewal_chain_id=renewal_chain_id,
                personal_drop_in_booking_id=personal_drop_in_booking_id,
                command_idempotency_key=command_idempotency_key,
                command_fingerprint=resolved_command_fingerprint,
                _locked_rollout_state=locked_rollout_state,
            )
        if getattr(payment, "_command_replayed", False):
            replay_order = (
                BankPaymentOrder.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(payment_id=payment.id)
                .order_by("-id")
                .first()
            )
            if replay_order is not None:
                replay_order._command_replayed = True
                return replay_order
        subscription = payment.subscription
        if subscription is None:
            raise BusinessLogicError("Для онлайн-оплаты нужен абонемент", code="payment_subscription_required")

        purpose_snapshot = _payment_order_purpose(tariff=tariff, student=student)
        payment_intent_key = build_payment_intent_key(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            amount=payment.amount,
            currency="RUB",
            purpose_snapshot=purpose_snapshot,
            debt_ids=debt_ids,
            target_schedule_id=target_schedule_id,
            target_training_group_id=target_training_group_id,
            target_start_date=target_start_date,
            renewal_chain_id=renewal_chain_id,
            personal_booking_reservation_id=personal_booking_reservation_id,
            personal_drop_in_booking_id=personal_drop_in_booking_id,
        )
        order = BankPaymentOrder.objects.create(
            club_id=club_id,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=provider_name,
            source=source,
            status=BankPaymentOrder.Status.CREATED,
            amount_snapshot=payment.amount,
            currency="RUB",
            purpose_snapshot=purpose_snapshot,
            provider_payment_modes=list(SBP_ONLY_PAYMENT_MODES),
            # These configured identities are part of the local creation claim.
            # Persist them before provider I/O so a lost response can be
            # matched against authenticated list evidence without guessing.
            provider_customer_code=(
                str(settings.TOCHKA_CUSTOMER_CODE or "").strip()
                if provider_name == BankPaymentOrder.Provider.TOCHKA
                else ""
            ),
            provider_merchant_id=(
                str(settings.TOCHKA_MERCHANT_ID or "").strip()
                if provider_name == BankPaymentOrder.Provider.TOCHKA
                else ""
            ),
            expires_at=order_expires_at,
            receipt_mode=receipt_mode,
            buyer_email=(buyer_email or "").strip(),
            buyer_phone=(buyer_phone or "").strip(),
            receipt_status=_receipt_status_for_mode(receipt_mode),
            fiscal_item_snapshot=_fiscal_item_snapshot(tariff=tariff, amount=payment.amount),
            renewed_from_subscription=renewed_from_subscription,
            renewal_chain_id=renewal_chain_id,
            payment_intent_key=payment_intent_key,
            personal_booking_reservation_id_snapshot=personal_booking_reservation_id,
            personal_drop_in_booking_id_snapshot=personal_drop_in_booking_id,
            # Persist the conservative dispatched state in the same commit as
            # the canonical financial family. A crash after this commit must
            # enter authenticated recovery instead of stranding CLAIMED rows.
            link_creation_state=BankPaymentOrder.LinkCreationState.DISPATCHED,
            link_creation_claimed_at=now,
            link_creation_dispatched_at=now,
            created_by_id=created_by_id,
        )
        order.provider_payment_link_id = _generate_provider_payment_link_id(order.id)
        order.full_clean()
        order.save(
            update_fields=[
                "provider_payment_link_id",
                "payment_intent_key",
                "link_creation_state",
                "link_creation_claimed_at",
                "link_creation_dispatched_at",
                "updated_at",
            ]
        )

    # Commit the claim before dispatch. A lost provider response is never
    # permission to issue a replacement bearer link.
    try:
        if provider_name == BankPaymentOrder.Provider.TOCHKA:
            from apps.billing.payment_providers.base import online_payments_enabled

            if not online_payments_enabled():
                raise BusinessLogicError(
                    "Онлайн-оплата Точки отключена перед отправкой",
                    code="tochka_payment_creation_not_ready",
                )
        link = provider.create_payment_link(order=order)
    except BusinessLogicError as exc:
        if provider_name == BankPaymentOrder.Provider.TOCHKA:
            if exc.code in {
                "bank_payment_order_ttl_too_short",
                "payment_return_origin_invalid",
                "receipt_buyer_email_required",
                "tochka_credentials_missing",
                "tochka_payment_creation_not_ready",
            }:
                _fail_bank_payment_order_and_cleanup(
                    club_id=club_id,
                    order_id=order.id,
                    error_code=exc.code,
                    error_message=exc.message,
                    actor_user_id=created_by_id,
                    link_creation_state=BankPaymentOrder.LinkCreationState.READY,
                )
                raise
            mark_provider_creation_unknown(order, error_code=exc.code)
            return order
        _fail_bank_payment_order_and_cleanup(
            club_id=club_id,
            order_id=order.id,
            error_code=exc.code,
            error_message=exc.message,
            actor_user_id=created_by_id,
        )
        raise
    except Exception:
        if provider_name == BankPaymentOrder.Provider.TOCHKA:
            mark_provider_creation_unknown(order, error_code="provider_creation_unknown")
            return order
        _fail_bank_payment_order_and_cleanup(
            club_id=club_id,
            order_id=order.id,
            error_code="provider_unexpected_error",
            error_message="Provider failed while creating payment link",
            actor_user_id=created_by_id,
            cleanup_reason="provider_unexpected_error",
        )
        raise

    try:
        provider_payment_modes = validate_sbp_only_payment_modes(link.payment_modes)
    except BusinessLogicError as exc:
        if provider_name == BankPaymentOrder.Provider.TOCHKA:
            return record_provider_creation_manual_review(
                club_id=club_id,
                order_id=order.id,
                link=link,
                error_code=exc.code,
            )
        _fail_bank_payment_order_and_cleanup(
            club_id=club_id,
            order_id=order.id,
            error_code=exc.code,
            error_message=exc.message,
            actor_user_id=created_by_id,
        )
        raise

    order = apply_provider_creation_result(
        club_id=club_id,
        order_id=order.id,
        link=link,
        provider_payment_modes=provider_payment_modes,
    )
    logger.info(
        "bank_payment_order_created",
        extra={"order_id": order.id, "payment_id": payment.id, "club_id": club_id, "provider": provider_name},
    )
    return order

def cancel_bank_payment_order(
    *,
    club_id: int,
    order_id: int,
    actor_user_id: int,
    allowed_student_id: int | None = None,
    allowed_sources: set[str] | None = None,
    reason: str = "cancelled_by_user",
) -> BankPaymentOrder:
    with transaction.atomic():
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        lock_training_group_mutation_scope(club_id=club_id)
        ordered_scope = lock_complete_personal_scopes(club_id=club_id, order_ids=[order_id])
        order_queryset = BankPaymentOrder.objects.for_club(club_id).select_related("payment", "subscription")
        order = (
            order_queryset.get(id=order_id)
            if ordered_scope is not None
            else order_queryset.select_for_update(of=("self",)).filter(id=order_id).first()
        )
        if order is None:
            raise BusinessLogicError(
                "Ссылка на оплату не найдена",
                code="bank_payment_order_not_found",
            )
        if allowed_student_id is not None and order.student_id != allowed_student_id:
            raise BusinessLogicError(
                "Ссылка на оплату недоступна",
                code="bank_payment_order_forbidden",
            )
        if allowed_sources is not None and order.source not in allowed_sources:
            raise BusinessLogicError(
                "Ссылку на оплату нельзя отменить в этом разделе",
                code="bank_payment_order_cancel_source_forbidden",
            )
        if order.status == BankPaymentOrder.Status.CANCELLED:
            return order
        if order.status not in LIVE_BANK_PAYMENT_ORDER_STATUSES:
            raise BusinessLogicError(
                "Можно отменить только активную неоплаченную ссылку",
                code="bank_payment_order_not_cancellable",
            )
        if provider_dispatch_blocks_cancellation(order):
            raise BusinessLogicError(
                "Ссылка Точки уже могла быть выдана банком и ждёт оплаты или истечения",
                code="bank_payment_order_provider_dispatch_not_cancellable",
            )
        if order.payment.status != Payment.Status.PENDING:
            raise BusinessLogicError(
                "Оплата по этой ссылке уже обработана",
                code="bank_payment_order_payment_not_pending",
            )
        if order.subscription and order.subscription.status != Subscription.Status.PENDING:
            raise BusinessLogicError(
                "Абонемент по этой ссылке уже обработан",
                code="bank_payment_order_subscription_not_pending",
            )
        order.status = BankPaymentOrder.Status.CANCELLED
        order.last_error_code = ""
        order.last_error_message = ""
        order.save(update_fields=["status", "last_error_code", "last_error_message", "updated_at"])
        _cancel_pending_bank_order_artifacts(
            order=order,
            reason=reason,
            actor_user_id=actor_user_id,
        )
    order.refresh_from_db()
    return order

def expire_bank_payment_orders(*, now=None) -> int:
    now = now or timezone.now()
    order_ids = list(
        BankPaymentOrder.objects.unscoped()
        .filter(
            status__in=CLOSABLE_BANK_PAYMENT_ORDER_STATUSES,
            expires_at__lt=now,
        )
        .exclude(
            provider=BankPaymentOrder.Provider.TOCHKA,
            link_creation_state__in=[
                BankPaymentOrder.LinkCreationState.CLAIMED,
                BankPaymentOrder.LinkCreationState.DISPATCHED,
                BankPaymentOrder.LinkCreationState.UNKNOWN,
            ],
        )
        .values_list("id", flat=True)
    )
    updated = 0
    for order_id in order_ids:
        preview = (
            BankPaymentOrder.objects.unscoped()
            .filter(id=order_id)
            .values("club_id")
            .first()
        )
        if preview is None:
            continue
        with transaction.atomic():
            from apps.attendance.services.personal_locking import lock_complete_personal_scopes
            from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

            lock_training_group_mutation_scope(club_id=preview["club_id"])
            ordered_scope = lock_complete_personal_scopes(
                club_id=preview["club_id"],
                order_ids=[order_id],
            )
            order = (
                BankPaymentOrder.objects.unscoped().get(id=order_id)
                if ordered_scope is not None
                else BankPaymentOrder.objects.unscoped().select_for_update().get(id=order_id)
            )
            if order.status not in CLOSABLE_BANK_PAYMENT_ORDER_STATUSES or order.expires_at >= now:
                continue
            if provider_dispatch_blocks_cancellation(order):
                continue
            order.status = BankPaymentOrder.Status.EXPIRED
            order.save(update_fields=["status", "updated_at"])
            _cancel_pending_bank_order_artifacts(
                order=order,
                reason="bank_payment_link_expired",
            )
            updated += 1
    return updated
