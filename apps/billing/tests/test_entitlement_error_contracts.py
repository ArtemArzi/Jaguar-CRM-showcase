from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.billing.models import Subscription, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.entitlements import (
    _allocate_component_paid_amounts,
    _deduct_subscription_component_for_checkin,
    _resolve_package_owner_trainer_id_for_components,
    _resolve_package_owner_trainer_id_for_sale,
    _student_has_current_subscription,
)
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory


def test_component_allocation_rejects_non_positive_component_total():
    components = [
        SimpleNamespace(paid_amount_basis=Decimal("0")),
        SimpleNamespace(paid_amount_basis=Decimal("0")),
    ]

    with pytest.raises(BusinessLogicError) as exc_info:
        _allocate_component_paid_amounts(
            components=components,
            paid_amount=Decimal("100"),
        )

    assert exc_info.value.code == "invalid_component_total"


def test_component_allocation_rejects_discount_that_rounds_component_to_zero():
    components = [
        SimpleNamespace(paid_amount_basis=Decimal("50")),
        SimpleNamespace(paid_amount_basis=Decimal("50")),
    ]

    with pytest.raises(BusinessLogicError) as exc_info:
        _allocate_component_paid_amounts(
            components=components,
            paid_amount=Decimal("0.01"),
        )

    assert exc_info.value.code == "component_discount_allocation_required"


@pytest.mark.django_db
def test_deduct_component_rejects_exhausted_credit_without_mutation(club):
    component = SubscriptionComponentFactory(
        club=club,
        credits_total=1,
        credits_left=0,
        credits_used=1,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        _deduct_subscription_component_for_checkin(component=component)

    assert exc_info.value.code == "subscription_component_credits_exhausted"
    component.refresh_from_db()
    assert component.credits_left == 0
    assert component.credits_used == 1


def test_package_owner_resolvers_preserve_distinct_required_messages():
    with pytest.raises(BusinessLogicError) as sale_error:
        _resolve_package_owner_trainer_id_for_sale(
            training_type_kind=TrainingType.Kind.PERSONAL,
            seller_trainer_id=None,
            package_owner_trainer_id=None,
        )
    assert sale_error.value.code == "package_owner_trainer_required"
    assert "пакета" in str(sale_error.value)

    components = [
        SimpleNamespace(
            training_type=SimpleNamespace(kind=TrainingType.Kind.MINI_GROUP),
        )
    ]
    with pytest.raises(BusinessLogicError) as component_error:
        _resolve_package_owner_trainer_id_for_components(
            components=components,
            seller_trainer_id=None,
            package_owner_trainer_id=None,
        )
    assert component_error.value.code == "package_owner_trainer_required"
    assert "компонента" in str(component_error.value)


@pytest.mark.django_db
def test_legacy_student_current_subscription_helper_remains_available(club):
    training_type = TrainingTypeFactory(club=club)
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club)
    SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        scope=Tariff.Scope.CLUB,
        location=None,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
    )

    assert _student_has_current_subscription(
        club_id=club.id,
        student_id=student.id,
        training_type_id=training_type.id,
        scope=Tariff.Scope.CLUB,
        location_id=None,
    )
