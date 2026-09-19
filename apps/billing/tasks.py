from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import Payment, SubscriptionComponent, SubscriptionFreeze, Tariff, TrainingType
from apps.billing.recognition import payment_recognition_date
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def create_sale_earning(payment_id: int, club_id: int) -> None:
    """Create one-time sale earning for trainer who sold a GROUP subscription.

    Triggered from verify_payment(action='confirm') only when:
      - subscription's tariff.training_type.kind == GROUP
      - payment.seller_trainer_id is set
    Idempotent via partial UniqueConstraint unique_sale_earning_per_payment.
    """
    from apps.trainers.models import TrainerEarning

    payment = (
        Payment.objects.for_club(club_id)
        .select_related("subscription__tariff__training_type", "seller_trainer", "package_owner_trainer")
        .get(id=payment_id)
    )

    if payment.status != Payment.Status.CONFIRMED:
        logger.warning(
            "sale_earning_skipped_payment_not_confirmed",
            extra={"payment_id": payment_id, "status": payment.status},
        )
        return

    active_components = SubscriptionComponent.objects.for_club(club_id).filter(
        subscription=payment.subscription,
        is_active=True,
    )
    sale_components = list(
        active_components.filter(
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
        )
        .select_related("training_type")
        .order_by("id")
    )
    if sale_components:
        existing_component_ids = set(
            TrainerEarning.objects.for_club(club_id)
            .filter(payment_id=payment_id, subscription_component_id__isnull=False)
            .values_list("subscription_component_id", flat=True)
        )
        if existing_component_ids.issuperset({component.id for component in sale_components}):
            logger.info("sale_earning_already_created", extra={"payment_id": payment_id})
            return

        target_date = payment_recognition_date(payment=payment)
        if target_date is None:
            raise BusinessLogicError("Дата оплаты не подтверждена", code="payment_recognition_date_missing")
        from apps.trainers.services import lock_and_assert_trainer_payroll_date_open

        with transaction.atomic():
            lock_and_assert_trainer_payroll_date_open(club_id=club_id, target_date=target_date)
            for component in sale_components:
                if TrainerEarning.objects.for_club(club_id).filter(
                    payment_id=payment_id,
                    subscription_component_id=component.id,
                ).exists():
                    continue
                if component.sale_trainer_id_snapshot is None:
                    logger.warning(
                        "sale_earning_skipped_incomplete_snapshot",
                        extra={"payment_id": payment_id, "component_id": component.id, "club_id": club_id},
                    )
                    continue
                if component.sale_snapshot_provenance != (
                    Payment.SaleSnapshotProvenance.OPENING_REVIEWED
                    if payment.origin == Payment.Origin.OPENING
                    else Payment.SaleSnapshotProvenance.CONFIRM_TIME
                ):
                    raise BusinessLogicError(
                        f"Снапшот продажи для компонента отсутствует (component {component.id})",
                        code="sale_earning_snapshot_missing",
                    )
                if component.sale_rate_percent_snapshot is None:
                    logger.warning(
                        "sale_earning_skipped_no_rate",
                        extra={
                            "payment_id": payment_id,
                            "component_id": component.id,
                            "trainer_id": component.sale_trainer_id_snapshot,
                        },
                    )
                    continue
                amount = (
                    component.paid_amount_basis_snapshot
                    * component.sale_rate_percent_snapshot
                    / Decimal("100")
                ).quantize(Decimal("0.01"))
                earning = TrainerEarning.objects.create(
                    club_id=club_id,
                    trainer_id=component.sale_trainer_id_snapshot,
                    checkin=None,
                    payment=payment,
                    subscription_component=component,
                    earning_source=TrainerEarning.Source.SALE,
                    earning_type=component.training_type.kind,
                    amount=amount,
                    rate_percent=component.sale_rate_percent_snapshot,
                    subscription_price=component.paid_amount_basis_snapshot,
                    payout_policy_snapshot=component.trainer_payout_policy_snapshot,
                    component_id_snapshot=component.id,
                    component_paid_amount_basis_snapshot=component.paid_amount_basis_snapshot,
                )
                from apps.trainers.settlement_services import note_earning_created

                note_earning_created(earning=earning)
        logger.info("sale_earning_created", extra={"payment_id": payment_id, "club_id": club_id})
        return

    # Component snapshots are authoritative for package compensation. A seller
    # may still be recorded for lead conversion or attribution even when every
    # component explicitly uses no on-payment payout.
    if active_components.exists():
        logger.info(
            "sale_earning_skipped_no_on_payment_component",
            extra={"payment_id": payment_id, "club_id": club_id},
        )
        return

    if not payment.seller_trainer_id:
        logger.warning(
            "sale_earning_skipped_no_seller",
            extra={"payment_id": payment_id, "club_id": club_id},
        )
        return

    # Legacy idempotency guard for pre-component rows.
    if TrainerEarning.objects.for_club(club_id).filter(payment_id=payment_id).exists():
        logger.info("sale_earning_already_created", extra={"payment_id": payment_id})
        return

    if payment.sale_earning_snapshot_recorded:
        provenance = payment.sale_snapshot_provenance
        if provenance == Payment.SaleSnapshotProvenance.LEGACY_BACKFILL_CURRENT_STATE:
            raise BusinessLogicError(
                f"Исторический снапшот продажи требует ручной сверки (payment {payment_id})",
                code="sale_earning_legacy_reconciliation_required",
            )
        if provenance != (
            Payment.SaleSnapshotProvenance.OPENING_REVIEWED
            if payment.origin == Payment.Origin.OPENING
            else Payment.SaleSnapshotProvenance.CONFIRM_TIME
        ):
            raise BusinessLogicError(
                f"Источник снапшота продажи не подтвержден (payment {payment_id})",
                code="sale_earning_snapshot_missing",
            )

    sub = payment.subscription
    training_type_kind = (
        payment.sale_training_type_kind_snapshot
        if payment.sale_earning_snapshot_recorded
        else sub.tariff.training_type.kind if sub else ""
    )
    if not sub or training_type_kind != TrainingType.Kind.GROUP:
        logger.warning(
            "sale_earning_skipped_not_group",
            extra={"payment_id": payment_id, "club_id": club_id},
        )
        return

    if payment.sale_earning_snapshot_recorded:
        rate = payment.sale_rate_percent_snapshot
        trainer_id = payment.sale_trainer_id_snapshot
        amount_basis = payment.sale_amount_basis_snapshot
        training_type_id = payment.sale_training_type_id_snapshot
    else:
        raise BusinessLogicError(
            f"Снапшот продажи для расчета зарплаты отсутствует (payment {payment_id})",
            code="sale_earning_snapshot_missing",
        )

    if rate is None:
        logger.warning(
            "sale_earning_skipped_no_rate",
            extra={
                "payment_id": payment_id,
                "trainer_id": trainer_id,
                "training_type_id": training_type_id,
            },
        )
        return
    if trainer_id is None or amount_basis is None:
        logger.warning(
            "sale_earning_skipped_incomplete_snapshot",
            extra={"payment_id": payment_id, "club_id": club_id},
        )
        return

    amount = (amount_basis * rate / Decimal("100")).quantize(Decimal("0.01"))

    target_date = payment_recognition_date(payment=payment)
    if target_date is None:
        raise BusinessLogicError("Дата оплаты не подтверждена", code="payment_recognition_date_missing")
    from apps.trainers.services import lock_and_assert_trainer_payroll_date_open

    try:
        with transaction.atomic():
            lock_and_assert_trainer_payroll_date_open(club_id=club_id, target_date=target_date)
            if TrainerEarning.objects.for_club(club_id).filter(payment_id=payment_id).exists():
                logger.info("sale_earning_already_created", extra={"payment_id": payment_id})
                return
            earning = TrainerEarning.objects.create(
                club_id=club_id,
                trainer_id=trainer_id,
                checkin=None,
                payment=payment,
                earning_source=TrainerEarning.Source.SALE,
                earning_type=TrainerEarning.EarningType.GROUP,
                amount=amount,
                rate_percent=rate,
                subscription_price=amount_basis,
            )
            from apps.trainers.settlement_services import note_earning_created

            note_earning_created(earning=earning)
    except IntegrityError:
        # Partial unique constraint races -> idempotent skip
        if TrainerEarning.objects.for_club(club_id).filter(payment_id=payment_id).exists():
            logger.info("sale_earning_race_skipped", extra={"payment_id": payment_id})
            return
        raise

    logger.info(
        "sale_earning_created",
        extra={
            "payment_id": payment_id,
            "trainer_id": trainer_id,
            "club_id": club_id,
        },
    )


def auto_unfreeze_expired() -> dict:
    """Periodic task: auto-unfreeze subscriptions whose freeze period has elapsed.

    Iterates over freezes with ends_at IS NULL, checks
    starts_at + days <= now, calls unfreeze_subscription per freeze.
    Errors are caught per-freeze (skipped count) so one failure doesn't
    stall the whole batch.
    """
    from apps.billing.services import unfreeze_subscription

    now = timezone.now()
    # Cross-tenant cron: explicit .unscoped() per .claude/rules/tenant.md so
    # the lint test_no_unscoped_querysets passes and intent is documented.
    stale_freezes = (
        SubscriptionFreeze.objects.unscoped()
        .filter(
            ends_at__isnull=True,
            status=SubscriptionFreeze.FreezeStatus.APPROVED,
        )
        .select_related("subscription")
    )
    unfrozen = 0
    skipped = 0
    for f in stale_freezes:
        if f.starts_at + timedelta(days=f.days) > now:
            continue
        try:
            with transaction.atomic():
                unfreeze_subscription(freeze_id=f.id, club_id=f.club_id)
            unfrozen += 1
        except BusinessLogicError as exc:
            logger.info(
                "auto_unfreeze_skipped",
                extra={"freeze_id": f.id, "code": getattr(exc, "code", None)},
            )
            skipped += 1
        except Exception:
            logger.exception("auto_unfreeze_error", extra={"freeze_id": f.id})
            skipped += 1
    logger.info("auto_unfreeze_run", extra={"unfrozen": unfrozen, "skipped": skipped})
    return {"unfrozen": unfrozen, "skipped": skipped}


def expire_pending_bank_payment_orders() -> dict:
    from apps.billing.service_modules.payment_returns import purge_expired_return_states
    from apps.billing.services import expire_bank_payment_orders

    expired = expire_bank_payment_orders()
    purged_return_states = purge_expired_return_states()
    logger.info(
        "bank_payment_orders_expired",
        extra={"expired": expired, "purged_return_states": purged_return_states},
    )
    return {"expired": expired, "purged_return_states": purged_return_states}


def replay_deferred_bank_payment_provider_events_task(*, club_id: int) -> dict[str, int]:
    """Run the durable, per-club replay queued after reconciliation exit."""
    from apps.billing.services import replay_deferred_bank_payment_provider_events

    return replay_deferred_bank_payment_provider_events(club_id=club_id)


def reconcile_bank_payment_order_task(*, club_id: int, order_id: int) -> str:
    from apps.billing.service_modules.provider_events import reconcile_provider_payment_order

    return reconcile_provider_payment_order(club_id=club_id, order_id=order_id)


def process_due_bank_payment_provider_work() -> dict[str, dict[str, int]]:
    if not settings.TOCHKA_PAYMENT_RECONCILIATION_ENABLED:
        return {
            "reconciliation": {"disabled": 1},
            "creation_recovery": {"disabled": 1},
        }
    from apps.billing.service_modules.provider_events import (
        process_due_provider_reconciliations,
        process_unknown_provider_creations,
    )

    try:
        reconciliation = process_due_provider_reconciliations()
    except Exception:
        logger.exception("bank_payment_reconciliation_phase_failed")
        reconciliation = {"error": 1}
    try:
        creation_recovery = process_unknown_provider_creations()
    except Exception:
        logger.exception("bank_payment_creation_recovery_phase_failed")
        creation_recovery = {"error": 1}
    logger.info(
        "bank_payment_provider_work_processed",
        extra={
            "reconciliation_count": sum(reconciliation.values()),
            "creation_recovery_count": sum(creation_recovery.values()),
        },
    )
    return {
        "reconciliation": reconciliation,
        "creation_recovery": creation_recovery,
    }


def refresh_tochka_payment_readiness_task() -> dict[str, int]:
    """Refresh redacted retailer readiness only under the explicit live gate."""

    if not bool(getattr(settings, "TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED", False)):
        return {"disabled": 1}
    if str(getattr(settings, "PAYMENT_PROVIDER", "") or "") != "tochka":
        return {"provider_not_selected": 1}
    if not (
        bool(getattr(settings, "ONLINE_PAYMENT_ORDER_CREATION_ENABLED", False))
        or bool(getattr(settings, "TOCHKA_PAYMENT_RECONCILIATION_ENABLED", False))
    ):
        return {"operations_disabled": 1}

    from apps.billing.service_modules.payment_readiness import refresh_tochka_retailer_readback

    try:
        snapshot = refresh_tochka_retailer_readback()
    except Exception:
        logger.error("tochka_retailer_readiness_refresh_failed")
        return {"error": 1}
    logger.info(
        "tochka_retailer_readiness_refreshed",
        extra={
            "retailer_status": snapshot.retailer_status,
            "is_active": snapshot.is_active,
            "checked_at": snapshot.checked_at,
        },
    )
    return {"stored": 1}



def notify_payment_verification(payment_id: int, club_id: int) -> None:
    from apps.clubs.models import ClubMembership
    from apps.notifications.services import send_push_to_user

    payment = Payment.objects.for_club(club_id).select_related("student", "tariff").get(id=payment_id)
    owner_user_ids = list(
        ClubMembership.objects.filter(
            club_id=club_id,
            role__in=[ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN],
            is_active=True,
        ).values_list("user_id", flat=True)
    )
    if not owner_user_ids:
        return

    title = "Верификация оплаты"
    payment_method_label = payment.get_payment_method_display().lower()
    if payment_method_label.endswith("ые"):
        payment_method_label = f"{payment_method_label[:-2]}ыми"
    body = f"{payment.student} -- {payment.amount} руб. {payment_method_label}"

    for user_id in owner_user_ids:
        send_push_to_user(
            user_id=user_id,
            title=title,
            body=body,
            url="/dashboard/billing/",
            actions=[
                {"action": "confirm", "title": "Подтвердить"},
                {"action": "reject", "title": "Отклонить"},
            ],
            data={"payment_id": payment.id, "tag": f"payment-verify-{payment.id}"},
        )

    logger.info(
        "payment_verification_push_sent",
        extra={"payment_id": payment_id, "club_id": club_id, "owner_count": len(owner_user_ids)},
    )
