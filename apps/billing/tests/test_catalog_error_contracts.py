from decimal import Decimal, InvalidOperation

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

import apps.billing.service_modules.catalog as catalog_service
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Discount, Expense, Tariff, TrainingType
from apps.billing.services import (
    create_discount,
    create_expense,
    create_tariff,
    create_training_type,
    delete_expense,
    is_training_type_kind_locked,
    update_discount,
    update_expense,
    update_tariff,
    update_training_type,
)
from apps.billing.tests.factories import (
    DiscountFactory,
    ExpenseFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory


@pytest.mark.django_db
def test_training_type_kind_lock_returns_false_for_missing_and_foreign_rows(club, other_club):
    foreign = TrainingTypeFactory(club=other_club)

    assert is_training_type_kind_locked(club_id=club.id, training_type_id=-1) is False
    assert is_training_type_kind_locked(club_id=club.id, training_type_id=foreign.id) is False


@pytest.mark.django_db
def test_training_type_kind_lock_detects_schedule_usage(club):
    training_type = TrainingTypeFactory(club=club)
    ScheduleFactory(club=club, training_type=training_type)

    assert is_training_type_kind_locked(
        club_id=club.id,
        training_type_id=training_type.id,
    )


@pytest.mark.django_db
def test_training_type_kind_lock_detects_checkin_usage_without_schedule_match(club):
    training_type = TrainingTypeFactory(club=club)
    other_training_type = TrainingTypeFactory(club=club)
    schedule = ScheduleFactory(club=club, training_type=other_training_type)
    CheckinFactory(
        club=club,
        schedule=schedule,
        training_type=training_type,
    )

    assert is_training_type_kind_locked(
        club_id=club.id,
        training_type_id=training_type.id,
    )


@pytest.mark.django_db
def test_training_type_kind_lock_detects_trainer_rate_usage(club):
    training_type = TrainingTypeFactory(club=club)
    TrainerRateFactory(club=club, training_type=training_type)

    assert is_training_type_kind_locked(
        club_id=club.id,
        training_type_id=training_type.id,
    )


@pytest.mark.django_db
def test_training_type_kind_lock_detects_component_package_allocation_usage(club):
    tariff_training_type = TrainingTypeFactory(club=club)
    component_training_type = TrainingTypeFactory(club=club)
    tariff = TariffFactory(club=club, training_type=tariff_training_type)
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=component_training_type,
    )
    student = StudentFactory(club=club)
    subscription = SubscriptionFactory(
        club=club,
        tariff=tariff,
        student=student,
    )
    owner_trainer = TrainerFactory(club=club)
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        training_type=component_training_type,
        owner_trainer=owner_trainer,
        source=TrainerPackageAllocation.Source.MANUAL_SUBSCRIPTION,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=tariff.trainings_limit,
        amount_snapshot=tariff.price,
        activated_at=timezone.now(),
    )

    assert is_training_type_kind_locked(
        club_id=club.id,
        training_type_id=component_training_type.id,
    )


@pytest.mark.django_db
def test_create_training_type_rejects_non_positive_drop_in_without_mutation(club):
    with pytest.raises(BusinessLogicError) as exc_info:
        create_training_type(
            club_id=club.id,
            name="Invalid",
            slug="invalid",
            drop_in_price=Decimal("0"),
        )

    assert exc_info.value.code == "invalid_money_amount"
    assert not TrainingType.objects.for_club(club.id).filter(slug="invalid").exists()


@pytest.mark.django_db(transaction=True)
def test_create_training_type_missing_club_integrity_error_has_no_row():
    with pytest.raises(IntegrityError), transaction.atomic():
        create_training_type(
            club_id=-1,
            name="Missing club",
            slug="missing-club",
        )

    assert not TrainingType.objects.filter(slug="missing-club").exists()


@pytest.mark.django_db
def test_update_training_type_missing_or_foreign_is_stable_not_found(club, other_club):
    foreign = TrainingTypeFactory(club=other_club)

    for training_type_id in (-1, foreign.id):
        with pytest.raises(BusinessLogicError) as exc_info:
            update_training_type(
                training_type_id=training_type_id,
                club_id=club.id,
                name="Hidden",
            )
        assert exc_info.value.code == "training_type_not_found"


@pytest.mark.django_db
def test_update_training_type_rejects_unknown_fields_without_mutation(club):
    training_type = TrainingTypeFactory(club=club)

    with pytest.raises(BusinessLogicError) as exc_info:
        update_training_type(
            training_type_id=training_type.id,
            club_id=club.id,
            slug="changed",
        )

    assert exc_info.value.code == "invalid_fields"
    training_type.refresh_from_db()
    assert training_type.slug != "changed"


@pytest.mark.django_db
def test_create_tariff_rejects_missing_or_foreign_training_type_without_mutation(club, other_club):
    foreign = TrainingTypeFactory(club=other_club)

    for training_type_id in (-1, foreign.id):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_tariff(
                club_id=club.id,
                name=f"Invalid {training_type_id}",
                training_type_id=training_type_id,
                price=Decimal("1000"),
                trainings_limit=1,
                duration_days=30,
            )
        assert exc_info.value.code == "training_type_not_found"
    assert not Tariff.objects.for_club(club.id).exists()


@pytest.mark.django_db
def test_create_tariff_rejects_foreign_location_and_invalid_payout_without_mutation(
    club,
    other_club,
):
    training_type = TrainingTypeFactory(club=club)
    foreign_location = LocationFactory(club=other_club)

    cases = [
        (
            {"scope": Tariff.Scope.LOCATION, "location_id": foreign_location.id},
            "location_not_found",
        ),
        ({"trainer_payout_policy": "later"}, "invalid_payout_policy"),
    ]
    for fields, expected_code in cases:
        with pytest.raises(BusinessLogicError) as exc_info:
            create_tariff(
                club_id=club.id,
                name=expected_code,
                training_type_id=training_type.id,
                price=Decimal("1000"),
                trainings_limit=1,
                duration_days=30,
                **fields,
            )
        assert exc_info.value.code == expected_code
    assert not Tariff.objects.for_club(club.id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("component_overrides", "expected_code"),
    [
        ({"credits_total": None}, "component_credits_required"),
        (
            {
                "entitlement_kind": "weekly_limit",
                "credits_total": None,
                "weekly_limit": None,
            },
            "component_weekly_limit_required",
        ),
        ({"scope": "region"}, "invalid_component_scope"),
        ({"scope": Tariff.Scope.LOCATION, "location_id": None}, "location_required"),
        ({"trainer_payout_policy": "later"}, "invalid_payout_policy"),
    ],
)
def test_create_tariff_component_business_errors_roll_back_tariff(
    club,
    component_overrides,
    expected_code,
):
    training_type = TrainingTypeFactory(club=club)
    component = {
        "name": "Component",
        "training_type_id": training_type.id,
        "entitlement_kind": "finite_credits",
        "credits_total": 1,
        "weekly_limit": None,
        "scope": Tariff.Scope.CLUB,
        "location_id": None,
        "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
        "paid_amount_basis": Decimal("1000"),
    }
    component.update(component_overrides)

    with pytest.raises(BusinessLogicError) as exc_info:
        create_tariff(
            club_id=club.id,
            name=expected_code,
            training_type_id=training_type.id,
            price=Decimal("1000"),
            trainings_limit=1,
            duration_days=30,
            components=[component],
        )

    assert exc_info.value.code == expected_code
    assert not Tariff.objects.for_club(club.id).filter(name=expected_code).exists()


@pytest.mark.django_db
def test_create_tariff_component_foreign_training_and_location_are_hidden(club, other_club):
    training_type = TrainingTypeFactory(club=club)
    foreign_training_type = TrainingTypeFactory(club=other_club)
    foreign_location = LocationFactory(club=other_club)
    base = {
        "name": "Component",
        "training_type_id": training_type.id,
        "entitlement_kind": "finite_credits",
        "credits_total": 1,
        "weekly_limit": None,
        "scope": Tariff.Scope.CLUB,
        "location_id": None,
        "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
        "paid_amount_basis": Decimal("1000"),
    }
    cases = [
        ({"training_type_id": foreign_training_type.id}, "training_type_not_found"),
        (
            {
                "scope": Tariff.Scope.LOCATION,
                "location_id": foreign_location.id,
            },
            "location_not_found",
        ),
    ]

    for overrides, expected_code in cases:
        component = {**base, **overrides}
        with pytest.raises(BusinessLogicError) as exc_info:
            create_tariff(
                club_id=club.id,
                name=expected_code,
                training_type_id=training_type.id,
                price=Decimal("1000"),
                trainings_limit=1,
                duration_days=30,
                components=[component],
            )
        assert exc_info.value.code == expected_code


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("component", "exception_type"),
    [
        ({"paid_amount_basis": "1000"}, KeyError),
        ({"training_type_id": "bad", "paid_amount_basis": "1000"}, ValueError),
        ({"training_type_id": 1, "paid_amount_basis": "bad"}, InvalidOperation),
    ],
)
def test_malformed_component_exceptions_and_tariff_rollback(
    club,
    component,
    exception_type,
):
    training_type = TrainingTypeFactory(club=club)
    if component.get("training_type_id") == 1:
        component = {**component, "training_type_id": training_type.id}

    with pytest.raises(exception_type), transaction.atomic():
        create_tariff(
            club_id=club.id,
            name=exception_type.__name__,
            training_type_id=training_type.id,
            price=Decimal("1000"),
            trainings_limit=1,
            duration_days=30,
            components=[component],
        )

    assert not Tariff.objects.for_club(club.id).filter(name=exception_type.__name__).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_days", -1),
        ("trainings_limit", -1),
    ],
)
def test_create_tariff_database_constraints_escape_and_roll_back(club, field, value):
    training_type = TrainingTypeFactory(club=club)
    fields = {
        "club_id": club.id,
        "name": field,
        "training_type_id": training_type.id,
        "price": Decimal("1000"),
        "trainings_limit": 1,
        "duration_days": 30,
    }
    fields[field] = value

    with pytest.raises(IntegrityError), transaction.atomic():
        create_tariff(**fields)

    assert not Tariff.objects.for_club(club.id).filter(name=field).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("entitlement_kind", "count_field"),
    [
        ("finite_credits", "credits_total"),
        ("weekly_limit", "weekly_limit"),
    ],
)
def test_negative_component_counts_raise_integrity_error_and_roll_back_create_and_update(
    club,
    entitlement_kind,
    count_field,
):
    training_type = TrainingTypeFactory(club=club)
    component = {
        "name": count_field,
        "training_type_id": training_type.id,
        "entitlement_kind": entitlement_kind,
        "credits_total": None,
        "weekly_limit": None,
        "scope": Tariff.Scope.CLUB,
        "location_id": None,
        "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
        "paid_amount_basis": Decimal("1000"),
        count_field: -1,
    }

    with pytest.raises(IntegrityError), transaction.atomic():
        create_tariff(
            club_id=club.id,
            name=f"create-{count_field}",
            training_type_id=training_type.id,
            price=Decimal("1000"),
            trainings_limit=1,
            duration_days=30,
            components=[component],
        )
    assert not Tariff.objects.for_club(club.id).filter(
        name=f"create-{count_field}",
    ).exists()

    tariff = TariffFactory(club=club, training_type=training_type)
    original_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
    )
    update_component = {
        **component,
        "paid_amount_basis": tariff.price,
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        update_tariff(
            tariff_id=tariff.id,
            club_id=club.id,
            components=[update_component],
        )

    original_component.refresh_from_db()
    assert original_component.is_active is True
    assert tariff.components.filter(is_active=True).count() == 1


@pytest.mark.django_db
def test_update_tariff_missing_foreign_unknown_fields_and_empty_components(club, other_club):
    foreign = TariffFactory(club=other_club)
    for tariff_id in (-1, foreign.id):
        with pytest.raises(BusinessLogicError) as exc_info:
            update_tariff(tariff_id=tariff_id, club_id=club.id, name="Hidden")
        assert exc_info.value.code == "tariff_not_found"

    tariff = TariffFactory(club=club)
    component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=tariff.training_type,
    )
    with pytest.raises(BusinessLogicError) as invalid_fields:
        update_tariff(tariff_id=tariff.id, club_id=club.id, scope="location")
    assert invalid_fields.value.code == "invalid_fields"

    with pytest.raises(BusinessLogicError) as empty_components:
        update_tariff(tariff_id=tariff.id, club_id=club.id, components=[])
    assert empty_components.value.code == "package_components_required"
    component.refresh_from_db()
    assert component.is_active is True


@pytest.mark.django_db
def test_personal_tariff_trainer_scope_rejects_foreign_rows_without_mutation(club, other_club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    foreign_trainer = TrainerFactory(club=other_club)

    with pytest.raises(BusinessLogicError) as resolve_error:
        create_tariff(
            club_id=club.id,
            name="Foreign trainer personal tariff",
            training_type_id=training_type.id,
            price=Decimal("2000.00"),
            trainings_limit=1,
            duration_days=30,
            personal_booking_trainer_id=foreign_trainer.id,
        )
    assert resolve_error.value.code == "personal_booking_trainer_not_found"
    assert resolve_error.value.message == "Тренер персонального тарифа не найден"
    assert not Tariff.objects.for_club(club.id).filter(name="Foreign trainer personal tariff").exists()

    with transaction.atomic(), pytest.raises(BusinessLogicError) as lock_error:
        catalog_service._lock_catalog_trainers(
            club_id=club.id,
            trainer_ids=[foreign_trainer.id],
        )
    assert lock_error.value.code == "personal_booking_trainer_not_found"
    assert lock_error.value.message == "Тренер персонального тарифа не найден"


@pytest.mark.django_db
def test_update_tariff_rolls_back_when_row_disappears_after_catalog_lock(club, monkeypatch):
    tariff = TariffFactory(club=club)
    original_lock = catalog_service._lock_catalog_training_type

    def delete_tariff_after_lock(**kwargs):
        training_type = original_lock(**kwargs)
        Tariff.objects.for_club(club).filter(id=tariff.id).delete()
        return training_type

    monkeypatch.setattr(catalog_service, "_lock_catalog_training_type", delete_tariff_after_lock)

    with pytest.raises(BusinessLogicError) as exc_info:
        update_tariff(tariff_id=tariff.id, club_id=club.id, name="Hidden")
    assert exc_info.value.code == "tariff_not_found"
    assert exc_info.value.message == "Тариф не найден"
    assert Tariff.objects.for_club(club.id).filter(id=tariff.id).exists()


@pytest.mark.django_db
def test_update_tariff_rolls_back_when_trainer_scope_changes_after_lock(club, monkeypatch):
    first_trainer = TrainerFactory(club=club)
    second_trainer = TrainerFactory(club=club)
    tariff = TariffFactory(club=club, personal_booking_trainer=first_trainer)
    original_lock = catalog_service._lock_catalog_trainers

    def change_scope_after_lock(*, club_id, trainer_ids):
        original_lock(club_id=club_id, trainer_ids=trainer_ids)
        Tariff.objects.for_club(club).filter(id=tariff.id).update(
            personal_booking_trainer_id=second_trainer.id,
        )

    monkeypatch.setattr(catalog_service, "_lock_catalog_trainers", change_scope_after_lock)

    with pytest.raises(BusinessLogicError) as exc_info:
        update_tariff(tariff_id=tariff.id, club_id=club.id, name="Hidden")
    assert exc_info.value.code == "personal_booking_catalog_scope_changed"
    assert exc_info.value.message == "Область персонального тарифа изменилась во время блокировки"
    tariff.refresh_from_db()
    assert tariff.personal_booking_trainer_id == first_trainer.id


@pytest.mark.django_db
def test_update_tariff_malformed_component_and_constraint_failures_roll_back(club):
    training_type = TrainingTypeFactory(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        duration_days=30,
    )
    original_name = tariff.name

    malformed_cases = [
        ({"paid_amount_basis": "1000"}, KeyError),
        ({"training_type_id": "bad", "paid_amount_basis": "1000"}, ValueError),
        (
            {
                "training_type_id": tariff.training_type_id,
                "paid_amount_basis": "bad",
            },
            InvalidOperation,
        ),
    ]
    for component, exception_type in malformed_cases:
        with pytest.raises(exception_type):
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                name="Malformed",
                components=[component],
            )
        tariff.refresh_from_db()
        assert tariff.name == original_name

    with pytest.raises(IntegrityError), transaction.atomic():
        update_tariff(
            tariff_id=tariff.id,
            club_id=club.id,
            duration_days=-1,
        )
    tariff.refresh_from_db()
    assert tariff.duration_days == 30


@pytest.mark.django_db
def test_discount_type_missing_rows_and_unknown_fields_are_stable(club, other_club):
    with pytest.raises(BusinessLogicError) as invalid_type:
        create_discount(
            club_id=club.id,
            name="Invalid",
            discount_type="tiered",
            value=Decimal("1"),
        )
    assert invalid_type.value.code == "invalid_discount_type"

    foreign = DiscountFactory(club=other_club)
    for discount_id in (-1, foreign.id):
        with pytest.raises(Discount.DoesNotExist):
            update_discount(discount_id=discount_id, club_id=club.id, name="Hidden")

    discount = DiscountFactory(club=club)
    with pytest.raises(BusinessLogicError) as invalid_fields:
        update_discount(discount_id=discount.id, club_id=club.id, club_id_override=other_club.id)
    assert invalid_fields.value.code == "invalid_fields"


@pytest.mark.django_db(transaction=True)
def test_create_discount_missing_club_integrity_error_has_no_row():
    with pytest.raises(IntegrityError), transaction.atomic():
        create_discount(
            club_id=-1,
            name="Missing club",
            discount_type=Discount.Type.FIXED,
            value=Decimal("1"),
        )

    assert not Discount.objects.filter(name="Missing club").exists()


@pytest.mark.django_db
def test_expense_missing_rows_unknown_fields_and_model_validation_are_stable(club, other_club):
    foreign = ExpenseFactory(club=other_club)
    for operation in (update_expense, delete_expense):
        for expense_id in (-1, foreign.id):
            with pytest.raises(Expense.DoesNotExist):
                operation(expense_id=expense_id, club_id=club.id)

    expense = ExpenseFactory(club=club)
    with pytest.raises(BusinessLogicError) as invalid_fields:
        update_expense(expense_id=expense.id, club_id=club.id, deleted_at=timezone.now())
    assert invalid_fields.value.code == "invalid_fields"

    with pytest.raises(ValidationError):
        update_expense(expense_id=expense.id, club_id=club.id, name="")
    expense.refresh_from_db()
    assert expense.name != ""

    deleted = ExpenseFactory(club=club)
    deleted.soft_delete()
    with pytest.raises(Expense.DoesNotExist):
        delete_expense(expense_id=deleted.id, club_id=club.id)

    with pytest.raises(ValidationError):
        create_expense(
            club_id=club.id,
            name="",
            amount=Decimal("100"),
            date=timezone.localdate(),
        )
    assert not Expense.objects.for_club(club.id).filter(name="").exists()


@pytest.mark.django_db
def test_create_expense_missing_club_validation_error_has_no_row():
    with pytest.raises(ValidationError):
        create_expense(
            club_id=-1,
            name="Missing club",
            amount=Decimal("100"),
            date=timezone.localdate(),
        )

    assert not Expense.objects.filter(name="Missing club").exists()
