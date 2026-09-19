"""Owner/admin balance and expiry corrections with immutable command receipts."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    Debt,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    SubscriptionCorrection,
    SubscriptionFreeze,
    SubscriptionRenewalEvent,
    TariffComponent,
)
from apps.billing.service_modules.entitlements import refresh_subscription_counters, subscription_component_counters
from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings
from apps.clubs.models import Club, ClubMembership
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student


def _fail(message, code):
    raise BusinessLogicError(message, code=code)


def _authorize(*, club_id, actor_user_id):
    if not ClubMembership.objects.filter(
        club_id=club_id,
        user_id=actor_user_id,
        is_active=True,
        user__is_active=True,
        role__in=[ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN],
    ).exists():
        _fail("Действие доступно владельцу или администратору клуба.", "actor_not_authorized")


def _json(value):
    def serialize(item):
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat()
        return str(item)

    return json.loads(json.dumps(value, default=serialize, sort_keys=True))


def _digest(value):
    return sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(subscription, components):
    return _json(
        {
            "status": subscription.status,
            "expires_at": subscription.expires_at,
            "trainings_left": subscription.trainings_left,
            "trainings_used": subscription.trainings_used,
            "components": [
                {
                    "id": c.id,
                    "credits_total": c.credits_total,
                    "credits_left": c.credits_left,
                    "credits_used": c.credits_used,
                    "is_active": c.is_active,
                    "unit_amount_basis_snapshot": c.unit_amount_basis_snapshot,
                }
                for c in components
            ],
        }
    )


def _locked_scope(*, club_id, subscription_id):
    # NO KEY UPDATE still serializes Club-fenced commands, but permits a
    # subscription-locked freeze writer to commit its deferred Club FK check.
    # FOR UPDATE here would deadlock against that existing owner.
    Club.objects.select_for_update(no_key=True).get(id=club_id)
    target = Subscription.objects.for_club(club_id).filter(id=subscription_id, deleted_at__isnull=True).first()
    if target is None:
        _fail("Абонемент недоступен.", "target_not_available")
    Student.objects.for_club(club_id).select_for_update().get(id=target.student_id)
    list(Payment.objects.for_club(club_id).select_for_update().filter(subscription=target).order_by("id"))
    subscription = Subscription.objects.for_club(club_id).select_for_update().get(id=target.id)
    components = list(
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update()
        .filter(
            subscription=subscription,
        )
        .order_by("id")
    )
    return subscription, components


def _dependencies(subscription):
    from apps.attendance.models import PersonalBookingPaymentReservation, TrainingGroupMembership
    from apps.attendance.services.enrollment import _active_personal_entitlement_reservations

    club_id = subscription.club_id
    if _active_personal_entitlement_reservations(subscription=subscription).exists():
        _fail("Сначала завершите или отмените связанные брони занятий.", "correction_reservation_conflict")
    if subscription.deleted_at or subscription.status not in [Subscription.Status.ACTIVE, Subscription.Status.EXPIRED]:
        _fail("Сначала завершите оплату, отмену или заморозку абонемента.", "correction_subscription_unavailable")
    freezes = list(
        SubscriptionFreeze.objects.for_club(club_id)
        .filter(subscription=subscription)
        .order_by("id")
        .values(
            "id",
            "status",
            "starts_at",
            "ends_at",
            "updated_at",
        )
    )
    if any(
        f["status"] == "pending"
        or (f["status"] == "approved" and (f["ends_at"] is None or f["ends_at"] > timezone.now()))
        for f in freezes
    ):
        _fail("Сначала завершите заморозку и откройте форму заново.", "correction_freeze_conflict")
    if SubscriptionRenewalEvent.objects.for_club(club_id).filter(renewed_from=subscription).exists():
        _fail("Этот абонемент уже продлён. Откройте действующий пакет.", "correction_non_leaf")
    if (
        Subscription.objects.for_club(club_id)
        .filter(renewed_from=subscription, deleted_at__isnull=True)
        .exclude(
            status=Subscription.Status.CANCELLED,
        )
        .exists()
    ):
        _fail("Сначала завершите связанное продление.", "correction_renewal_conflict")
    payments = Payment.objects.for_club(club_id).filter(subscription=subscription)
    if (
        payments.filter(status=Payment.Status.PENDING).exists()
        or Debt.objects.for_club(club_id)
        .filter(
            settlement_payment__in=payments,
            resolved_at__isnull=True,
        )
        .exists()
    ):
        _fail("Сначала завершите связанную оплату или зачёт долга.", "correction_settlement_conflict")
    if (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(
            subscription=subscription,
            status__in=["pending_payment", "manual_review"],
        )
        .exists()
    ):
        _fail("Сначала завершите резерв оплаты персонального занятия.", "correction_reservation_conflict")
    if (
        PaymentRefundCase.objects.for_club(club_id)
        .filter(order__payment__in=payments)
        .exclude(status="resolved")
        .exists()
    ):
        _fail("Сначала завершите возврат.", "correction_refund_conflict")
    refunds = list(
        PaymentRefund.objects.for_club(club_id)
        .filter(subscription=subscription)
        .order_by("id")
        .values(
            "id",
            "status",
            "entitlement_disposition",
            "updated_at",
        )
    )
    if any(r["status"] != "completed" or r["entitlement_disposition"] == "revoke_remaining" for r in refunds):
        _fail("Возврат ограничивает исправление этого абонемента.", "correction_refund_conflict")
    # Membership is separate authority: retained in preview, never revived here.
    memberships = list(
        TrainingGroupMembership.objects.for_club(club_id)
        .filter(
            student_id=subscription.student_id,
        )
        .order_by("id")
        .values("id", "training_group_id", "status", "starts_on", "ends_on", "updated_at")
    )
    return {"freezes": freezes, "refunds": refunds, "memberships": memberships}


@dataclass(frozen=True)
class SubscriptionCorrectionPreview:
    fingerprint: str
    before: dict
    after: dict
    balance_delta: int
    component_id: int | None
    dependencies: dict


def _state_fingerprint(*, subscription, components, before, dependencies):
    return _digest({
        "subscription_id": subscription.id,
        "before": before,
        "dependencies": dependencies,
        "updated_at": subscription.updated_at,
        "components_updated": [(c.id, c.updated_at) for c in components],
        "corrections": list(SubscriptionCorrection.objects.for_club(subscription.club_id).filter(
            subscription=subscription,
        ).values_list("id", flat=True)),
    })


@transaction.atomic
def get_subscription_correction_state(*, club_id, actor_user_id, subscription_id):
    """Open a short correction form without proposing a fake balance change."""
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    subscription, components = _locked_scope(club_id=club_id, subscription_id=subscription_id)
    dependencies = _dependencies(subscription)
    before = _snapshot(subscription, components)
    return {
        "before": before,
        "fingerprint": _state_fingerprint(
            subscription=subscription, components=components, before=before, dependencies=dependencies,
        ),
    }


def _preview(*, subscription, components, component_id, desired_remaining, desired_expires_on, reverses_id):
    dependencies = _dependencies(subscription)
    before = _snapshot(subscription, components)
    selected = next((c for c in components if c.id == component_id), None)
    if component_id is not None and selected is None:
        _fail("Компонент недоступен.", "target_not_available")
    expiry = subscription.expires_at
    original = None
    if reverses_id is not None:
        if desired_remaining is not None or desired_expires_on is not None:
            _fail("Компенсация принимает только исходное исправление.", "invalid_correction")
        original = (
            SubscriptionCorrection.objects.for_club(subscription.club_id)
            .filter(
                id=reverses_id,
                subscription=subscription,
                component_id=component_id,
            )
            .first()
        )
        if original is None:
            _fail("Исходное исправление недоступно.", "target_not_available")
        if SubscriptionCorrection.objects.for_club(subscription.club_id).filter(reverses=original).exists():
            _fail("Исправление уже компенсировано.", "correction_already_reversed")
        if original.balance_delta:
            desired_remaining = selected.credits_left - original.balance_delta if selected else None
        if original.before["expires_at"] != original.after["expires_at"]:
            later = SubscriptionCorrection.objects.for_club(subscription.club_id).filter(
                subscription=subscription,
                id__gt=original.id,
            )
            if (
                before["expires_at"] != original.after["expires_at"]
                or any(row.before["expires_at"] != row.after["expires_at"] for row in later)
                or any(
                    f["updated_at"] > original.created_at
                    for f in SubscriptionFreeze.objects.for_club(
                        subscription.club_id,
                    )
                    .filter(subscription=subscription)
                    .values("updated_at")
                )
            ):
                _fail("После исправления менялся срок. Нужна новая сверка.", "correction_expiry_dependency")
            expiry = datetime.fromisoformat(original.before["expires_at"]) if original.before["expires_at"] else None
    elif desired_expires_on is not None:
        if type(desired_expires_on) is not date or desired_expires_on == date.max:
            _fail("Укажите дату окончания.", "invalid_correction_expiry")
        expiry = timezone.make_aware(
            datetime.combine(desired_expires_on + timedelta(days=1), time.min), club_zoneinfo(subscription.club)
        )

    delta = 0
    if desired_remaining is not None:
        if type(desired_remaining) is not int or not 0 <= desired_remaining <= 2147483647:
            _fail("Остаток должен быть неотрицательным целым числом.", "invalid_correction_remaining")
        if selected is None or not selected.is_active:
            _fail("Нужно сверить точный компонент и исходный лимит.", "correction_component_required")
        if selected.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS:
            _fail("У безлимитного или недельного пакета нельзя менять остаток.", "correction_non_finite")
        findings = subscription_balance_findings(subscription=subscription, components=components)
        # A proven component can repair the known compatibility projection gap;
        # an unexplained component balance must never become a new baseline.
        if any(f["code"] != "aggregate_balance_mismatch" for f in findings):
            _fail("Остаток не согласован с покупкой и переносом. Нужна сверка.", "correction_balance_needs_review")
        delta = desired_remaining - selected.credits_left
        selected.credits_left = desired_remaining
    subscription.expires_at = expiry
    if components:
        subscription.trainings_left, subscription.trainings_used = subscription_component_counters(
            subscription=subscription, components=components,
        )
    usable = subscription.trainings_left is None or subscription.trainings_left > 0
    subscription.status = (
        Subscription.Status.ACTIVE
        if usable and (expiry is None or expiry > timezone.now())
        else Subscription.Status.EXPIRED
    )
    after = _snapshot(subscription, components)
    if before == after:
        _fail("Нет изменений для сохранения.", "correction_no_change")
    fingerprint = _state_fingerprint(
        subscription=subscription, components=components, before=before, dependencies=dependencies,
    )
    return SubscriptionCorrectionPreview(fingerprint, before, after, delta, component_id, _json(dependencies))


@transaction.atomic
def preview_subscription_correction(
    *,
    club_id,
    actor_user_id,
    subscription_id,
    component_id=None,
    desired_remaining=None,
    desired_expires_on=None,
    reverses_id=None,
):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    subscription, components = _locked_scope(club_id=club_id, subscription_id=subscription_id)
    return _preview(
        subscription=subscription,
        components=components,
        component_id=component_id,
        desired_remaining=desired_remaining,
        desired_expires_on=desired_expires_on,
        reverses_id=reverses_id,
    )


@transaction.atomic
def correct_subscription(
    *,
    club_id,
    actor_user_id,
    subscription_id,
    reason,
    command_key,
    expected_fingerprint,
    channel="admin",
    component_id=None,
    desired_remaining=None,
    desired_expires_on=None,
    reverses_id=None,
):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        _fail("Укажите причину исправления.", "correction_reason_required")
    if not isinstance(command_key, str) or not command_key.strip() or len(command_key) > 120:
        _fail("Нужен ключ команды.", "invalid_command_key")
    if channel not in {"admin", "api", "assistant_cli", "test"}:
        _fail("Неизвестный канал исправления.", "invalid_correction_channel")
    Club.objects.select_for_update(no_key=True).get(id=club_id)
    payload = _digest(
        {
            "subscription_id": subscription_id,
            "component_id": component_id,
            "desired_remaining": desired_remaining,
            "desired_expires_on": desired_expires_on,
            "reverses_id": reverses_id,
            "reason": reason.strip(),
        }
    )
    receipt = SubscriptionCorrection.objects.for_club(club_id).filter(command_key=command_key).first()
    if receipt:
        if receipt.payload_fingerprint != payload:
            _fail("Ключ уже использован для другого исправления.", "idempotency_conflict")
        return receipt
    # Accepted results and exact compensations remain drainable with new intent off.
    if not settings.STUDENT_ADMIN_CORRECTIONS_ENABLED and reverses_id is None:
        _fail("Новые исправления пока выключены.", "student_corrections_disabled")
    subscription, components = _locked_scope(club_id=club_id, subscription_id=subscription_id)
    preview = _preview(
        subscription=subscription,
        components=components,
        component_id=component_id,
        desired_remaining=desired_remaining,
        desired_expires_on=desired_expires_on,
        reverses_id=reverses_id,
    )
    if preview.fingerprint != expected_fingerprint:
        _fail("После открытия формы данные изменились. Проверьте исправление ещё раз.", "correction_stale_preview")
    if component_id is not None and preview.balance_delta:
        selected = next(c for c in components if c.id == component_id)
        selected.save(update_fields=["credits_left", "updated_at"])
    refresh_subscription_counters(subscription=subscription, components=components)
    subscription.save(update_fields=["expires_at", "status", "updated_at"])
    receipt = SubscriptionCorrection(
        club_id=club_id,
        subscription=subscription,
        component_id=component_id,
        actor_id=actor_user_id,
        channel=channel,
        reason=reason.strip(),
        command_key=command_key,
        payload_fingerprint=payload,
        expected_fingerprint=expected_fingerprint,
        balance_delta=preview.balance_delta,
        before=preview.before,
        after=preview.after,
        reverses_id=reverses_id,
    )
    receipt.full_clean()
    receipt.save()
    return receipt
