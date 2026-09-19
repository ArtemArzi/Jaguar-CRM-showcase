import factory
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory
from apps.pipelines.models import Pipeline, PipelineExecution, PipelineStep


class PipelineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Pipeline

    club = factory.SubFactory(ClubFactory)
    name = "Test Pipeline"
    pipeline_type = Pipeline.PipelineType.FOLLOW_UP
    is_active = True


class PipelineStepFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PipelineStep

    club = factory.LazyAttribute(lambda o: o.pipeline.club)
    pipeline = factory.SubFactory(PipelineFactory)
    order = factory.Sequence(lambda n: n + 1)
    delay_hours = 2
    action_type = PipelineStep.ActionType.CREATE_TASK
    action_config = factory.LazyFunction(lambda: {"message": "Test task"})
    is_terminal = False


class PipelineExecutionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PipelineExecution

    club = factory.LazyAttribute(lambda o: o.pipeline.club)
    pipeline = factory.SubFactory(PipelineFactory)
    student = factory.SubFactory(
        "apps.students.tests.factories.StudentFactory",
        club=factory.SelfAttribute("..club"),
    )
    next_step_at = factory.LazyFunction(timezone.now)
