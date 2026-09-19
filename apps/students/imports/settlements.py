"""Typed settlement rows and dependency ordering for the existing private batch."""

import hashlib
import json
from datetime import date
from decimal import Decimal

from django.conf import settings

from apps.clubs.models import Club
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.students.imports.parsing import parse_date, parse_money
from apps.students.imports.schemas import boolean, integer
from apps.students.models import OpeningImportItem, OpeningImportItemReceipt
from apps.trainers.models import Trainer, TrainerSettlementEntry
from apps.trainers.settlement_selectors import get_settlement_financial_scope
from apps.trainers.settlement_services import preview_trainer_settlement, record_trainer_settlement


def _fail(message, code="settlement_import_needs_review"):
    raise BusinessLogicError(message, code=code)


def settlement_terms(*, values, namespace):
    kind = {"Начальный расчёт": "opening", "Выплата": "payout"}.get(values.get("Вид записи"))
    if kind is None or not boolean(values.get("Подтверждено")):
        _fail("Укажите вид записи и подтвердите исходные расчёты.")
    amount = parse_money(values.get("Сумма"))
    if kind == "payout" and amount <= 0:
        _fail("Сумма выплаты должна быть положительной.")
    method = ""
    if kind == "payout":
        method = {"Наличные": "cash", "Перевод": "transfer", "cash": "cash", "transfer": "transfer"}.get(
            values.get("Способ выплаты")
        )
        if method is None:
            _fail("Укажите способ фактической выплаты: наличные или перевод.")
    return {
        "trainer_id": integer(values.get("ID тренера")),
        "kind": kind,
        "effective_on": str(parse_date(values.get("Дата"))),
        "reason": str(values.get("Причина") or "").strip(),
        "source_namespace": namespace,
        "source_key": str(values.get("source_key") or "").strip(),
        "balance_delta": str(amount) if kind == "opening" else None,
        "amount": str(amount) if kind == "payout" else None,
        "payment_method": method,
        "confirm_advance": boolean(values.get("Аванс подтверждён") or False),
        "channel": "import",
    }


def _kwargs(terms):
    return {**terms, "effective_on": date.fromisoformat(terms["effective_on"])}


def terms_fingerprint(terms):
    return hashlib.sha256(json.dumps(terms, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def add_settlement_selection(*, club_id, actor_user_id, batch, items, selected, selection):
    """Opening depends on reviewed entitlements; payouts depend on its baseline.

    Optional source keys narrow dependencies. Without them a new opening waits
    for all selected entitlement rows, including unresolved ones.
    """
    entitlement_items = [i for i in items if i.id in selected and i.kind == "entitlement"]
    by_key = {i.source_key: i for i in entitlement_items}
    settlements = [
        i for i in items if i.id in selected and i.kind != "entitlement" and i.status not in {"applied", "replayed"}
    ]
    parsed, errors = {}, {}
    for item in settlements:
        try:
            terms = settlement_terms(values=item.source_data, namespace=batch.source_namespace)
            item.kind = "settlement_opening" if terms["kind"] == "opening" else "settlement_payout"
            if not Trainer.objects.for_club(club_id).filter(id=terms["trainer_id"]).exists():
                _fail("Тренер недоступен.", "target_not_available")
            if date.fromisoformat(terms["effective_on"]) > club_localdate(Club.objects.get(id=club_id)):
                _fail("Будущая расчётная запись не поддержана.", "settlement_future_date")
            parsed[item.id] = terms
        except BusinessLogicError as exc:
            errors[item.id] = exc
    sources, openings = {}, {}
    for item_id, terms in parsed.items():
        sources.setdefault((terms["kind"], terms["source_key"]), []).append(item_id)
        if terms["kind"] == "opening":
            openings.setdefault(terms["trainer_id"], []).append(item_id)
    for ids in [*sources.values(), *openings.values()]:
        if len(ids) > 1:
            for item_id in ids:
                errors[item_id] = BusinessLogicError(
                    "Повтор ключа или начальной сверки внутри партии.", code="opening_source_conflict"
                )
    new_entries = {}
    for item in sorted(settlements, key=lambda i: (i.kind != "settlement_opening", i.id)):
        try:
            if item.id in errors:
                raise errors[item.id]
            terms = parsed[item.id]
            dependencies = []
            requested = str(item.source_data.get("Ключи абонементов") or "").strip()
            if terms["kind"] == "opening":
                keys = [key.strip() for key in requested.split(",") if key.strip()]
                if keys and any(key not in by_key for key in keys):
                    _fail("Зависимый абонемент не выбран в этой проверке.", "import_dependency_unresolved")
                deps = [by_key[key] for key in keys] if keys else entitlement_items
                if any(dep.status not in {"ready", "applied", "replayed"} for dep in deps):
                    _fail("Сначала разрешите выбранные исторические абонементы.", "import_dependency_unresolved")
                dependencies = [dep.id for dep in deps]
            try:
                preview = preview_trainer_settlement(club_id=club_id, actor_user_id=actor_user_id, **_kwargs(terms))
            except BusinessLogicError as exc:
                opening_ids = openings.get(terms["trainer_id"], [])
                opening_entry = new_entries.get(opening_ids[0]) if len(opening_ids) == 1 else None
                if terms["kind"] != "payout" or exc.code != "settlement_opening_required" or opening_entry is None:
                    raise
                if terms["effective_on"] < opening_entry["terms"]["effective_on"]:
                    _fail("Выплата партии должна быть не раньше её начальной сверки.", "import_dependency_unresolved")
                if (
                    Decimal(terms["amount"]) > Decimal(opening_entry["terms"]["balance_delta"])
                    and not terms["confirm_advance"]
                ):
                    _fail(
                        "Подтвердите возможный аванс или сверьте начисления после начального остатка.",
                        "settlement_advance_confirmation",
                    )
                dependencies = opening_ids
                preview = {
                    "replay": False,
                    "after_dependency": True,
                    "scope": get_settlement_financial_scope(club=club_id, trainer_id=terms["trainer_id"]),
                }
            item.source_key, item.normalized_data, item.errors, item.status = terms["source_key"], terms, [], "ready"
            entry = {
                "item_id": item.id,
                "kind": item.kind,
                "terms": terms,
                "scope": preview.get("scope", {}),
                "summary": preview,
                "dependencies": dependencies,
            }
            new_entries[item.id] = entry
            selection.append(entry)
        except BusinessLogicError as exc:
            item.status, item.normalized_data = "needs_review", {}
            item.errors = [{"code": exc.code, "message": str(exc)}]
        item.save(update_fields=["kind", "source_key", "normalized_data", "errors", "status", "updated_at"])
    return selection


def apply_settlement_item(*, club_id, batch, item, entry):
    terms = entry["terms"]
    if (
        OpeningImportItem.objects.for_club(club_id)
        .filter(
            batch=batch,
            id__in=entry.get("dependencies", []),
        )
        .exclude(status__in=["applied", "replayed"])
        .exists()
    ):
        _fail("Связанная запись не применена. Сначала разрешите её.", "import_dependency_unresolved")
    existing = (
        TrainerSettlementEntry.objects.for_club(club_id)
        .filter(
            source_namespace=terms["source_namespace"],
            source_key=terms["source_key"],
            kind=terms["kind"],
        )
        .first()
    )
    if not existing and not settings.STUDENT_OPENING_IMPORT_ENABLED:
        _fail("Применение нового переноса отключено.", "opening_import_disabled")
    if not existing:
        expected = _without_batch_effects(scope=entry["scope"], club_id=club_id, batch=batch)
        current = _without_batch_effects(
            scope=get_settlement_financial_scope(club=club_id, trainer_id=terms["trainer_id"]),
            club_id=club_id,
            batch=batch,
        )
        if expected != current:
            _fail("Расчётные данные изменились после проверки. Сверьте запись заново.", "import_preview_stale")
    result = record_trainer_settlement(club_id=club_id, actor_user_id=batch.apply_actor_id, **_kwargs(terms))
    fingerprint = terms_fingerprint(terms)
    receipt = (
        OpeningImportItemReceipt.objects.for_club(club_id)
        .filter(
            source_namespace=terms["source_namespace"],
            kind=item.kind,
            source_key=terms["source_key"],
        )
        .first()
    )
    if receipt is None:
        receipt = OpeningImportItemReceipt.objects.create(
            club_id=club_id,
            item=item,
            source_namespace=terms["source_namespace"],
            kind=item.kind,
            source_key=terms["source_key"],
            payload_fingerprint=fingerprint,
            domain_result={"settlement_entry_id": result.id, "trainer_id": result.trainer_id},
            actor_id=batch.apply_actor_id,
            channel=batch.apply_channel,
        )
    elif receipt.payload_fingerprint != fingerprint:
        _fail("Ключ принят с другими условиями.", "opening_source_conflict")
    item.result_receipt, item.status, item.errors = receipt, ("replayed" if existing else "applied"), []
    item.save(update_fields=["result_receipt", "status", "errors", "updated_at"])


def _without_batch_effects(*, scope, club_id, batch):
    scope = json.loads(json.dumps(scope))
    results = (
        OpeningImportItemReceipt.objects.for_club(club_id)
        .filter(
        result_items__batch=batch,
        result_items__status__in=["applied", "replayed"],
        )
        .values_list("domain_result", flat=True)
    )
    payment_ids, entry_ids = set(), set()
    for result in results:
        if result.get("payment_id"):
            payment_ids.add(result["payment_id"])
        if result.get("settlement_entry_id"):
            entry_ids.add(result["settlement_entry_id"])
    scope["entries"] = [identity for identity in scope["entries"] if identity not in entry_ids]
    if scope["opening_id"] in entry_ids:
        scope["opening_id"] = None
    scope["earnings"] = [row for row in scope["earnings"] if row["payment_id"] not in payment_ids]
    return scope
