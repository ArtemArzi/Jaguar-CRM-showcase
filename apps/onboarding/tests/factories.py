import factory

from apps.clubs.tests.factories import ClubFactory
from apps.onboarding.models import OnboardingDraft


class OnboardingDraftFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OnboardingDraft

    club = factory.SubFactory(ClubFactory)
    current_step = 1
    data = factory.LazyFunction(lambda: {"_schema_version": 2, "_skipped_steps": []})
    is_completed = False
