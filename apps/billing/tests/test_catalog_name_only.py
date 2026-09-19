from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.db.models import F
from django.utils import timezone

from apps.attendance.models import PersonalServiceTermsSnapshot
from apps.attendance.services.drop_in import book_personal_drop_in
from apps.billing.models import (
    Subscription,
    Tariff,
    TariffComponent,
    TariffPriceRevision,
    TrainingType,
)
from apps.billing.service_modules.catalog import _set_personal_booking_default, update_tariff
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.renewals import create_manual_subscription_renewal
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerLocation, TrainerRate
from apps.trainers.tests.factories import TrainerFactory


def _finite_tariff(*, club, name="Package", price=Decimal("8000")):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
        drop_in_price=None,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name=name,
        price=price,
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        name=name,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
        sort_order=0,
    )
    return tariff, component


def _component_payload(component, *, name: str) -> dict:
    return {
        "name": name,
        "training_type_id": component.training_type_id,
        "entitlement_kind": component.entitlement_kind,
        "credits_total": component.credits_total,
        "weekly_limit": component.weekly_limit,
        "scope": component.scope,
        "location_id": component.location_id,
        "trainer_payout_policy": component.trainer_payout_policy,
        "paid_amount_basis": component.paid_amount_basis,
    }


def _open_personal_booking(*, club, owner_user):
    trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=Decimal("2000"),
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="Персональная разовая",
        price=Decimal("2000"),
        trainings_limit=1,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        name=tariff.name,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        paid_amount_basis=tariff.price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        sort_order=0,
    )
    TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
    TrainerRate.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        percent=Decimal("50"),
    )
    student = StudentFactory(club=club, status=Student.Status.LEAD)
    starts_at = (timezone.now() + timedelta(days=7)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    result = book_personal_drop_in(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        location_id=location.id,
        training_type_id=training_type.id,
        tariff_id=tariff.id,
        actor_user_id=owner_user.id,
        idempotency_key="catalog-name-only-open-booking",
    )
    return tariff, component, result.booking


@pytest.mark.django_db
def test_name_only_default_edit_with_open_personal_booking_preserves_identity_and_snapshots(
    club,
    owner_user,
):
    tariff, component, booking = _open_personal_booking(club=club, owner_user=owner_user)
    terms = PersonalServiceTermsSnapshot.objects.for_club(club).get(booking=booking)
    component_id = component.id
    component_financials = (
        component.training_type_id,
        component.entitlement_kind,
        component.credits_total,
        component.weekly_limit,
        component.scope,
        component.location_id,
        component.trainer_payout_policy,
        component.paid_amount_basis,
        component.sort_order,
    )
    purchased_snapshot = (
        booking.tariff_name_snapshot,
        booking.price_snapshot,
        terms.tariff_id_snapshot,
        terms.tariff_name_snapshot,
        terms.base_amount,
        terms.payable_amount,
    )

    update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        name="Персональная разовая · новый зал",
    )

    tariff.refresh_from_db()
    component.refresh_from_db()
    booking.refresh_from_db()
    terms.refresh_from_db()
    assert tariff.name == "Персональная разовая · новый зал"
    assert component.id == component_id
    assert component.name == tariff.name
    assert (
        component.training_type_id,
        component.entitlement_kind,
        component.credits_total,
        component.weekly_limit,
        component.scope,
        component.location_id,
        component.trainer_payout_policy,
        component.paid_amount_basis,
        component.sort_order,
    ) == component_financials
    assert (
        booking.tariff_name_snapshot,
        booking.price_snapshot,
        terms.tariff_id_snapshot,
        terms.tariff_name_snapshot,
        terms.base_amount,
        terms.payable_amount,
    ) == purchased_snapshot
    assert TariffComponent.objects.for_club(club).filter(tariff=tariff, is_active=True).count() == 1


@pytest.mark.django_db
def test_open_personal_booking_still_blocks_actual_price_change(club, owner_user):
    tariff, component, _booking = _open_personal_booking(club=club, owner_user=owner_user)

    with pytest.raises(BusinessLogicError) as exc_info:
        update_tariff(
            tariff_id=tariff.id,
            club_id=club.id,
            price=Decimal("2200"),
        )

    assert exc_info.value.code == "personal_drop_in_contract_change_blocked"
    tariff.refresh_from_db()
    component.refresh_from_db()
    assert tariff.price == Decimal("2000.00")
    assert component.paid_amount_basis == Decimal("2000.00")


@pytest.mark.django_db
def test_name_only_default_edit_keeps_remaining_credits_on_same_tariff_renewal(
    club,
    owner_user,
):
    tariff, component = _finite_tariff(club=club)
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=4,
        expires_at=timezone.now() + timedelta(days=7),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=component,
        credits_left=4,
    )

    update_tariff(tariff_id=tariff.id, club_id=club.id, name="Package renamed")

    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="catalog-name-only-renewal",
        )
        verify_payment(
            club_id=club.id,
            payment_id=payment.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

    child = payment.subscription
    child.refresh_from_db()
    assert child.tariff_id == tariff.id
    assert child.trainings_left == 12
    assert child.components.get().credits_left == 12


@pytest.mark.django_db
def test_explicit_component_name_edit_retains_replacement_semantics(club):
    tariff, component = _finite_tariff(club=club)

    update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        components=[_component_payload(component, name="Explicit component label")],
    )

    component.refresh_from_db()
    replacement = TariffComponent.objects.for_club(club).get(
        tariff=tariff,
        is_active=True,
    )
    assert component.is_active is False
    assert replacement.id != component.id
    assert replacement.name == "Explicit component label"


@pytest.mark.django_db
def test_sealed_source_and_current_tariff_reject_name_only_edit(club, owner_user):
    source, _component = _finite_tariff(club=club)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Package current",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-name-only-sealed",
    )

    for tariff_id in (source.id, revision.target_tariff_id):
        with pytest.raises(BusinessLogicError) as exc_info:
            update_tariff(
                tariff_id=tariff_id,
                club_id=club.id,
                name="Forbidden rename",
            )
        assert exc_info.value.code == "tariff_revision_contract_sealed"
        assert exc_info.value.message == (
            "Версия тарифа зафиксирована изменением цены. Для новой цены используйте действие «Изменить цену»."
        )


@pytest.mark.django_db
def test_personal_booking_default_helper_rejects_sealed_source(club, owner_user):
    source, _component = _finite_tariff(club=club)
    revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Package current",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-default-sealed",
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        _set_personal_booking_default(tariff=source, enabled=True)

    assert exc_info.value.code == "tariff_revision_contract_sealed"
    assert exc_info.value.message == "Версия тарифа зафиксирована изменением цены."
    source.refresh_from_db()
    assert source.is_personal_booking_default is False


@pytest.mark.django_db
def test_revision_seals_source_lifecycle_and_preserves_current_leaf_archive_restore(
    club,
    owner_user,
):
    source, _component = _finite_tariff(club=club)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Package current",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-sealed-lifecycle",
    )

    source.refresh_from_db()
    assert source.is_active is False
    source.is_active = True
    with pytest.raises(ValidationError):
        source.save()
    source.refresh_from_db()
    with pytest.raises(ValidationError):
        Tariff.objects.for_club(club).filter(id=source.id).update(is_active=1)
    with pytest.raises(ValidationError):
        Tariff.objects.for_club(club).filter(id=source.id).update(is_active=F("is_active"))

    current = revision.target_tariff
    Tariff.objects.for_club(club).filter(id=current.id).update(is_active=False)
    Tariff.objects.for_club(club).filter(id=current.id).update(is_active=True)
    current.refresh_from_db()
    assert current.is_active is True
    current.is_active = 1
    with pytest.raises(ValidationError):
        current.save(update_fields=["is_active"])


@pytest.mark.django_db
def test_revision_guards_original_tenant_identity_and_bulk_create_validation(
    club,
    other_club,
    owner_user,
):
    source, source_component = _finite_tariff(club=club)
    revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Sealed source",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-tenant-sealed",
    )
    foreign_tariff, foreign_component = _finite_tariff(club=other_club, name="Foreign")

    source.club_id = other_club.id
    with pytest.raises(ValidationError):
        source.save(update_fields=["club", "updated_at"])
    source.refresh_from_db()
    assert source.club_id == club.id

    source_component.club_id = other_club.id
    with pytest.raises(ValidationError):
        source_component.save(update_fields=["club", "updated_at"])
    source_component.refresh_from_db()
    assert source_component.club_id == club.id

    revision = TariffPriceRevision(
        club_id=club.id,
        source_tariff=source,
        target_tariff=foreign_tariff,
        source_component=source_component,
        target_component=foreign_component,
        actor_id=owner_user.id,
        idempotency_key="catalog-cross-tenant-bulk",
        payload_fingerprint="cross-tenant",
    )
    with pytest.raises(ValidationError):
        TariffPriceRevision.objects.for_club(club).bulk_create([revision])
    assert not TariffPriceRevision.objects.unscoped().filter(
        idempotency_key="catalog-cross-tenant-bulk",
    ).exists()


@pytest.mark.django_db
def test_revision_seals_component_reparenting_and_orm_upserts(club, owner_user):
    source, source_component = _finite_tariff(club=club)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Sealed target",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-component-parent-sealed",
    )
    movable_tariff, movable_component = _finite_tariff(club=club, name="Movable")

    movable_component.tariff_id = revision.target_tariff_id
    with pytest.raises(ValidationError):
        movable_component.save(update_fields=["tariff_id", "updated_at"])
    movable_component.refresh_from_db()
    assert movable_component.tariff_id == movable_tariff.id
    with pytest.raises(ValidationError):
        TariffComponent.objects.for_club(club).filter(id=movable_component.id).update(
            tariff_id=revision.target_tariff_id,
        )

    late_component = TariffComponent(
        club_id=club.id,
        tariff_id=revision.target_tariff_id,
        name="Late component",
        training_type_id=revision.target_tariff.training_type_id,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=Decimal("8500"),
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    with pytest.raises(ValidationError):
        TariffComponent.objects.bulk_create([late_component])

    tariff_upsert = Tariff(
        id=source.id,
        club_id=club.id,
        training_type_id=source.training_type_id,
        name="Upserted source",
        price=Decimal("9999"),
        trainings_limit=8,
        duration_days=30,
    )
    with pytest.raises(ValidationError):
        Tariff.objects.bulk_create(
            [tariff_upsert],
            update_conflicts=True,
            update_fields=["price"],
            unique_fields=["id"],
        )

    component_upsert = TariffComponent(
        id=source_component.id,
        club_id=club.id,
        tariff_id=source.id,
        name=source_component.name,
        training_type_id=source_component.training_type_id,
        entitlement_kind=source_component.entitlement_kind,
        credits_total=source_component.credits_total,
        paid_amount_basis=Decimal("9999"),
        trainer_payout_policy=source_component.trainer_payout_policy,
    )
    with pytest.raises(ValidationError):
        TariffComponent.objects.bulk_create(
            [component_upsert],
            update_conflicts=True,
            update_fields=["paid_amount_basis"],
            unique_fields=["id"],
        )

    with pytest.raises(ValidationError):
        TariffPriceRevision.objects.bulk_create(
            [revision],
            update_conflicts=True,
            update_fields=["payload_fingerprint"],
            unique_fields=["id"],
        )


@pytest.mark.django_db
def test_explicit_pk_existing_tariff_and_component_rows_use_sealed_guards(
    club,
    other_club,
    owner_user,
):
    source, source_component = _finite_tariff(club=club)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source.id,
        new_price=Decimal("8500"),
        new_name="Explicit PK target",
        actor_user_id=owner_user.id,
        idempotency_key="catalog-explicit-pk-sealed",
    )
    source.refresh_from_db()
    source_payload = {
        "id": source.id,
        "club_id": club.id,
        "training_type_id": source.training_type_id,
        "name": source.name,
        "price": source.price,
        "trainings_limit": source.trainings_limit,
        "duration_days": source.duration_days,
        "scope": source.scope,
        "is_active": True,
        "trainer_payout_policy": source.trainer_payout_policy,
    }
    with pytest.raises(ValidationError):
        Tariff(**source_payload).save()

    changed_club_payload = {**source_payload, "club_id": other_club.id, "is_active": False}
    with pytest.raises(ValidationError):
        Tariff(**changed_club_payload).save()

    unsealed_tariff, _unsealed_component = _finite_tariff(club=club, name="Explicit PK destination")
    component_payload = {
        "id": source_component.id,
        "club_id": club.id,
        "tariff_id": unsealed_tariff.id,
        "name": source_component.name,
        "training_type_id": source_component.training_type_id,
        "entitlement_kind": source_component.entitlement_kind,
        "credits_total": source_component.credits_total,
        "weekly_limit": source_component.weekly_limit,
        "scope": source_component.scope,
        "location_id": source_component.location_id,
        "trainer_payout_policy": source_component.trainer_payout_policy,
        "paid_amount_basis": source_component.paid_amount_basis,
        "sort_order": source_component.sort_order,
        "is_active": source_component.is_active,
    }
    with pytest.raises(ValidationError):
        TariffComponent(**component_payload).save()

    new_explicit_component = TariffComponent(
        id=source_component.id + 100000,
        club_id=club.id,
        tariff_id=revision.target_tariff_id,
        name="Explicit PK new component",
        training_type_id=revision.target_tariff.training_type_id,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=Decimal("8500"),
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    with pytest.raises(ValidationError):
        new_explicit_component.save()


@pytest.mark.django_db
def test_bulk_catalog_writes_validate_all_linked_tenant_rows(
    club,
    other_club,
):
    local_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    foreign_type = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.GROUP)
    foreign_location = LocationFactory(club=other_club)
    foreign_trainer = TrainerFactory(club=other_club)

    def tariff_payload(**overrides):
        return Tariff(
            name="Bulk tenant guard",
            price=Decimal("8000"),
            trainings_limit=8,
            duration_days=30,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
            **overrides,
        )

    with pytest.raises(ValidationError):
        Tariff.objects.bulk_create(
            [tariff_payload(club_id=club.id, training_type_id=foreign_type.id)],
        )
    with pytest.raises(ValidationError):
        Tariff.objects.bulk_create(
            [
                tariff_payload(
                    club_id=club.id,
                    training_type_id=local_type.id,
                    scope=Tariff.Scope.LOCATION,
                    location_id=foreign_location.id,
                )
            ],
        )
    with pytest.raises(ValidationError):
        Tariff.objects.bulk_create(
            [
                tariff_payload(
                    club_id=club.id,
                    training_type_id=local_type.id,
                    personal_booking_trainer_id=foreign_trainer.id,
                )
            ],
        )

    local_tariff = TariffFactory(
        club=club,
        training_type=local_type,
        name="Bulk component parent",
        price=Decimal("8000"),
        trainings_limit=8,
    )

    def component_payload(**overrides):
        payload = {
            "tariff_id": local_tariff.id,
            "name": "Bulk component guard",
            "training_type_id": local_type.id,
            "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
            "credits_total": 8,
            "paid_amount_basis": Decimal("8000"),
            "trainer_payout_policy": Tariff.PayoutPolicy.ON_PAYMENT,
        }
        payload.update(overrides)
        return TariffComponent(
            **payload,
        )

    with pytest.raises(ValidationError):
        TariffComponent.objects.bulk_create(
            [component_payload(club_id=other_club.id)],
        )
    with pytest.raises(ValidationError):
        TariffComponent.objects.bulk_create(
            [component_payload(club_id=club.id, training_type_id=foreign_type.id)],
        )
    with pytest.raises(ValidationError):
        TariffComponent.objects.bulk_create(
            [
                component_payload(
                    club_id=club.id,
                    scope=Tariff.Scope.LOCATION,
                    location_id=foreign_location.id,
                )
            ],
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "new_price",
    [Decimal("100000000.00"), Decimal("NaN"), Decimal("Infinity")],
)
def test_revision_rejects_nonrepresentable_money_before_catalog_writes(
    club,
    owner_user,
    new_price,
):
    source, _component = _finite_tariff(club=club)

    with pytest.raises(BusinessLogicError) as exc_info:
        revise_tariff_price(
            club_id=club.id,
            source_tariff_id=source.id,
            new_price=new_price,
            new_name="Invalid price",
            actor_user_id=owner_user.id,
            idempotency_key=f"catalog-invalid-money-{new_price}",
        )

    assert exc_info.value.code == "invalid_money_amount"
    assert not TariffPriceRevision.objects.for_club(club).exists()
    source.refresh_from_db()
    assert source.is_active is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics for edit-versus-revision race",
)
@pytest.mark.parametrize("edit_mode", ["model", "queryset"])
def test_postgresql_edit_waits_for_revision_then_rechecks_sealed_edge(
    club,
    owner_user,
    edit_mode,
    monkeypatch,
):
    source, _component = _finite_tariff(club=club)
    transaction.commit()
    edit_probe_ready = Event()
    revision_before_edge = Event()
    edit_save_attempted = Event()

    def edit_catalog():
        close_old_connections()
        try:
            if edit_mode == "model":
                tariff = Tariff.objects.unscoped().get(id=source.id)
                tariff.name = "Raced edit"
            else:
                queryset = Tariff.objects.unscoped().filter(id=source.id)
                list(queryset.values_list("id", flat=True))
            edit_probe_ready.set()
            if not revision_before_edge.wait(timeout=10):
                return "revision-did-not-reach-edge"
            if edit_mode == "model":
                edit_save_attempted.set()
                tariff.save()
            else:
                edit_save_attempted.set()
                queryset.update(name="Raced edit")
            return "saved"
        except ValidationError:
            return "blocked"
        finally:
            connection.close()
            connections.close_all()

    original_create = TariffPriceRevision.objects.create

    def paused_revision_create(**kwargs):
        revision_before_edge.set()
        assert edit_save_attempted.wait(timeout=10)
        return original_create(**kwargs)

    monkeypatch.setattr(TariffPriceRevision.objects, "create", paused_revision_create)
    with ThreadPoolExecutor(max_workers=2) as executor:
        edit_future = executor.submit(edit_catalog)
        assert edit_probe_ready.wait(timeout=10)
        revision_future = executor.submit(
            revise_tariff_price,
            club_id=club.id,
            source_tariff_id=source.id,
            new_price=Decimal("8500"),
            new_name="Raced revision",
            actor_user_id=owner_user.id,
            idempotency_key=f"catalog-race-{edit_mode}",
        )
        revision = revision_future.result(timeout=20)
        assert edit_future.result(timeout=20) == "blocked"

    assert revision.source_tariff_id == source.id
    assert Tariff.objects.unscoped().get(id=source.id).is_active is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics for component insertion race",
)
def test_postgresql_component_insert_waits_for_revision_before_sealed_edge_check(
    club,
    owner_user,
    monkeypatch,
):
    source, _component = _finite_tariff(club=club)
    transaction.commit()
    insertion_probe_ready = Event()
    revision_before_edge = Event()
    insertion_attempted = Event()

    def insert_component():
        close_old_connections()
        try:
            insertion_probe_ready.set()
            if not revision_before_edge.wait(timeout=10):
                return "revision-did-not-reach-edge"
            component = TariffComponent(
                club_id=club.id,
                tariff_id=source.id,
                name="Late component",
                training_type_id=source.training_type_id,
                entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
                credits_total=8,
                paid_amount_basis=Decimal("5000"),
                trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
            )
            insertion_attempted.set()
            TariffComponent.objects.bulk_create([component])
            return "inserted"
        except ValidationError:
            return "blocked"
        finally:
            connection.close()
            connections.close_all()

    original_create = TariffPriceRevision.objects.create

    def paused_revision_create(**kwargs):
        revision_before_edge.set()
        assert insertion_attempted.wait(timeout=10)
        return original_create(**kwargs)

    monkeypatch.setattr(TariffPriceRevision.objects, "create", paused_revision_create)
    with ThreadPoolExecutor(max_workers=2) as executor:
        insertion_future = executor.submit(insert_component)
        assert insertion_probe_ready.wait(timeout=10)
        revision_future = executor.submit(
            revise_tariff_price,
            club_id=club.id,
            source_tariff_id=source.id,
            new_price=Decimal("8500"),
            new_name="Raced revision",
            actor_user_id=owner_user.id,
            idempotency_key="catalog-component-insert-race",
        )
        revision = revision_future.result(timeout=20)
        assert insertion_future.result(timeout=20) == "blocked"

    assert revision.source_tariff_id == source.id
    assert not TariffComponent.objects.for_club(club).filter(name="Late component").exists()
