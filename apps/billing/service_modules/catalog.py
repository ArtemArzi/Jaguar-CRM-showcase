from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.billing.models import (
    Discount,
    Payment,
    Subscription,
    Tariff,
    TariffComponent,
    TariffPriceRevision,
    TrainingType,
)
from apps.billing.service_modules._shared import _apply_updates, _validate_positive_money
from apps.billing.service_modules.tariff_components import (
    _component_from_tariff_defaults,
    _replace_tariff_components,
    _tariff_component_contracts_equal,
    _tariff_has_default_component,
    _validate_payout_policy,
)
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError
from apps.grades.models import GradeSystem
from apps.trainers.models import Trainer

logger = logging.getLogger(__name__)

_UPDATE_TRAINING_TYPE_FIELDS = frozenset(
    {"name", "kind", "is_active", "grade_system_id", "drop_in_price", "trial_free"}
)
_UPDATE_TARIFF_FIELDS = frozenset({
    "name",
    "description",
    "trainings_limit",
    "duration_days",
    "is_active",
    "price",
    "trainer_payout_policy",
    "personal_booking_trainer_id",
})
_UPDATE_DISCOUNT_FIELDS = frozenset({"name", "discount_type", "value", "is_active"})


def _validate_discount_value(*, discount_type: str, value: Decimal) -> None:
    if discount_type == Discount.Type.PERCENT:
        if value < Decimal("0") or value > Decimal("100"):
            raise BusinessLogicError(
                "Процентная скидка должна быть от 0 до 100",
                code="invalid_discount_value",
            )
        return

    if discount_type == Discount.Type.FIXED:
        if value < Decimal("0"):
            raise BusinessLogicError(
                "Фиксированная скидка не может быть отрицательной",
                code="invalid_discount_value",
            )
        return

    raise BusinessLogicError("Некорректный тип скидки", code="invalid_discount_type")


def _resolve_grade_system_id_for_training_type(
    *, club_id: int, grade_system_id: int | None,
) -> int | None:
    if grade_system_id is None:
        return None

    if not GradeSystem.objects.for_club(club_id).filter(id=grade_system_id, is_active=True).exists():
        raise BusinessLogicError(
            "Система грейдов не найдена",
            code="grade_system_not_found",
        )
    return grade_system_id


def _validate_training_type_kind(kind: str) -> str:
    if kind not in TrainingType.Kind.values:
        raise BusinessLogicError(
            "Некорректный формат тренировки",
            code="invalid_training_type_kind",
        )
    return kind


def _resolve_personal_booking_trainer_id(*, club_id: int, trainer_id: int | None) -> int | None:
    if trainer_id is None:
        return None
    trainer = Trainer.objects.for_club(club_id).filter(id=trainer_id).first()
    if trainer is None:
        raise BusinessLogicError(
            "Тренер персонального тарифа не найден",
            code="personal_booking_trainer_not_found",
        )
    return trainer.id


def is_training_type_kind_locked(*, club_id: int, training_type_id: int) -> bool:
    from apps.attendance.models import Checkin, Schedule
    from apps.trainers.models import TrainerPackageAllocation, TrainerRate

    return (
        Tariff.objects.for_club(club_id).filter(training_type_id=training_type_id).exists()
        or Subscription.objects.for_club(club_id)
        .filter(tariff__training_type_id=training_type_id)
        .exists()
        or Schedule.objects.for_club(club_id).filter(training_type_id=training_type_id).exists()
        or Checkin.objects.for_club(club_id).filter(training_type_id=training_type_id).exists()
        or TrainerRate.objects.for_club(club_id).filter(training_type_id=training_type_id).exists()
        or TrainerPackageAllocation.objects.for_club(club_id)
        .filter(training_type_id=training_type_id)
        .exists()
    )


def create_training_type(
    *,
    club_id: int,
    name: str,
    slug: str,
    kind: str = TrainingType.Kind.GROUP,
    grade_system_id: int | None = None,
    drop_in_price: Decimal | None = None,
) -> TrainingType:
    if drop_in_price is not None:
        _validate_positive_money(drop_in_price)
    resolved_grade_system_id = _resolve_grade_system_id_for_training_type(
        club_id=club_id,
        grade_system_id=grade_system_id,
    )
    training_type = TrainingType.objects.create(
        club_id=club_id,
        name=name,
        slug=slug,
        kind=_validate_training_type_kind(kind),
        grade_system_id=resolved_grade_system_id,
        drop_in_price=drop_in_price,
    )
    logger.info("training_type_created", extra={"id": training_type.id, "club_id": club_id})
    return training_type


def _has_open_personal_drop_in_for_contract_change(*, club_id: int, booking_filter: Q) -> bool:
    from apps.attendance.models import PersonalDropInBooking

    open_booking_ids = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(booking_filter)
        .filter(
            Q(state=PersonalDropInBooking.State.SCHEDULED)
            | Q(
                state=PersonalDropInBooking.State.ATTENDED,
                debt__isnull=False,
                debt__resolved_at__isnull=True,
            )
        )
        .values("id")
    )
    # The attended branch joins nullable Debt. Select candidates there, but
    # lock only the base PersonalDropInBooking rows in the outer query.
    return (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(id__in=open_booking_ids)
        .select_for_update(of=("self",))
        .only("id")
        .order_by("id")
        .first()
        is not None
    )


def _lock_catalog_training_type(
    *,
    club_id: int,
    training_type_id: int,
    require_club: bool = True,
) -> TrainingType:
    """The training-type row is the common catalog lock for offer flips."""
    queryset = (
        TrainingType.objects.for_club(club_id)
        if require_club
        else TrainingType.objects.unscoped()
    )
    training_type = (
        queryset
        .select_for_update(of=("self",))
        .filter(id=training_type_id)
        .first()
    )
    if training_type is None:
        raise BusinessLogicError("Тип тренировки не найден", code="training_type_not_found")
    return training_type


def _lock_catalog_trainers(*, club_id: int, trainer_ids: list[int]) -> None:
    if not trainer_ids:
        return
    locked_ids = list(
        Trainer.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=trainer_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if locked_ids != trainer_ids:
        raise BusinessLogicError(
            "Тренер персонального тарифа не найден",
            code="personal_booking_trainer_not_found",
        )


def _set_personal_booking_default(*, tariff: Tariff, enabled: bool) -> None:
    """Switch one exact policy scope under the shared catalog lock."""
    if enabled and TariffPriceRevision.objects.for_club(tariff.club_id).filter(
        source_tariff_id=tariff.id,
    ).exists():
        raise BusinessLogicError(
            "Версия тарифа зафиксирована изменением цены.",
            code="tariff_revision_contract_sealed",
        )
    from apps.billing.service_modules.personal_offers import validate_personal_booking_default

    _lock_catalog_training_type(club_id=tariff.club_id, training_type_id=tariff.training_type_id)
    same_scope = Tariff.objects.for_club(tariff.club_id).select_for_update(of=("self",)).filter(
        training_type_id=tariff.training_type_id,
        scope=tariff.scope,
        personal_booking_trainer_id=tariff.personal_booking_trainer_id,
    )
    if tariff.scope == Tariff.Scope.LOCATION:
        same_scope = same_scope.filter(location_id=tariff.location_id)
    else:
        same_scope = same_scope.filter(location__isnull=True)
    # Evaluate the lock query before changing any row.  PostgreSQL then gives
    # an offer acceptor either the old complete contract or the new one.
    list(same_scope.order_by("id").values_list("id", flat=True))
    if not enabled:
        if tariff.is_personal_booking_default:
            tariff.is_personal_booking_default = False
            tariff.save(update_fields=["is_personal_booking_default", "updated_at"])
        return

    validate_personal_booking_default(tariff=tariff, lock=True)
    same_scope.exclude(id=tariff.id).filter(is_personal_booking_default=True).update(
        is_personal_booking_default=False,
    )
    if not tariff.is_personal_booking_default:
        tariff.is_personal_booking_default = True
        tariff.save(update_fields=["is_personal_booking_default", "updated_at"])


def _rename_derived_default_component_in_place(*, tariff: Tariff, club_id: int) -> bool:
    """Keep the default component identity stable for a tariff name edit."""
    if not _tariff_has_default_component(tariff=tariff, club_id=club_id):
        return False

    component = (
        TariffComponent.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(tariff=tariff, is_active=True)
        .order_by("sort_order", "id")
        .first()
    )
    if component is None:
        return False
    if component.name == tariff.name:
        return True
    component.name = tariff.name
    component.save(update_fields=["name", "updated_at"])
    return True


@transaction.atomic
def update_training_type(*, training_type_id: int, club_id: int, **fields) -> TrainingType:
    training_type = (
        TrainingType.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=training_type_id)
        .first()
    )
    if not training_type:
        raise BusinessLogicError("Тип тренировки не найден", code="training_type_not_found")
    if "grade_system_id" in fields:
        fields["grade_system_id"] = _resolve_grade_system_id_for_training_type(
            club_id=club_id,
            grade_system_id=fields["grade_system_id"],
        )
    if fields.get("drop_in_price") is not None:
        _validate_positive_money(fields["drop_in_price"])
    if "kind" in fields:
        fields["kind"] = _validate_training_type_kind(fields["kind"])
        if fields["kind"] != training_type.kind and is_training_type_kind_locked(
            club_id=club_id,
            training_type_id=training_type.id,
        ):
            raise BusinessLogicError(
                "Формат типа тренировки нельзя менять после использования",
                code="training_type_kind_locked",
            )
    protected_drop_in_fields = {
        field_name
        for field_name in {"kind", "is_active", "drop_in_price"}
        if field_name in fields and getattr(training_type, field_name) != fields[field_name]
    }
    if protected_drop_in_fields:
        if _has_open_personal_drop_in_for_contract_change(
            club_id=club_id,
            booking_filter=Q(enrollment__schedule__training_type_id=training_type.id),
        ):
            raise BusinessLogicError(
                "Нельзя менять контракт типа тренировки, пока есть открытая разовая персоналка",
                code="personal_drop_in_contract_change_blocked",
            )
    changed = _apply_updates(training_type, fields, _UPDATE_TRAINING_TYPE_FIELDS)
    training_type.save(update_fields=[*changed, "updated_at"])
    logger.info("training_type_updated", extra={"id": training_type.id, "club_id": club_id})
    return training_type


def create_tariff(
    *,
    club_id: int,
    name: str,
    training_type_id: int,
    price: Decimal,
    trainings_limit: int | None,
    duration_days: int,
    scope: str = "club",
    location_id: int | None = None,
    description: str = "",
    trainer_payout_policy: str = "",
    components: list[dict] | None = None,
    personal_booking_trainer_id: int | None = None,
    is_personal_booking_default: bool = False,
) -> Tariff:
    _validate_positive_money(price)

    if trainer_payout_policy:
        _validate_payout_policy(trainer_payout_policy)

    if scope not in {Tariff.Scope.CLUB, Tariff.Scope.LOCATION}:
        raise BusinessLogicError("Некорректная область действия тарифа", code="invalid_tariff_scope")

    if scope == "location":
        if not location_id:
            raise BusinessLogicError(
                "Location is required for location-scoped tariff",
                code="location_required",
            )
        location = Location.objects.filter(id=location_id, club_id=club_id).first()
        if location is None:
            raise BusinessLogicError("Локация не найдена", code="location_not_found")
    else:
        location = None
    personal_booking_trainer_id = _resolve_personal_booking_trainer_id(
        club_id=club_id,
        trainer_id=personal_booking_trainer_id,
    )

    with transaction.atomic():
        training_type = _lock_catalog_training_type(
            club_id=club_id,
            training_type_id=training_type_id,
        )
        if personal_booking_trainer_id is not None:
            # Personal offer acceptance locks catalog type -> trainer -> tariff.
            # Keep catalog creation on the same prefix before FK inserts.
            _lock_catalog_trainers(
                club_id=club_id,
                trainer_ids=[personal_booking_trainer_id],
            )
        tariff = Tariff.objects.create(
            club_id=club_id,
            name=name,
            training_type=training_type,
            price=price,
            trainings_limit=trainings_limit,
            duration_days=duration_days,
            scope=scope,
            location=location,
            description=description,
            trainer_payout_policy=trainer_payout_policy,
            personal_booking_trainer_id=personal_booking_trainer_id,
        )
        _replace_tariff_components(
            tariff=tariff,
            club_id=club_id,
            components=components or [_component_from_tariff_defaults(tariff)],
        )
        if is_personal_booking_default:
            _set_personal_booking_default(tariff=tariff, enabled=True)
    logger.info("tariff_created", extra={"id": tariff.id, "club_id": club_id})
    return tariff


@transaction.atomic
def update_tariff(
    *,
    tariff_id: int,
    club_id: int,
    components: list[dict] | None = None,
    is_personal_booking_default: bool | None = None,
    **fields,
) -> Tariff:
    training_type_id = (
        Tariff.objects.for_club(club_id)
        .filter(id=tariff_id)
        .values_list("training_type_id", flat=True)
        .first()
    )
    if training_type_id is None:
        raise BusinessLogicError("Тариф не найден", code="tariff_not_found")
    _lock_catalog_training_type(
        club_id=club_id,
        training_type_id=training_type_id,
        require_club=False,
    )
    # Another update may have completed while this transaction waited for the
    # shared training-type lock. Re-read the mutable trainer scope only after
    # that serialization point; never carry a stale trainer into the prefix.
    current_scope = (
        Tariff.objects.for_club(club_id)
        .filter(id=tariff_id, training_type_id=training_type_id)
        .values("personal_booking_trainer_id")
        .first()
    )
    if current_scope is None:
        raise BusinessLogicError("Тариф не найден", code="tariff_not_found")
    current_trainer_id = current_scope["personal_booking_trainer_id"]
    requested_trainer_id = (
        _resolve_personal_booking_trainer_id(
            club_id=club_id,
            trainer_id=fields["personal_booking_trainer_id"],
        )
        if "personal_booking_trainer_id" in fields
        else current_trainer_id
    )
    trainer_ids = sorted({
        trainer_id
        for trainer_id in (
            current_trainer_id,
            requested_trainer_id,
        )
        if trainer_id is not None
    })
    _lock_catalog_trainers(club_id=club_id, trainer_ids=trainer_ids)
    tariff = (
        Tariff.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("training_type", "location", "personal_booking_trainer")
        .filter(id=tariff_id)
        .first()
    )
    if tariff.personal_booking_trainer_id != current_trainer_id:
        raise BusinessLogicError(
            "Область персонального тарифа изменилась во время блокировки",
            code="personal_booking_catalog_scope_changed",
        )

    revision_edges = TariffPriceRevision.objects.for_club(club_id)
    has_outgoing_revision = revision_edges.filter(source_tariff_id=tariff.id).exists()
    has_any_revision = has_outgoing_revision or revision_edges.filter(
        target_tariff_id=tariff.id,
    ).exists()
    field_to_attribute = {
        "training_type": "training_type_id",
        "location": "location_id",
        "personal_booking_trainer": "personal_booking_trainer_id",
    }
    sealed_contract_changed = any(
        field_name in fields
        and getattr(tariff, field_to_attribute.get(field_name, field_name)) != value
        for field_name, value in fields.items()
        if field_name
        in {
            "name",
            "training_type",
            "training_type_id",
            "price",
            "trainings_limit",
            "duration_days",
            "scope",
            "location",
            "location_id",
            "trainer_payout_policy",
            "personal_booking_trainer",
            "personal_booking_trainer_id",
        }
    )
    name_changed = "name" in fields and fields["name"] != tariff.name
    default_designation_changed = (
        is_personal_booking_default is not None
        and is_personal_booking_default != tariff.is_personal_booking_default
    )
    if has_any_revision and (
        sealed_contract_changed
        or components is not None
        or (
            has_outgoing_revision
            and (
                fields.get("is_active") is True
                or is_personal_booking_default is True
            )
        )
    ):
        raise BusinessLogicError(
            "Версия тарифа зафиксирована изменением цены. Для новой цены используйте действие «Изменить цену».",
            code="tariff_revision_contract_sealed",
        )

    if "trainer_payout_policy" in fields and fields["trainer_payout_policy"] is None:
        fields["trainer_payout_policy"] = ""
    if "personal_booking_trainer_id" in fields:
        fields["personal_booking_trainer_id"] = requested_trainer_id
    if "price" in fields:
        _validate_positive_money(fields["price"])
    if "trainer_payout_policy" in fields and fields["trainer_payout_policy"]:
        _validate_payout_policy(fields["trainer_payout_policy"])

    changed_contract_fields = {
        field_name
        for field_name in {
            "trainer_payout_policy",
            "price",
            "trainings_limit",
            "duration_days",
            "is_active",
            "personal_booking_trainer_id",
        }
        if field_name in fields and getattr(tariff, field_name) != fields[field_name]
    }
    price_changed = "price" in changed_contract_fields
    component_contract_changed = False
    component_payload_changed = False
    if components is not None:
        component_contract_changed = (
            price_changed
            or not _tariff_component_contracts_equal(
                tariff=tariff,
                club_id=club_id,
                components=components,
            )
        )
        component_payload_changed = (
            price_changed
            or not _tariff_component_contracts_equal(
                tariff=tariff,
                club_id=club_id,
                components=components,
                include_display_fields=True,
            )
        )
    changing_payroll_contract = bool(
        component_contract_changed
        or changed_contract_fields
        & {"trainer_payout_policy", "price", "trainings_limit", "duration_days"}
    )
    changing_drop_in_contract = changing_payroll_contract or "is_active" in changed_contract_fields
    changing_designated_contract = (
        changing_drop_in_contract
        or component_contract_changed
        or "personal_booking_trainer_id" in changed_contract_fields
    )
    if (
        tariff.is_personal_booking_default
        and changing_designated_contract
        and is_personal_booking_default is not False
    ):
        raise BusinessLogicError(
            "Сначала снимите назначение personal-тарифа в той же операции",
            code="personal_offer_default_clear_required",
        )
    has_active_or_pending_usage = tariff.subscriptions.filter(
        status__in=[Subscription.Status.ACTIVE, Subscription.Status.PENDING],
        deleted_at__isnull=True,
    ).exists() or tariff.payments.filter(status=Payment.Status.PENDING, deleted_at__isnull=True).exists()

    if changing_payroll_contract and has_active_or_pending_usage:
        raise BusinessLogicError(
            "Нельзя менять финансовые правила тарифа с активными или ожидающими продажами",
            code="tariff_contract_change_blocked",
        )
    if changing_drop_in_contract:
        if _has_open_personal_drop_in_for_contract_change(
            club_id=club_id,
            booking_filter=Q(tariff_id=tariff.id),
        ):
            raise BusinessLogicError(
                "Нельзя менять тариф, пока есть открытая разовая персоналка",
                code="personal_drop_in_contract_change_blocked",
            )

    had_default_component = _tariff_has_default_component(tariff=tariff, club_id=club_id)
    with transaction.atomic():
        # Clearing is deliberately first: it makes an explicit clear +
        # contract update one atomic catalog operation and never validates a
        # stale pre-replacement component contract as the new default.
        if is_personal_booking_default is False:
            _set_personal_booking_default(tariff=tariff, enabled=False)
        changed = _apply_updates(tariff, fields, _UPDATE_TARIFF_FIELDS)
        if changed:
            tariff.save(update_fields=[*changed, "updated_at"])
        tariff.refresh_from_db()
        if components is not None and component_payload_changed:
            _replace_tariff_components(tariff=tariff, club_id=club_id, components=components)
        elif had_default_component:
            name_only_default_component_change = (
                components is None
                and name_changed
                and not default_designation_changed
                and not changed_contract_fields
            )
            if name_only_default_component_change:
                _rename_derived_default_component_in_place(
                    tariff=tariff,
                    club_id=club_id,
                )
            elif name_changed or changed_contract_fields.intersection(
                {"price", "trainings_limit", "trainer_payout_policy"}
            ):
                _replace_tariff_components(
                    tariff=tariff,
                    club_id=club_id,
                    components=[_component_from_tariff_defaults(tariff)],
                )
        if is_personal_booking_default is True:
            _set_personal_booking_default(
                tariff=tariff,
                enabled=True,
            )
    logger.info("tariff_updated", extra={"id": tariff.id, "club_id": club_id})
    return tariff


def update_discount(*, discount_id: int, club_id: int, **fields) -> Discount:
    discount = Discount.objects.for_club(club_id).get(id=discount_id)
    discount_type = fields.get("discount_type", discount.discount_type)
    value = fields.get("value", discount.value)
    _validate_discount_value(discount_type=discount_type, value=value)
    changed = _apply_updates(discount, fields, _UPDATE_DISCOUNT_FIELDS)
    discount.save(update_fields=[*changed, "updated_at"])
    logger.info("discount_updated", extra={"id": discount_id, "club_id": club_id})
    return discount


def create_discount(*, club_id: int, name: str, discount_type: str, value: Decimal) -> Discount:
    _validate_discount_value(discount_type=discount_type, value=value)

    discount = Discount.objects.create(
        club_id=club_id,
        name=name,
        discount_type=discount_type,
        value=value,
    )
    logger.info("discount_created", extra={"id": discount.id, "club_id": club_id})
    return discount
