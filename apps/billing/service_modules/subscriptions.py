from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    Payment,
    Subscription,
    Tariff,
)
from apps.billing.service_modules._shared import _validate_positive_money
from apps.billing.service_modules.debts import (
    _assert_personal_drop_in_payment_path,
    _attach_debts_to_subscription,
    record_debt_settlement_events,
)
from apps.billing.service_modules.entitlements import (
    _create_subscription_components,
    _resolve_package_owner_trainer_id_for_components,
    _student_has_current_component_subscription,
)
from apps.billing.service_modules.sale_earnings import (
    _capture_sale_earning_snapshots_for_components,
    _enqueue_sale_earning_after_commit,
)
from apps.billing.service_modules.tariff_components import (
    _ensure_tariff_components,
    _resolve_tariff_payout_policy,
)
from apps.clubs.timezones import club_localdate_by_id
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student

logger = logging.getLogger(__name__)


def _validate_direct_subscription_payment_method(
    *,
    recorded_by_id: int | None,
    payment_method: str | None,
) -> str | None:
    """Require an explicit manual method for an already-confirmed direct sale."""
    if recorded_by_id is None:
        if payment_method is not None:
            raise BusinessLogicError(
                "Способ оплаты можно указать только вместе с автором оплаты",
                code="payment_recorder_required",
            )
        return None
    if payment_method is None:
        raise BusinessLogicError(
            "Укажите способ оплаты",
            code="manual_payment_method_required",
        )
    if payment_method == Payment.Method.ONLINE:
        raise BusinessLogicError(
            "Онлайн-оплата должна проходить через платёжную ссылку",
            code="online_payment_requires_bank_order",
        )
    if payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER}:
        raise BusinessLogicError(
            "Некорректный способ оплаты",
            code="invalid_payment_method",
        )
    return payment_method


def create_subscription(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    seller_trainer_id: int | None = None,
    package_owner_trainer_id: int | None = None,
    recorded_by_id: int | None = None,
    payment_method: str | None = None,
    debt_ids: list[int] | None = None,
) -> Subscription:
    """Create an active subscription and, for a recorded direct sale, a paired
    CONFIRMED manual Payment with its explicitly selected cash/transfer method.
    """
    from apps.trainers.models import Trainer

    resolved_payment_method = _validate_direct_subscription_payment_method(
        recorded_by_id=recorded_by_id,
        payment_method=payment_method,
    )
    effective_verified_at = timezone.now()
    tariff = (
        Tariff.objects.for_club(club_id)
        .select_related("training_type", "location")
        .get(id=tariff_id, is_active=True)
    )
    _validate_positive_money(tariff.price)
    components = _ensure_tariff_components(tariff, club_id=club_id)
    resolved_package_owner_trainer_id: int | None = None
    requested_debt_ids = list(dict.fromkeys(debt_ids or []))

    if requested_debt_ids and recorded_by_id is None:
        raise BusinessLogicError(
            "Выбранные долги можно закрыть только вместе с подтвержденной оплатой",
            code="debt_settlement_requires_payment",
        )

    if seller_trainer_id is not None:
        if (
            not Trainer.objects.for_club(club_id)
            .filter(id=seller_trainer_id)
            .exists()
        ):
            raise BusinessLogicError(
                "Тренер-продавец не найден",
                code="seller_trainer_not_found",
            )
    if package_owner_trainer_id is not None:
        if (
            not Trainer.objects.for_club(club_id)
            .filter(id=package_owner_trainer_id)
            .exists()
        ):
            raise BusinessLogicError(
                "Тренер-владелец пакета не найден",
                code="package_owner_trainer_not_found",
            )
    if recorded_by_id is not None:
        resolved_package_owner_trainer_id = (
            _resolve_package_owner_trainer_id_for_components(
                components=components,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
                allow_seller_fallback=True,
            )
        )

    salary_checkin_ids: list[int] = []
    payment: Payment | None = None
    converted_lead_after_subscription = False
    with transaction.atomic():
        if recorded_by_id is not None and any(
            component.trainer_payout_policy == Tariff.PayoutPolicy.ON_PAYMENT
            for component in components
        ):
            from apps.trainers.services import (
                lock_and_assert_trainer_payroll_date_open,
            )

            lock_and_assert_trainer_payroll_date_open(
                club_id=club_id,
                target_date=club_localdate_by_id(
                    club_id,
                    effective_verified_at,
                ),
            )
        Student.objects.for_club(club_id).select_for_update().get(id=student_id)
        _assert_personal_drop_in_payment_path(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
        )
        if _student_has_current_component_subscription(
            club_id=club_id,
            student_id=student_id,
            components=components,
            effective_at=effective_verified_at,
        ):
            raise BusinessLogicError(
                "У ученика уже есть активный абонемент этого типа",
                code="active_subscription_exists",
            )

        subscription = Subscription.objects.create(
            club_id=club_id,
            student_id=student_id,
            tariff=tariff,
            paid_amount=tariff.price,
            trainings_left=tariff.trainings_limit,
            expires_at=effective_verified_at
            + timedelta(days=tariff.duration_days),
            scope=tariff.scope,
            location=tariff.location,
            trainer_payout_policy_snapshot=_resolve_tariff_payout_policy(tariff),
        )
        _create_subscription_components(
            subscription=subscription,
            club_id=club_id,
            paid_amount=tariff.price,
        )
        # Admin/API direct subscription creation is an already-confirmed sale.
        # Keep a paired payment record so financial history stays audit-friendly.
        if recorded_by_id is not None:
            payment = Payment.objects.create(
                club_id=club_id,
                student_id=student_id,
                tariff=tariff,
                subscription=subscription,
                amount=tariff.price,
                original_amount=tariff.price,
                payment_method=resolved_payment_method,
                status=Payment.Status.CONFIRMED,
                recorded_by_id=recorded_by_id,
                verified_by_id=recorded_by_id,
                verified_at=effective_verified_at,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=resolved_package_owner_trainer_id,
            )
            if (
                seller_trainer_id is not None
                or resolved_package_owner_trainer_id is not None
            ):
                _capture_sale_earning_snapshots_for_components(
                    payment=payment,
                    subscription=subscription,
                    club_id=club_id,
                )
            if payment.sale_earning_snapshot_recorded:
                _enqueue_sale_earning_after_commit(
                    payment_id=payment.id,
                    club_id=club_id,
                )

        salary_checkin_ids = _attach_debts_to_subscription(
            subscription=subscription,
            club_id=club_id,
            student_id=student_id,
            resolution_type="subscription",
            debt_ids=requested_debt_ids,
            attach_all_matching=False,
            settlement_payment_id=payment.id if payment is not None else None,
            created_at_lte=subscription.created_at,
            actor_user_id=recorded_by_id,
            lifecycle_event_type=DebtLifecycleEvent.EventType.ATTACHED,
        )
        if payment is not None and requested_debt_ids:
            confirmed_debt_ids = list(
                Debt.objects.for_club(club_id)
                .filter(
                    id__in=requested_debt_ids,
                    settlement_payment_id=payment.id,
                    resolved_at__isnull=False,
                )
                .values_list("id", flat=True)
            )
            record_debt_settlement_events(
                club_id=club_id,
                payment=payment,
                debt_ids=confirmed_debt_ids,
                event_type=DebtSettlementEvent.EventType.CONFIRMED,
            )
        from apps.trainers.services import create_package_allocation_for_subscription

        create_package_allocation_for_subscription(
            club_id=club_id,
            subscription_id=subscription.id,
            owner_trainer_id=resolved_package_owner_trainer_id,
            payment_id=payment.id if payment is not None else None,
            created_by_id=recorded_by_id,
            source="payment" if payment is not None else "manual_subscription",
        )

        from apps.leads.services import convert_lead_after_subscription_payment

        converted_lead_after_subscription = convert_lead_after_subscription_payment(
            club_id=club_id,
            student_id=student_id,
            actor_user_id=recorded_by_id,
        )

    if salary_checkin_ids:
        from django_q.tasks import async_task

        for checkin_id in salary_checkin_ids:
            async_task(
                "apps.attendance.tasks.calculate_salary",
                checkin_id,
                club_id=club_id,
            )

    # Auto-close lead/trial/renewal tasks when subscription is created
    if not converted_lead_after_subscription:
        from apps.retention.services import auto_close_tasks_on_subscription

        auto_close_tasks_on_subscription(
            student_id=student_id,
            club_id=club_id,
        )

    logger.info(
        "subscription_created",
        extra={
            "id": subscription.id,
            "student_id": student_id,
            "club_id": club_id,
        },
    )
    return subscription
