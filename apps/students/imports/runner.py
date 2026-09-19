"""Bounded resumable item runner, shared by CLI and django-q workers."""

import copy
import json

from django.db import transaction
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment, TrainingGroupMembership
from apps.billing.models import OpeningEntitlementSnapshot, Payment
from apps.billing.service_modules.opening_subscriptions import (
    _authorize,
    _fingerprint,
    issue_opening_entitlement,
    preview_opening_entitlement,
)
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError
from apps.students.imports.schemas import terms_from_payload
from apps.students.imports.selectors import batch_status, get_batch
from apps.students.models import (
    OpeningImportBatch,
    OpeningImportItem,
    OpeningImportItemReceipt,
    OpeningImportPreview,
    Student,
)

CHUNK_SIZE = 25


def accept_batch(*, club_id: int, actor_user_id: int, batch_id: int, revision: int,
                 selection_token: str, channel: str):
    get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    if channel not in {"assistant_cli", "htmx"}:
        raise BusinessLogicError("Канал применения недоступен.", code="actor_not_authorized")
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        batch = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
        preview = OpeningImportPreview.objects.for_club(club_id).filter(
            batch=batch, revision=revision, selection_token=selection_token,
        ).first()
        if preview is None or batch.revision != revision:
            raise BusinessLogicError("Проверка устарела. Выполните её заново.", code="import_preview_stale")
        if batch.accepted_preview_id:
            if batch.accepted_preview_id != preview.id:
                raise BusinessLogicError("Уже принят другой набор записей.", code="import_preview_stale")
            return batch
        if preview.expires_at <= timezone.now() or not preview.selection:
            raise BusinessLogicError("Нужна новая проверка готовых записей.", code="import_preview_stale")
        batch.accepted_preview, batch.apply_actor_id, batch.apply_channel = preview, actor_user_id, channel
        batch.status = "applying"
        batch.save(update_fields=["accepted_preview", "apply_actor", "apply_channel", "status", "updated_at"])
    return batch


def _expected_scope(*, entry, effects):
    scope = copy.deepcopy(entry["scope"])
    if not scope:
        return scope
    terms = entry["terms"]
    student_key = str(scope["student"]["id"]) if scope["student"] else f"key:{terms['student_source_key']}"
    own = effects.get(student_key)
    if not own:
        return scope
    scope["student"] = own["student"]
    if scope["group"]:
        group_effect = own["groups"].get(str(terms["training_group_id"]))
        if group_effect:
            scope["group"].update(group_effect)
    own_payments = [payment["state"] for payment in own["payments"] if payment["amount"] == terms["paid_amount"]]
    original_ids = {payment["id"] for payment in scope["payments"]}
    scope["payments"].extend(payment for payment in own_payments if payment["id"] not in original_ids)
    scope["payments"].sort(key=lambda payment: payment["id"])
    return scope


def _capture_effects(*, club_id, receipt, terms, effects):
    student_id = receipt.subscription.student_id
    fields = (
        "id", "updated_at", "status", "assigned_trainer_id", "lead_status", "became_student_at",
        "first_name", "last_name", "phone", "guardian_phone", "date_of_birth", "is_child",
    )
    student = Student.objects.for_club(club_id).values(*fields).get(id=student_id)
    prior = effects.get(str(student_id), {"groups": {}, "payments": []})
    own = {"student": student, "groups": prior["groups"], "payments": prior["payments"]}
    payment = Payment.objects.for_club(club_id).get(id=receipt.payment_id)
    if payment.target_group_membership_id:
        membership = TrainingGroupMembership.objects.for_club(club_id).get(id=payment.target_group_membership_id)
        own["groups"][str(payment.target_training_group_id)] = {
            "membership": [membership.id, membership.updated_at, membership.authority, membership.status,
                           membership.starts_on, membership.ends_on],
            "enrollments": list(ScheduleEnrollment.objects.for_club(club_id).filter(
                student_id=student_id, schedule__training_group_id=payment.target_training_group_id,
            ).order_by("id").values(
                "id", "updated_at", "status", "starts_on", "ends_on", "training_group_membership_id",
            )),
        }
    if payment.id not in {row["state"]["id"] for row in own["payments"]}:
        own["payments"].append({"amount": str(payment.amount), "state": {
            "id": payment.id, "status": payment.status, "subscription_id": payment.subscription_id,
            "updated_at": payment.updated_at,
        }})
    own = json.loads(json.dumps(own, default=str))
    effects[str(student_id)] = own
    effects[f"key:{terms.student_source_key}"] = own


def _apply_item(*, club_id, batch_id, preview_id, entry):
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        batch = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
        if batch.accepted_preview_id != preview_id:
            raise BusinessLogicError("Принятая ревизия партии изменилась.", code="import_runner_stale")
        _authorize(club_id=club_id, actor_user_id=batch.apply_actor_id)
        item = OpeningImportItem.objects.for_club(club_id).select_for_update().get(id=entry["item_id"], batch=batch)
        if item.status != "ready":
            return
        if batch.status != "applying":
            raise BusinessLogicError("Партия больше не применяется.", code="import_runner_stale")
        if item.kind != OpeningImportItem.Kind.ENTITLEMENT:
            from apps.students.imports.settlements import apply_settlement_item

            apply_settlement_item(club_id=club_id, batch=batch, item=item, entry=entry)
            batch.heartbeat_at = timezone.now()
            batch.save(update_fields=["heartbeat_at", "updated_at"])
            return
        terms = terms_from_payload(entry["terms"])
        existing = OpeningEntitlementSnapshot.objects.for_club(club_id).filter(
            source_namespace=terms.source_namespace, entitlement_source_key=terms.entitlement_source_key,
        ).first()
        fingerprint = ""
        if not existing:
            preview = preview_opening_entitlement(club_id=club_id, actor_user_id=batch.apply_actor_id, terms=terms)
            expected = _expected_scope(entry=entry, effects=batch.expected_effects)
            if expected != preview.scope:
                raise BusinessLogicError("Данные изменились после проверки. Сверьте запись заново.",
                                         code="import_preview_stale")
            fingerprint = _fingerprint(preview.scope)
        receipt = issue_opening_entitlement(
            club_id=club_id, actor_user_id=batch.apply_actor_id, terms=terms,
            preview_fingerprint=fingerprint, channel=batch.apply_channel,
        )
        # A cross-batch replay references the already durable source receipt;
        # only the first accepted item creates its canonical financial receipt.
        canonical = OpeningImportItemReceipt.objects.for_club(club_id).filter(
            source_namespace=terms.source_namespace, kind=item.kind, source_key=terms.entitlement_source_key,
        ).first()
        if canonical is None:
            canonical = OpeningImportItemReceipt.objects.create(
                club_id=club_id, item=item, source_namespace=terms.source_namespace,
                kind=item.kind, source_key=terms.entitlement_source_key, payload_fingerprint=terms.fingerprint(),
                domain_result={"opening_snapshot_id": receipt.id, "student_id": receipt.subscription.student_id,
                               "subscription_id": receipt.subscription_id, "payment_id": receipt.payment_id},
                actor_id=batch.apply_actor_id, channel=batch.apply_channel,
            )
        elif canonical.payload_fingerprint != terms.fingerprint():
            raise BusinessLogicError("Ключ принят с другими условиями.", code="opening_source_conflict")
        item.status, item.errors = ("replayed" if existing else "applied"), []
        item.result_receipt = canonical
        item.save(update_fields=["status", "errors", "result_receipt", "updated_at"])
        if not existing:
            _capture_effects(club_id=club_id, receipt=receipt, terms=terms, effects=batch.expected_effects)
        batch.heartbeat_at = timezone.now()
        batch.save(update_fields=["expected_effects", "heartbeat_at", "updated_at"])


def run_batch_chunk(*, club_id: int, batch_id: int):
    batch = OpeningImportBatch.objects.for_club(club_id).select_related("accepted_preview").get(id=batch_id)
    if batch.accepted_preview_id is None:
        raise BusinessLogicError("Набор не принят.", code="import_preview_required")
    _authorize(club_id=club_id, actor_user_id=batch.apply_actor_id)
    preview_id = batch.accepted_preview_id
    pending = set(batch.items.for_club(club_id).filter(status="ready").values_list("id", flat=True))
    entries = [entry for entry in batch.accepted_preview.selection if entry["item_id"] in pending][:CHUNK_SIZE]
    for entry in entries:
        try:
            _apply_item(club_id=club_id, batch_id=batch_id, preview_id=preview_id, entry=entry)
        except BusinessLogicError as exc:
            if exc.code in {"actor_not_authorized", "import_runner_stale"}:
                raise
            with transaction.atomic():
                Club.objects.select_for_update().get(id=club_id)
                current = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
                if current.accepted_preview_id != preview_id:
                    raise BusinessLogicError("Принятая ревизия партии изменилась.", code="import_runner_stale")
                item = OpeningImportItem.objects.for_club(club_id).select_for_update().get(id=entry["item_id"])
                if item.status == "ready":
                    item.status = "needs_review"
                    item.errors = [{"code": exc.code, "message": str(exc)}]
                    item.save(update_fields=["status", "errors", "updated_at"])
    with transaction.atomic():
        Club.objects.select_for_update().get(id=club_id)
        batch = OpeningImportBatch.objects.for_club(club_id).select_for_update().get(id=batch_id)
        if batch.accepted_preview_id != preview_id:
            raise BusinessLogicError("Принятая ревизия партии изменилась.", code="import_runner_stale")
        statuses = set(batch.items.for_club(club_id).values_list("status", flat=True))
        selected_ids = [entry["item_id"] for entry in batch.accepted_preview.selection]
        remaining = batch.items.for_club(club_id).filter(id__in=selected_ids, status="ready").exists()
        batch.status = "applying" if remaining else (
            "completed" if statuses <= {"applied", "replayed"} else "partial"
        )
        batch.heartbeat_at = timezone.now()
        batch.save(update_fields=["status", "heartbeat_at", "updated_at"])
    return batch_status(club_id=club_id, actor_user_id=batch.apply_actor_id, batch_id=batch_id)


def run_batch_worker(*, club_id: int, batch_id: int):
    from django_q.tasks import async_task

    result = run_batch_chunk(club_id=club_id, batch_id=batch_id)
    if result["status"] == "applying":
        async_task("apps.students.imports.runner.run_batch_worker", club_id=club_id, batch_id=batch_id)
    return {"batch_id": result["batch_id"], "status": result["status"], "counts": result["counts"]}
