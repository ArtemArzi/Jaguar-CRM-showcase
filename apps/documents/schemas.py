from __future__ import annotations

from datetime import datetime

from ninja import Schema


class DocumentTypeIn(Schema):
    name: str
    description: str = ""
    is_required: bool = True
    scope: str = "all"


class DocumentTypeUpdateIn(Schema):
    name: str | None = None
    description: str | None = None
    is_required: bool | None = None
    scope: str | None = None


class DocumentTypeOut(Schema):
    id: int
    name: str
    description: str
    is_required: bool
    scope: str
    is_active: bool
    created_at: datetime


class ChecklistItemOut(Schema):
    document_type: DocumentTypeOut
    is_provided: bool
    has_file: bool


class MarkDocumentIn(Schema):
    document_type_id: int
    is_provided: bool = True
    notes: str = ""


class StudentDocumentOut(Schema):
    id: int
    document_type_id: int
    is_provided: bool
    has_file: bool
    uploaded_at: datetime | None
    notes: str

    @staticmethod
    def resolve_has_file(obj) -> bool:
        return bool(obj.file)

    @staticmethod
    def resolve_notes(obj) -> str:
        if getattr(obj, "_force_safe_notes", False):
            return ""
        return obj.notes
