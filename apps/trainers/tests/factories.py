from decimal import Decimal

import factory

from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.trainers.models import Trainer, TrainerEarning, TrainerLocation, TrainerRate


class TrainerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Trainer

    club = factory.SubFactory(ClubFactory)
    first_name = factory.Sequence(lambda n: f"Trainer{n}")
    last_name = "Coach"
    phone = factory.Sequence(lambda n: f"+7911000{n:04d}")


class TrainerLocationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainerLocation

    club = factory.LazyAttribute(lambda o: o.trainer.club)
    trainer = factory.SubFactory(TrainerFactory)
    location = factory.SubFactory(LocationFactory)

    # Legacy defaults — stripped inside _create and mirrored to TrainerRate
    # rows so that historical tests that don't explicitly pass rates still
    # get working rates on existing TrainingType rows.
    rate_group = 20.00
    rate_personal = 50.00
    rate_mini_group = 40.00

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        """Back-compat for tests that still pass rate_group=/rate_personal=/
        rate_mini_group= as kwargs. Columns are gone (Wave 4 migration
        0008), so we strip them and upsert TrainerRate rows instead,
        keyed by matching TrainingType.kind in the same club. Tests that
        want rates + call this AFTER creating the TrainingType rows Just
        Work; tests that don't care about rates pass no kwargs and get
        an empty rate set.
        """
        from decimal import Decimal

        from apps.billing.models import TrainingType

        legacy_rates = {}
        for key in ("rate_group", "rate_personal", "rate_mini_group"):
            if key in kwargs:
                legacy_rates[key] = Decimal(str(kwargs.pop(key)))

        obj = super()._create(model_class, *args, **kwargs)

        if not legacy_rates:
            return obj

        kind_to_key = {
            TrainingType.Kind.GROUP: "rate_group",
            TrainingType.Kind.PERSONAL: "rate_personal",
            TrainingType.Kind.MINI_GROUP: "rate_mini_group",
        }
        for tt in TrainingType.objects.filter(club_id=obj.club_id):
            value = legacy_rates.get(kind_to_key.get(tt.kind))
            if value is None:
                continue
            TrainerRate.objects.update_or_create(
                club_id=obj.club_id,
                trainer_id=obj.trainer_id,
                location_id=obj.location_id,
                training_type=tt,
                defaults={"percent": value},
            )
        return obj


class TrainerRateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainerRate

    club = factory.LazyAttribute(lambda o: o.trainer.club)
    trainer = factory.SubFactory(TrainerFactory)
    location = factory.SubFactory(LocationFactory)
    training_type = factory.SubFactory(
        "apps.billing.tests.factories.TrainingTypeFactory",
        club=factory.SelfAttribute("..club"),
    )
    percent = factory.LazyFunction(lambda: Decimal("50.00"))


class TrainerEarningFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainerEarning

    club = factory.SubFactory(ClubFactory)
    trainer = factory.SubFactory(TrainerFactory, club=factory.SelfAttribute("..club"))
    checkin = factory.SubFactory(
        "apps.attendance.tests.factories.CheckinFactory",
        club=factory.SelfAttribute("..club"),
    )
    earning_type = "group"
    amount = factory.LazyFunction(lambda: Decimal("1000.00"))
    rate_percent = factory.LazyFunction(lambda: Decimal("20.00"))
    subscription_price = factory.LazyFunction(lambda: Decimal("5000.00"))
