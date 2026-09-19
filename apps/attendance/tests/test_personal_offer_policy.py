import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from decimal import Decimal
from threading import Barrier
from urllib.parse import urlparse

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import RequestFactory
from django.utils import timezone

import apps.billing.service_modules.catalog as catalog_service
from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalServiceTermsSnapshot,
    Schedule,
    is_complete_personal_terms,
)
from apps.attendance.personal_offers import personal_offer_payload
from apps.attendance.selectors import (
    get_self_service_personal_availability_options,
    get_trainer_personal_availability_calendar,
)
from apps.attendance.services import book_personal_drop_in, generate_personal_availability_slots
from apps.attendance.services.personal_terms import create_complete_personal_terms_snapshot
from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.tests.factories import (
    DiscountFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import on_business_error


def _future_window():
    starts_at = timezone.now() + timedelta(days=7)
    return starts_at.replace(hour=10, minute=0, second=0, microsecond=0), starts_at.replace(
        hour=11, minute=0, second=0, microsecond=0
    )


def _complete_default(
    *,
    club,
    training_type,
    location=None,
    price=Decimal("1200.00"),
    personal_booking_trainer=None,
):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=price,
        trainings_limit=1,
        duration_days=30,
        scope=Tariff.Scope.LOCATION if location else Tariff.Scope.CLUB,
        location=location,
        personal_booking_trainer=personal_booking_trainer,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        is_active=True,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        name="One personal session",
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        scope=tariff.scope,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=price,
    )
    update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        is_personal_booking_default=True,
    )
    tariff.refresh_from_db()
    return tariff


@pytest.mark.django_db
def test_catalog_update_rereads_trainer_scope_after_waiting_for_training_type(
    club,
    monkeypatch,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    first_trainer = TrainerFactory(club=club, is_active=True)
    second_trainer = TrainerFactory(club=club, is_active=True)
    tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        personal_booking_trainer=first_trainer,
    )
    original_training_type_lock = catalog_service._lock_catalog_training_type
    original_trainer_lock = catalog_service._lock_catalog_trainers
    locked_trainer_scopes: list[list[int]] = []
    changed_scope = False

    def change_scope_after_training_type_lock(**kwargs):
        nonlocal changed_scope
        locked = original_training_type_lock(**kwargs)
        if not changed_scope:
            Tariff.objects.for_club(club).filter(id=tariff.id).update(
                personal_booking_trainer_id=second_trainer.id,
            )
            changed_scope = True
        return locked

    def capture_trainer_scope(*, club_id, trainer_ids):
        locked_trainer_scopes.append(trainer_ids)
        return original_trainer_lock(club_id=club_id, trainer_ids=trainer_ids)

    monkeypatch.setattr(catalog_service, "_lock_catalog_training_type", change_scope_after_training_type_lock)
    monkeypatch.setattr(catalog_service, "_lock_catalog_trainers", capture_trainer_scope)

    updated = update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        is_personal_booking_default=True,
    )

    assert locked_trainer_scopes == [[second_trainer.id]]
    assert updated.personal_booking_trainer_id == second_trainer.id


def _terms_reservation(*, club, owner_user, trainer, location, training_type, tariff):
    starts_at, ends_at = _future_window()
    from apps.attendance.models import PersonalBookingPaymentReservation
    from apps.students.tests.factories import StudentFactory

    return PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=StudentFactory(club=club),
        trainer=trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
        starts_at=starts_at,
        ends_at=ends_at,
        expires_at=ends_at,
        created_by=owner_user,
    )


@pytest.mark.django_db
def test_trainer_personal_default_precedes_generic_and_switches_only_its_owner_scope(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    another_trainer = TrainerFactory(club=club)
    fallback_trainer = TrainerFactory(club=club)
    generic = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1200.00"),
    )
    trainer_default = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1500.00"),
        personal_booking_trainer=trainer,
    )
    other_trainer_default = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1700.00"),
        personal_booking_trainer=another_trainer,
    )

    assert resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
    ).tariff.id == trainer_default.id
    assert resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=None,
        training_type_id=training_type.id,
        location_id=location.id,
    ).tariff.id == generic.id
    assert resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=fallback_trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
    ).tariff.id == generic.id

    replacement = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("1550.00"),
        trainings_limit=1,
        duration_days=30,
        scope=Tariff.Scope.LOCATION,
        location=location,
        personal_booking_trainer=trainer,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=replacement,
        training_type=training_type,
        name="Replacement personal session",
        credits_total=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=replacement.price,
    )
    update_tariff(
        tariff_id=replacement.id,
        club_id=club.id,
        is_personal_booking_default=True,
    )
    generic.refresh_from_db()
    trainer_default.refresh_from_db()
    other_trainer_default.refresh_from_db()
    replacement.refresh_from_db()
    assert generic.is_personal_booking_default is True
    assert trainer_default.is_personal_booking_default is False
    assert other_trainer_default.is_personal_booking_default is True
    assert replacement.is_personal_booking_default is True


@pytest.mark.django_db
def test_database_allows_one_default_per_generic_or_trainer_scope(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)

    generic_club = TariffFactory(
        club=club,
        training_type=training_type,
        scope=Tariff.Scope.CLUB,
        is_personal_booking_default=True,
    )
    trainer_club = TariffFactory(
        club=club,
        training_type=training_type,
        scope=Tariff.Scope.CLUB,
        personal_booking_trainer=trainer,
        is_personal_booking_default=True,
    )
    generic_location = TariffFactory(
        club=club,
        training_type=training_type,
        scope=Tariff.Scope.LOCATION,
        location=location,
        is_personal_booking_default=True,
    )
    trainer_location = TariffFactory(
        club=club,
        training_type=training_type,
        scope=Tariff.Scope.LOCATION,
        location=location,
        personal_booking_trainer=trainer,
        is_personal_booking_default=True,
    )
    assert {
        generic_club.id,
        trainer_club.id,
        generic_location.id,
        trainer_location.id,
    } == set(
        Tariff.objects.for_club(club)
        .filter(is_personal_booking_default=True)
        .values_list("id", flat=True)
    )

    for kwargs in (
        {"scope": Tariff.Scope.CLUB},
        {"scope": Tariff.Scope.CLUB, "personal_booking_trainer": trainer},
        {"scope": Tariff.Scope.LOCATION, "location": location},
        {
            "scope": Tariff.Scope.LOCATION,
            "location": location,
            "personal_booking_trainer": trainer,
        },
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            TariffFactory(
                club=club,
                training_type=training_type,
                is_personal_booking_default=True,
                **kwargs,
            )


@pytest.mark.django_db
def test_tariff_rejects_foreign_personal_booking_trainer(club):
    tariff = TariffFactory(
        club=club,
        personal_booking_trainer=TrainerFactory(),
    )

    with pytest.raises(ValidationError, match="Personal booking trainer"):
        tariff.full_clean()


@pytest.mark.django_db
def test_personal_offer_resolves_one_active_discount_and_complete_v2_terms(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1200.00"),
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Coach-approved 15%",
        discount_type="percent",
        value=Decimal("15.00"),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
        discount_id=discount.id,
    )

    assert offer.base_amount == Decimal("1200.00")
    assert offer.discount_amount == Decimal("180.00")
    assert offer.payable_amount == Decimal("1020.00")
    assert offer.price == Decimal("1020.00")
    assert offer.discount == discount

    reservation = _terms_reservation(
        club=club,
        owner_user=owner_user,
        trainer=trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
    )
    terms = create_complete_personal_terms_snapshot(offer=offer, reservation=reservation)

    assert terms.terms_version == PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2
    assert terms.discount_id_snapshot == discount.id
    assert terms.discount_name_snapshot == "Coach-approved 15%"
    assert terms.discount_type_snapshot == "percent"
    assert terms.discount_value_snapshot == Decimal("15.00")
    assert terms.payable_amount == Decimal("1020.00")
    assert terms.component_paid_amount_basis == Decimal("1020.00")
    assert terms.component_unit_amount_basis == Decimal("1020.00")
    assert is_complete_personal_terms(terms)


@pytest.mark.django_db
def test_personal_offer_rejects_inactive_foreign_or_non_positive_discount(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1200.00"),
        personal_booking_trainer=trainer,
    )
    inactive = DiscountFactory(club=club, is_active=False)
    foreign = DiscountFactory()
    too_large = DiscountFactory(club=club, discount_type="fixed", value=Decimal("1200.00"))

    for discount, code in (
        (inactive, "personal_offer_discount_inactive"),
        (foreign, "personal_offer_discount_not_found"),
        (too_large, "personal_offer_discount_non_positive"),
    ):
        with pytest.raises(BusinessLogicError) as exc_info:
            resolve_personal_booking_offer(
                club_id=club.id,
                trainer_id=trainer.id,
                training_type_id=training_type.id,
                location_id=location.id,
                discount_id=discount.id,
            )
        assert exc_info.value.code == code


@pytest.mark.django_db
def test_personal_default_prefers_location_and_fails_closed_for_bad_contract(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    first_location = LocationFactory(club=club)
    second_location = LocationFactory(club=club)
    club_tariff = _complete_default(club=club, training_type=training_type, price=Decimal("1000.00"))
    location_tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=first_location,
        price=Decimal("1500.00"),
    )

    assert resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=first_location.id,
    ).tariff.id == location_tariff.id
    assert resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=second_location.id,
    ).tariff.id == club_tariff.id

    bad = TariffFactory(
        club=club,
        training_type=training_type,
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=second_location,
        is_personal_booking_default=True,
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        resolve_personal_booking_offer(
            club_id=club.id,
            training_type_id=training_type.id,
            location_id=second_location.id,
        )
    assert exc_info.value.code == "personal_offer_wrong_component"
    bad.refresh_from_db()


@pytest.mark.django_db
def test_default_flip_is_atomic_scope_switch_and_deactivation_requires_clear(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    old = _complete_default(club=club, training_type=training_type)
    new = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("1400.00"),
        trainings_limit=1,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=new,
        training_type=training_type,
        name="Replacement session",
        credits_total=1,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=new.price,
    )
    update_tariff(tariff_id=new.id, club_id=club.id, is_personal_booking_default=True)
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_personal_booking_default is False
    assert new.is_personal_booking_default is True
    with pytest.raises(BusinessLogicError) as exc_info:
        update_tariff(tariff_id=new.id, club_id=club.id, is_active=False)
    assert exc_info.value.code == "personal_offer_default_clear_required"
    update_tariff(
        tariff_id=new.id,
        club_id=club.id,
        is_active=False,
        is_personal_booking_default=False,
    )
    new.refresh_from_db()
    assert new.is_active is False and new.is_personal_booking_default is False


@pytest.mark.django_db
def test_database_prevents_two_defaults_for_the_same_personal_scope(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    _complete_default(club=club, training_type=training_type)
    with pytest.raises(IntegrityError):
        TariffFactory(
            club=club,
            training_type=training_type,
            scope=Tariff.Scope.CLUB,
            is_personal_booking_default=True,
        )


@pytest.mark.django_db
def test_flag_on_stale_digest_writes_no_artifacts_and_carries_current_safe_offer(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    tariff = _complete_default(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, status="active")
    starts_at, ends_at = _future_window()
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    digest = personal_offer_payload(slot=slot, offer=offer)["offer_digest"]
    tariff.price = Decimal("1300.00")
    tariff.save(update_fields=["price", "updated_at"])
    component = tariff.components.get(is_active=True)
    component.paid_amount_basis = tariff.price
    component.save(update_fields=["paid_amount_basis", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        book_personal_drop_in(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            tariff_id=tariff.id,
            actor_user_id=owner_user.id,
            availability_slot_id=slot.id,
            offer_digest=digest,
            idempotency_key="stale-personal-offer",
        )
    assert exc_info.value.code == "personal_offer_changed"
    assert exc_info.value.safe_payload["current_offer"]["offer_tariff_id"] == tariff.id
    assert not Schedule.objects.for_club(club).filter(one_time_date=starts_at.date()).exists()
    assert not PersonalServiceTermsSnapshot.objects.for_club(club).exists()


@pytest.mark.django_db
def test_flag_on_drop_in_writes_complete_append_only_terms_and_flag_off_keeps_legacy(settings, club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL, drop_in_price=Decimal("1200.00"))
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    tariff = _complete_default(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, status="active")
    starts_at, ends_at = _future_window()
    slot = PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    legacy = book_personal_drop_in(
        club_id=club.id, student_id=student.id, trainer_id=trainer.id,
        starts_at=starts_at, ends_at=ends_at, location_id=location.id,
        training_type_id=training_type.id, tariff_id=tariff.id, actor_user_id=owner_user.id,
        availability_slot_id=slot.id, idempotency_key="flag-off-legacy",
    )
    legacy_terms = PersonalServiceTermsSnapshot.objects.for_club(club).get(
        booking=legacy.booking,
    )
    assert (
        legacy_terms.terms_version
        == PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL
    )
    assert legacy_terms.payable_amount == tariff.price

    # A distinct future slot proves dual write under the enabled capability.
    starts_at, ends_at = starts_at + timedelta(days=1), ends_at + timedelta(days=1)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    digest = personal_offer_payload(
        slot=slot,
        offer=resolve_personal_booking_offer(
            club_id=club.id, training_type_id=training_type.id, location_id=location.id,
        ),
    )["offer_digest"]
    result = book_personal_drop_in(
        club_id=club.id, student_id=student.id, trainer_id=trainer.id,
        starts_at=starts_at, ends_at=ends_at, location_id=location.id,
        training_type_id=training_type.id, tariff_id=None, actor_user_id=owner_user.id,
        availability_slot_id=slot.id, offer_digest=digest, idempotency_key="flag-on-complete",
    )
    terms = PersonalServiceTermsSnapshot.objects.for_club(club).get(booking=result.booking)
    assert terms.terms_version == PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2
    assert terms.payable_amount == tariff.price
    with pytest.raises(ValidationError):
        PersonalServiceTermsSnapshot.objects.for_club(club).filter(id=terms.id).update(currency="USD")
    with pytest.raises(ValidationError):
        PersonalServiceTermsSnapshot.objects.unscoped().filter(id=terms.id).delete()


@pytest.mark.django_db
def test_publication_is_blocked_when_flag_on_offer_missing_and_options_do_not_fallback(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    student = StudentFactory(club=club, status="active")
    starts_at, ends_at = _future_window()
    PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )
    options = get_self_service_personal_availability_options(
        club=club, student_id=student.id, target_date=starts_at.date(),
    )
    assert options[0]["booking_status"] == "blocked"
    assert options[0]["reason_code"] == "personal_booking_tariff_not_configured"
    with pytest.raises(BusinessLogicError) as exc_info:
        generate_personal_availability_slots(
            club_id=club.id, trainer_id=trainer.id, date_from=starts_at.date(), date_to=starts_at.date(),
            weekdays=[starts_at.weekday()], start_time=time(12), end_time=time(13),
            location_id=location.id, training_type_id=training_type.id,
        )
    assert exc_info.value.code == "personal_booking_tariff_not_configured"


@pytest.mark.django_db
def test_trainer_and_student_receive_the_same_resolved_personal_offer(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    student = StudentFactory(club=club, status="active")
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1777.00"))
    starts_at, ends_at = _future_window()
    PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )
    trainer_item = get_trainer_personal_availability_calendar(
        club=club, trainer_id=trainer.id, date_from=starts_at.date(), date_to=starts_at.date(),
    )[0]
    student_item = get_self_service_personal_availability_options(
        club=club, student_id=student.id, target_date=starts_at.date(),
    )[0]
    assert trainer_item["offer_tariff_id"] == student_item["offer_tariff_id"] == tariff.id
    assert trainer_item["offer_price"] == student_item["offer_price"] == "1777.00"
    assert trainer_item["offer_digest"] == student_item["offer_digest"]


@pytest.mark.django_db
def test_offer_rejects_inactive_and_mini_group_and_slot_change_is_stale(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    location = LocationFactory(club=club)
    mini_group = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
    with pytest.raises(BusinessLogicError) as exc_info:
        resolve_personal_booking_offer(
            club_id=club.id, training_type_id=mini_group.id, location_id=location.id,
        )
    assert exc_info.value.code == "personal_offer_mini_group"

    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = _complete_default(club=club, training_type=training_type, location=location)
    tariff.is_active = False
    tariff.save(update_fields=["is_active", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        resolve_personal_booking_offer(
            club_id=club.id, training_type_id=training_type.id, location_id=location.id,
        )
    assert exc_info.value.code == "personal_offer_default_inactive"
    tariff.is_active = True
    tariff.save(update_fields=["is_active", "updated_at"])

    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    student = StudentFactory(club=club, status="active")
    starts_at, ends_at = _future_window()
    slot = PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )
    digest = personal_offer_payload(
        slot=slot,
        offer=resolve_personal_booking_offer(
            club_id=club.id, training_type_id=training_type.id, location_id=location.id,
        ),
    )["offer_digest"]
    slot.block_reason = "changed metadata"
    slot.save(update_fields=["block_reason", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        book_personal_drop_in(
            club_id=club.id, student_id=student.id, trainer_id=trainer.id,
            starts_at=starts_at, ends_at=ends_at, location_id=location.id,
            training_type_id=training_type.id, tariff_id=tariff.id, actor_user_id=owner_user.id,
            availability_slot_id=slot.id, offer_digest=digest, idempotency_key="stale-slot",
        )
    assert exc_info.value.code == "personal_offer_changed"
    assert not PersonalServiceTermsSnapshot.objects.for_club(club).exists()


@pytest.mark.django_db
def test_postgresql_personal_default_flip_race_gate_requires_opt_in_disposable_database():
    database_url = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_URL", "")
    gate_required = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_GATE_REQUIRED") == "1"
    if not database_url and not gate_required:
        pytest.skip("unified client journey PostgreSQL race gate is opt-in")
    assert database_url, "UNIFIED_CLIENT_JOURNEY_POSTGRES_URL is required"
    parsed = urlparse(database_url)
    assert parsed.scheme in {"postgres", "postgresql"}
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    assert re.search(r"(?:^|[_-])(?:test|e2e|journey)(?:[_-]|$)", parsed.path.lstrip("/"))
    assert connection.vendor == "postgresql"


def test_personal_offer_changed_http_response_exposes_only_safe_current_offer():
    error = BusinessLogicError("Offer changed", code="personal_offer_changed")
    error.safe_payload = {
        "detail": "must not override",
        "code": "must_not_override",
        "current_offer": {
            "offer_tariff_id": 42,
            "offer_name": "Personal session",
            "offer_price": "1200.00",
            "offer_digest": "safe-digest",
        },
    }

    response = on_business_error(RequestFactory().get("/api/personal-availability/"), error)

    assert response.status_code == 400
    assert json.loads(response.content) == {
        "detail": "Offer changed",
        "code": "personal_offer_changed",
        "current_offer": {
            "offer_tariff_id": 42,
            "offer_name": "Personal session",
            "offer_price": "1200.00",
            "offer_digest": "safe-digest",
        },
    }


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL catalog locks")
@pytest.mark.django_db(transaction=True)
def test_postgresql_default_flip_and_offer_acceptance_use_whole_contract(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    student = StudentFactory(club=club, status="active")
    old = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    new = TariffFactory(
        club=club, training_type=training_type, price=Decimal("1300.00"), trainings_limit=1,
        duration_days=30, scope=Tariff.Scope.LOCATION, location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club, tariff=new, training_type=training_type, name="New exact session",
        credits_total=1, scope=Tariff.Scope.LOCATION, location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN, paid_amount_basis=new.price,
    )
    starts_at, ends_at = _future_window()
    slot = PersonalAvailabilitySlot.objects.create(
        club=club, trainer=trainer, location=location, training_type=training_type,
        starts_at=starts_at, ends_at=ends_at,
    )
    old_digest = personal_offer_payload(
        slot=slot,
        offer=resolve_personal_booking_offer(
            club_id=club.id, training_type_id=training_type.id, location_id=location.id,
        ),
    )["offer_digest"]
    barrier = Barrier(2)

    def accept():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return book_personal_drop_in(
                club_id=club.id, student_id=student.id, trainer_id=trainer.id,
                starts_at=starts_at, ends_at=ends_at, location_id=location.id,
                training_type_id=training_type.id, tariff_id=old.id, actor_user_id=owner_user.id,
                availability_slot_id=slot.id, offer_digest=old_digest, idempotency_key="pg-default-flip",
            )
        except BusinessLogicError as exc:
            return exc.code
        finally:
            close_old_connections()

    def flip():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            update_tariff(tariff_id=new.id, club_id=club.id, is_personal_booking_default=True)
            return "flipped"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted, flipped = list(executor.map(lambda func: func(), (accept, flip)))
    assert flipped == "flipped"
    assert accepted == "personal_offer_changed" or accepted.booking.tariff_id == old.id
    snapshots = list(PersonalServiceTermsSnapshot.objects.for_club(club).order_by("id"))
    assert len(snapshots) <= 1
    assert not snapshots or snapshots[0].payable_amount == Decimal("1200.00")
