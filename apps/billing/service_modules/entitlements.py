from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.billing.models import (
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.selectors import current_active_subscription_q
from apps.billing.service_modules._shared import _money
from apps.billing.service_modules.tariff_components import _ensure_tariff_components
from apps.common.exceptions import BusinessLogicError


def _component_unit_basis(
    *,
    paid_amount_basis: Decimal,
    credits_total: int | None,
) -> Decimal | None:
    if not credits_total:
        return None
    return _money(paid_amount_basis / Decimal(credits_total))


def _allocate_component_paid_amounts(
    *,
    components: list[TariffComponent],
    paid_amount: Decimal,
) -> list[Decimal]:
    paid_amount = _money(paid_amount)
    if not components:
        return []
    if len(components) == 1:
        return [paid_amount]

    component_total = _money(
        sum(
            (component.paid_amount_basis for component in components),
            Decimal("0.00"),
        )
    )
    if component_total <= 0:
        raise BusinessLogicError(
            "Сумма компонентов должна быть больше нуля",
            code="invalid_component_total",
        )

    allocated = Decimal("0.00")
    amounts: list[Decimal] = []
    for component in components[:-1]:
        amount = _money(paid_amount * component.paid_amount_basis / component_total)
        amounts.append(amount)
        allocated += amount
    amounts.append(_money(paid_amount - allocated))

    if any(amount <= 0 for amount in amounts):
        raise BusinessLogicError(
            "Скидка не может обнулить стоимость компонента пакета",
            code="component_discount_allocation_required",
        )
    return amounts


def _create_subscription_components(
    *,
    subscription: Subscription,
    club_id: int,
    paid_amount: Decimal,
) -> list[SubscriptionComponent]:
    tariff = subscription.tariff
    components = _ensure_tariff_components(tariff, club_id=club_id)
    paid_amounts = _allocate_component_paid_amounts(
        components=components,
        paid_amount=paid_amount,
    )

    created: list[SubscriptionComponent] = []
    for component, paid_amount_basis in zip(components, paid_amounts, strict=True):
        credits_total = component.credits_total
        sub_component = SubscriptionComponent.objects.create(
            club_id=club_id,
            subscription=subscription,
            tariff_component=component,
            name_snapshot=component.name,
            training_type=component.training_type,
            entitlement_kind=component.entitlement_kind,
            credits_total=credits_total,
            credits_left=(
                credits_total
                if component.entitlement_kind
                == TariffComponent.EntitlementKind.FINITE_CREDITS
                else None
            ),
            weekly_limit=component.weekly_limit,
            scope=component.scope,
            location=component.location,
            trainer_payout_policy_snapshot=component.trainer_payout_policy,
            paid_amount_basis_snapshot=paid_amount_basis,
            unit_amount_basis_snapshot=_component_unit_basis(
                paid_amount_basis=paid_amount_basis,
                credits_total=credits_total,
            ),
        )
        created.append(sub_component)
    refresh_subscription_counters(subscription=subscription, components=created)
    return created


def component_aggregate_counters(*, components):
    """Active components own capacity; non-finite capacity has no numeric sum."""
    active = [component for component in components if component.is_active]
    left = (
        None
        if any(component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS for component in active)
        else sum(component.credits_left or 0 for component in active)
    )
    # Inactive component visits remain historical consumption.
    return left, sum(component.credits_used for component in components)


def subscription_component_counters(*, subscription, components):
    left, used = component_aggregate_counters(components=components)
    if subscription.renewed_from_id and left is not None:
        from apps.billing.models import SubscriptionRenewalEvent

        event = SubscriptionRenewalEvent.objects.for_club(subscription.club_id).filter(renewed_to=subscription).first()
        if event:
            # Preserve pre-journal scalar carry until explicit reconciliation
            # can prove its component destination. The audit reports that gap.
            left += event.carry_snapshot.get("legacy_finite_credits", 0)
    return left, used


def refresh_subscription_counters(*, subscription, components=None):
    """Refresh the compatibility projection while the caller holds the subscription lock.

    Never infer consumption from capacity, or rewrite purchased/unit snapshots.
    Component-less legacy subscriptions retain their existing counters.
    """
    if components is None:
        components = list(SubscriptionComponent.objects.for_club(subscription.club_id).filter(
            subscription=subscription,
        ).order_by("id"))
    if not components:
        return
    subscription.trainings_left, subscription.trainings_used = subscription_component_counters(
        subscription=subscription, components=components,
    )
    subscription.save(update_fields=["trainings_left", "trainings_used", "updated_at"])


def _subscription_component_has_weekly_capacity(
    *,
    component: SubscriptionComponent,
    checkin_date: date,
) -> bool:
    if not component.weekly_limit:
        return True
    from apps.attendance.models import Checkin

    week_start = checkin_date - timedelta(days=checkin_date.weekday())
    week_end = week_start + timedelta(days=6)
    used_this_week = Checkin.objects.for_club(component.club_id).filter(
        subscription_component=component,
        date__gte=week_start,
        date__lte=week_end,
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    ).count()
    return used_this_week < component.weekly_limit


def _find_subscription_component_for_checkin(
    *,
    subscription: Subscription,
    club_id: int,
    checkin,
) -> SubscriptionComponent | None:
    components = (
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update()
        .filter(
            subscription=subscription,
            training_type_id=checkin.training_type_id,
            is_active=True,
        )
        .filter(Q(credits_left__isnull=True) | Q(credits_left__gt=0))
        .order_by("id")
    )
    for scoped_components in (
        components.filter(
            scope=Tariff.Scope.LOCATION,
            location_id=checkin.location_id,
        ),
        components.filter(scope=Tariff.Scope.CLUB),
    ):
        for component in scoped_components:
            if _subscription_component_has_weekly_capacity(
                component=component,
                checkin_date=checkin.date,
            ):
                return component
    return None


def _deduct_subscription_component_for_checkin(
    *,
    component: SubscriptionComponent | None,
) -> None:
    if component is None:
        return
    if component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
        if component.credits_left is None or component.credits_left <= 0:
            raise BusinessLogicError(
                "В компоненте абонемента закончились посещения",
                code="subscription_component_credits_exhausted",
            )
        component.credits_left -= 1
    component.credits_used += 1
    component.save(update_fields=["credits_left", "credits_used", "updated_at"])


def _resolve_package_owner_trainer_id_for_sale(
    *,
    training_type_kind: str,
    seller_trainer_id: int | None,
    package_owner_trainer_id: int | None,
    allow_seller_fallback: bool = False,
) -> int | None:
    if training_type_kind == TrainingType.Kind.GROUP:
        return None

    owner_trainer_id = package_owner_trainer_id
    if owner_trainer_id is None and allow_seller_fallback:
        owner_trainer_id = seller_trainer_id
    if owner_trainer_id is None:
        raise BusinessLogicError(
            "Для персонального или мини-группового пакета укажите владельца пакета",
            code="package_owner_trainer_required",
        )
    return owner_trainer_id


def _requires_package_owner_for_components(
    components: list[TariffComponent],
) -> bool:
    return any(
        component.training_type.kind
        in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}
        for component in components
    )


def _resolve_package_owner_trainer_id_for_components(
    *,
    components: list[TariffComponent],
    seller_trainer_id: int | None,
    package_owner_trainer_id: int | None,
    allow_seller_fallback: bool = False,
) -> int | None:
    if not _requires_package_owner_for_components(components):
        return None

    owner_trainer_id = package_owner_trainer_id
    if owner_trainer_id is None and allow_seller_fallback:
        owner_trainer_id = seller_trainer_id
    if owner_trainer_id is None:
        raise BusinessLogicError(
            "Для персонального или мини-группового компонента укажите владельца пакета",
            code="package_owner_trainer_required",
        )
    return owner_trainer_id


def _student_has_current_subscription(
    *,
    club_id: int,
    student_id: int,
    training_type_id: int,
    scope: str,
    location_id: int | None,
) -> bool:
    return (
        Subscription.objects.for_club(club_id)
        .select_for_update()
        .filter(
            student_id=student_id,
            tariff__training_type_id=training_type_id,
            scope=scope,
            location_id=location_id,
            deleted_at__isnull=True,
        )
        .filter(
            current_active_subscription_q()
            | Q(status=Subscription.Status.PENDING)
        )
        .exists()
    )


def _student_has_current_component_subscription(
    *,
    club_id: int,
    student_id: int,
    components: list[TariffComponent],
    effective_at=None,
) -> bool:
    if not components:
        return False
    scope_filter = Q()
    for component in components:
        component_filter = Q(training_type_id=component.training_type_id)
        if component.scope == Tariff.Scope.LOCATION:
            component_filter &= Q(scope=Tariff.Scope.CLUB) | Q(
                scope=Tariff.Scope.LOCATION,
                location_id=component.location_id,
            )
        scope_filter |= component_filter

    if not scope_filter:
        return False

    component_match_exists = (
        SubscriptionComponent.objects.for_club(club_id)
        .select_for_update()
        .filter(
            subscription__student_id=student_id,
            subscription__deleted_at__isnull=True,
            is_active=True,
        )
        .filter(scope_filter)
        .filter(
            ~Q(entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS)
            | Q(credits_left__gt=0)
        )
        .filter(
            Q(subscription__trainings_left__isnull=True)
            | Q(subscription__trainings_left__gt=0)
        )
        .filter(
            Q(subscription__status=Subscription.Status.PENDING)
            | Q(
                subscription__status=Subscription.Status.ACTIVE,
                subscription__expires_at__gt=effective_at or timezone.now(),
            )
            | Q(
                subscription__status=Subscription.Status.ACTIVE,
                subscription__expires_at__isnull=True,
            )
        )
        .exists()
    )
    if component_match_exists:
        return True

    legacy_scope_filter = Q()
    for component in components:
        legacy_component_filter = Q(
            tariff__training_type_id=component.training_type_id
        )
        if component.scope == Tariff.Scope.LOCATION:
            legacy_component_filter &= Q(scope=Tariff.Scope.CLUB) | Q(
                scope=Tariff.Scope.LOCATION,
                location_id=component.location_id,
            )
        legacy_scope_filter |= legacy_component_filter

    if not legacy_scope_filter:
        return False

    subscriptions_with_components = SubscriptionComponent.objects.for_club(
        club_id
    ).values("subscription_id")
    return (
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            student_id=student_id,
            deleted_at__isnull=True,
        )
        .exclude(id__in=subscriptions_with_components)
        .filter(legacy_scope_filter)
        .filter(
            current_active_subscription_q(effective_at)
            | Q(status=Subscription.Status.PENDING)
        )
        .exists()
    )
