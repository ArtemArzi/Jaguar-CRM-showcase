"""Exact-source renewal creation and one shared finalization owner."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from importlib import import_module

from django.db import OperationalError, transaction
from django.utils import timezone

from apps.billing.models import (
    BankPaymentOrder,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionRenewalEvent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.tariff_revisions import (
    get_renewal_tariff_component_mapping as _get_catalogue_component_mapping,
)
from apps.billing.service_modules.tariff_revisions import (
    resolve_current_renewal_tariff as _resolve_catalogue_current_tariff,
)
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError

RENEWAL_MANUAL_REVIEW_CODES = frozenset(
    {
        "renewal_source_closed",
        "renewal_source_mismatch",
        "renewal_source_not_found",
        "renewal_source_finalized_successor",
        "opening_renewal_component_needs_review",
        "renewal_offer_required",
        "renewal_offer_stale",
    }
)


@dataclass(frozen=True)
class RenewalOffer:
    """The current compatible target for one historical source tariff.

    The source fields are deliberately retained beside the target fields.  A
    subscription/payment keeps the source snapshot while a new renewal intent
    is priced from the target.
    """

    source_tariff_id: int
    source_tariff_name: str
    source_tariff_price: Decimal
    target_tariff: Tariff

    @property
    def target_tariff_id(self) -> int:
        return self.target_tariff.id

    @property
    def target_tariff_name(self) -> str:
        return self.target_tariff.name

    @property
    def target_price(self) -> Decimal:
        return self.target_tariff.price

    @property
    def is_revision(self) -> bool:
        return self.target_tariff_id != self.source_tariff_id

    @property
    def is_available(self) -> bool:
        """Only an active target can be presented as a purchasable offer."""

        return bool(self.target_tariff.is_active)


def resolve_current_renewal_tariff(
    *,
    club_id: int,
    source_tariff_id: int,
    lock: bool = False,
) -> Tariff:
    """Return the current compatible target, preserving same-ID legacy calls.

    The catalogue worker owns the revision graph.  With no edge, its adapter
    returns the historical source itself so existing renewals keep their exact
    behavior.
    """

    target = _resolve_catalogue_current_tariff(
        club_id=club_id,
        source_tariff_id=source_tariff_id,
        lock=lock,
    )
    if lock:
        target = (
            Tariff.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("training_type", "location")
            .get(id=target.id)
        )
    return target


def get_renewal_tariff_component_mapping(
    *,
    club_id: int,
    source_tariff_id: int,
    target_tariff_id: int,
) -> dict:
    """Return audited source-component to target-component IDs for a revision."""

    if source_tariff_id == target_tariff_id:
        return {}
    mapping = _get_catalogue_component_mapping(
        club_id=club_id,
        source_tariff_id=source_tariff_id,
        target_tariff_id=target_tariff_id,
    )
    return mapping


def get_renewal_offer(
    *,
    club_id: int,
    source_tariff: Tariff | None = None,
    source_tariff_id: int | None = None,
    lock: bool = False,
) -> RenewalOffer:
    """Build a source-preserving current renewal offer for an API/card."""

    if source_tariff is None:
        if source_tariff_id is None:
            raise ValueError("source_tariff or source_tariff_id is required")
        source_tariff = (
            Tariff.objects.for_club(club_id)
            .select_related("training_type", "location")
            .get(id=source_tariff_id)
        )
    target = resolve_current_renewal_tariff(
        club_id=club_id,
        source_tariff_id=source_tariff.id,
        lock=lock,
    )
    return RenewalOffer(
        source_tariff_id=source_tariff.id,
        source_tariff_name=source_tariff.name,
        source_tariff_price=source_tariff.price,
        target_tariff=target,
    )


def _lock_renewal_catalog_scope(*, club_id: int, source_tariff: Tariff) -> None:
    """Acquire the catalog lock prefix used by price revisions.

    Revision creation locks training type, personal trainer, then tariff. A
    fresh renewal must take that same prefix before it locks the source tariff;
    otherwise a concurrent revision can deadlock with the source subscription
    lock held by the renewal transaction.
    """

    from apps.trainers.models import Trainer

    TrainingType.objects.for_club(club_id).select_for_update(of=("self",)).get(
        id=source_tariff.training_type_id,
    )
    if source_tariff.personal_booking_trainer_id is not None:
        Trainer.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=source_tariff.personal_booking_trainer_id,
        )


def _normalise_expected_renewal_offer(
    *,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
) -> tuple[int | None, Decimal | None]:
    if expected_target_price is None:
        return expected_target_tariff_id, None
    try:
        price = Decimal(str(expected_target_price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BusinessLogicError(
            "Некорректная цена предложения продления",
            code="renewal_offer_invalid",
        ) from exc
    if not price.is_finite():
        raise BusinessLogicError(
            "Некорректная цена предложения продления",
            code="renewal_offer_invalid",
        )
    return expected_target_tariff_id, price


def validate_expected_renewal_offer(
    *,
    offer: RenewalOffer,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
) -> None:
    """Require a matching target/price only for revised fresh intent."""

    expected_id, expected_price = _normalise_expected_renewal_offer(
        expected_target_tariff_id=expected_target_tariff_id,
        expected_target_price=expected_target_price,
    )
    if not offer.is_available:
        raise BusinessLogicError(
            "Тариф продления больше недоступен. Обновите карточку",
            code="renewal_offer_stale",
        )
    if not offer.is_revision and expected_id is None and expected_price is None:
        return
    if offer.is_revision and (expected_id is None or expected_price is None):
        raise BusinessLogicError(
            "Обновите предложение продления перед оплатой",
            code="renewal_offer_required",
        )
    if expected_id != offer.target_tariff_id or expected_price != offer.target_price:
        raise BusinessLogicError(
            "Цена или версия тарифа продления уже изменилась. Обновите предложение",
            code="renewal_offer_stale",
        )


def _revision_source_component_contract(
    *,
    club_id: int,
    source_tariff_id: int,
    target_tariff_id: int,
) -> dict | None:
    """Read the immutable source component contract for cross-ID carry checks."""

    if source_tariff_id == target_tariff_id:
        return None
    from apps.billing.models import TariffPriceRevision
    current_tariff_id = source_tariff_id
    visited: set[int] = set()
    source_contract = None
    while current_tariff_id not in visited:
        visited.add(current_tariff_id)
        edge = (
            TariffPriceRevision.objects.for_club(club_id)
            .filter(source_tariff_id=current_tariff_id)
            .first()
        )
        if edge is None:
            return None
        source_contract = source_contract or (edge.source_contract or {}).get("component")
        current_tariff_id = edge.target_tariff_id
        if source_contract and current_tariff_id == target_tariff_id:
            return source_contract
    return None


def _subscription_component_matches_revision_contract(
    component: SubscriptionComponent,
    contract: dict | None,
    *,
    opening_source: bool = False,
) -> bool:
    if contract is None:
        return True
    non_money_contract_matches = (
        component.training_type_id == contract.get("training_type_id")
        and component.entitlement_kind == contract.get("entitlement_kind")
        and component.weekly_limit == contract.get("weekly_limit")
        and component.scope == contract.get("scope")
        and component.location_id == contract.get("location_id")
    )
    if not non_money_contract_matches:
        return False
    if opening_source:
        # Reviewed opening imports preserve the independently audited total and
        # payout snapshot.  The opening receipt is the authority for those
        # fields; the revision edge still proves the non-money shape above.
        return True
    return (
        component.credits_total == contract.get("credits_total")
        and component.trainer_payout_policy_snapshot == contract.get("trainer_payout_policy")
    )


def find_existing_renewal_payment_family(
    *,
    club_id: int,
    source_subscription_id: int,
    payment_method: str | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date=None,
    discount_ids: list[int] | None = None,
    debt_ids: list[int] | None = None,
) -> Payment | None:
    """Return a matching pending/accepted child before current-leaf lookup.

    A renewal family is a commercial receipt for one exact channel and target
    context.  In particular, a manual cash/transfer receipt can never become
    the payment behind a later online bank order merely because both point at
    the same source subscription.
    """

    payments = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("subscription", "tariff")
        .filter(
            subscription__renewed_from_id=source_subscription_id,
            subscription__deleted_at__isnull=True,
            status__in=[Payment.Status.PENDING, Payment.Status.CONFIRMED],
        )
        .order_by("id")
    )
    if payment_method is not None:
        payments = payments.filter(payment_method=payment_method)
    if target_schedule_id is not None:
        payments = payments.filter(target_schedule_id=target_schedule_id)
    else:
        payments = payments.filter(target_schedule__isnull=True)
    if target_training_group_id is not None:
        payments = payments.filter(target_training_group_id=target_training_group_id)
    else:
        payments = payments.filter(target_training_group__isnull=True)
    if target_start_date is not None:
        payments = payments.filter(target_start_date=target_start_date)
    elif target_schedule_id is None:
        payments = payments.filter(target_start_date__isnull=True)
    payment = payments.first()
    if payment is None:
        return None

    requested_discount_ids = set(dict.fromkeys(discount_ids or []))
    existing_discount_ids = set(payment.applied_discounts.values_list("id", flat=True))
    if existing_discount_ids != requested_discount_ids:
        return None

    from apps.billing.models import Debt

    requested_debt_ids = set(dict.fromkeys(debt_ids or []))
    existing_debt_ids = set(
        Debt.objects.for_club(club_id)
        .filter(settlement_payment_id=payment.id, resolved_at__isnull=True)
        .values_list("id", flat=True)
    )
    if existing_debt_ids != requested_debt_ids:
        return None
    return payment


def build_subscription_command_fingerprint(
    *,
    student_id: int,
    tariff_id: int,
    payment_method: str,
    discount_ids: list[int] | None = None,
    debt_ids: list[int] | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date=None,
    renewed_from_subscription_id: int | None = None,
    seller_trainer_id: int | None = None,
    package_owner_trainer_id: int | None = None,
    offer_digest: str | None = None,
    buyer_email_hash: str | None = None,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
) -> str:
    """Fingerprint commercial terms only; submitting actor is deliberately absent."""

    material_parts = [
        "subscription-command-v1",
        str(student_id),
        str(tariff_id),
        payment_method,
        ",".join(str(value) for value in sorted(set(discount_ids or []))),
        ",".join(str(value) for value in sorted(set(debt_ids or []))),
        str(target_schedule_id or ""),
        str(target_training_group_id or ""),
        target_start_date.isoformat() if target_start_date is not None else "",
        str(renewed_from_subscription_id or ""),
        str(seller_trainer_id or ""),
        str(package_owner_trainer_id or ""),
        offer_digest or "",
        buyer_email_hash or "",
    ]
    if expected_target_tariff_id is not None or expected_target_price is not None:
        expected_price_part = ""
        if expected_target_price is not None:
            try:
                expected_price_part = f"{Decimal(str(expected_target_price)).quantize(Decimal('0.01')):.2f}"
            except (InvalidOperation, TypeError, ValueError):
                expected_price_part = str(expected_target_price)
        material_parts.extend(
            [str(expected_target_tariff_id or ""), expected_price_part]
        )
    material = "|".join(material_parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _require_command_identity(*, command_idempotency_key: str, command_fingerprint: str) -> str:
    key = command_idempotency_key.strip()
    if not key:
        raise BusinessLogicError(
            "Контекстная команда оплаты требует стабильный ключ",
            code="idempotency_key_required",
        )
    if len(key) > 120 or len(command_fingerprint) != 64:
        raise BusinessLogicError("Некорректная идентичность команды", code="invalid_command_identity")
    return key


def is_renewal_manual_review_error(exc: BusinessLogicError) -> bool:
    """Provider paths classify only expected immutable renewal conflicts."""

    return exc.code in RENEWAL_MANUAL_REVIEW_CODES


def validate_locked_renewal_source(
    *,
    source: Subscription,
    expected_child_id: int | None = None,
    finalized_at=None,
    components: list[SubscriptionComponent] | None = None,
) -> None:
    """Reject forks after a source has already finalized to another child.

    Callers must hold the exact source row.  Natural expiry/exhaustion is a
    valid renewal without carry; an early administrative closure is not.
    """

    finalized = SubscriptionRenewalEvent.objects.for_club(source.club_id).filter(
        renewed_from_id=source.id
    )
    if expected_child_id is not None:
        finalized = finalized.exclude(renewed_to_id=expected_child_id)
    if finalized.exists():
        raise BusinessLogicError(
            "Продление необходимо создавать от последнего абонемента цепочки",
            code="renewal_source_finalized_successor",
        )
    confirmed_successors = Subscription.objects.for_club(source.club_id).filter(
        renewed_from_id=source.id,
        deleted_at__isnull=True,
        payment__status=Payment.Status.CONFIRMED,
    )
    if expected_child_id is not None:
        confirmed_successors = confirmed_successors.exclude(id=expected_child_id)
    if confirmed_successors.exists():
        raise BusinessLogicError(
            "Продление необходимо создавать от последнего абонемента цепочки",
            code="renewal_source_finalized_successor",
        )
    eligibility = get_renewal_source_eligibility(
        source=source,
        at=finalized_at,
        components=components,
        lock_components=components is None,
    )
    if eligibility.early_closed:
        raise BusinessLogicError(
            "Исходный абонемент был закрыт до подтверждения продления",
            code="renewal_source_closed",
        )


def lock_and_validate_exact_renewal_source(
    *,
    club_id: int,
    student_id: int,
    renewed_from_subscription_id: int,
    tariff_id: int | None = None,
    validate: bool = True,
) -> Subscription:
    """Lock the source after Club/Student and validate one exact chain leaf."""

    filters = {
        "id": renewed_from_subscription_id,
        "student_id": student_id,
        "deleted_at__isnull": True,
    }
    if tariff_id is not None:
        filters["tariff_id"] = tariff_id
    source = (
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("tariff")
        .filter(**filters)
        .first()
    )
    if source is None:
        raise BusinessLogicError("Абонемент для продления не найден", code="renewal_source_not_found")
    if validate:
        validate_locked_renewal_source(source=source)
    return source


def _replay_payment_command(
    *,
    club_id: int,
    command_idempotency_key: str,
    command_fingerprint: str,
) -> Payment | None:
    existing = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("subscription")
        .filter(command_idempotency_key=command_idempotency_key)
        .first()
    )
    if existing is None:
        return None
    if existing.command_fingerprint != command_fingerprint:
        raise BusinessLogicError(
            "Idempotency key was already used for another command",
            code="idempotency_conflict",
        )
    existing._command_replayed = True
    return existing


def _create_manual_subscription_renewal_once(
    *,
    club_id: int,
    student_id: int,
    renewed_from_subscription_id: int,
    payment_method: str,
    recorded_by_id: int,
    command_idempotency_key: str,
    discount_ids: list[int] | None = None,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
) -> Payment:
    """Create one pending cash/transfer renewal from the exact source only."""

    if payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER}:
        raise BusinessLogicError("Продление вручную доступно только для cash/transfer", code="invalid_payment_method")
    key = command_idempotency_key.strip()
    if not key:
        raise BusinessLogicError(
            "Контекстная команда оплаты требует стабильный ключ",
            code="idempotency_key_required",
        )
    if len(key) > 120:
        raise BusinessLogicError("Некорректная идентичность команды", code="invalid_command_identity")
    expected_target_tariff_id, expected_target_price = _normalise_expected_renewal_offer(
        expected_target_tariff_id=expected_target_tariff_id,
        expected_target_price=expected_target_price,
    )
    with transaction.atomic():
        # D12: command root, student, then exact subscription source.
        Club.objects.select_for_update(of=("self",)).get(id=club_id)
        existing_command = (
            Payment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("subscription", "subscription__renewed_from", "tariff")
            .filter(command_idempotency_key=key)
            .first()
        )
        if existing_command is not None:
            stored_source_tariff_id = getattr(
                getattr(existing_command.subscription, "renewed_from", None),
                "tariff_id",
                existing_command.tariff_id,
            )
            stored_is_revision = stored_source_tariff_id != existing_command.tariff_id
            replay_fingerprint = build_subscription_command_fingerprint(
                student_id=student_id,
                tariff_id=existing_command.tariff_id,
                payment_method=payment_method,
                discount_ids=discount_ids,
                renewed_from_subscription_id=renewed_from_subscription_id,
                package_owner_trainer_id=existing_command.package_owner_trainer_id,
                # Same-ID legacy renewals intentionally keep the historical
                # fingerprint shape. New clients may echo the displayed offer
                # on replay, but those fields are not part of that command.
                expected_target_tariff_id=(
                    (
                        expected_target_tariff_id
                        if expected_target_tariff_id is not None
                        else existing_command.tariff_id
                    )
                    if stored_is_revision
                    else None
                ),
                expected_target_price=(
                    (
                        expected_target_price
                        if expected_target_price is not None
                        else existing_command.tariff.price
                    )
                    if stored_is_revision
                    else None
                ),
            )
            if existing_command.command_fingerprint != replay_fingerprint:
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            existing_command._command_replayed = True
            return existing_command
        source_preview = (
            Subscription.objects.for_club(club_id)
            .select_related("tariff")
            .filter(
                id=renewed_from_subscription_id,
                student_id=student_id,
                deleted_at__isnull=True,
            )
            .first()
        )
        if source_preview is None:
            raise BusinessLogicError(
                "Абонемент для продления не найден",
                code="renewal_source_not_found",
            )
        _lock_renewal_catalog_scope(club_id=club_id, source_tariff=source_preview.tariff)
        from apps.students.models import Student

        Student.objects.for_club(club_id).select_for_update(of=("self",)).get(id=student_id)
        source = lock_and_validate_exact_renewal_source(
            club_id=club_id,
            student_id=student_id,
            renewed_from_subscription_id=renewed_from_subscription_id,
            validate=False,
        )
        existing_family = find_existing_renewal_payment_family(
            club_id=club_id,
            source_subscription_id=source.id,
            payment_method=payment_method,
            discount_ids=discount_ids,
            debt_ids=[],
        )
        if existing_family is not None:
            if existing_family.command_idempotency_key == key:
                existing_family._command_replayed = True
                return existing_family
            raise BusinessLogicError(
                "У этого абонемента уже есть ожидающее или подтверждённое продление",
                code="renewal_source_finalized_successor",
            )
        offer = get_renewal_offer(
            club_id=club_id,
            source_tariff=source.tariff,
            lock=True,
        )
        validate_expected_renewal_offer(
            offer=offer,
            expected_target_tariff_id=expected_target_tariff_id,
            expected_target_price=expected_target_price,
        )
        validate_locked_renewal_source(source=source)
        from apps.trainers.models import TrainerPackageAllocation

        # Renewal inherits the exact package owner, never the student's assigned
        # trainer or a current seller. Transfers may have changed this allocation
        # since the original payment, so read its current canonical ownership.
        package_owner_trainer_id = (
            TrainerPackageAllocation.objects.for_club(club_id)
            .filter(subscription=source, is_active=True)
            .values_list("owner_trainer_id", flat=True)
            .first()
        )
        fingerprint = build_subscription_command_fingerprint(
            student_id=student_id,
            tariff_id=offer.target_tariff_id,
            payment_method=payment_method,
            discount_ids=discount_ids,
            renewed_from_subscription_id=source.id,
            package_owner_trainer_id=package_owner_trainer_id,
            expected_target_tariff_id=offer.target_tariff_id if offer.is_revision else None,
            expected_target_price=offer.target_price if offer.is_revision else None,
        )
        _require_command_identity(
            command_idempotency_key=key,
            command_fingerprint=fingerprint,
        )
        pending = (
            Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                renewed_from_id=source.id,
                status=Subscription.Status.PENDING,
                deleted_at__isnull=True,
            )
            .first()
        )
        if pending is not None:
            raise BusinessLogicError("У этого абонемента уже есть ожидающее продление", code="renewal_pending_exists")
        payment_creation = import_module(
            "apps.billing.service_modules.payment_creation"
        )
        payment = payment_creation.create_payment(
            club_id=club_id,
            student_id=student_id,
            tariff_id=offer.target_tariff_id,
            payment_method=payment_method,
            discount_ids=discount_ids,
            debt_ids=[],
            recorded_by_id=recorded_by_id,
            allow_renewal=True,
            renewed_from_subscription_id=source.id,
            renewal_chain_id=source.renewal_chain_id,
            command_idempotency_key=key,
            command_fingerprint=fingerprint,
            package_owner_trainer_id=package_owner_trainer_id,
            renewal_source_tariff_id=source.tariff_id,
            expected_target_tariff_id=offer.target_tariff_id if offer.is_revision else None,
            expected_target_price=offer.target_price if offer.is_revision else None,
        )
        return payment


def _is_postgresql_deadlock(error: OperationalError) -> bool:
    cause = error.__cause__
    return (
        getattr(cause, "sqlstate", None) == "40P01"
        or "deadlock detected" in str(error).lower()
    )


def create_manual_subscription_renewal(
    *,
    club_id: int,
    student_id: int,
    renewed_from_subscription_id: int,
    payment_method: str,
    recorded_by_id: int,
    command_idempotency_key: str,
    discount_ids: list[int] | None = None,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
) -> Payment:
    """Create a renewal, retrying one transient PostgreSQL deadlock.

    Price revision and renewal intentionally arbitrate the same catalog rows.
    PostgreSQL can abort the renewal side when the two transactions cross at
    an FK check; the failed transaction is safe to retry because command
    identity and family lookup are both durable and idempotent.
    """

    for attempt in range(2):
        try:
            return _create_manual_subscription_renewal_once(
                club_id=club_id,
                student_id=student_id,
                renewed_from_subscription_id=renewed_from_subscription_id,
                payment_method=payment_method,
                recorded_by_id=recorded_by_id,
                command_idempotency_key=command_idempotency_key,
                discount_ids=discount_ids,
                expected_target_tariff_id=expected_target_tariff_id,
                expected_target_price=expected_target_price,
            )
        except OperationalError as error:
            if attempt or not _is_postgresql_deadlock(error):
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def _component_identity(component: SubscriptionComponent) -> tuple:
    return (
        component.training_type_id,
        component.entitlement_kind,
        component.scope,
        component.location_id,
    )


def _locked_subscription_components(*, subscription: Subscription) -> list[SubscriptionComponent]:
    return list(
        SubscriptionComponent.objects.for_club(subscription.club_id)
        .select_for_update(of=("self",))
        .filter(subscription=subscription, is_active=True)
        .order_by("id")
    )


@dataclass(frozen=True)
class RenewalSourceEligibility:
    """One entitlement-aware renewal decision used by read/create/finalize."""

    outcome: str
    source_usable: bool
    early_closed: bool


def get_renewal_source_eligibility(
    *,
    source: Subscription,
    at=None,
    components: list[SubscriptionComponent] | None = None,
    lock_components: bool = False,
) -> RenewalSourceEligibility:
    """Classify exact-source carry eligibility without inferring a replacement.

    An EXPIRED subscription with a future/null expiry is legitimate only when
    its finite aggregate/component entitlement has been exhausted.  A usable
    package in that state was administratively closed and must fail closed.
    """

    checked_at = at or timezone.now()
    if components is None:
        component_qs = SubscriptionComponent.objects.for_club(source.club_id).filter(
            subscription=source,
            is_active=True,
        ).order_by("id")
        if lock_components:
            component_qs = component_qs.select_for_update(of=("self",))
        components = list(component_qs)
    has_usable_entitlement = _subscription_has_usable_entitlement(
        subscription=source,
        components=components,
    )
    naturally_expired = source.expires_at is not None and source.expires_at <= checked_at
    if source.status in {
        Subscription.Status.CANCELLED,
        Subscription.Status.FROZEN,
        Subscription.Status.PENDING,
    }:
        return RenewalSourceEligibility(
            outcome="closed",
            source_usable=False,
            early_closed=True,
        )
    if source.status == Subscription.Status.EXPIRED:
        if naturally_expired:
            return RenewalSourceEligibility(
                outcome="expired",
                source_usable=False,
                early_closed=False,
            )
        if not has_usable_entitlement:
            return RenewalSourceEligibility(
                outcome="exhausted",
                source_usable=False,
                early_closed=False,
            )
        return RenewalSourceEligibility(
            outcome="closed",
            source_usable=False,
            early_closed=True,
        )
    if naturally_expired:
        return RenewalSourceEligibility(
            outcome="expired",
            source_usable=False,
            early_closed=False,
        )
    if not has_usable_entitlement:
        return RenewalSourceEligibility(
            outcome="exhausted",
            source_usable=False,
            early_closed=False,
        )
    return RenewalSourceEligibility(
        outcome="carried",
        source_usable=True,
        early_closed=False,
    )


def is_exact_renewal_source_actionable(*, source: Subscription, at=None) -> bool:
    """Read-side exact-source gate; ambiguous/terminal families stay hidden."""

    eligibility = get_renewal_source_eligibility(source=source, at=at)
    if eligibility.early_closed:
        return False
    if SubscriptionRenewalEvent.objects.for_club(source.club_id).filter(
        renewed_from_id=source.id
    ).exists():
        return False
    if Subscription.objects.for_club(source.club_id).filter(
        renewed_from_id=source.id,
        deleted_at__isnull=True,
        payment__status=Payment.Status.CONFIRMED,
    ).exists():
        return False
    return not Subscription.objects.for_club(source.club_id).filter(
        renewed_from_id=source.id,
        deleted_at__isnull=True,
        status=Subscription.Status.PENDING,
    ).exists()


def _subscription_has_usable_entitlement(
    *,
    subscription: Subscription,
    components: list[SubscriptionComponent],
) -> bool:
    """Weekly/unlimited proves usability but is never summed into a renewal."""

    if not components:
        return subscription.trainings_left is None or subscription.trainings_left > 0
    return any(
        component.entitlement_kind
        in {
            TariffComponent.EntitlementKind.WEEKLY_LIMIT,
            TariffComponent.EntitlementKind.UNLIMITED,
        }
        or (
            component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
            and component.credits_left is not None
            and component.credits_left > 0
        )
        for component in components
    )


def _carry_compatible_components(
    *,
    old_subscription: Subscription,
    new_subscription: Subscription,
    component_mapping: dict | None = None,
) -> list[dict]:
    old_components = _locked_subscription_components(subscription=old_subscription)
    new_components = _locked_subscription_components(subscription=new_subscription)
    from apps.billing.models import OpeningEntitlementSnapshot

    opening_component_ids = set(OpeningEntitlementSnapshot.objects.for_club(old_subscription.club_id).filter(
        subscription=old_subscription,
    ).values_list("component_id", flat=True))
    new_by_tariff_component = {
        component.tariff_component_id: component
        for component in new_components
        if component.tariff_component_id is not None
    }
    mapped_component_ids = component_mapping or {}
    revision_contract = _revision_source_component_contract(
        club_id=old_subscription.club_id,
        source_tariff_id=old_subscription.tariff_id,
        target_tariff_id=new_subscription.tariff_id,
    )
    legacy_new_by_identity: dict[tuple, list[SubscriptionComponent]] = {}
    for component in new_components:
        if component.tariff_component_id is None:
            legacy_new_by_identity.setdefault(_component_identity(component), []).append(component)
    legacy_offsets: dict[tuple, int] = {}
    carried: list[dict] = []
    for old_component in old_components:
        if old_component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS:
            continue
        opening_source = old_component.id in opening_component_ids
        if (
            old_subscription.tariff_id != new_subscription.tariff_id
            and (
                revision_contract is None
                or not _subscription_component_matches_revision_contract(
                    old_component,
                    revision_contract,
                    opening_source=opening_source,
                )
            )
        ):
            if old_component.credits_left and old_component.credits_left > 0:
                raise BusinessLogicError(
                    "Покупка не соответствует контракту версии тарифа.",
                    code="renewal_source_mismatch",
                )
            continue
        if old_component.tariff_component_id is not None:
            target_component_id = mapped_component_ids.get(old_component.tariff_component_id)
            if target_component_id is None and old_subscription.tariff_id == new_subscription.tariff_id:
                target_component_id = old_component.tariff_component_id
            new_component = new_by_tariff_component.get(target_component_id)
        else:
            identity = _component_identity(old_component)
            if old_component.id in opening_component_ids:
                # An opening receipt proves one reviewed finite source even if
                # its catalog component did not exist at import time. Renewal
                # may just have materialized that catalog row. Require exactly
                # one compatible child; never silently lose reviewed carry.
                candidates = [component for component in new_components if _component_identity(component) == identity]
                new_component = candidates[0] if len(candidates) == 1 else None
            else:
                offset = legacy_offsets.get(identity, 0)
                candidates = legacy_new_by_identity.get(identity, [])
                new_component = candidates[offset] if offset < len(candidates) else None
                legacy_offsets[identity] = offset + 1
        if old_component.id in opening_component_ids and old_component.credits_left and new_component is None:
            raise BusinessLogicError(
                "Остаток перенесённого пакета нельзя однозначно сопоставить с продлением.",
                code="opening_renewal_component_needs_review",
            )
        if (
            old_subscription.tariff_id != new_subscription.tariff_id
            and old_component.credits_left
            and old_component.credits_left > 0
            and new_component is None
        ):
            raise BusinessLogicError(
                "Компонент версии тарифа нельзя однозначно сопоставить с продлением.",
                code="renewal_source_mismatch",
            )
        if new_component is None or old_component.credits_left is None or new_component.credits_left is None:
            continue
        if old_component.credits_left <= 0:
            continue
        new_component.credits_left += old_component.credits_left
        new_component.save(update_fields=["credits_left", "updated_at"])
        carried.append(
            {
                "kind": "finite_credits",
                "from_component_id": old_component.id,
                "to_component_id": new_component.id,
                "credits": old_component.credits_left,
            }
        )
    return carried


def _lock_renewal_subscriptions(*, club_id: int, old_id: int, new_id: int) -> tuple[Subscription, Subscription]:
    """Acquire source/new subscription locks in ascending primary-key order."""

    subscriptions = list(
        Subscription.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("tariff")
        .filter(id__in=[old_id, new_id])
        .order_by("id")
    )
    by_id = {subscription.id: subscription for subscription in subscriptions}
    if old_id not in by_id or new_id not in by_id:
        raise BusinessLogicError("Абонемент для продления не найден", code="renewal_source_not_found")
    return by_id[old_id], by_id[new_id]


def _grandfather_legacy_renewal_chain(*, old_subscription: Subscription, new_subscription: Subscription) -> None:
    """Backfill only an unambiguous pre-Slice6 source chain under both locks."""

    if old_subscription.renewal_chain_id is not None:
        return
    candidate_chain = new_subscription.renewal_chain_id
    order_chain = (
        BankPaymentOrder.objects.for_club(old_subscription.club_id)
        .filter(subscription_id=new_subscription.id)
        .exclude(renewal_chain_id__isnull=True)
        .values_list("renewal_chain_id", flat=True)
        .first()
    )
    if candidate_chain is not None and order_chain is not None and candidate_chain != order_chain:
        raise BusinessLogicError("Цепочка продления требует ручной проверки", code="renewal_source_mismatch")
    candidate_chain = candidate_chain or order_chain
    if candidate_chain is None:
        raise BusinessLogicError("Цепочка продления требует ручной проверки", code="renewal_source_mismatch")
    if (
        Subscription.objects.for_club(old_subscription.club_id)
        .filter(renewed_from_id=old_subscription.id)
        .exclude(id=new_subscription.id)
        .exclude(renewal_chain_id__isnull=True)
        .exclude(renewal_chain_id=candidate_chain)
        .exists()
    ):
        raise BusinessLogicError("Цепочка продления требует ручной проверки", code="renewal_source_mismatch")
    old_subscription.renewal_chain_id = candidate_chain
    old_subscription.save(update_fields=["renewal_chain_id", "updated_at"])


def finalize_subscription_renewal(
    *,
    club_id: int,
    payment_id: int,
    finalized_at,
) -> SubscriptionRenewalEvent | None:
    """Finalize an exact renewal before payment state becomes confirmed.

    The caller owns the surrounding financial transaction.  Raising here leaves
    the pending payment/subscription untouched, which allows provider callers
    to classify it as manual review without inventing a second rule set.
    """

    payment = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("subscription")
        .get(id=payment_id)
    )
    if payment.subscription_id is None:
        return None
    new_subscription_preview = (
        Subscription.objects.for_club(club_id)
        .only("id", "renewed_from_id")
        .get(id=payment.subscription_id)
    )
    if new_subscription_preview.renewed_from_id is None:
        return None
    old_subscription, new_subscription = _lock_renewal_subscriptions(
        club_id=club_id,
        old_id=new_subscription_preview.renewed_from_id,
        new_id=new_subscription_preview.id,
    )
    existing = (
        SubscriptionRenewalEvent.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(renewed_to_id=new_subscription.id)
        .first()
    )
    if existing is not None:
        return existing
    if old_subscription.student_id != new_subscription.student_id:
        raise BusinessLogicError("Источник продления не принадлежит ученику", code="renewal_source_not_found")
    component_mapping = {}
    if old_subscription.tariff_id != new_subscription.tariff_id:
        try:
            component_mapping = get_renewal_tariff_component_mapping(
                club_id=club_id,
                source_tariff_id=old_subscription.tariff_id,
                target_tariff_id=new_subscription.tariff_id,
            )
        except BusinessLogicError as exc:
            if exc.code in {
                "tariff_revision_lineage_missing",
                "tariff_revision_mapping_missing",
                "tariff_revision_incompatible",
                "tariff_revision_cycle",
            }:
                raise BusinessLogicError(
                    "Тариф продления не совпадает с источником",
                    code="renewal_source_mismatch",
                ) from exc
            raise
    old_components = _locked_subscription_components(subscription=old_subscription)
    validate_locked_renewal_source(
        source=old_subscription,
        expected_child_id=new_subscription.id,
        finalized_at=finalized_at,
        components=old_components,
    )
    _grandfather_legacy_renewal_chain(
        old_subscription=old_subscription,
        new_subscription=new_subscription,
    )
    if (
        old_subscription.renewal_chain_id is None
        or new_subscription.renewal_chain_id != old_subscription.renewal_chain_id
    ):
        raise BusinessLogicError("Цепочка продления не совпадает с источником", code="renewal_source_mismatch")

    eligibility = get_renewal_source_eligibility(
        source=old_subscription,
        at=finalized_at,
        components=old_components,
    )
    carry_snapshot: dict = {
        "source_status": old_subscription.status,
        "outcome": eligibility.outcome,
        "carried_days": 0,
        "legacy_finite_credits": 0,
        "components": [],
    }
    expiry_carried = False
    legacy_credits_carried = False
    legacy_component_carry = []
    if eligibility.source_usable:
        if old_subscription.expires_at is not None:
            new_subscription.expires_at = old_subscription.expires_at + timedelta(
                days=new_subscription.tariff.duration_days
            )
            carry_snapshot["carried_days"] = max(
                0,
                (old_subscription.expires_at.date() - finalized_at.date()).days,
            )
            expiry_carried = True
        # Legacy subscriptions without component snapshots retain their finite
        # aggregate only. Component packages carry finite components below;
        # weekly/unlimited capacity is deliberately never summed.
        old_has_components = bool(old_components)
        if (
            not old_has_components
            and old_subscription.trainings_left is not None
            and new_subscription.trainings_left is not None
            and old_subscription.trainings_left > 0
        ):
            new_components = _locked_subscription_components(subscription=new_subscription)
            if new_components:
                if not (
                    len(new_components) == 1
                    and new_components[0].entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
                    and new_components[0].training_type_id == old_subscription.tariff.training_type_id
                    and new_components[0].scope == old_subscription.scope
                    and new_components[0].location_id == old_subscription.location_id
                ):
                    raise BusinessLogicError(
                        "Нужно сверить, в какой компонент переносить старый остаток.",
                        code="opening_renewal_component_needs_review",
                    )
                target_component = new_components[0]
                target_component.credits_left += old_subscription.trainings_left
                target_component.save(update_fields=["credits_left", "updated_at"])
                legacy_component_carry = [{
                    "kind": "finite_credits", "from_legacy_subscription_id": old_subscription.id,
                    "to_component_id": target_component.id, "credits": old_subscription.trainings_left,
                    "source_training_type_id": target_component.training_type_id,
                    "source_scope": old_subscription.scope, "source_location_id": old_subscription.location_id,
                }]
            else:
                new_subscription.trainings_left += old_subscription.trainings_left
                carry_snapshot["legacy_finite_credits"] = old_subscription.trainings_left
                legacy_credits_carried = True
        carry_snapshot["components"] = _carry_compatible_components(
            old_subscription=old_subscription,
            new_subscription=new_subscription,
            component_mapping=component_mapping,
        )
        carry_snapshot["components"].extend(legacy_component_carry)
    # A successful renewal never leaves an active source in parallel with the
    # confirmed child, even when no finite entitlement was carryable.
    if old_subscription.status == Subscription.Status.ACTIVE:
        old_subscription.status = Subscription.Status.EXPIRED
        old_subscription.save(update_fields=["status", "updated_at"])

    update_fields = ["updated_at"]
    if expiry_carried:
        update_fields.append("expires_at")
    if legacy_credits_carried:
        update_fields.append("trainings_left")
    new_subscription.save(update_fields=update_fields)
    event = SubscriptionRenewalEvent(
        club_id=club_id,
        renewed_from=old_subscription,
        renewed_to=new_subscription,
        payment=payment,
        finalized_at=finalized_at,
        carry_snapshot=carry_snapshot,
    )
    event.full_clean()
    event.save()
    if not legacy_credits_carried:
        from apps.billing.service_modules.entitlements import refresh_subscription_counters

        refresh_subscription_counters(subscription=new_subscription)
    return event
