from datetime import timedelta
from decimal import Decimal

import factory
from django.utils import timezone

from apps.billing.models import (
    Debt,
    Discount,
    Expense,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionFreeze,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.students.tests.factories import StudentFactory


class TrainingTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainingType

    club = factory.SubFactory(ClubFactory)
    name = factory.Sequence(lambda n: f"Type{n}")
    slug = factory.Sequence(lambda n: f"type-{n}")
    kind = TrainingType.Kind.PERSONAL
    drop_in_price = Decimal("1000")
    trial_free = True


class TariffFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tariff

    club = factory.LazyAttribute(lambda o: o.training_type.club)
    training_type = factory.SubFactory(TrainingTypeFactory)
    name = factory.Sequence(lambda n: f"Tariff{n}")
    price = factory.LazyFunction(lambda: 5000)
    trainings_limit = 8
    duration_days = 30
    scope = "club"


class SubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Subscription

    club = factory.LazyAttribute(lambda o: o.tariff.club)
    student = factory.SubFactory(StudentFactory)
    tariff = factory.SubFactory(TariffFactory)
    trainings_left = factory.LazyAttribute(lambda o: o.tariff.trainings_limit)
    trainings_used = 0
    expires_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=30))
    scope = factory.LazyAttribute(lambda o: o.tariff.scope)
    location = factory.LazyAttribute(lambda o: o.tariff.location)


class TariffComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TariffComponent

    club = factory.LazyAttribute(lambda o: o.tariff.club)
    tariff = factory.SubFactory(TariffFactory)
    name = factory.Sequence(lambda n: f"Component{n}")
    training_type = factory.LazyAttribute(lambda o: o.tariff.training_type)
    entitlement_kind = TariffComponent.EntitlementKind.FINITE_CREDITS
    credits_total = 8
    weekly_limit = None
    scope = factory.LazyAttribute(lambda o: o.tariff.scope)
    location = factory.LazyAttribute(lambda o: o.tariff.location)
    trainer_payout_policy = Tariff.PayoutPolicy.ON_CHECKIN
    paid_amount_basis = factory.LazyAttribute(lambda o: o.tariff.price)


class SubscriptionComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubscriptionComponent

    club = factory.LazyAttribute(lambda o: o.subscription.club)
    subscription = factory.SubFactory(SubscriptionFactory)
    tariff_component = factory.SubFactory(
        TariffComponentFactory,
        tariff=factory.SelfAttribute("..subscription.tariff"),
    )
    name_snapshot = factory.LazyAttribute(lambda o: o.tariff_component.name)
    training_type = factory.LazyAttribute(lambda o: o.tariff_component.training_type)
    entitlement_kind = factory.LazyAttribute(lambda o: o.tariff_component.entitlement_kind)
    credits_total = factory.LazyAttribute(lambda o: o.tariff_component.credits_total)
    credits_left = factory.LazyAttribute(lambda o: o.tariff_component.credits_total)
    weekly_limit = factory.LazyAttribute(lambda o: o.tariff_component.weekly_limit)
    scope = factory.LazyAttribute(lambda o: o.tariff_component.scope)
    location = factory.LazyAttribute(lambda o: o.tariff_component.location)
    trainer_payout_policy_snapshot = factory.LazyAttribute(lambda o: o.tariff_component.trainer_payout_policy)
    paid_amount_basis_snapshot = factory.LazyAttribute(lambda o: o.tariff_component.paid_amount_basis)
    unit_amount_basis_snapshot = factory.LazyAttribute(
        lambda o: o.tariff_component.paid_amount_basis / o.tariff_component.credits_total
        if o.tariff_component.credits_total
        else None
    )


class DebtFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Debt

    club = factory.SubFactory(ClubFactory)
    student = factory.SubFactory(StudentFactory)
    reason = "no_subscription"


class SubscriptionFreezeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubscriptionFreeze

    club = factory.LazyAttribute(lambda o: o.subscription.club)
    subscription = factory.SubFactory(SubscriptionFactory)
    days = 10
    reason = "vacation"
    frozen_by = factory.SubFactory(UserFactory)
    starts_at = factory.LazyFunction(timezone.now)


class DiscountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Discount

    club = factory.SubFactory(ClubFactory)
    name = factory.Sequence(lambda n: f"Discount {n}")
    discount_type = "percent"
    value = 10
    is_active = True


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    club = factory.LazyAttribute(lambda o: o.tariff.club)
    student = factory.SubFactory(StudentFactory)
    tariff = factory.SubFactory(TariffFactory)
    amount = factory.LazyAttribute(lambda o: o.tariff.price)
    original_amount = factory.LazyAttribute(lambda o: o.tariff.price)
    payment_method = "cash"
    status = Payment.Status.PENDING
    recorded_by = factory.SubFactory(UserFactory)


class ExpenseFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Expense

    club = factory.SubFactory(ClubFactory)
    name = factory.Sequence(lambda n: f"Expense {n}")
    amount = 10000
    date = factory.LazyFunction(lambda: timezone.now().date())
    is_recurring = False
