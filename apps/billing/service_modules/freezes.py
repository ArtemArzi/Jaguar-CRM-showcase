from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.billing.models import Subscription, SubscriptionFreeze
from apps.billing.service_modules.club_settings import get_or_create_club_settings
from apps.clubs.models import ClubSettings
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def _validate_positive_days(days: int) -> None:
    if days <= 0:
        raise BusinessLogicError(
            "Количество дней заморозки должно быть больше нуля",
            code="invalid_freeze_days",
        )


def _validate_freeze_reason(reason: str) -> str:
    normalized = reason.strip()
    if normalized not in SubscriptionFreeze.Reason.values:
        raise BusinessLogicError(
            "Некорректная причина заморозки",
            code="invalid_freeze_reason",
        )
    return normalized


def _mark_expired_subscription_if_needed(*, club_id: int, subscription_id: int) -> None:
    now = timezone.now()
    Subscription.objects.for_club(club_id).filter(
        id=subscription_id,
        status=Subscription.Status.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
    ).update(status=Subscription.Status.EXPIRED, updated_at=now)


def _validate_subscription_freeze_policy(
    *,
    subscription: Subscription,
    club_settings: ClubSettings,
    days: int,
) -> None:
    now = timezone.now()
    if (
        subscription.status == Subscription.Status.ACTIVE
        and subscription.expires_at is not None
        and subscription.expires_at <= now
    ):
        subscription.status = Subscription.Status.EXPIRED

    if (
        subscription.status != Subscription.Status.ACTIVE
        or (subscription.expires_at is not None and subscription.expires_at <= now)
    ):
        raise BusinessLogicError(
            "Only active subscriptions can be frozen",
            code="invalid_subscription_status",
        )

    min_required = club_settings.min_trainings_to_freeze
    if (
        subscription.trainings_left is not None
        and subscription.trainings_left < min_required
    ):
        raise BusinessLogicError(
            f"Нельзя заморозить: осталось меньше {min_required} тренировок",
            code="freeze_below_min_trainings",
        )

    approved_freezes = subscription.freezes.filter(
        status=SubscriptionFreeze.FreezeStatus.APPROVED,
    )
    agg = approved_freezes.aggregate(total_days=Sum("days"), total_count=Count("id"))
    used_days = agg["total_days"] or 0
    freeze_count = agg["total_count"]

    if used_days + days > club_settings.freeze_max_days:
        remaining = club_settings.freeze_max_days - used_days
        raise BusinessLogicError(
            f"Freeze limit exceeded. Remaining: {remaining} days",
            code="freeze_limit_exceeded",
        )

    if club_settings.freeze_max_count is not None and freeze_count >= club_settings.freeze_max_count:
        raise BusinessLogicError(
            f"Maximum freeze count ({club_settings.freeze_max_count}) reached",
            code="freeze_count_exceeded",
        )


def _subscription_status_after_unfreeze(subscription: Subscription, *, now) -> str:
    if subscription.expires_at is not None and subscription.expires_at <= now:
        return Subscription.Status.EXPIRED
    if subscription.trainings_left is not None and subscription.trainings_left <= 0:
        return Subscription.Status.EXPIRED
    return Subscription.Status.ACTIVE


def freeze_subscription(
    *,
    club_id: int,
    subscription_id: int,
    days: int,
    reason: str,
    frozen_by_id: int,
    initiator_role: str = "",
) -> SubscriptionFreeze:
    _validate_positive_days(days)
    reason = _validate_freeze_reason(reason)
    club_settings = get_or_create_club_settings(club_id)

    if not club_settings.freeze_enabled:
        raise BusinessLogicError("Freeze is disabled for this club", code="freeze_disabled")

    is_trainer_initiated = initiator_role == "trainer"

    with transaction.atomic():
        subscription = Subscription.objects.for_club(club_id).select_for_update().get(id=subscription_id)
        _validate_subscription_freeze_policy(
            subscription=subscription,
            club_settings=club_settings,
            days=days,
        )

        now = timezone.now()

        if is_trainer_initiated:
            if SubscriptionFreeze.objects.for_club(club_id).filter(
                subscription=subscription,
                status=SubscriptionFreeze.FreezeStatus.PENDING,
            ).exists():
                raise BusinessLogicError(
                    "По этому абонементу уже есть заявка на заморозку",
                    code="freeze_pending_exists",
                )
            freeze = SubscriptionFreeze.objects.create(
                club_id=club_id,
                subscription=subscription,
                days=days,
                reason=reason,
                status=SubscriptionFreeze.FreezeStatus.PENDING,
                frozen_by_id=frozen_by_id,
                starts_at=now,
            )
        else:
            freeze = SubscriptionFreeze.objects.create(
                club_id=club_id,
                subscription=subscription,
                days=days,
                reason=reason,
                status=SubscriptionFreeze.FreezeStatus.APPROVED,
                frozen_by_id=frozen_by_id,
                approved_by_id=frozen_by_id,
                decision_at=now,
                decision_reason="",
                starts_at=now,
            )
            SubscriptionFreeze.objects.for_club(club_id).filter(
                subscription=subscription,
                status=SubscriptionFreeze.FreezeStatus.PENDING,
            ).update(
                status=SubscriptionFreeze.FreezeStatus.REJECTED,
                approved_by_id=None,
                rejected_by_id=frozen_by_id,
                decision_at=now,
                decision_reason="Закрыта автоматически: абонемент заморожен напрямую",
                updated_at=now,
            )
            subscription.status = Subscription.Status.FROZEN
            if subscription.expires_at is not None:
                subscription.expires_at = subscription.expires_at + timedelta(days=days)
            subscription.save(update_fields=["status", "expires_at", "updated_at"])

    logger.info(
        "freeze_created",
        extra={
            "freeze_id": freeze.id,
            "subscription_id": subscription_id,
            "club_id": club_id,
            "days": days,
            "status": freeze.status,
        },
    )
    return freeze


def _get_freeze_for_decision(*, freeze_id: int, club_id: int) -> SubscriptionFreeze:
    freeze = (
        SubscriptionFreeze.objects.for_club(club_id)
        .select_for_update()
        .get(id=freeze_id)
    )
    if freeze.status != SubscriptionFreeze.FreezeStatus.PENDING:
        raise BusinessLogicError(
            "Заявка на заморозку уже рассмотрена",
            code="freeze_already_decided",
        )
    return freeze


def approve_freeze(*, freeze_id: int, club_id: int, approved_by_id: int) -> SubscriptionFreeze:
    club_settings = get_or_create_club_settings(club_id)
    if not club_settings.freeze_enabled:
        raise BusinessLogicError("Freeze is disabled for this club", code="freeze_disabled")

    subscription_id = (
        SubscriptionFreeze.objects.for_club(club_id)
        .filter(id=freeze_id)
        .values_list("subscription_id", flat=True)
        .first()
    )
    if subscription_id is not None:
        _mark_expired_subscription_if_needed(
            club_id=club_id,
            subscription_id=subscription_id,
        )

    with transaction.atomic():
        freeze = _get_freeze_for_decision(freeze_id=freeze_id, club_id=club_id)
        subscription = Subscription.objects.for_club(club_id).select_for_update().get(
            id=freeze.subscription_id
        )
        _validate_subscription_freeze_policy(
            subscription=subscription,
            club_settings=club_settings,
            days=freeze.days,
        )
        now = timezone.now()
        freeze.status = SubscriptionFreeze.FreezeStatus.APPROVED
        freeze.approved_by_id = approved_by_id
        freeze.rejected_by_id = None
        freeze.decision_at = now
        freeze.decision_reason = ""
        freeze.starts_at = now
        freeze.save(
            update_fields=[
                "status",
                "approved_by",
                "rejected_by",
                "decision_at",
                "decision_reason",
                "starts_at",
                "updated_at",
            ]
        )
        SubscriptionFreeze.objects.for_club(club_id).filter(
            subscription=subscription,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        ).exclude(id=freeze.id).update(
            status=SubscriptionFreeze.FreezeStatus.REJECTED,
            approved_by_id=None,
            rejected_by_id=approved_by_id,
            decision_at=now,
            decision_reason="Закрыта автоматически: другая заявка по абонементу подтверждена",
            updated_at=now,
        )
        subscription.status = Subscription.Status.FROZEN
        if subscription.expires_at is not None:
            subscription.expires_at = subscription.expires_at + timedelta(days=freeze.days)
        subscription.save(update_fields=["status", "expires_at", "updated_at"])
    logger.info("freeze_approved", extra={"freeze_id": freeze_id, "club_id": club_id})
    return freeze


def reject_freeze(
    *,
    freeze_id: int,
    club_id: int,
    rejected_by_id: int,
    decision_reason: str = "",
) -> SubscriptionFreeze:
    with transaction.atomic():
        freeze = _get_freeze_for_decision(freeze_id=freeze_id, club_id=club_id)
        freeze.status = SubscriptionFreeze.FreezeStatus.REJECTED
        freeze.rejected_by_id = rejected_by_id
        freeze.approved_by_id = None
        freeze.decision_at = timezone.now()
        freeze.decision_reason = decision_reason.strip()
        freeze.save(
            update_fields=[
                "status",
                "approved_by",
                "rejected_by",
                "decision_at",
                "decision_reason",
                "updated_at",
            ]
        )
    logger.info("freeze_rejected", extra={"freeze_id": freeze_id, "club_id": club_id})
    return freeze


def unfreeze_subscription(*, freeze_id: int, club_id: int) -> SubscriptionFreeze:
    with transaction.atomic():
        freeze = SubscriptionFreeze.objects.for_club(club_id).get(id=freeze_id)
        subscription = Subscription.objects.for_club(club_id).select_for_update().get(id=freeze.subscription_id)

        if freeze.ends_at is not None:
            raise BusinessLogicError("This freeze has already ended", code="freeze_already_ended")

        if freeze.status != SubscriptionFreeze.FreezeStatus.APPROVED:
            raise BusinessLogicError("Only approved freezes can be ended", code="freeze_not_approved")

        now = timezone.now()
        actual_days = max((now - freeze.starts_at).days, 1)
        original_days = freeze.days

        freeze.ends_at = now
        freeze.days = actual_days
        freeze.save(update_fields=["ends_at", "days", "updated_at"])

        if actual_days < original_days and subscription.expires_at is not None:
            subscription.expires_at = subscription.expires_at - timedelta(days=original_days - actual_days)

        subscription.status = _subscription_status_after_unfreeze(subscription, now=now)
        subscription.save(update_fields=["status", "expires_at", "updated_at"])

    logger.info(
        "subscription_unfrozen",
        extra={"freeze_id": freeze_id, "club_id": club_id, "actual_days": actual_days},
    )
    return freeze
