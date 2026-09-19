"""Locked, fail-closed catalog policy for one-session personal offers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet

from apps.billing.models import Discount, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules._shared import _money
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer


@dataclass(frozen=True)
class PersonalBookingOffer:
    tariff: Tariff
    component: TariffComponent
    discount: Discount | None
    base_amount: Decimal
    discount_amount: Decimal
    payable_amount: Decimal

    @property
    def price(self) -> Decimal:
        return self.payable_amount


def _error(message: str, code: str) -> None:
    raise BusinessLogicError(message, code=code)


def _effective_tariff_payout_policy(tariff: Tariff) -> str:
    return tariff.trainer_payout_policy or Tariff.PayoutPolicy.ON_CHECKIN


def _locked_training_type(*, club_id: int, training_type_id: int, lock: bool) -> TrainingType:
    queryset: QuerySet[TrainingType] = TrainingType.objects.for_club(club_id)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    training_type = queryset.filter(id=training_type_id).first()
    if training_type is None:
        _error("Personal training type was not found", "personal_offer_training_type_not_found")
    if not training_type.is_active:
        _error("Personal training type is inactive", "personal_offer_training_type_inactive")
    if training_type.kind == TrainingType.Kind.MINI_GROUP:
        _error("Mini-group does not use the personal offer policy", "personal_offer_mini_group")
    if training_type.kind != TrainingType.Kind.PERSONAL:
        _error("A personal offer requires a personal training type", "personal_offer_training_type_invalid")
    return training_type


def _locked_trainer(*, club_id: int, trainer_id: int, lock: bool) -> Trainer:
    queryset: QuerySet[Trainer] = Trainer.objects.for_club(club_id)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    trainer = queryset.filter(id=trainer_id).first()
    if trainer is None:
        _error("Personal trainer was not found", "personal_offer_trainer_not_found")
    return trainer


def _locked_discount(*, club_id: int, discount_id: int | None, lock: bool) -> Discount | None:
    if discount_id is None:
        return None
    queryset: QuerySet[Discount] = Discount.objects.for_club(club_id)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    discount = queryset.filter(id=discount_id).first()
    if discount is None:
        _error("Personal booking discount was not found", "personal_offer_discount_not_found")
    if not discount.is_active:
        _error("Personal booking discount is inactive", "personal_offer_discount_inactive")
    return discount


def _discounted_amounts(
    *, base_amount: Decimal, discount: Discount | None,
) -> tuple[Decimal, Decimal, Decimal]:
    base_amount = _money(base_amount)
    if discount is None:
        return base_amount, Decimal("0.00"), base_amount
    if discount.discount_type == Discount.Type.PERCENT:
        if discount.value < 0 or discount.value > 100:
            _error("Personal booking discount is invalid", "personal_offer_discount_invalid")
        discount_amount = _money(base_amount * discount.value / Decimal("100"))
    elif discount.discount_type == Discount.Type.FIXED:
        if discount.value < 0:
            _error("Personal booking discount is invalid", "personal_offer_discount_invalid")
        discount_amount = _money(discount.value)
    else:
        _error("Personal booking discount is invalid", "personal_offer_discount_invalid")
    payable_amount = _money(base_amount - discount_amount)
    if payable_amount <= 0:
        _error(
            "Personal booking discount must leave a positive payable amount",
            "personal_offer_discount_non_positive",
        )
    return base_amount, discount_amount, payable_amount


def personal_booking_default_candidates(
    *,
    club_id: int,
    training_type_id: int,
    location_id: int,
    trainer_id: int | None,
    lock: bool,
) -> list[Tariff]:
    """Return the resolver's selected generic or trainer-specific default rows."""

    queryset = Tariff.objects.for_club(club_id).select_related(
        "training_type", "location", "personal_booking_trainer"
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    def scoped_candidates(*, personal_booking_trainer_id: int | None) -> list[Tariff]:
        scoped = queryset.filter(
            training_type_id=training_type_id,
            personal_booking_trainer_id=personal_booking_trainer_id,
            is_personal_booking_default=True,
        )
        location_rows = list(
            scoped.filter(
                scope=Tariff.Scope.LOCATION,
                location_id=location_id,
            ).order_by("id")
        )
        if location_rows:
            return location_rows
        return list(
            scoped.filter(
                scope=Tariff.Scope.CLUB,
                location__isnull=True,
            ).order_by("id")
        )

    if trainer_id is not None:
        trainer_rows = scoped_candidates(personal_booking_trainer_id=trainer_id)
        if trainer_rows:
            return trainer_rows
    # Location overrides are authoritative when configured, including their
    # failure state.  We must not silently fall back to a cheaper club tariff.
    return scoped_candidates(personal_booking_trainer_id=None)


def _validate_offer_contract(
    *,
    tariff: Tariff,
    training_type: TrainingType,
    location_id: int,
    lock: bool,
) -> TariffComponent:
    if not tariff.name or not training_type.name:
        _error("The designated tariff names are incomplete", "personal_offer_wrong_contract")
    if not tariff.is_active:
        _error("The designated personal tariff is inactive", "personal_offer_default_inactive")
    if tariff.training_type_id != training_type.id:
        _error("The designated tariff has a different training type", "personal_offer_wrong_training_type")
    if tariff.scope == Tariff.Scope.LOCATION and tariff.location_id != location_id:
        _error("The designated tariff does not cover this location", "personal_offer_wrong_scope")
    if tariff.scope == Tariff.Scope.CLUB and tariff.location_id is not None:
        _error("The designated club tariff has an invalid location", "personal_offer_wrong_scope")
    if tariff.trainings_limit != 1:
        _error("The designated tariff must contain one session", "personal_offer_wrong_contract")
    component_qs = (
        TariffComponent.objects.for_club(tariff.club_id)
        .filter(tariff_id=tariff.id, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )
    if lock:
        component_qs = component_qs.select_for_update(of=("self",))
    components = list(component_qs)
    if len(components) != 1:
        _error("The designated tariff must have exactly one active component", "personal_offer_wrong_component")
    component = components[0]
    if (
        not component.name
        or component.training_type_id != training_type.id
        or component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS
        or component.credits_total != 1
        or component.paid_amount_basis != tariff.price
    ):
        _error("The designated tariff component is not one paid personal credit", "personal_offer_wrong_component")
    if component.scope != tariff.scope or component.location_id != tariff.location_id:
        _error("The designated tariff component has a different scope", "personal_offer_wrong_component")
    if (
        _effective_tariff_payout_policy(tariff) != Tariff.PayoutPolicy.ON_CHECKIN
        or component.trainer_payout_policy != Tariff.PayoutPolicy.ON_CHECKIN
    ):
        _error("The designated tariff must pay the trainer on check-in", "personal_offer_wrong_payout")
    return component


def resolve_personal_booking_offer(
    *,
    club_id: int,
    trainer_id: int | None = None,
    training_type_id: int,
    location_id: int,
    discount_id: int | None = None,
    lock: bool = False,
) -> PersonalBookingOffer:
    """Resolve a current one-session offer; a locked call is offer acceptance."""

    training_type = _locked_training_type(
        club_id=club_id,
        training_type_id=training_type_id,
        lock=lock,
    )
    if trainer_id is not None:
        _locked_trainer(club_id=club_id, trainer_id=trainer_id, lock=lock)
    candidates = personal_booking_default_candidates(
        club_id=club_id,
        training_type_id=training_type.id,
        location_id=location_id,
        trainer_id=trainer_id,
        lock=lock,
    )
    if not candidates:
        _error(
            "Configure a designated personal tariff for this location",
            "personal_booking_tariff_not_configured",
        )
    if len(candidates) != 1:
        _error(
            "More than one designated personal tariff is configured",
            "personal_booking_tariff_ambiguous",
        )
    tariff = candidates[0]
    component = _validate_offer_contract(
        tariff=tariff,
        training_type=training_type,
        location_id=location_id,
        lock=lock,
    )
    discount = _locked_discount(club_id=club_id, discount_id=discount_id, lock=lock)
    base_amount, discount_amount, payable_amount = _discounted_amounts(
        base_amount=tariff.price,
        discount=discount,
    )
    return PersonalBookingOffer(
        tariff=tariff,
        component=component,
        discount=discount,
        base_amount=base_amount,
        discount_amount=discount_amount,
        payable_amount=payable_amount,
    )


def validate_personal_booking_default(*, tariff: Tariff, lock: bool = False) -> PersonalBookingOffer:
    """Validate a tariff before it may become the scoped default."""

    if tariff.personal_booking_trainer_id is not None:
        _locked_trainer(
            club_id=tariff.club_id,
            trainer_id=tariff.personal_booking_trainer_id,
            lock=lock,
        )
    training_type = _locked_training_type(
        club_id=tariff.club_id,
        training_type_id=tariff.training_type_id,
        lock=lock,
    )
    location_id = tariff.location_id or 0
    return PersonalBookingOffer(
        tariff=tariff,
        component=_validate_offer_contract(
            tariff=tariff,
            training_type=training_type,
            location_id=location_id,
            lock=lock,
        ),
        discount=None,
        base_amount=_money(tariff.price),
        discount_amount=Decimal("0.00"),
        payable_amount=_money(tariff.price),
    )
