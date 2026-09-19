import factory
from django.contrib.auth import get_user_model

from apps.clubs.models import Club, ClubMembership, ClubSettings, Location

User = get_user_model()


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    password = factory.PostGenerationMethodCall("set_password", "testpass123")


class ClubFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Club

    name = factory.Sequence(lambda n: f"Fight Club {n}")
    city = "Moscow"
    disciplines = ["boxing"]

    @factory.post_generation
    def training_group_rollout_state(self, create, extracted, **kwargs):
        if not create:
            return
        from apps.attendance.models import TrainingGroupRolloutState

        TrainingGroupRolloutState.objects.get_or_create(
            club=self,
            defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
        )


class LocationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Location

    club = factory.SubFactory(ClubFactory)
    name = factory.LazyAttribute(lambda o: f"{o.club.name} - Main")


class ClubSettingsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ClubSettings

    club = factory.SubFactory(ClubFactory)
    freeze_enabled = True
    freeze_max_days = 30
    freeze_max_count = None
    primary_color = "#C45A3B"
    accent_color = "#C45A3B"
    club_name_display = "Jaguar Muay Thai"
    logo_url = ""


class ClubMembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ClubMembership

    user = factory.SubFactory(UserFactory)
    club = factory.SubFactory(ClubFactory)
    role = "owner"
