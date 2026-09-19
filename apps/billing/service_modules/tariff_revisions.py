"""Immutable compatible tariff versions for price-only catalogue changes."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.billing.models import (
    Tariff,
    TariffComponent,
    TariffPriceRevision,
    TrainingType,
)
from apps.billing.service_modules._shared import _money, _validate_positive_money
from apps.billing.service_modules.catalog import (
    _has_open_personal_drop_in_for_contract_change,
    _lock_catalog_trainers,
    _lock_catalog_training_type,
)
from apps.clubs.models import ClubMembership
from apps.common.exceptions import BusinessLogicError

_MAX_TARIFF_PRICE = Decimal("99999999.99")


def _fail(message: str, code: str) -> None:
    raise BusinessLogicError(message, code=code)


def _authorize(*, club_id: int, actor_user_id: int) -> None:
    if not ClubMembership.objects.filter(
        club_id=club_id,
        user_id=actor_user_id,
        user__is_active=True,
        is_active=True,
        role__in=[ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN],
    ).exists():
        _fail(
            "Действие доступно владельцу или администратору клуба.",
            "actor_not_authorized",
        )


def _normalize_price(value: Decimal) -> Decimal:
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BusinessLogicError("Некорректная цена", code="invalid_money_amount") from exc
    if not raw.is_finite():
        _fail("Некорректная цена", "invalid_money_amount")
    try:
        normalized = _money(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BusinessLogicError("Некорректная цена", code="invalid_money_amount") from exc
    if normalized > _MAX_TARIFF_PRICE:
        _fail("Цена превышает допустимый предел.", "invalid_money_amount")
    _validate_positive_money(normalized)
    return normalized


def _normalize_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        _fail("Укажите название тарифа.", "tariff_revision_name_required")
    if len(name) > 200:
        _fail("Название тарифа слишком длинное.", "tariff_revision_name_too_long")
    return name


def _payload_fingerprint(*, source_tariff_id: int, new_price: Decimal, new_name: str) -> str:
    payload = json.dumps(
        {
            "source_tariff_id": source_tariff_id,
            "new_name": new_name,
            "new_price": str(new_price),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode()).hexdigest()


def _clean_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        _fail("Укажите ключ идемпотентности.", "idempotency_key_required")
    if len(key) > 120:
        _fail("Ключ идемпотентности слишком длинный.", "idempotency_key_too_long")
    return key


def _active_components(*, club_id: int, tariff_id: int, lock: bool) -> list[TariffComponent]:
    queryset = (
        TariffComponent.objects.for_club(club_id)
        .filter(tariff_id=tariff_id, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return list(queryset)


def _validate_single_finite_component(
    *,
    source: Tariff,
    components: list[TariffComponent],
) -> TariffComponent:
    if source.trainings_limit is None or source.trainings_limit <= 0:
        _fail(
            "Изменение цены доступно только для конечного пакета.",
            "tariff_revision_shape_unsupported",
        )
    if len(components) != 1:
        _fail(
            "Изменение цены доступно только для тарифа с одним компонентом.",
            "tariff_revision_shape_unsupported",
        )
    component = components[0]
    if (
        component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS
        or component.credits_total != source.trainings_limit
        or component.training_type_id != source.training_type_id
        or component.scope != source.scope
        or component.location_id != source.location_id
        or component.paid_amount_basis != source.price
    ):
        _fail(
            "Контракт компонента не поддерживает изменение только цены.",
            "tariff_revision_shape_unsupported",
        )
    return component


def _historical_compatible_components(
    *,
    club_id: int,
    source: Tariff,
    active_component: TariffComponent,
) -> list[TariffComponent]:
    """Include prior same-tariff component ids used by accepted subscriptions.

    Component display names and paid amount bases are historical snapshots. A
    prior component can still carry a live subscription even when the active
    catalogue row was regenerated, so lineage maps those ids only when every
    non-money contract field still matches the source component.
    """
    candidates = list(
        TariffComponent.objects.for_club(club_id)
        .filter(tariff_id=source.id)
        .select_for_update(of=("self",))
        .order_by("id")
    )
    compatible = []
    for component in candidates:
        if (
            component.entitlement_kind == active_component.entitlement_kind
            and component.credits_total == active_component.credits_total
            and component.weekly_limit == active_component.weekly_limit
            and component.training_type_id == active_component.training_type_id
            and component.scope == active_component.scope
            and component.location_id == active_component.location_id
            and component.trainer_payout_policy == active_component.trainer_payout_policy
            and component.sort_order == active_component.sort_order
        ):
            compatible.append(component)
    return compatible


def _component_contract(component: TariffComponent) -> dict:
    return {
        "id": component.id,
        "name": component.name,
        "training_type_id": component.training_type_id,
        "entitlement_kind": component.entitlement_kind,
        "credits_total": component.credits_total,
        "weekly_limit": component.weekly_limit,
        "scope": component.scope,
        "location_id": component.location_id,
        "trainer_payout_policy": component.trainer_payout_policy,
        "paid_amount_basis": str(component.paid_amount_basis),
        "sort_order": component.sort_order,
    }


def _tariff_contract(*, tariff: Tariff, component: TariffComponent) -> dict:
    return {
        "id": tariff.id,
        "name": tariff.name,
        "price": str(tariff.price),
        "training_type_id": tariff.training_type_id,
        "trainings_limit": tariff.trainings_limit,
        "duration_days": tariff.duration_days,
        "scope": tariff.scope,
        "location_id": tariff.location_id,
        "is_active": tariff.is_active,
        "is_personal_booking_default": tariff.is_personal_booking_default,
        "personal_booking_trainer_id": tariff.personal_booking_trainer_id,
        "trainer_payout_policy": tariff.trainer_payout_policy,
        "component": _component_contract(component),
    }


def _validate_personal_default(*, source: Tariff) -> None:
    if not source.is_personal_booking_default:
        return
    if source.training_type.kind != TrainingType.Kind.PERSONAL:
        _fail(
            "Назначенный персональный тариф имеет неподдерживаемый тип тренировки.",
            "tariff_revision_personal_contract_invalid",
        )
    from apps.billing.service_modules.personal_offers import validate_personal_booking_default

    validate_personal_booking_default(tariff=source, lock=False)


def _check_personal_open_bookings(*, source: Tariff) -> None:
    if source.training_type.kind != TrainingType.Kind.PERSONAL:
        return
    if _has_open_personal_drop_in_for_contract_change(
        club_id=source.club_id,
        booking_filter=Q(tariff_id=source.id),
    ):
        _fail(
            "Нельзя менять персональный тариф, пока есть открытая разовая персоналка.",
            "personal_drop_in_contract_change_blocked",
        )
    if (
        source.is_personal_booking_default
        and source.personal_booking_trainer_id is None
        and _has_open_personal_drop_in_for_contract_change(
            club_id=source.club_id,
            booking_filter=Q(enrollment__schedule__training_type_id=source.training_type_id),
        )
    ):
        _fail(
            "Нельзя менять цену персональной тренировки, пока есть открытая разовая персоналка.",
            "personal_drop_in_contract_change_blocked",
        )


def _existing_receipt(*, club_id: int, idempotency_key: str) -> TariffPriceRevision | None:
    return (
        TariffPriceRevision.objects.for_club(club_id)
        .select_related("source_tariff", "target_tariff", "source_component", "target_component")
        .filter(idempotency_key=idempotency_key)
        .first()
    )


def _assert_replay_matches(
    *,
    revision: TariffPriceRevision,
    source_tariff_id: int,
    payload_fingerprint: str,
) -> TariffPriceRevision:
    if revision.source_tariff_id != source_tariff_id or revision.payload_fingerprint != payload_fingerprint:
        _fail(
            "Ключ идемпотентности уже использован для другого изменения цены.",
            "idempotency_key_conflict",
        )
    return revision


@transaction.atomic
def revise_tariff_price(
    *,
    club_id: int,
    source_tariff_id: int,
    new_price: Decimal,
    new_name: str,
    actor_user_id: int,
    idempotency_key: str,
) -> TariffPriceRevision:
    """Create one immutable successor tariff and retire the source from sales."""

    # Authorization deliberately precedes replay lookup.  An old receipt is
    # not a bearer token for a former staff member.
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    key = _clean_idempotency_key(idempotency_key)
    normalized_price = _normalize_price(new_price)
    normalized_name = _normalize_name(new_name)
    fingerprint = _payload_fingerprint(
        source_tariff_id=source_tariff_id,
        new_price=normalized_price,
        new_name=normalized_name,
    )

    existing = _existing_receipt(club_id=club_id, idempotency_key=key)
    if existing is not None:
        return _assert_replay_matches(
            revision=existing,
            source_tariff_id=source_tariff_id,
            payload_fingerprint=fingerprint,
        )

    source_probe = (
        Tariff.objects.for_club(club_id)
        .filter(id=source_tariff_id)
        .values("training_type_id")
        .first()
    )
    if source_probe is None:
        _fail("Тариф не найден.", "tariff_not_found")

    # Keep the same catalog lock prefix used by tariff updates and personal
    # offer acceptance: training type -> trainer -> tariff -> components.
    _lock_catalog_training_type(
        club_id=club_id,
        training_type_id=source_probe["training_type_id"],
    )
    current_scope = (
        Tariff.objects.for_club(club_id)
        .filter(id=source_tariff_id)
        .values("training_type_id", "personal_booking_trainer_id")
        .first()
    )
    if current_scope is None:
        _fail("Тариф не найден.", "tariff_not_found")
    if current_scope["training_type_id"] != source_probe["training_type_id"]:
        _fail("Контракт тарифа изменился во время блокировки.", "tariff_revision_source_changed")
    _lock_catalog_trainers(
        club_id=club_id,
        trainer_ids=[
            current_scope["personal_booking_trainer_id"]
        ]
        if current_scope["personal_booking_trainer_id"] is not None
        else [],
    )
    source = (
        Tariff.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("training_type", "location", "personal_booking_trainer")
        .filter(id=source_tariff_id)
        .first()
    )
    if source is None:
        _fail("Тариф не найден.", "tariff_not_found")

    # A retry can have arrived while the catalog lock was acquired.  Recheck
    # the receipt before requiring the source to remain a current leaf.
    existing = _existing_receipt(club_id=club_id, idempotency_key=key)
    if existing is not None:
        return _assert_replay_matches(
            revision=existing,
            source_tariff_id=source_tariff_id,
            payload_fingerprint=fingerprint,
        )

    if TariffPriceRevision.objects.for_club(club_id).filter(source_tariff_id=source.id).exists():
        _fail(
            "Исходная версия тарифа уже заменена.",
            "tariff_revision_source_not_current",
        )
    if not source.is_active:
        _fail("Нельзя менять неактивную версию тарифа.", "tariff_revision_source_inactive")
    if normalized_price == _money(source.price) and normalized_name == source.name:
        _fail("Новая версия тарифа должна изменить цену или название.", "tariff_revision_no_change")

    components = _active_components(club_id=club_id, tariff_id=source.id, lock=True)
    source_component = _validate_single_finite_component(source=source, components=components)
    _validate_personal_default(source=source)
    _check_personal_open_bookings(source=source)

    source_contract = _tariff_contract(tariff=source, component=source_component)
    was_personal_default = source.is_personal_booking_default
    if was_personal_default:
        # The source is still unsealed until the receipt is written, so the
        # existing designation transfer rules can be performed atomically.
        source.is_active = False
        source.is_personal_booking_default = False
        source.save(update_fields=["is_active", "is_personal_booking_default", "updated_at"])
    else:
        source.is_active = False
        source.save(update_fields=["is_active", "updated_at"])

    training_type = source.training_type
    if was_personal_default and source.personal_booking_trainer_id is None:
        training_type.drop_in_price = normalized_price
        training_type.save(update_fields=["drop_in_price", "updated_at"])

    target = Tariff.objects.create(
        club_id=club_id,
        name=normalized_name,
        training_type_id=source.training_type_id,
        price=normalized_price,
        trainings_limit=source.trainings_limit,
        duration_days=source.duration_days,
        scope=source.scope,
        location_id=source.location_id,
        is_active=True,
        is_personal_booking_default=was_personal_default,
        personal_booking_trainer_id=source.personal_booking_trainer_id,
        description=source.description,
        trainer_payout_policy=source.trainer_payout_policy,
    )
    target_component = TariffComponent.objects.create(
        club_id=club_id,
        tariff=target,
        name=normalized_name,
        training_type_id=source_component.training_type_id,
        entitlement_kind=source_component.entitlement_kind,
        credits_total=source_component.credits_total,
        weekly_limit=source_component.weekly_limit,
        scope=source_component.scope,
        location_id=source_component.location_id,
        trainer_payout_policy=source_component.trainer_payout_policy,
        paid_amount_basis=normalized_price,
        sort_order=source_component.sort_order,
        is_active=True,
    )
    target_contract = _tariff_contract(tariff=target, component=target_component)
    compatible_source_components = _historical_compatible_components(
        club_id=club_id,
        source=source,
        active_component=source_component,
    )
    component_mapping = {
        str(component.id): target_component.id
        for component in compatible_source_components
    }
    if str(source_component.id) not in component_mapping:
        component_mapping[str(source_component.id)] = target_component.id
    compatibility_snapshot = {
        "shape": "single_finite_component",
        "changed_fields": ["name", "price"],
        "unchanged_fields": [
            "training_type",
            "trainings_limit",
            "duration_days",
            "scope",
            "location",
            "trainer_payout_policy",
            "personal_booking_trainer",
            "component_entitlement_kind",
            "component_credits_total",
            "component_scope",
            "component_location",
            "component_trainer_payout_policy",
        ],
        "historical_source_component_ids": [component.id for component in compatible_source_components],
    }
    try:
        # Keep the unique-conflict handling inside a savepoint.  The outer
        # transaction still owns source retirement and target creation, so a
        # conflict must abort the whole operation after it is classified.
        with transaction.atomic():
            revision = TariffPriceRevision.objects.create(
                club_id=club_id,
                source_tariff=source,
                target_tariff=target,
                source_component=source_component,
                target_component=target_component,
                actor_id=actor_user_id,
                idempotency_key=key,
                payload_fingerprint=fingerprint,
                source_contract=source_contract,
                target_contract=target_contract,
                compatibility_snapshot=compatibility_snapshot,
                component_mapping=component_mapping,
            )
    except IntegrityError as exc:
        if _existing_receipt(club_id=club_id, idempotency_key=key) is not None:
            _fail(
                "Запрос изменения цены уже выполняется параллельно.",
                "tariff_revision_concurrent_replay",
            )
        if TariffPriceRevision.objects.for_club(club_id).filter(source_tariff_id=source.id).exists():
            _fail(
                "Исходная версия тарифа уже заменена.",
                "tariff_revision_source_not_current",
            )
        raise exc
    return revision


def resolve_current_renewal_tariff(
    *,
    club_id: int,
    source_tariff_id: int,
    lock: bool = False,
) -> Tariff:
    """Return the current compatible leaf, preserving legacy same-ID callers."""

    current = (
        Tariff.objects.for_club(club_id)
        .select_related("training_type", "location", "personal_booking_trainer")
        .filter(id=source_tariff_id)
        .first()
    )
    if current is None:
        _fail("Тариф не найден.", "tariff_not_found")
    visited: set[int] = set()
    while True:
        if current.id in visited:
            _fail("Обнаружен цикл версий тарифа.", "tariff_revision_cycle")
        visited.add(current.id)
        if lock:
            current = (
                Tariff.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("training_type", "location", "personal_booking_trainer")
                .get(id=current.id)
            )
        edge = (
            TariffPriceRevision.objects.for_club(club_id)
            .filter(source_tariff_id=current.id)
            .select_related("target_tariff")
            .first()
        )
        if edge is None:
            return current
        target = (
            Tariff.objects.for_club(club_id)
            .select_related("training_type", "location", "personal_booking_trainer")
            .filter(id=edge.target_tariff_id)
            .first()
        )
        if target is None:
            _fail("Преемник тарифа недоступен.", "tariff_revision_target_missing")
        current = target


def get_renewal_tariff_component_mapping(
    *,
    club_id: int,
    source_tariff_id: int,
    target_tariff_id: int,
) -> dict[int, int]:
    """Compose only persisted revision mappings between two tariff versions."""

    if not Tariff.objects.for_club(club_id).filter(id=source_tariff_id).exists():
        _fail("Исходный тариф не найден.", "tariff_not_found")
    if not Tariff.objects.for_club(club_id).filter(id=target_tariff_id).exists():
        _fail("Целевой тариф не найден.", "tariff_not_found")
    if source_tariff_id == target_tariff_id:
        # Same-ID legacy renewals already carry identical component ids.
        return {}

    mapping: dict[int, int] = {}
    current_tariff_id = source_tariff_id
    visited: set[int] = set()
    while True:
        if current_tariff_id in visited:
            _fail("Обнаружен цикл версий тарифа.", "tariff_revision_cycle")
        visited.add(current_tariff_id)
        edge = (
            TariffPriceRevision.objects.for_club(club_id)
            .filter(source_tariff_id=current_tariff_id)
            .first()
        )
        if edge is None:
            _fail(
                "Для этих тарифов нет подтвержденной совместимой версии.",
                "tariff_revision_lineage_missing",
            )
        edge_mapping = {
            int(source_component_id): int(target_component_id)
            for source_component_id, target_component_id in edge.component_mapping.items()
        }
        if not edge_mapping:
            _fail(
                "В версии тарифа отсутствует сопоставление компонентов.",
                "tariff_revision_mapping_missing",
            )
        if not mapping:
            mapping = edge_mapping
        else:
            try:
                mapping = {
                    source_component_id: edge_mapping[target_component_id]
                    for source_component_id, target_component_id in mapping.items()
                }
            except KeyError:
                _fail(
                    "Цепочка версий тарифа содержит несовместимые компоненты.",
                    "tariff_revision_mapping_invalid",
                )
        current_tariff_id = edge.target_tariff_id
        if current_tariff_id == target_tariff_id:
            return mapping
