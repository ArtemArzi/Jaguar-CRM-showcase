from __future__ import annotations

import logging

from django.db import transaction

from apps.billing.models import (
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TrainingType,
)
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def _enqueue_sale_earning_after_commit(*, payment_id: int, club_id: int) -> None:
    def enqueue() -> None:
        from django_q.tasks import async_task

        async_task(
            "apps.billing.tasks.create_sale_earning",
            payment_id,
            club_id=club_id,
        )

    transaction.on_commit(enqueue)


def _capture_sale_earning_snapshot(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
) -> None:
    if payment.origin == Payment.Origin.OPENING:
        raise BusinessLogicError("Условия переноса уже подтверждены", code="opening_snapshot_immutable")
    sale_trainer_id = payment.sale_trainer_id_snapshot or payment.seller_trainer_id
    if (
        not sale_trainer_id
        or subscription.tariff.training_type.kind != TrainingType.Kind.GROUP
    ):
        return

    from apps.trainers.selectors import resolve_trainer_rate

    location_id = subscription.tariff.location_id
    training_type_id = subscription.tariff.training_type_id
    training_type_kind = subscription.tariff.training_type.kind
    if payment.target_schedule_id:
        location_id = payment.target_location_id_snapshot
        training_type_id = payment.target_training_type_id_snapshot
        training_type_kind = payment.target_training_type_kind_snapshot

    rate = resolve_trainer_rate(
        club_id=club_id,
        trainer_id=sale_trainer_id,
        location_id=location_id,
        training_type_id=training_type_id,
        fallback_any_location=True,
    )
    payment.sale_earning_snapshot_recorded = True
    payment.sale_trainer_id_snapshot = sale_trainer_id
    payment.sale_training_type_id_snapshot = training_type_id
    payment.sale_training_type_kind_snapshot = training_type_kind
    payment.sale_rate_percent_snapshot = rate
    payment.sale_amount_basis_snapshot = payment.amount
    payment.sale_snapshot_provenance = Payment.SaleSnapshotProvenance.CONFIRM_TIME
    payment.save(
        update_fields=[
            "sale_earning_snapshot_recorded",
            "sale_trainer_id_snapshot",
            "sale_training_type_id_snapshot",
            "sale_training_type_kind_snapshot",
            "sale_rate_percent_snapshot",
            "sale_amount_basis_snapshot",
            "sale_snapshot_provenance",
            "updated_at",
        ]
    )


def _sale_trainer_id_for_component(
    *,
    payment: Payment,
    component: SubscriptionComponent,
) -> int | None:
    if component.training_type.kind == TrainingType.Kind.GROUP:
        return payment.sale_trainer_id_snapshot or payment.seller_trainer_id
    return payment.package_owner_trainer_id


def _capture_sale_earning_snapshots_for_components(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
) -> None:
    if payment.origin == Payment.Origin.OPENING:
        raise BusinessLogicError("Условия переноса уже подтверждены", code="opening_snapshot_immutable")
    from apps.trainers.selectors import resolve_trainer_rate

    components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .filter(
            subscription=subscription,
            is_active=True,
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
        )
        .select_related("training_type", "location")
        .order_by("id")
    )
    if not components:
        return

    first_recorded: SubscriptionComponent | None = None
    for component in components:
        trainer_id = _sale_trainer_id_for_component(
            payment=payment,
            component=component,
        )
        if trainer_id is None:
            logger.warning(
                "component_sale_earning_skipped_no_trainer",
                extra={
                    "payment_id": payment.id,
                    "component_id": component.id,
                    "club_id": club_id,
                },
            )
            continue

        rate = resolve_trainer_rate(
            club_id=club_id,
            trainer_id=trainer_id,
            location_id=component.location_id or subscription.location_id,
            training_type_id=component.training_type_id,
            fallback_any_location=True,
        )
        component.sale_trainer_id_snapshot = trainer_id
        component.sale_rate_percent_snapshot = rate
        component.sale_snapshot_provenance = (
            Payment.SaleSnapshotProvenance.CONFIRM_TIME
        )
        component.save(
            update_fields=[
                "sale_trainer_id_snapshot",
                "sale_rate_percent_snapshot",
                "sale_snapshot_provenance",
                "updated_at",
            ]
        )
        if first_recorded is None:
            first_recorded = component

    if first_recorded is None:
        return

    payment.sale_earning_snapshot_recorded = True
    payment.sale_trainer_id_snapshot = first_recorded.sale_trainer_id_snapshot
    payment.sale_training_type_id_snapshot = first_recorded.training_type_id
    payment.sale_training_type_kind_snapshot = first_recorded.training_type.kind
    payment.sale_rate_percent_snapshot = first_recorded.sale_rate_percent_snapshot
    payment.sale_amount_basis_snapshot = first_recorded.paid_amount_basis_snapshot
    payment.sale_snapshot_provenance = Payment.SaleSnapshotProvenance.CONFIRM_TIME
    payment.save(
        update_fields=[
            "sale_earning_snapshot_recorded",
            "sale_trainer_id_snapshot",
            "sale_training_type_id_snapshot",
            "sale_training_type_kind_snapshot",
            "sale_rate_percent_snapshot",
            "sale_amount_basis_snapshot",
            "sale_snapshot_provenance",
            "updated_at",
        ],
    )
