"""Entitlement lifecycle shared by every attendance cancellation adapter."""
from django.utils import timezone

from apps.billing.models import PaymentRefund, PaymentRefundCase, Subscription, SubscriptionRenewalEvent
from apps.common.exceptions import BusinessLogicError


def assert_checkin_cancellation_entitlement(*, subscription, component=None):
    """A returned visit belongs only to its exact, non-revoked leaf source.

    The caller holds attendance identity and financial locks. Never transfer a
    returned credit into the successor, or undo an unrelated refund lifecycle.
    """
    club_id = subscription.club_id
    if SubscriptionRenewalEvent.objects.for_club(club_id).filter(renewed_from=subscription).exists():
        raise BusinessLogicError(
            "Абонемент посещения уже продлён. Нужна отдельная сверка переноса.",
            code="checkin_cancellation_non_leaf",
        )
    if Subscription.objects.for_club(club_id).filter(
        renewed_from=subscription, deleted_at__isnull=True,
    ).exclude(status=Subscription.Status.CANCELLED).exists():
        raise BusinessLogicError(
            "Сначала завершите или отмените ожидающее продление.",
            code="checkin_cancellation_renewal_pending",
        )
    if subscription.deleted_at is not None or subscription.status == Subscription.Status.CANCELLED:
        raise BusinessLogicError(
            "Право по этому абонементу отменено. Нужна сверка связанной операции.",
            code="checkin_cancellation_subscription_revoked",
        )
    if component is not None and not component.is_active:
        raise BusinessLogicError(
            "Компонент посещения больше не действует. Нужна сверка.",
            code="checkin_cancellation_subscription_revoked",
        )
    if PaymentRefundCase.objects.for_club(club_id).filter(
        order__payment__subscription=subscription,
    ).exclude(status="resolved").exists() or PaymentRefund.objects.for_club(club_id).filter(
        subscription=subscription,
    ).exclude(status="completed").exists() or PaymentRefund.objects.for_club(club_id).filter(
        subscription=subscription, entitlement_disposition="revoke_remaining",
    ).exists():
        raise BusinessLogicError(
            "Возврат ограничивает отмену посещения. Завершите сверку возврата.",
            code="checkin_cancellation_refund_conflict",
        )


def refresh_subscription_status_after_cancellation(*, subscription):
    """Only usable, in-date ACTIVE/EXPIRED leaves can become ACTIVE again."""
    if subscription.status not in {Subscription.Status.ACTIVE, Subscription.Status.EXPIRED}:
        return
    has_capacity = subscription.trainings_left is None or subscription.trainings_left > 0
    in_date = subscription.expires_at is None or subscription.expires_at > timezone.now()
    subscription.status = Subscription.Status.ACTIVE if has_capacity and in_date else Subscription.Status.EXPIRED
