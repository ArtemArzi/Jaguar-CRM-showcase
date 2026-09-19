from django.db.models import Q

from apps.documents.models import DocumentType, StudentDocument
from apps.students.models import Student


def list_active_document_types(*, club):
    return DocumentType.objects.for_club(club).filter(is_active=True).order_by("name")


def get_document_checklist(*, club, student_id: int) -> list[dict]:
    student = Student.objects.for_club(club).get(id=student_id, deleted_at__isnull=True)
    scope_filter = Q(scope=DocumentType.Scope.ALL)
    if student.is_child:
        scope_filter |= Q(scope=DocumentType.Scope.CHILDREN)
    else:
        scope_filter |= Q(scope=DocumentType.Scope.ADULTS)

    doc_types = list(DocumentType.objects.for_club(club).filter(scope_filter, is_active=True).order_by("name"))
    existing = {
        sd.document_type_id: sd
        for sd in StudentDocument.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .select_related("document_type")
    }
    visible_doc_type_ids = {dt.id for dt in doc_types}
    historical_doc_types = sorted(
        [
            sd.document_type
            for sd in existing.values()
            if (sd.is_provided or bool(sd.file)) and sd.document_type_id not in visible_doc_type_ids
        ],
        key=lambda dt: dt.name,
    )
    result = []
    for dt in [*doc_types, *historical_doc_types]:
        sd = existing.get(dt.id)
        result.append(
            {
                "document_type": dt,
                "is_provided": sd.is_provided if sd else False,
                "has_file": bool(sd and sd.file) if sd else False,
                "student_document": sd,
            }
        )
    return result


def get_missing_documents_count(*, club, student_id: int) -> int:
    student = Student.objects.for_club(club).get(id=student_id, deleted_at__isnull=True)
    scope_filter = Q(scope=DocumentType.Scope.ALL)
    if student.is_child:
        scope_filter |= Q(scope=DocumentType.Scope.CHILDREN)
    else:
        scope_filter |= Q(scope=DocumentType.Scope.ADULTS)

    required_type_ids = set(
        DocumentType.objects.for_club(club)
        .filter(scope_filter, is_active=True, is_required=True)
        .values_list("id", flat=True)
    )
    provided_type_ids = set(
        StudentDocument.objects.for_club(club)
        .filter(student_id=student_id, deleted_at__isnull=True)
        .filter(Q(is_provided=True) | ~Q(file=""))
        .values_list("document_type_id", flat=True)
    )
    return len(required_type_ids - provided_type_ids)
