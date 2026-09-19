from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction

from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.service_modules.tariff_revisions import (
    get_renewal_tariff_component_mapping,
    resolve_current_renewal_tariff,
    revise_tariff_price,
)
from apps.billing.tests.factories import TariffComponentFactory, TariffFactory, TrainingTypeFactory
from apps.common.exceptions import BusinessLogicError


def _finite_tariff(*, club, name="Group 8", price=Decimal("5000"), default=False):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL if default else TrainingType.Kind.GROUP,
        drop_in_price=price if default else None,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name=name,
        price=price,
        trainings_limit=8 if not default else 1,
        duration_days=30,
        is_personal_booking_default=default,
        trainer_payout_policy="on_checkin" if default else "on_payment",
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        name=name,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=tariff.trainings_limit,
        paid_amount_basis=price,
        trainer_payout_policy="on_checkin" if default else "on_payment",
    )
    return tariff


@pytest.mark.django_db
def test_revision_clones_price_only_contract_and_resolves_current_leaf(club, owner_user):
    source = _finite_tariff(club=club)
    source_component = source.components.get(is_active=True)

    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("5500"),
        new_name="Group 8 updated",
        actor_user_id=owner_user.id,
        idempotency_key="revision-1",
    )

    source.refresh_from_db()
    target = revision.target_tariff
    target_component = revision.target_component
    assert source.is_active is False
    assert target.is_active is True
    assert source.price == Decimal("5000.00")
    assert target.price == Decimal("5500.00")
    assert target.name == "Group 8 updated"
    assert target.trainings_limit == source.trainings_limit == 8
    assert target.duration_days == source.duration_days == 30
    assert target_component.credits_total == source_component.credits_total == 8
    assert target_component.trainer_payout_policy == source_component.trainer_payout_policy
    assert get_renewal_tariff_component_mapping(
        club_id=club.id,
        source_tariff_id=source.id,
        target_tariff_id=target.id,
    ) == {source_component.id: target_component.id}
    assert resolve_current_renewal_tariff(club_id=club.id, source_tariff_id=source.id).id == target.id


@pytest.mark.django_db
def test_revision_maps_historical_source_component_ids(club, owner_user):
    source = _finite_tariff(club=club)
    old_component = TariffComponentFactory(
        club=club,
        tariff=source,
        name="Old display name",
        training_type=source.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source.price,
        trainer_payout_policy="on_payment",
        is_active=False,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("5500"),
        new_name="New display name",
        actor_user_id=owner_user.id,
        idempotency_key="revision-historical-component",
    )

    assert revision.component_mapping[str(old_component.id)] == revision.target_component_id
    mapping = get_renewal_tariff_component_mapping(
        club_id=club.id,
        source_tariff_id=source.id,
        target_tariff_id=revision.target_tariff_id,
    )
    assert mapping[old_component.id] == revision.target_component_id


@pytest.mark.django_db
def test_revision_chain_composes_lineage_and_replays_only_after_auth(club, owner_user, trainer_user):
    source = _finite_tariff(club=club)
    first = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("5500"),
        new_name="Version B",
        actor_user_id=owner_user.id,
        idempotency_key="revision-chain-a-b",
    )
    second = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=first.target_tariff_id,
        new_price=Decimal("6000"),
        new_name="Version C",
        actor_user_id=owner_user.id,
        idempotency_key="revision-chain-b-c",
    )

    assert resolve_current_renewal_tariff(club_id=club.id, source_tariff_id=source.id).id == second.target_tariff_id
    source_component = source.components.get(is_active=True)
    assert get_renewal_tariff_component_mapping(
        club_id=club.id,
        source_tariff_id=source.id,
        target_tariff_id=second.target_tariff_id,
    )[source_component.id] == second.target_component_id

    replay = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("5500"),
        new_name="Version B",
        actor_user_id=owner_user.id,
        idempotency_key="revision-chain-a-b",
    )
    assert replay.id == first.id

    with pytest.raises(BusinessLogicError) as unauthorized:
        revise_tariff_price(
            club_id=club.id,
            source_tariff_id=source.id,
            new_price=Decimal("5500"),
            new_name="Version B",
            actor_user_id=trainer_user.id,
            idempotency_key="revision-chain-a-b",
        )
    assert unauthorized.value.code == "actor_not_authorized"


@pytest.mark.django_db
def test_revision_rejects_unsupported_shape_and_seals_both_tariff_versions(club, owner_user):
    source = _finite_tariff(club=club)
    TariffComponentFactory(
        club=club,
        tariff=source,
        name="Second component",
        training_type=source.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=Decimal("0.01"),
        trainer_payout_policy="on_payment",
        sort_order=1,
    )
    with pytest.raises(BusinessLogicError) as unsupported:
        revise_tariff_price(
            club_id=club.id,
            source_tariff_id=source.id,
            new_price=Decimal("5500"),
            new_name="Unsupported",
            actor_user_id=owner_user.id,
            idempotency_key="revision-unsupported",
        )
    assert unsupported.value.code == "tariff_revision_shape_unsupported"

    source = _finite_tariff(club=club, name="Sealed", price=Decimal("5000"))
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("5500"),
        new_name="Sealed next",
        actor_user_id=owner_user.id,
        idempotency_key="revision-sealed",
    )
    for tariff in (source, revision.target_tariff):
        tariff.name = "Direct mutation"
        with pytest.raises(ValidationError):
            tariff.save()
    with pytest.raises(ValidationError):
        source.__class__.objects.filter(id=source.id).update(is_active=True)
    revision.target_tariff.__class__.objects.filter(id=revision.target_tariff_id).update(
        is_active=False,
    )
    revision.target_tariff.__class__.objects.filter(id=revision.target_tariff_id).update(
        is_active=True,
    )
    with pytest.raises(BusinessLogicError) as guarded:
        update_tariff(tariff_id=revision.target_tariff_id, club_id=club.id, price=Decimal("7000"))
    assert guarded.value.code == "tariff_revision_contract_sealed"


@pytest.mark.django_db
def test_revision_transfers_generic_personal_default_and_drop_in_price(club, owner_user):
    source = _finite_tariff(club=club, name="Personal", price=Decimal("2000"), default=True)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("2500"),
        new_name="Personal current",
        actor_user_id=owner_user.id,
        idempotency_key="revision-personal-default",
    )
    source.refresh_from_db()
    training_type = source.training_type
    training_type.refresh_from_db()
    assert source.is_personal_booking_default is False
    assert revision.target_tariff.is_personal_booking_default is True
    assert training_type.drop_in_price == Decimal("2500.00")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics for competing revisions",
)
def test_postgresql_competing_revisions_leave_one_successor(club, owner_user):
    source = _finite_tariff(club=club, name="Race source")
    barrier = Barrier(2)
    transaction.commit()

    def revise(*, price: str, key: str):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            revision = revise_tariff_price(
                club_id=club.id,
                source_tariff_id=source.id,
                new_price=Decimal(price),
                new_name=f"Concurrent {price}",
                actor_user_id=owner_user.id,
                idempotency_key=key,
            )
            return "ok", revision.id
        except BusinessLogicError as exc:
            return "error", exc.code
        finally:
            connection.close()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda args: revise(price=args[0], key=args[1]),
                (("5500", "pg-revision-a"), ("6000", "pg-revision-b")),
            )
        )

    assert sum(kind == "ok" for kind, _value in outcomes) == 1, outcomes
    assert ("error", "tariff_revision_source_not_current") in outcomes
    assert Tariff.objects.for_club(club).filter(is_active=True).count() == 1
    assert Tariff.objects.for_club(club).filter(name__startswith="Concurrent ").count() == 1
