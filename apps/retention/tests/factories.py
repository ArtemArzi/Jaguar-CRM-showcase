import factory
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory
from apps.retention.models import RetentionTask


class RetentionTaskFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RetentionTask

    club = factory.SubFactory(ClubFactory)
    student = factory.SubFactory(
        "apps.students.tests.factories.StudentFactory",
        club=factory.SelfAttribute("..club"),
    )
    trainer = factory.SubFactory(
        "apps.trainers.tests.factories.TrainerFactory",
        club=factory.SelfAttribute("..club"),
    )
    level = RetentionTask.Level.YELLOW
    due_date = factory.LazyFunction(lambda: timezone.now().date())
