import factory

from apps.clubs.tests.factories import ClubFactory
from apps.documents.models import DocumentType, StudentDocument
from apps.students.tests.factories import StudentFactory


class DocumentTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DocumentType

    club = factory.SubFactory(ClubFactory)
    name = factory.Sequence(lambda n: f"Document Type {n}")
    description = ""
    is_required = True
    scope = DocumentType.Scope.ALL
    is_active = True


class StudentDocumentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StudentDocument

    club = factory.LazyAttribute(lambda o: o.student.club)
    student = factory.SubFactory(StudentFactory)
    document_type = factory.SubFactory(DocumentTypeFactory)
    is_provided = False
    notes = ""
