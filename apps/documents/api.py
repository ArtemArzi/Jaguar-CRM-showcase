from types import SimpleNamespace

from django.http import Http404
from ninja import File, Form, Router, UploadedFile
from ninja.errors import HttpError

from apps.common.permissions import role_required
from apps.documents.schemas import (
    ChecklistItemOut,
    DocumentTypeIn,
    DocumentTypeOut,
    DocumentTypeUpdateIn,
    MarkDocumentIn,
    StudentDocumentOut,
)
from apps.documents.selectors import get_document_checklist, list_active_document_types
from apps.documents.services import (
    create_document_type,
    deactivate_document_type,
    mark_document_provided,
    update_document_type,
    upload_student_document,
)
from apps.students.models import Student
from apps.students.scopes import actor_can_manage_student_sensitive_actions, actor_is_scoped_to_student

router = Router(tags=["documents"])


def _ensure_student_document_access(request, student_id: int, *, sensitive_action: bool = False) -> None:
    """Verify scoped users can only access documents for their own/allowed student."""
    membership = getattr(request, "_membership", None)
    if not membership:
        return

    if membership.role == "trainer":
        if sensitive_action:
            allowed = actor_can_manage_student_sensitive_actions(
                club=request.club,
                membership_role=membership.role,
                user=request.user,
                student_id=student_id,
            )
        else:
            allowed = actor_is_scoped_to_student(
                club=request.club,
                membership_role=membership.role,
                user=request.user,
                student_id=student_id,
            )
        if not allowed:
            raise HttpError(403, "Access denied: not your student")
        return

    if membership.role == "student":
        from apps.students.selectors import get_student_by_user

        own_student = get_student_by_user(club=request.club, user_id=request.user.id)
        if own_student.id != student_id:
            raise Http404
        return

    if membership.role == "parent":
        Student.objects.for_club(request.club).get(
            id=student_id,
            parent_user_id=request.user.id,
            is_child=True,
            deleted_at__isnull=True,
        )


def _student_safe_document_response(document):
    """Return a response object that keeps model fields but hides staff-only notes."""
    return SimpleNamespace(
        id=document.id,
        document_type_id=document.document_type_id,
        is_provided=document.is_provided,
        file=document.file,
        uploaded_at=document.uploaded_at,
        notes=document.notes,
        _force_safe_notes=True,
    )


@router.post("/types/", response={201: DocumentTypeOut})
@role_required("owner", "admin")
def create_type(request, payload: DocumentTypeIn):
    dt = create_document_type(
        club_id=request.club.id,
        name=payload.name,
        description=payload.description,
        is_required=payload.is_required,
        scope=payload.scope,
    )
    return 201, dt


@router.get("/types/", response=list[DocumentTypeOut])
@role_required("owner", "admin", "trainer")
def list_types(request):
    return list_active_document_types(club=request.club)


@router.patch("/types/{type_id}/", response=DocumentTypeOut)
@role_required("owner", "admin")
def update_type(request, type_id: int, payload: DocumentTypeUpdateIn):
    fields = {}
    if payload.name is not None:
        fields["name"] = payload.name
    if payload.description is not None:
        fields["description"] = payload.description
    if payload.is_required is not None:
        fields["is_required"] = payload.is_required
    if payload.scope is not None:
        fields["scope"] = payload.scope
    return update_document_type(club_id=request.club.id, document_type_id=type_id, **fields)


@router.delete("/types/{type_id}/", response={204: None})
@role_required("owner", "admin")
def delete_type(request, type_id: int):
    deactivate_document_type(club_id=request.club.id, document_type_id=type_id)
    return 204, None


@router.get("/students/{student_id}/checklist/", response=list[ChecklistItemOut])
@role_required("owner", "admin", "trainer", "student", "parent")
def student_checklist(request, student_id: int):
    _ensure_student_document_access(request, student_id)
    return get_document_checklist(club=request.club, student_id=student_id)


@router.post("/students/{student_id}/mark/", response=StudentDocumentOut)
@role_required("owner", "admin", "trainer")
def mark_provided(request, student_id: int, payload: MarkDocumentIn):
    _ensure_student_document_access(request, student_id, sensitive_action=True)
    return mark_document_provided(
        club_id=request.club.id,
        student_id=student_id,
        document_type_id=payload.document_type_id,
        is_provided=payload.is_provided,
        notes=payload.notes,
    )


@router.post("/students/{student_id}/upload/", response=StudentDocumentOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def upload_document(
    request,
    student_id: int,
    document_type_id: int = Form(...),
    file: UploadedFile = File(...),
):
    _ensure_student_document_access(request, student_id, sensitive_action=True)
    document = upload_student_document(
        club_id=request.club.id,
        student_id=student_id,
        document_type_id=document_type_id,
        file=file,
    )
    if getattr(request, "_membership", None) and request._membership.role in {"student", "parent"}:
        return _student_safe_document_response(document)
    return document
