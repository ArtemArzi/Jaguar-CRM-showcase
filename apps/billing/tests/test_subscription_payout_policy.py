from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import create_checkin
from apps.attendance.tasks import calculate_salary
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Debt, SubscriptionComponent, Tariff, TrainingType
from apps.billing.services import create_payment, create_tariff, verify_payment
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning
from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory


@pytest.fixture(autouse=True)
def _disable_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)


@pytest.mark.django_db
def test_personal_package_on_checkin_uses_paid_unit_basis(club):
    location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(club=club, slug="policy-personal", kind=TrainingType.Kind.PERSONAL)
    trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("50.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="5 персоналок",
        training_type_id=personal_type.id,
        price=Decimal("10000.00"),
        trainings_limit=5,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        package_owner_trainer_id=trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    assert not TrainerEarning.objects.filter(payment=payment).exists()

    schedule = ScheduleFactory(club=club, location=location, trainer=trainer, training_type=personal_type)
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)

    earning = TrainerEarning.objects.get(checkin=checkin)
    assert earning.subscription_price == Decimal("2000.00")
    assert earning.amount == Decimal("1000.00")


@pytest.mark.django_db
def test_mini_group_package_defaults_to_checkin_payout(club):
    location = LocationFactory(club=club)
    mini_group_type = TrainingTypeFactory(
        club=club,
        slug="policy-mini-group",
        kind=TrainingType.Kind.MINI_GROUP,
    )
    trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=mini_group_type,
        percent=Decimal("30.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="4 мини-группы",
        training_type_id=mini_group_type.id,
        price=Decimal("8000.00"),
        trainings_limit=4,
        duration_days=30,
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        package_owner_trainer_id=trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    assert not TrainerEarning.objects.filter(payment=payment).exists()

    schedule = ScheduleFactory(club=club, location=location, trainer=trainer, training_type=mini_group_type)
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=mini_group_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)

    earning = TrainerEarning.objects.get(checkin=checkin)
    assert earning.payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN
    assert earning.subscription_price == Decimal("2000.00")
    assert earning.amount == Decimal("600.00")


@pytest.mark.django_db
def test_personal_package_on_payment_pays_owner_once_and_skips_checkin_salary(club):
    location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(club=club, slug="policy-personal-upfront", kind=TrainingType.Kind.PERSONAL)
    seller = TrainerFactory(club=club)
    owner = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=owner,
        location=location,
        training_type=personal_type,
        percent=Decimal("20.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Персоналки upfront",
        training_type_id=personal_type.id,
        price=Decimal("10000.00"),
        trainings_limit=5,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        seller_trainer_id=seller.id,
        package_owner_trainer_id=owner.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    create_sale_earning(payment.id, club.id)

    sale = TrainerEarning.objects.get(payment=payment)
    assert sale.trainer_id == owner.id
    assert sale.amount == Decimal("2000.00")
    assert sale.subscription_price == Decimal("10000.00")

    schedule = ScheduleFactory(club=club, location=location, trainer=owner, training_type=personal_type)
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)

    assert TrainerEarning.objects.filter(checkin=checkin).count() == 0


@pytest.mark.django_db
def test_none_payout_policy_never_creates_trainer_earning(club):
    location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(club=club, slug="policy-personal-none", kind=TrainingType.Kind.PERSONAL)
    trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("50.00"),
    )
    tariff = create_tariff(
        club_id=club.id,
        name="Без выплат",
        training_type_id=personal_type.id,
        price=Decimal("10000.00"),
        trainings_limit=5,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.NONE,
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        recorded_by_id=user.id,
        package_owner_trainer_id=trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)

    schedule = ScheduleFactory(club=club, location=location, trainer=trainer, training_type=personal_type)
    result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    checkin = Checkin.objects.get(id=result["checkin_id"])
    calculate_salary(checkin.id, club.id)

    assert TrainerEarning.objects.filter(payment=payment).count() == 0
    assert TrainerEarning.objects.filter(checkin=checkin).count() == 0


@pytest.mark.django_db
def test_debt_settlement_on_payment_component_skips_checkin_salary(club):
    location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(
        club=club,
        slug="policy-debt-on-payment",
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=Decimal("2000.00"),
        trial_free=False,
    )
    trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("20.00"),
    )
    schedule = ScheduleFactory(club=club, location=location, trainer=trainer, training_type=personal_type)
    student = StudentFactory(club=club)
    debt_result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    debt_checkin = Checkin.objects.get(id=debt_result["checkin_id"])
    debt = Debt.objects.get(checkin=debt_checkin)
    tariff = create_tariff(
        club_id=club.id,
        name="Долг on payment",
        training_type_id=personal_type.id,
        price=Decimal("10000.00"),
        trainings_limit=5,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    user = UserFactory()

    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        debt_ids=[debt.id],
        recorded_by_id=user.id,
        package_owner_trainer_id=trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    debt_checkin.refresh_from_db()

    assert debt_checkin.subscription_component_id is not None
    assert TrainerEarning.objects.filter(payment=payment, earning_source=TrainerEarning.Source.SALE).count() == 1
    calculate_salary(debt_checkin.id, club.id)
    assert TrainerEarning.objects.filter(checkin=debt_checkin).count() == 0


@pytest.mark.django_db
def test_debt_settlement_none_component_skips_all_trainer_earnings(club):
    location = LocationFactory(club=club)
    personal_type = TrainingTypeFactory(
        club=club,
        slug="policy-debt-none",
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=Decimal("2000.00"),
        trial_free=False,
    )
    trainer = TrainerFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=personal_type,
        percent=Decimal("20.00"),
    )
    schedule = ScheduleFactory(club=club, location=location, trainer=trainer, training_type=personal_type)
    student = StudentFactory(club=club)
    debt_result = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=personal_type.id,
        source=Checkin.Source.MANUAL,
        checkin_date=date(2026, 7, 6),
    )
    debt_checkin = Checkin.objects.get(id=debt_result["checkin_id"])
    debt = Debt.objects.get(checkin=debt_checkin)
    tariff = create_tariff(
        club_id=club.id,
        name="Долг none",
        training_type_id=personal_type.id,
        price=Decimal("10000.00"),
        trainings_limit=5,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.NONE,
    )
    user = UserFactory()

    payment = create_payment(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        payment_method="cash",
        debt_ids=[debt.id],
        recorded_by_id=user.id,
        package_owner_trainer_id=trainer.id,
    )
    verify_payment(payment_id=payment.id, club_id=club.id, verified_by_id=user.id, action="confirm")
    create_sale_earning(payment.id, club.id)
    debt_checkin.refresh_from_db()

    assert debt_checkin.subscription_component_id is not None
    calculate_salary(debt_checkin.id, club.id)
    assert TrainerEarning.objects.filter(payment=payment).count() == 0
    assert TrainerEarning.objects.filter(checkin=debt_checkin).count() == 0


@pytest.mark.django_db
def test_migration_0022_backfills_legacy_default_components_and_unit_basis(club):
    import importlib

    personal_type = TrainingTypeFactory(club=club, slug="legacy-personal", kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(
        club=club,
        training_type=personal_type,
        price=Decimal("10000.00"),
        trainings_limit=5,
        trainer_payout_policy="",
    )
    subscription = SubscriptionFactory(
        club=club,
        tariff=tariff,
        paid_amount=Decimal("9000.00"),
        trainings_left=4,
        trainings_used=1,
        trainer_payout_policy_snapshot="",
    )

    migration = importlib.import_module(
        "apps.billing.migrations.0022_subscription_trainer_payout_policy_snapshot_and_more"
    )
    migration.backfill_package_components(django_apps, None)

    tariff.refresh_from_db()
    subscription.refresh_from_db()
    component = tariff.components.get(is_active=True)
    sub_component = SubscriptionComponent.objects.get(subscription=subscription, tariff_component=component)
    assert tariff.trainer_payout_policy == Tariff.PayoutPolicy.ON_CHECKIN
    assert subscription.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN
    assert component.paid_amount_basis == Decimal("10000.00")
    assert sub_component.paid_amount_basis_snapshot == Decimal("9000.00")
    assert sub_component.unit_amount_basis_snapshot == Decimal("1800.00")
    assert sub_component.credits_left == 4
