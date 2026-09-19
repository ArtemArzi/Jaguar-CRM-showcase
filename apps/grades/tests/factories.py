import factory

from apps.clubs.tests.factories import ClubFactory
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.tests.factories import StudentFactory


class GradeSystemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = GradeSystem

    club = factory.SubFactory(ClubFactory)
    discipline = factory.Sequence(lambda n: f"Discipline{n}")
    is_active = True


class GradeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Grade

    club = factory.LazyAttribute(lambda o: o.grade_system.club)
    grade_system = factory.SubFactory(GradeSystemFactory)
    name = factory.Sequence(lambda n: f"Grade{n}")
    order = factory.Sequence(lambda n: n)
    min_trainings = 0


class StudentGradeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StudentGrade

    club = factory.LazyAttribute(lambda o: o.student.club)
    student = factory.SubFactory(StudentFactory)
    grade_system = factory.SubFactory(GradeSystemFactory)
    current_grade = None
    trainings_since_last_grade = 0
