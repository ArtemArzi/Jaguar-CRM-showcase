import logging
from typing import Any

from django.db import IntegrityError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.documents.models import DocumentType
from apps.documents.services import create_document_type, update_document_type

from ._helpers import _settings_context

logger = logging.getLogger(__name__)

_DOCUMENTS_BASE_PATH = "/dashboard/settings/documents/"
_FORM_PATH = "/dashboard/settings/documents/types/form/"
_EDIT_PATH_TEMPLATE = "/dashboard/settings/documents/types/{document_type_id}/form/"
_VALID_SCOPES = {choice for choice, _ in DocumentType.Scope.choices}


def _document_types_for_club(club) -> list[DocumentType]:
    return list(
        DocumentType.objects.filter(club=club).order_by("-is_active", "name", "id")
    )


def _render_documents_tab(
    request: HttpRequest, *, saved: bool = False, error: str | None = None
) -> HttpResponse:
    ctx = _settings_context("documents", request)
    ctx["document_types"] = _document_types_for_club(request.club)
    ctx["saved"] = saved
    ctx["error"] = error
    template = "dashboard/settings/documents.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


def _form_context(
    *,
    document_type: DocumentType | None = None,
    error: str | None = None,
    form_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = form_values or {}
    required_checked = values.get("is_required")
    if required_checked is None:
        required_checked = document_type.is_required if document_type else True

    return {
        "document_type": document_type,
        "error": error,
        "form_values": values,
        "required_checked": required_checked,
        "initial_name": values.get("name", document_type.name if document_type else ""),
        "initial_description": values.get("description", document_type.description if document_type else ""),
        "initial_scope": values.get("scope", document_type.scope if document_type else DocumentType.Scope.ALL),
        "create_path": _FORM_PATH,
        "edit_path": _EDIT_PATH_TEMPLATE.format(
            document_type_id=document_type.id
        ) if document_type else "",
    }


def _friendly_integrity_error() -> str:
    return "Документ с таким названием уже существует среди активных."


@management_view_required
def settings_documents_view(request: HttpRequest) -> HttpResponse:
    return _render_documents_tab(request)


@management_view_required
def document_type_form(
    request: HttpRequest, document_type_id: int | None = None
) -> HttpResponse:
    club = request.club
    document_type = None

    if document_type_id:
        document_type = (
            DocumentType.objects.filter(club=club, id=document_type_id).first()
        )
        if not document_type:
            raise Http404

    if request.method == "GET":
        return render(
            request,
            "dashboard/settings/documents/_document_type_form.html",
            _form_context(document_type=document_type),
        )

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    scope = request.POST.get("scope", DocumentType.Scope.ALL).strip()
    is_required = request.POST.get("is_required") == "on"

    form_values = {
        "name": name,
        "description": description,
        "scope": scope or DocumentType.Scope.ALL,
        "is_required": is_required,
    }

    if not name:
        return render(
            request,
            "dashboard/settings/documents/_document_type_form.html",
            _form_context(
                document_type=document_type,
                error="Укажите название документа.",
                form_values=form_values,
            ),
        )

    if scope not in _VALID_SCOPES:
        return render(
            request,
            "dashboard/settings/documents/_document_type_form.html",
            _form_context(
                document_type=document_type,
                error="Выберите корректную область действия документа.",
                form_values=form_values,
            ),
        )

    try:
        if document_type:
            update_document_type(
                club_id=club.id,
                document_type_id=document_type.id,
                name=name,
                description=description,
                is_required=is_required,
                scope=scope,
            )
        else:
            create_document_type(
                club_id=club.id,
                name=name,
                description=description,
                is_required=is_required,
                scope=scope,
            )
    except BusinessLogicError as exc:
        logger.warning(
            "document_type_form_business_error",
            extra={"club_id": club.id, "document_type_id": document_type_id, "error": str(exc)},
        )
        return render(
            request,
            "dashboard/settings/documents/_document_type_form.html",
            _form_context(
                document_type=document_type,
                error=str(exc),
                form_values=form_values,
            ),
        )
    except IntegrityError:
        return render(
            request,
            "dashboard/settings/documents/_document_type_form.html",
            _form_context(
                document_type=document_type,
                error=_friendly_integrity_error(),
                form_values=form_values,
            ),
        )

    response = _render_documents_tab(request, saved=True)
    response["HX-Retarget"] = "#content"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeSlideOver"
    return response


@management_view_required
@require_POST
def document_type_toggle(request: HttpRequest, document_type_id: int) -> HttpResponse:
    document_type = (
        DocumentType.objects.filter(club=request.club, id=document_type_id).first()
    )
    if not document_type:
        raise Http404

    try:
        update_document_type(
            club_id=request.club.id,
            document_type_id=document_type.id,
            is_active=not document_type.is_active,
        )
    except BusinessLogicError as exc:
        error = _friendly_integrity_error() if exc.code == "duplicate_document_type_name" else str(exc)
        return _render_documents_tab(request, error=error)
    except IntegrityError:
        return _render_documents_tab(
            request,
            error=_friendly_integrity_error(),
        )

    return _render_documents_tab(request)
