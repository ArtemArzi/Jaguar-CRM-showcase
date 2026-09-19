from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


class LeadFactory(StudentFactory):
    class Meta:
        model = Student

    status = "lead"
    lead_status = "new"
