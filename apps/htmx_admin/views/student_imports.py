"""Private HTMX adapters for the shared reviewed opening-import workflow."""

from pathlib import Path
from uuid import UUID, uuid4

from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from redis.exceptions import RedisError

from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.students.imports.parsing import ENTITLEMENTS_SHEET, MAX_FILE_BYTES, WorkbookRow
from apps.students.imports.runner import accept_batch
from apps.students.imports.schemas import COLUMNS, EXTRA_COLUMNS
from apps.students.imports.selectors import get_batch, import_workspace, list_import_batches
from apps.students.imports.services import (
    create_template,
    export_result,
    prepare_batch,
    update_draft_item,
    validate_batch,
)
from apps.students.imports.storage import private_path, save_private
from apps.students.imports.workbooks import SETTLEMENT_COLUMNS, workbook_bytes

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KEY_COLUMNS = {"student_key", "source_subscription_key", "source_payment_key", "source_key"}


def _scope(request):
    return {"club_id": request.club.id, "actor_user_id": request.user.id}


def _private(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _download(name):
    try:
        stream = private_path(name=name).open("rb")
    except OSError as exc:
        raise Http404("Файл больше недоступен") from exc
    return _private(FileResponse(stream, as_attachment=True, filename="student-opening.xlsx", content_type=XLSX_MIME))


def _page(request, template, context):
    template += "#content" if request.headers.get("HX-Request") else ""
    return _private(render(request, template, context))


def _batch(request, batch_id):
    try:
        return get_batch(**_scope(request), batch_id=batch_id)
    except BusinessLogicError as exc:
        if exc.code == "target_not_available":
            raise Http404 from exc
        raise


def _workspace(request, batch_id, error=None):
    _batch(request, batch_id)
    context = import_workspace(**_scope(request), batch_id=batch_id, page=request.GET.get("page", 1))
    context["error"] = error
    return _page(request, "dashboard/student_imports/batch.html", context)


@management_view_required
@require_http_methods(["GET", "POST"])
def import_home(request):
    error = None
    if request.method == "POST":
        try:
            upload = request.FILES.get("workbook")
            if (
                upload is None
                or Path(upload.name).suffix.lower() != ".xlsx"
                or upload.content_type not in {XLSX_MIME, "application/zip"}
            ):
                raise BusinessLogicError("Выберите книгу .xlsx.", code="import_invalid_workbook")
            if upload.size > MAX_FILE_BYTES:
                raise BusinessLogicError("Размер книги превышает 10 МБ.", code="import_invalid_workbook")
            content = upload.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise BusinessLogicError("Размер книги превышает 10 МБ.", code="import_invalid_workbook")
            name = save_private(content=content, suffix="xlsx")
            batch = prepare_batch(**_scope(request), path=private_path(name=name))
            validate_batch(**_scope(request), batch_id=batch.id)
            response = redirect("student-opening-batch", batch_id=batch.id)
            if request.headers.get("HX-Request"):
                response.status_code = 200
                response["HX-Redirect"] = reverse("student-opening-batch", kwargs={"batch_id": batch.id})
            return _private(response)
        except BusinessLogicError as exc:
            error = exc.message
    return _page(
        request,
        "dashboard/student_imports/home.html",
        {
            "batches_page": list_import_batches(**_scope(request), page=request.GET.get("page", 1)),
            "error": error,
        },
    )


@management_view_required
@require_http_methods(["GET"])
def import_template(request):
    return _download(create_template(**_scope(request)))


@management_view_required
@require_http_methods(["GET"])
def import_batch(request, batch_id):
    return _workspace(request, batch_id)


@management_view_required
@require_http_methods(["POST"])
def import_validate(request, batch_id):
    _batch(request, batch_id)
    try:
        validate_batch(**_scope(request), batch_id=batch_id)
        return _workspace(request, batch_id)
    except BusinessLogicError as exc:
        return _workspace(request, batch_id, exc.message)


@management_view_required
@require_http_methods(["POST"])
def import_apply(request, batch_id):
    _batch(request, batch_id)
    try:
        accept_batch(
            **_scope(request),
            batch_id=batch_id,
            revision=int(request.POST.get("revision", "")),
            selection_token=str(UUID(request.POST.get("selection_token", ""))),
            channel="htmx",
        )
        from django_q.tasks import async_task

        async_task("apps.students.imports.runner.run_batch_worker", club_id=request.club.id, batch_id=batch_id)
        return _workspace(request, batch_id)
    except BusinessLogicError as exc:
        return _workspace(request, batch_id, exc.message)
    except (OSError, RedisError):
        return _workspace(
            request, batch_id, "Запуск принят, но очередь пока недоступна. Можно продолжить обработку позже."
        )
    except (ValueError, TypeError):
        return _workspace(request, batch_id, "Проверка устарела. Откройте партию и проверьте записи заново.")


@management_view_required
@require_http_methods(["GET"])
def import_export(request, batch_id):
    _batch(request, batch_id)
    return _download(export_result(**_scope(request), batch_id=batch_id))


def _fields(values, columns):
    choices = {
        "Правило начислений": [("on_payment", "С оплаты"), ("on_checkin", "За занятие"), ("none", "Без начисления")],
        "Способ оплаты": [("cash", "Наличные"), ("transfer", "Перевод"), ("unknown", "Неизвестен")],
        "Тип пакета": [("Групповой", "Групповой"), ("Персональный", "Персональный")],
        "Валюта": [("RUB", "Рубли (RUB)")],
        "Вид записи": [("Начальный расчёт", "Начальный расчёт"), ("Выплата", "Выплата")],
        "Способ выплаты": [("Наличные", "Наличные"), ("Перевод", "Перевод")],
    }
    bool_columns = {
        "Ребёнок",
        "После сверки изменений не было",
        "Ранее занимался",
        "Подтверждён отдельный ребёнок",
        "Подтверждено возобновление или переход",
        "Изменить ответственного",
        "Заморожен",
        "Подтверждено",
        "Аванс подтверждён",
    }
    result = []
    for column in columns:
        if column in KEY_COLUMNS:
            continue
        value = values.get(column)
        if isinstance(value, bool):
            value = "Да" if value else "Нет"
        options = [("Да", "Да"), ("Нет", "Нет")] if column in bool_columns else choices.get(column, [])
        if options and value not in {v for v, _ in options}:
            value = next((v for v, label in options if label == value), value)
        result.append(
            {
                "label": column,
                "value": value if value is not None else "",
                "options": [{"value": v, "label": label} for v, label in options],
                "placeholder": "ГГГГ-ММ-ДД ЧЧ:ММ, время клуба"
                if column in {"Сверено по", "Начало работы в CRM"}
                else "",
            }
        )
    return result


@management_view_required
@require_http_methods(["GET", "POST"])
def import_item(request, batch_id, item_id):
    batch = _batch(request, batch_id)
    item = batch.items.for_club(request.club).filter(id=item_id).first()
    if item is None:
        raise Http404
    columns = [*COLUMNS.values(), *EXTRA_COLUMNS] if item.kind == "entitlement" else SETTLEMENT_COLUMNS
    values = dict(item.source_data)
    error = None
    if request.method == "POST":
        values.update({column: request.POST.get(column, "") for column in columns if column not in KEY_COLUMNS})
        try:
            update_draft_item(**_scope(request), batch_id=batch_id, item_id=item_id, values=values)
            validate_batch(**_scope(request), batch_id=batch_id)
            return _private(redirect("student-opening-batch", batch_id=batch_id))
        except BusinessLogicError as exc:
            error = exc.message
    return _page(
        request,
        "dashboard/student_imports/item.html",
        {
            "batch": batch,
            "item": item,
            "fields": _fields(values, columns),
            "error": error,
            "readonly": bool(item.result_receipt_id or batch.status == "applying"),
        },
    )


@management_view_required
@require_http_methods(["GET", "POST"])
def import_single(request):
    columns = [*COLUMNS.values(), *EXTRA_COLUMNS]
    values = {column: request.POST.get(column, "") for column in columns if column not in KEY_COLUMNS}
    error = None
    if request.method == "POST":
        try:
            content = workbook_bytes(
                **_scope(request), namespace=uuid4().hex, rows=[WorkbookRow(ENTITLEMENTS_SHEET, 2, values)]
            )
            name = save_private(content=content, suffix="xlsx")
            batch = prepare_batch(**_scope(request), path=private_path(name=name))
            validate_batch(**_scope(request), batch_id=batch.id)
            return _private(redirect("student-opening-batch", batch_id=batch.id))
        except BusinessLogicError as exc:
            error = exc.message
    return _page(
        request,
        "dashboard/student_imports/item.html",
        {
            "fields": _fields(values, columns),
            "error": error,
            "single": True,
        },
    )
