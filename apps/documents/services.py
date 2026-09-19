import logging
from pathlib import Path
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.common.exceptions import BusinessLogicError
from apps.documents.models import DocumentType, StudentDocument
from apps.students.models import Student

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
ALLOWED_FILE_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
IMAGE_FORMATS_BY_EXTENSION = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
PDF_HEADER_READ_SIZE = 1024
VALID_SCOPES = {choice for choice, _ in DocumentType.Scope.choices}


def _raise_invalid_file_type(content_type: str | None = None) -> None:
    message = f"Unsupported file type: {content_type}" if content_type else "Unsupported file type"
    raise BusinessLogicError(message, code="invalid_file_type")


def _rewind_file(file) -> None:
    try:
        file.seek(0)
    except (AttributeError, OSError):
        return


def _validate_pdf_content(file, *, content_type: str) -> None:
    _rewind_file(file)
    header = file.read(PDF_HEADER_READ_SIZE)
    _rewind_file(file)
    if not isinstance(header, bytes) or not header.lstrip().startswith(b"%PDF-"):
        _raise_invalid_file_type(content_type)


def _validate_image_content(file, *, extension: str, content_type: str) -> None:
    expected_format = IMAGE_FORMATS_BY_EXTENSION[extension]
    _rewind_file(file)
    try:
        with Image.open(file) as image:
            actual_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        _rewind_file(file)
        _raise_invalid_file_type(content_type)
    _rewind_file(file)
    if actual_format != expected_format:
        _raise_invalid_file_type(content_type)


def _validate_upload_file(file) -> str:
    extension = Path(getattr(file, "name", "") or "").suffix.lower()
    content_type = (getattr(file, "content_type", "") or "").lower()
    expected_content_type = ALLOWED_FILE_EXTENSIONS.get(extension)

    if expected_content_type is None or content_type not in ALLOWED_MIME_TYPES or content_type != expected_content_type:
        _raise_invalid_file_type(content_type or None)

    if extension == ".pdf":
        _validate_pdf_content(file, content_type=content_type)
    else:
        _validate_image_content(file, extension=extension, content_type=content_type)

    return extension


def _set_safe_upload_name(file, *, extension: str) -> None:
    file.name = f"{uuid4().hex}{extension}"


def _normalize_document_type_integrity_error(exc: IntegrityError) -> None:
    raise BusinessLogicError(
        "Document type name must be unique among active types",
        code="duplicate_document_type_name",
    ) from exc


def _student_matches_scope(*, student: Student, document_type: DocumentType) -> bool:
    if document_type.scope == DocumentType.Scope.ALL:
        return True
    if document_type.scope == DocumentType.Scope.CHILDREN:
        return student.is_child
    if document_type.scope == DocumentType.Scope.ADULTS:
        return not student.is_child
    return False


def _validate_scope(scope: str) -> str:
    if scope not in VALID_SCOPES:
        raise BusinessLogicError("Invalid document scope", code="invalid_scope")
    return scope


def _get_valid_student_document_type(
    *,
    club_id: int,
    student_id: int,
    document_type_id: int,
) -> tuple[Student, DocumentType]:
    student = Student.objects.for_club(club_id).get(id=student_id, deleted_at__isnull=True)
    document_type = DocumentType.objects.for_club(club_id).filter(id=document_type_id).first()
    if (
        document_type is None
        or not document_type.is_active
        or not _student_matches_scope(student=student, document_type=document_type)
    ):
        raise BusinessLogicError("Document type is not available for this student", code="invalid_document_type")
    return student, document_type


def create_document_type(
    *, club_id: int, name: str, description: str = "", is_required: bool = True, scope: str = "all"
) -> DocumentType:
    scope = _validate_scope(scope)
    try:
        with transaction.atomic():
            dt = DocumentType.objects.create(
                club_id=club_id,
                name=name,
                description=description,
                is_required=is_required,
                scope=scope,
            )
    except IntegrityError as exc:
        _normalize_document_type_integrity_error(exc)
    logger.info("document_type_created", extra={"document_type_id": dt.id, "club_id": club_id})
    return dt


_UPDATE_DOCUMENT_TYPE_FIELDS = frozenset({"name", "description", "is_required", "scope", "is_active"})


def update_document_type(*, club_id: int, document_type_id: int, **fields) -> DocumentType:
    bad = set(fields) - _UPDATE_DOCUMENT_TYPE_FIELDS
    if bad:
        raise BusinessLogicError(f"Fields not allowed: {bad}", code="invalid_fields")
    if "scope" in fields:
        fields["scope"] = _validate_scope(fields["scope"])
    dt = DocumentType.objects.for_club(club_id).get(id=document_type_id)
    for key, value in fields.items():
        setattr(dt, key, value)
    try:
        with transaction.atomic():
            dt.save(update_fields=[*fields.keys(), "updated_at"])
    except IntegrityError as exc:
        _normalize_document_type_integrity_error(exc)
    return dt


def deactivate_document_type(*, club_id: int, document_type_id: int) -> DocumentType:
    return update_document_type(club_id=club_id, document_type_id=document_type_id, is_active=False)


def mark_document_provided(
    *,
    club_id: int,
    student_id: int,
    document_type_id: int,
    is_provided: bool = True,
    notes: str = "",
) -> StudentDocument:
    _, document_type = _get_valid_student_document_type(
        club_id=club_id,
        student_id=student_id,
        document_type_id=document_type_id,
    )
    sd, _ = StudentDocument.objects.update_or_create(
        club_id=club_id,
        student_id=student_id,
        document_type=document_type,
        deleted_at__isnull=True,
        defaults={"is_provided": is_provided, "notes": notes},
    )
    logger.info(
        "document_marked",
        extra={"student_id": student_id, "document_type_id": document_type_id, "club_id": club_id},
    )
    return sd


def upload_student_document(*, club_id: int, student_id: int, document_type_id: int, file) -> StudentDocument:
    if file.size > MAX_FILE_SIZE:
        raise BusinessLogicError("File too large (max 10MB)", code="file_too_large")
    extension = _validate_upload_file(file)
    _set_safe_upload_name(file, extension=extension)
    _, document_type = _get_valid_student_document_type(
        club_id=club_id,
        student_id=student_id,
        document_type_id=document_type_id,
    )
    sd, _ = StudentDocument.objects.update_or_create(
        club_id=club_id,
        student_id=student_id,
        document_type=document_type,
        deleted_at__isnull=True,
        defaults={"is_provided": True, "file": file, "uploaded_at": timezone.now()},
    )
    logger.info(
        "document_uploaded",
        extra={"student_id": student_id, "document_type_id": document_type_id, "club_id": club_id},
    )
    return sd
