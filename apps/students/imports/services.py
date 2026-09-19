"""Reviewed private batches. Domain issuance and durable results commit per item."""

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.billing.models import OpeningEntitlementSnapshot, Tariff
from apps.billing.service_modules.opening_subscriptions import _authorize, preview_opening_entitlement
from apps.clubs.models import Club
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.students.identity_services import normalize_person_identity
from apps.students.imports.parsing import ENTITLEMENTS_SHEET, MAX_FILE_BYTES, WorkbookRow, read_workbook
from apps.students.imports.schemas import terms_from_row
from apps.students.imports.selectors import get_batch
from apps.students.imports.storage import private_path, save_private
from apps.students.imports.workbooks import workbook_bytes
from apps.students.models import OpeningImportBatch, OpeningImportItem, OpeningImportPreview

DRAFT_TTL = timedelta(days=7)
PREVIEW_TTL = timedelta(hours=24)


def _key_identity(values):
    from apps.students.imports.parsing import parse_date, parse_phone
    from apps.students.imports.schemas import boolean

    try:
        is_child = boolean(values.get("Ребёнок"))
        birth = parse_date(values["Дата рождения"]) if values.get("Дата рождения") else None
        identity = normalize_person_identity(
            first_name=str(values.get("Имя") or ""), last_name=str(values.get("Фамилия") or ""),
            is_child=is_child, phone=parse_phone(values.get("Телефон")),
            guardian_phone=parse_phone(values.get("Телефон родителя")), date_of_birth=birth,
        )
        if not identity.first_name or (is_child and (birth is None or not identity.guardian_phone)):
            return None
        if not is_child and not identity.phone:
            return None
        return (identity.first_name.casefold(), identity.last_name.casefold(), is_child,
                identity.phone, identity.guardian_phone, birth)
    except BusinessLogicError:
        return None


def _error(code, message):
    return {"code": code, "message": message}


def _fail(message, code="import_needs_review"):
    raise BusinessLogicError(message, code=code)


def _resolve_names(*, club_id, values):
    from apps.attendance.models import TrainingGroup
    from apps.trainers.models import Trainer

    result = dict(values)
    mappings = (
        ("Тариф", "ID тарифа", Tariff), ("Группа", "ID группы", TrainingGroup),
        ("Ответственный тренер", "ID ответственного тренера", Trainer),
        ("Владелец пакета", "ID владельца пакета", Trainer),
        ("Получатель комиссии", "ID получателя комиссии", Trainer),
    )
    for label, id_label, model in mappings:
        name = str(values.get(label) or "").strip().casefold()
        if not name:
            continue
        matches = []
        for record in model.objects.for_club(club_id).order_by("id"):
            display = f"{record.first_name} {record.last_name}" if model is Trainer else record.name
            if display.strip().casefold() == name:
                matches.append(record.id)
        supplied = values.get(id_label)
        if supplied not in (None, ""):
            from apps.students.imports.schemas import integer

            if integer(supplied) not in matches:
                _fail("Название не совпадает с выбранным ID. Сверьте справочник.", "import_mapping_needs_review")
        elif len(matches) == 1:
            result[id_label] = matches[0]
        else:
            _fail("Название не найдено или неоднозначно. Выберите ID из справочника.", "import_mapping_needs_review")
    return result


def create_template(*, club_id: int, actor_user_id: int):
    content = workbook_bytes(club_id=club_id, actor_user_id=actor_user_id, namespace=uuid.uuid4().hex)
    return save_private(content=content, suffix="xlsx")


def prepare_batch(*, club_id: int, actor_user_id: int, path):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        _fail("Нужен файл .xlsx.", "import_invalid_workbook")
    with path.open("rb") as source:
        content = source.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        _fail("Размер книги превышает 10 МБ.", "import_invalid_workbook")
    source_file = save_private(content=content, suffix="xlsx")
    book = read_workbook(path=private_path(name=source_file))
    namespace = book.namespace or uuid.uuid4().hex
    rows, person_keys = [], {}
    for row in book.rows:
        values = dict(row.values)
        for column in ("Начало", "Действует по", "Дата оплаты", "Дата рождения", "Дата"):
            if isinstance(values.get(column), datetime) and values[column].time().isoformat() == "00:00:00":
                values[column] = values[column].date().isoformat()
        if row.sheet == ENTITLEMENTS_SHEET:
            # Only a fully identified person can share an automatically assigned
            # opaque key. These transient values are never durable source keys.
            identity = _key_identity(values)
            if identity is not None and values.get("student_key"):
                person_keys.setdefault(identity, values["student_key"])
            if not values.get("student_key"):
                values["student_key"] = (
                    person_keys.setdefault(identity, uuid.uuid4().hex) if identity is not None else uuid.uuid4().hex
                )
            values.setdefault("source_subscription_key", None)
            values.setdefault("source_payment_key", None)
            values["source_subscription_key"] = values["source_subscription_key"] or uuid.uuid4().hex
            values["source_payment_key"] = values["source_payment_key"] or uuid.uuid4().hex
        else:
            values["source_key"] = values.get("source_key") or uuid.uuid4().hex
        rows.append(replace(row, values=values))
    prepared = workbook_bytes(club_id=club_id, actor_user_id=actor_user_id, namespace=namespace, rows=rows)
    prepared_file = save_private(content=prepared, suffix="xlsx")
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        batch = OpeningImportBatch.objects.create(
            club_id=club_id, source_namespace=namespace, created_by_id=actor_user_id,
            source_file=source_file, prepared_file=prepared_file, expires_at=timezone.now() + DRAFT_TTL,
        )
        for row in rows:
            entitlement = row.sheet == ENTITLEMENTS_SHEET
            kind = OpeningImportItem.Kind.ENTITLEMENT if entitlement else (
                OpeningImportItem.Kind.SETTLEMENT_OPENING if row.values.get("Вид записи") == "Начальный расчёт"
                else OpeningImportItem.Kind.SETTLEMENT_PAYOUT
            )
            OpeningImportItem.objects.create(
                club_id=club_id, batch=batch, kind=kind, source_row=row.number, source_sheet=row.sheet,
                source_key=row.values["source_subscription_key" if entitlement else "source_key"],
                source_data=json.loads(json.dumps(row.values, default=str)),
            )
    return batch


def _draft_available(batch):
    if batch.expires_at <= timezone.now():
        _fail("Срок черновика истёк. Подготовьте новую книгу.", "import_draft_expired")
    if batch.status == "applying":
        _fail("Набор уже принят. Результат доступен в состоянии партии.", "import_already_accepted")


def update_draft_item(*, club_id: int, actor_user_id: int, batch_id: int, item_id: int, values: dict):
    get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        batch = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
        _draft_available(batch)
        item = batch.items.for_club(club_id).filter(id=item_id).first()
        if item is None:
            _fail("Запись недоступна.", "target_not_available")
        if item.result_receipt_id or item.status in {"applied", "replayed"}:
            _fail("Принятую запись нельзя редактировать. Нужна отдельная коррекция.", "opening_source_conflict")
        # This is private draft content, never model creation kwargs or live data.
        item.source_data = json.loads(json.dumps(values, default=str))
        item.normalized_data, item.errors, item.status = {}, [], "draft"
        item.save(update_fields=["source_data", "normalized_data", "errors", "status", "updated_at"])
        batch.revision += 1
        batch.status = "draft"
        batch.accepted_preview = None
        batch.expected_effects = {}
        batch.save(update_fields=["revision", "status", "accepted_preview", "expected_effects", "updated_at"])


def validate_batch(*, club_id: int, actor_user_id: int, batch_id: int, selected_item_ids=None):
    get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        batch = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
        _draft_available(batch)
        if batch.accepted_preview_id:
            _fail("Сначала исправьте неприменённую запись, затем выполните новую проверку.", "import_already_accepted")
        items = list(batch.items.for_club(club_id).order_by("id"))
        selected = set(selected_item_ids) if selected_item_ids is not None else {item.id for item in items}
        if not selected <= {item.id for item in items}:
            _fail("Выбранная запись недоступна.", "target_not_available")
        selection, person_payloads, source_keys, payment_keys = [], {}, set(), set()
        conflicting_people, conflicting_sources, conflicting_payments = set(), set(), set()
        zone = club_zoneinfo(Club.objects.get(id=club_id))
        for item in items:
            if item.id not in selected or item.status in {"applied", "replayed"}:
                continue
            if item.kind != OpeningImportItem.Kind.ENTITLEMENT:
                continue
            try:
                mapped = _resolve_names(club_id=club_id, values=item.source_data)
                terms = terms_from_row(values=mapped, namespace=batch.source_namespace, zone=zone)
                expected_kind = "group" if mapped["Тип пакета"] == "Групповой" else "personal"
                if not Tariff.objects.for_club(club_id).filter(
                    id=terms.tariff_id, training_type__kind=expected_kind,
                ).exists():
                    _fail("Вид пакета не совпадает с выбранным тарифом.", "import_mapping_needs_review")
                identity = tuple(terms.as_payload()[key] for key in (
                    "first_name", "last_name", "is_child", "phone", "guardian_phone", "date_of_birth", "student_id",
                ))
                prior = person_payloads.setdefault(terms.student_source_key, identity)
                if prior != identity:
                    conflicting_people.add(terms.student_source_key)
                    _fail("Один ключ ученика содержит разные данные. Сверьте строки.", "identity_needs_review")
                source = (item.kind, terms.entitlement_source_key)
                if source in source_keys:
                    conflicting_sources.add(terms.entitlement_source_key)
                    _fail("Ключ абонемента повторяется внутри партии.", "opening_source_conflict")
                source_keys.add(source)
                if terms.payment_source_key in payment_keys:
                    conflicting_payments.add(terms.payment_source_key)
                    _fail("Ключ оплаты повторяется внутри партии.", "opening_source_conflict")
                payment_keys.add(terms.payment_source_key)
                existing = OpeningEntitlementSnapshot.objects.for_club(club_id).filter(
                    source_namespace=batch.source_namespace, entitlement_source_key=terms.entitlement_source_key,
                ).first()
                scope = {}
                summary = {"replay": True, "student_id": existing.subscription.student_id} if existing else {}
                if existing:
                    if existing.payload_fingerprint != terms.fingerprint():
                        _fail("Ключ уже принят с другими условиями.", "opening_source_conflict")
                else:
                    preview = preview_opening_entitlement(
                        club_id=club_id, actor_user_id=actor_user_id, terms=terms,
                    )
                    scope = preview.scope
                    summary = {"replay": False, "student_id": preview.student_id,
                               "history_only": preview.history_only, "old_status": preview.old_status,
                               "new_status": preview.new_status}
                item.normalized_data, item.errors, item.status = terms.as_payload(), [], "ready"
                item.source_key = terms.entitlement_source_key
                selection.append({"item_id": item.id, "kind": item.kind, "terms": terms.as_payload(),
                                  "scope": scope, "summary": summary})
            except BusinessLogicError as exc:
                item.status = "needs_review"
                item.errors = [_error(exc.code, str(exc))]
                item.normalized_data = {}
            item.save(update_fields=["source_key", "normalized_data", "errors", "status", "updated_at"])
        conflicted_ids = {
            entry["item_id"] for entry in selection
            if entry["terms"]["student_source_key"] in conflicting_people
            or entry["terms"]["entitlement_source_key"] in conflicting_sources
            or entry["terms"]["payment_source_key"] in conflicting_payments
        }
        for item in items:
            if item.id in conflicted_ids:
                item.status, item.normalized_data = "needs_review", {}
                item.errors = [_error(
                    "opening_source_conflict", "Связанная строка содержит конфликт ключей или личности.",
                )]
                item.save(update_fields=["status", "normalized_data", "errors", "updated_at"])
        selection = [entry for entry in selection if entry["item_id"] not in conflicted_ids]
        # Earlier cutovers create group membership before later rows target it.
        selection.sort(key=lambda entry: (
            entry["terms"]["student_source_key"], entry["terms"]["operational_cutover"], entry["item_id"],
        ))
        from apps.students.imports.settlements import add_settlement_selection

        selection = add_settlement_selection(
            club_id=club_id, actor_user_id=actor_user_id, batch=batch, items=items,
            selected=selected, selection=selection,
        )
        selection = json.loads(json.dumps(selection, default=str))
        preview = OpeningImportPreview.objects.create(
            club_id=club_id, batch=batch, revision=batch.revision, selection=selection,
            actor_id=actor_user_id, expires_at=min(batch.expires_at, timezone.now() + PREVIEW_TTL),
        )
        batch.status = "validated"
        batch.save(update_fields=["status", "updated_at"])
        return preview


def export_result(*, club_id: int, actor_user_id: int, batch_id: int):
    batch = get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    items = list(batch.items.for_club(club_id).order_by("id"))
    rows = [WorkbookRow(item.source_sheet, item.source_row, item.source_data) for item in items]
    results = [{"id": item.id, "status": item.status, "message": "; ".join(
        error["message"] for error in item.errors
    ), "student_id": item.result_receipt.domain_result.get("student_id") if item.result_receipt_id else None,
               "trainer_id": item.result_receipt.domain_result.get("trainer_id") if item.result_receipt_id else None,
               "settlement_entry_id": item.result_receipt.domain_result.get("settlement_entry_id")
               if item.result_receipt_id else None}
        for item in items]
    return save_private(content=workbook_bytes(
        club_id=club_id, actor_user_id=actor_user_id, namespace=batch.source_namespace, rows=rows, results=results,
    ), suffix="xlsx")
