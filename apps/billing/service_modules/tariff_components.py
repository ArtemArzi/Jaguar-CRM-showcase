from __future__ import annotations

from decimal import Decimal

from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.billing.service_modules._shared import _money, _validate_positive_money
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError


def _default_payout_policy_for_kind(training_type_kind: str) -> str:
    if training_type_kind == TrainingType.Kind.GROUP:
        return Tariff.PayoutPolicy.ON_PAYMENT
    if training_type_kind in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}:
        return Tariff.PayoutPolicy.ON_CHECKIN
    return Tariff.PayoutPolicy.NONE


def _validate_payout_policy(policy: str) -> str:
    if policy not in Tariff.PayoutPolicy.values:
        raise BusinessLogicError("Некорректная политика выплаты", code="invalid_payout_policy")
    return policy


def _resolve_tariff_payout_policy(tariff: Tariff) -> str:
    if tariff.trainer_payout_policy:
        return _validate_payout_policy(tariff.trainer_payout_policy)
    return _default_payout_policy_for_kind(tariff.training_type.kind)


def _component_from_tariff_defaults(tariff: Tariff) -> dict:
    return {
        "name": tariff.name,
        "training_type_id": tariff.training_type_id,
        "entitlement_kind": (
            TariffComponent.EntitlementKind.FINITE_CREDITS
            if tariff.trainings_limit is not None
            else TariffComponent.EntitlementKind.UNLIMITED
        ),
        "credits_total": tariff.trainings_limit,
        "weekly_limit": None,
        "scope": tariff.scope,
        "location_id": tariff.location_id,
        "trainer_payout_policy": _resolve_tariff_payout_policy(tariff),
        "paid_amount_basis": tariff.price,
    }


def _validate_component_payloads(
    *,
    club_id: int,
    tariff_price: Decimal,
    components: list[dict],
) -> list[dict]:
    if not components:
        raise BusinessLogicError("Добавьте хотя бы один компонент пакета", code="package_components_required")

    normalized: list[dict] = []
    total = Decimal("0.00")
    for index, component in enumerate(components):
        training_type_id = int(component["training_type_id"])
        training_type = TrainingType.objects.for_club(club_id).filter(id=training_type_id).first()
        if training_type is None:
            raise BusinessLogicError("Тип тренировки не найден", code="training_type_not_found")

        paid_amount_basis = _money(Decimal(str(component["paid_amount_basis"])))
        _validate_positive_money(paid_amount_basis)
        total += paid_amount_basis

        entitlement_kind = component.get("entitlement_kind") or TariffComponent.EntitlementKind.FINITE_CREDITS
        credits_total = component.get("credits_total")
        weekly_limit = component.get("weekly_limit")
        if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS and not credits_total:
            raise BusinessLogicError(
                "Укажите количество посещений для компонента",
                code="component_credits_required",
            )
        if entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT and not weekly_limit:
            raise BusinessLogicError(
                "Укажите недельный лимит для компонента",
                code="component_weekly_limit_required",
            )

        scope = component.get("scope") or Tariff.Scope.CLUB
        location_id = component.get("location_id")
        location = None
        if scope not in {Tariff.Scope.CLUB, Tariff.Scope.LOCATION}:
            raise BusinessLogicError("Некорректная область действия компонента", code="invalid_component_scope")
        if scope == Tariff.Scope.LOCATION:
            if not location_id:
                raise BusinessLogicError("Локация обязательна для компонента", code="location_required")
            location = Location.objects.filter(id=location_id, club_id=club_id).first()
            if location is None:
                raise BusinessLogicError("Локация не найдена", code="location_not_found")

        payout_policy = (
            component.get("trainer_payout_policy")
            or _default_payout_policy_for_kind(training_type.kind)
        )
        payout_policy = _validate_payout_policy(payout_policy)

        normalized.append(
            {
                "name": component.get("name", ""),
                "training_type_id": training_type_id,
                "entitlement_kind": entitlement_kind,
                "credits_total": credits_total,
                "weekly_limit": weekly_limit,
                "scope": scope,
                "location": location,
                "trainer_payout_policy": payout_policy,
                "paid_amount_basis": paid_amount_basis,
                "sort_order": index,
            }
        )

    if _money(total) != _money(tariff_price):
        raise BusinessLogicError(
            "Сумма компонентов должна совпадать с ценой пакета",
            code="component_amount_sum_mismatch",
        )
    return normalized


def _replace_tariff_components(
    *,
    tariff: Tariff,
    club_id: int,
    components: list[dict],
) -> None:
    normalized = _validate_component_payloads(
        club_id=club_id,
        tariff_price=tariff.price,
        components=components,
    )
    TariffComponent.objects.for_club(club_id).filter(tariff=tariff, is_active=True).update(is_active=False)
    for component in normalized:
        TariffComponent.objects.create(
            club_id=club_id,
            tariff=tariff,
            name=component["name"],
            training_type_id=component["training_type_id"],
            entitlement_kind=component["entitlement_kind"],
            credits_total=component["credits_total"],
            weekly_limit=component["weekly_limit"],
            scope=component["scope"],
            location=component["location"],
            trainer_payout_policy=component["trainer_payout_policy"],
            paid_amount_basis=component["paid_amount_basis"],
            sort_order=component["sort_order"],
        )


def _ensure_tariff_components(tariff: Tariff, *, club_id: int) -> list[TariffComponent]:
    components = list(
        TariffComponent.objects.for_club(club_id)
        .filter(tariff=tariff, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )
    if components:
        return components

    _replace_tariff_components(
        tariff=tariff,
        club_id=club_id,
        components=[_component_from_tariff_defaults(tariff)],
    )
    return list(
        TariffComponent.objects.for_club(club_id)
        .filter(tariff=tariff, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )


def _active_tariff_components(tariff: Tariff, *, club_id: int) -> list[TariffComponent]:
    return list(
        TariffComponent.objects.for_club(club_id)
        .filter(tariff=tariff, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )


def _payment_preflight_tariff_components(
    tariff: Tariff,
    *,
    club_id: int,
) -> list[TariffComponent]:
    """Return component contracts for validation without backfilling durable rows."""
    components = _active_tariff_components(tariff, club_id=club_id)
    if components:
        return components

    normalized = _validate_component_payloads(
        club_id=club_id,
        tariff_price=tariff.price,
        components=[_component_from_tariff_defaults(tariff)],
    )[0]
    return [
        TariffComponent(
            club_id=club_id,
            tariff=tariff,
            name=normalized["name"],
            training_type=tariff.training_type,
            entitlement_kind=normalized["entitlement_kind"],
            credits_total=normalized["credits_total"],
            weekly_limit=normalized["weekly_limit"],
            scope=normalized["scope"],
            location=normalized["location"],
            trainer_payout_policy=normalized["trainer_payout_policy"],
            paid_amount_basis=normalized["paid_amount_basis"],
            sort_order=normalized["sort_order"],
        )
    ]


def _tariff_component_contracts_equal(
    *,
    tariff: Tariff,
    club_id: int,
    components: list[dict],
    include_display_fields: bool = False,
) -> bool:
    normalized = _validate_component_payloads(
        club_id=club_id,
        tariff_price=tariff.price,
        components=components,
    )
    existing = _active_tariff_components(tariff, club_id=club_id)
    if len(existing) != len(normalized):
        return False

    for existing_component, new_component in zip(existing, normalized, strict=True):
        if (
            existing_component.training_type_id != new_component["training_type_id"]
            or existing_component.entitlement_kind != new_component["entitlement_kind"]
            or existing_component.credits_total != new_component["credits_total"]
            or existing_component.weekly_limit != new_component["weekly_limit"]
            or existing_component.scope != new_component["scope"]
            or existing_component.location_id != (
                new_component["location"].id if new_component["location"] else None
            )
            or existing_component.trainer_payout_policy != new_component["trainer_payout_policy"]
            or _money(existing_component.paid_amount_basis) != _money(new_component["paid_amount_basis"])
        ):
            return False
        if include_display_fields and existing_component.name != new_component["name"]:
            return False
    return True


def _tariff_has_default_component(*, tariff: Tariff, club_id: int) -> bool:
    components = _active_tariff_components(tariff, club_id=club_id)
    if len(components) != 1:
        return False
    component = components[0]
    expected_entitlement = (
        TariffComponent.EntitlementKind.FINITE_CREDITS
        if tariff.trainings_limit is not None
        else TariffComponent.EntitlementKind.UNLIMITED
    )
    return (
        component.training_type_id == tariff.training_type_id
        and component.entitlement_kind == expected_entitlement
        and component.credits_total == tariff.trainings_limit
        and component.weekly_limit is None
        and component.scope == tariff.scope
        and component.location_id == tariff.location_id
        and component.trainer_payout_policy == _resolve_tariff_payout_policy(tariff)
        and _money(component.paid_amount_basis) == _money(tariff.price)
    )
