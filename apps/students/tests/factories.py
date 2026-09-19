from datetime import timedelta

import factory
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.students.models import ParentInvite, Student, StudentNote


class StudentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Student

    club = factory.SubFactory(ClubFactory)
    first_name = factory.Sequence(lambda n: f"Student{n}")
    last_name = "Testov"
    phone = factory.Sequence(lambda n: f"+7900000{n:04d}")
    status = "lead"
    lead_status = factory.LazyAttribute(
        lambda student: Student.LeadStatus.NEW if student.status == Student.Status.LEAD else None
    )
    parent_user = None


class StudentNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StudentNote

    club = factory.LazyAttribute(lambda o: o.student.club)
    student = factory.SubFactory(StudentFactory)
    author = factory.SubFactory(UserFactory)
    text = "Test note"


class ParentInviteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ParentInvite

    club = factory.LazyAttribute(lambda o: o.student.club)
    student = factory.SubFactory(StudentFactory, is_child=True)
    expires_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=7))
