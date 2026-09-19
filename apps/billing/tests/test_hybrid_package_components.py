from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import cancel_checkin, create_checkin
from apps.attendance.tasks import calculate_salary
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Debt, SubscriptionComponent, Tariff, TariffComponent, TrainingType
from apps.billing.services import create_payment, create_tariff, update_tariff, verify_payment
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import DiscountFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning
from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory


@pytest.fixture(autouse=True)
def _disable_payment_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)


def _hybrid_components(*, group_type, personal_type) -> list[dict]:
    return [
        {
            "name": "Группа",
            "training_type_id": group_type.id,
            "entitlement_kind": TariffComponent.EntitlementKind.WEEKLY_LIMIT,
            "weekly_limit": 2,
            "trainer_payout_policy": Tariff.PayoutPolicy.ON_PAYMENT,
            "paid_amount_basis": Decimal("3500.00"),
        },
        {
            "name": "Персоналки",
            "training_type_id": personal_type.id,
            "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
            "credits_total": 3,
            "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
            "paid_amount_basis": Decimal("6000.00"),
        },
    ]


@pytest.mark.django_db
def test_hybrid_package_uses_component_bases_for_sale_and_checkin_earnings(club):
    location = LocationFactory(club=club)
    group_type = TrainingTypeFactory(club=club, slug="hybrid-group", kind=TrainingType.Kind.GROUP)
    personal_type = TrainingTypeFactory(club=club, slug="hybrid-personal", kind=TrainingType.Kind.PERSONAL)
    group_trainer = TrainerFactory(club=club)
    personal_trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=group_trainer,
        location=location,
        training_type=group_type,
        percent=Decimal("1.00"),
    )
    TrainerRateFactory(
        club=club,
        trainer=personal_trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("2.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Гибрид Оптимум",
        training_type_id=group_type.id,
        price=Decimal("9500.00"),
        trainings_limit=None,
        duration_days=30,
        components=_hybrid_components(group_type=group_type, personal_type=personal_type),
    )
    student = StudentFactory(club=club)
    user = UserFactory()

    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        seller_trainer_id=group_trainer.id,
        package_owner_trainer_id=personal_trainer.id,
    )
    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=user.id,
        action="confirm",
    )
    payment.refresh_from_db()
    subscription = payment.subscription
    components = list(SubscriptionComponent.objects.for_club(club).filter(subscription=subscription))

    assert sum(component.paid_amount_basis_snapshot for component in components) == payment.amount

    create_sale_earning(payment.id, club.id)
    sale = TrainerEarning.objects.get(payment=payment, earning_source=TrainerEarning.Source.SALE)
    assert sale.amount == Decimal("35.00")
    assert sale.subscription_price == Decimal("3500.00")
    assert sale.component_paid_amount_basis_snapshot == Decimal("3500.00")

    personal_schedule = ScheduleFactory(
        club=club,
        location=location,
        trainer=personal_trainer,
        training_type=personal_type,
    )
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=personal_schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)

    personal_component = SubscriptionComponent.objects.get(id=checkin.subscription_component_id)
    personal_earning = TrainerEarning.objects.get(checkin=checkin)
    assert personal_component.credits_left == 2
    assert personal_earning.amount == Decimal("40.00")
    assert personal_earning.subscription_price == Decimal("2000.00")
    assert personal_earning.component_paid_amount_basis_snapshot == Decimal("6000.00")


@pytest.mark.django_db
def test_hybrid_package_discount_is_allocated_to_component_snapshots(club):
    location = LocationFactory(club=club)
    group_type = TrainingTypeFactory(club=club, slug="hybrid-discount-group", kind=TrainingType.Kind.GROUP)
    personal_type = TrainingTypeFactory(club=club, slug="hybrid-discount-personal", kind=TrainingType.Kind.PERSONAL)
    group_trainer = TrainerFactory(club=club)
    personal_trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=group_trainer,
        location=location,
        training_type=group_type,
        percent=Decimal("1.00"),
    )
    TrainerRateFactory(
        club=club,
        trainer=personal_trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("2.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Гибрид со скидкой",
        training_type_id=group_type.id,
        price=Decimal("9500.00"),
        trainings_limit=None,
        duration_days=30,
        components=_hybrid_components(group_type=group_type, personal_type=personal_type),
    )
    discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("10.00"))
    student = StudentFactory(club=club)
    user = UserFactory()

    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        discount_ids=[discount.id],
        recorded_by_id=user.id,
        seller_trainer_id=group_trainer.id,
        package_owner_trainer_id=personal_trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    payment.refresh_from_db()

    components = {
        component.training_type_id: component
        for component in SubscriptionComponent.objects.for_club(club).filter(subscription=payment.subscription)
    }
    assert payment.amount == Decimal("8550.00")
    assert components[group_type.id].paid_amount_basis_snapshot == Decimal("3150.00")
    assert components[personal_type.id].paid_amount_basis_snapshot == Decimal("5400.00")
    assert components[personal_type.id].unit_amount_basis_snapshot == Decimal("1800.00")

    create_sale_earning(payment.id, club.id)
    sale = TrainerEarning.objects.get(payment=payment, earning_source=TrainerEarning.Source.SALE)
    assert sale.amount == Decimal("31.50")
    assert sale.subscription_price == Decimal("3150.00")

    personal_schedule = ScheduleFactory(
        club=club,
        location=location,
        trainer=personal_trainer,
        training_type=personal_type,
    )
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=personal_schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)
    personal_earning = TrainerEarning.objects.get(checkin=checkin)
    assert personal_earning.amount == Decimal("36.00")
    assert personal_earning.subscription_price == Decimal("1800.00")


@pytest.mark.django_db
@pytest.mark.parametrize("finite_group", [False, True])
def test_hybrid_debt_settlement_attaches_matching_component_and_pays_checkin(club, finite_group):
    location = LocationFactory(club=club)
    group_type = TrainingTypeFactory(club=club, slug="hybrid-debt-group", kind=TrainingType.Kind.GROUP)
    personal_type = TrainingTypeFactory(club=club, slug="hybrid-debt-personal", kind=TrainingType.Kind.PERSONAL)
    group_trainer = TrainerFactory(club=club)
    personal_trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=personal_trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("2.00"),
    )
    personal_schedule = ScheduleFactory(
        club=club,
        location=location,
        trainer=personal_trainer,
        training_type=personal_type,
    )
    student = StudentFactory(club=club)
    debt_result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=personal_schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    debt_checkin = Checkin.objects.get(id=debt_result["checkin_id"])
    debt = Debt.objects.get(checkin=debt_checkin)

    components = _hybrid_components(group_type=group_type, personal_type=personal_type)
    if finite_group:
        components[0].update(entitlement_kind="finite_credits", credits_total=4, weekly_limit=None)
    tariff = create_tariff(
        club_id=club.id,
        name="Гибрид с долгом",
        training_type_id=group_type.id,
        price=Decimal("9500.00"),
        trainings_limit=None,
        duration_days=30,
        components=components,
    )
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        debt_ids=[debt.id],
        recorded_by_id=user.id,
        seller_trainer_id=group_trainer.id,
        package_owner_trainer_id=personal_trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")

    debt.refresh_from_db()
    debt_checkin.refresh_from_db()
    assert debt.resolved_at is not None
    assert debt.resolution_type == "payment"
    assert debt_checkin.subscription_id == payment.subscription_id
    assert debt_checkin.subscription_component_id is not None
    component = SubscriptionComponent.objects.get(id=debt_checkin.subscription_component_id)
    assert component.training_type_id == personal_type.id
    assert component.credits_left == 2
    payment.refresh_from_db()
    assert payment.subscription.trainings_used == 1
    assert payment.subscription.trainings_left == (6 if finite_group else None)
    from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings

    assert subscription_balance_findings(subscription=payment.subscription) == []

    calculate_salary(debt_checkin.id, club.id)
    earning = TrainerEarning.objects.get(checkin=debt_checkin)
    assert earning.amount == Decimal("40.00")
    assert earning.subscription_price == Decimal("2000.00")


@pytest.mark.django_db
def test_cancel_hybrid_checkin_restores_component_credits_and_keeps_sale_earning(club):
    location = LocationFactory(club=club)
    group_type = TrainingTypeFactory(club=club, slug="hybrid-cancel-group", kind=TrainingType.Kind.GROUP)
    personal_type = TrainingTypeFactory(club=club, slug="hybrid-cancel-personal", kind=TrainingType.Kind.PERSONAL)
    group_trainer = TrainerFactory(club=club)
    personal_trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=group_trainer,
        location=location,
        training_type=group_type,
        percent=Decimal("1.00"),
    )
    TrainerRateFactory(
        club=club,
        trainer=personal_trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("2.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Гибрид cancel",
        training_type_id=group_type.id,
        price=Decimal("9500.00"),
        trainings_limit=None,
        duration_days=30,
        components=_hybrid_components(group_type=group_type, personal_type=personal_type),
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        seller_trainer_id=group_trainer.id,
        package_owner_trainer_id=personal_trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    sale = TrainerEarning.objects.get(payment=payment, earning_source=TrainerEarning.Source.SALE)
    personal_schedule = ScheduleFactory(
        club=club,
        location=location,
        trainer=personal_trainer,
        training_type=personal_type,
    )
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=personal_schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    component = SubscriptionComponent.objects.get(id=checkin.subscription_component_id)
    assert component.credits_left == 2
    assert component.credits_used == 1
    calculate_salary(checkin.id, club.id)
    checkin_earning = TrainerEarning.objects.get(checkin=checkin)

    cancel_checkin(
        checkin_id=checkin.id,
        club_id=club.id,
        cancelled_by_user_id=user.id,
        user_role="owner",
    )

    component.refresh_from_db()
    checkin.refresh_from_db()
    assert checkin.cancelled_at is not None
    assert component.credits_left == 3
    assert component.credits_used == 0
    checkin_earning.refresh_from_db()
    sale.refresh_from_db()
    assert checkin_earning.cancelled is True
    assert sale.cancelled is False
    assert sale.amount == Decimal("35.00")


@pytest.mark.django_db
def test_hybrid_weekly_group_component_blocks_third_checkin_same_week(club):
    location = LocationFactory(club=club)
    group_type = TrainingTypeFactory(club=club, slug="hybrid-group-cap", kind=TrainingType.Kind.GROUP)
    personal_type = TrainingTypeFactory(club=club, slug="hybrid-personal-cap", kind=TrainingType.Kind.PERSONAL)
    group_trainer = TrainerFactory(club=club)
    personal_trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=group_trainer,
        location=location,
        training_type=group_type,
        percent=Decimal("1.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Гибрид Оптимум",
        training_type_id=group_type.id,
        price=Decimal("9500.00"),
        trainings_limit=None,
        duration_days=30,
        components=_hybrid_components(group_type=group_type, personal_type=personal_type),
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        seller_trainer_id=group_trainer.id,
        package_owner_trainer_id=personal_trainer.id,
    )
    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=user.id,
        action="confirm",
    )
    schedule = ScheduleFactory(
        club=club,
        location=location,
        trainer=group_trainer,
        training_type=group_type,
    )

    create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=group_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=group_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 7),
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=group_type.id,
            source=Checkin.Source.MANUAL,
            checkin_date=date(2026, 7, 8),
        )

    assert exc_info.value.code == "subscription_component_limit_exceeded"


@pytest.mark.django_db
def test_same_training_type_component_can_be_sold_for_different_location_scope(club):
    first_location = LocationFactory(club=club)
    second_location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(club=club, slug="location-personal", kind=TrainingType.Kind.PERSONAL)
    first_trainer = TrainerFactory(club=club)
    second_trainer = TrainerFactory(club=club)
    first_tariff = create_tariff(
        club_id=club.id,
        name="Персоналки зал 1",
        training_type_id=personal_type.id,
        price=Decimal("5000.00"),
        trainings_limit=5,
        duration_days=30,
        scope=Tariff.Scope.LOCATION,
        location_id=first_location.id,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    second_tariff = create_tariff(
        club_id=club.id,
        name="Персоналки зал 2",
        training_type_id=personal_type.id,
        price=Decimal("5000.00"),
        trainings_limit=5,
        duration_days=30,
        scope=Tariff.Scope.LOCATION,
        location_id=second_location.id,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    student = StudentFactory(club=club)
    user = UserFactory()

    first_payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=first_tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        package_owner_trainer_id=first_trainer.id,
    )
    verify_payment(payment_id=first_payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")

    second_payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=second_tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        package_owner_trainer_id=second_trainer.id,
    )

    assert second_payment.status == "pending"


@pytest.mark.django_db
def test_simple_tariff_payout_policy_update_refreshes_default_component(club):
    group_type = TrainingTypeFactory(club=club, slug="simple-policy-group", kind=TrainingType.Kind.GROUP)
    tariff = create_tariff(
        club_id=club.id,
        name="Группа",
        training_type_id=group_type.id,
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
    )
    component = TariffComponent.objects.for_club(club).get(tariff=tariff, is_active=True)
    assert component.trainer_payout_policy == Tariff.PayoutPolicy.ON_PAYMENT

    update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )

    assert not TariffComponent.objects.for_club(club).filter(id=component.id, is_active=True).exists()
    refreshed = TariffComponent.objects.for_club(club).get(tariff=tariff, is_active=True)
    assert refreshed.trainer_payout_policy == Tariff.PayoutPolicy.ON_CHECKIN


@pytest.mark.django_db
def test_name_update_does_not_overwrite_single_custom_component_policy(club):
    group_type = TrainingTypeFactory(
        club=club,
        slug="custom-single-policy-group",
        kind=TrainingType.Kind.GROUP,
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Группа",
        training_type_id=group_type.id,
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
    )
    component = TariffComponent.objects.for_club(club).get(tariff=tariff, is_active=True)
    TariffComponent.objects.for_club(club).filter(id=component.id).update(
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN
    )

    update_tariff(tariff_id=tariff.id, club_id=club.id, name="Группа база")

    component.refresh_from_db()
    assert component.is_active is True
    assert component.trainer_payout_policy == Tariff.PayoutPolicy.ON_CHECKIN
    assert TariffComponent.objects.for_club(club).filter(tariff=tariff, is_active=True).count() == 1
