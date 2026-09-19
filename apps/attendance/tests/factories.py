from datetime import date, datetime, time, timedelta

import factory

from apps.attendance.models import (
    Checkin,
    GroupSession,
    PersonalAvailabilitySlot,
    Schedule,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMembership,
    TrainingGroupRolloutState,
)
from apps.billing.tests.factories import TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


class ScheduleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Schedule

    club = factory.SubFactory(ClubFactory)
    day_of_week = 0  # Monday
    start_time = factory.LazyFunction(lambda: time(10, 0))
    end_time = factory.LazyAttribute(
        lambda schedule: (
            datetime.combine(date.min, schedule.start_time) + timedelta(hours=1)
        ).time()
    )
    group_name = "Adults Boxing"
    trainer = factory.SubFactory(TrainerFactory, club=factory.SelfAttribute("..club"))
    location = factory.SubFactory(LocationFactory, club=factory.SelfAttribute("..club"))
    training_type = factory.SubFactory(TrainingTypeFactory, club=factory.SelfAttribute("..club"))


class TrainingGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainingGroup

    club = factory.SubFactory(ClubFactory)
    name = factory.Sequence(lambda n: f"Training Group {n}")
    training_type = factory.SubFactory(
        TrainingTypeFactory,
        club=factory.SelfAttribute("..club"),
        kind="group",
    )
    location = factory.SubFactory(LocationFactory, club=factory.SelfAttribute("..club"))
    responsible_trainer = factory.SubFactory(TrainerFactory, club=factory.SelfAttribute("..club"))
    status = TrainingGroup.Status.ACTIVE


class TrainingGroupMembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainingGroupMembership

    club = factory.LazyAttribute(lambda obj: obj.training_group.club)
    student = factory.SubFactory(StudentFactory, club=factory.SelfAttribute("..club"))
    training_group = factory.SubFactory(TrainingGroupFactory)
    starts_on = factory.LazyFunction(lambda: date(2026, 7, 1))
    source = TrainingGroupMembership.Source.MANUAL


class TrainingGroupRolloutStateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TrainingGroupRolloutState
        django_get_or_create = ("club",)

    club = factory.SubFactory(ClubFactory)
    mode = TrainingGroupRolloutState.Mode.OFF


class ScheduleExceptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ScheduleException

    club = factory.LazyAttribute(lambda o: o.schedule.club)
    schedule = factory.SubFactory(ScheduleFactory)
    date = factory.LazyFunction(lambda: date(2026, 4, 1))
    exception_type = "cancelled"


class CheckinFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Checkin

    club = factory.SubFactory(ClubFactory)
    student = factory.SubFactory(StudentFactory, club=factory.SelfAttribute("..club"))
    schedule = factory.SubFactory(ScheduleFactory, club=factory.SelfAttribute("..club"))
    training_type = factory.SubFactory(TrainingTypeFactory, club=factory.SelfAttribute("..club"))
    trainer = factory.LazyAttribute(lambda o: o.schedule.trainer)
    location = factory.LazyAttribute(lambda o: o.schedule.location)
    date = factory.LazyFunction(lambda: date.today())
    source = "batch"


class GroupSessionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = GroupSession

    club = factory.SubFactory(ClubFactory)
    schedule = factory.SubFactory(ScheduleFactory, club=factory.SelfAttribute("..club"))
    date = factory.LazyFunction(lambda: date.today())
    trainer = factory.LazyAttribute(lambda o: o.schedule.trainer)
    attendee_count = 5


class PersonalAvailabilitySlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PersonalAvailabilitySlot

    club = factory.SubFactory(ClubFactory)
    trainer = factory.SubFactory(TrainerFactory, club=factory.SelfAttribute("..club"))
    location = factory.SubFactory(LocationFactory, club=factory.SelfAttribute("..club"))
    training_type = factory.SubFactory(TrainingTypeFactory, club=factory.SelfAttribute("..club"))
    starts_at = factory.LazyFunction(lambda: datetime(2026, 7, 7, 10, 0))
    ends_at = factory.LazyFunction(lambda: datetime(2026, 7, 7, 11, 0))
    status = PersonalAvailabilitySlot.Status.PUBLISHED
