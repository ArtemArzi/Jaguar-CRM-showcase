"""Tenant/role guarded private draft reads."""

from collections import Counter

from apps.billing.service_modules.opening_subscriptions import _authorize
from apps.common.exceptions import BusinessLogicError
from apps.students.models import OpeningImportBatch


def get_batch(*, club_id: int, actor_user_id: int, batch_id: int):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    batch = OpeningImportBatch.objects.for_club(club_id).filter(id=batch_id).first()
    if batch is None:
        raise BusinessLogicError("Партия недоступна.", code="target_not_available")
    return batch


def batch_status(*, club_id: int, actor_user_id: int, batch_id: int):
    batch = get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    return {
        "batch_id": batch.id,
        "revision": batch.revision,
        "status": batch.status,
        "counts": dict(Counter(batch.items.for_club(club_id).values_list("status", flat=True))),
    }


STATUS_LABELS = {
    "draft": "Черновик",
    "validated": "Проверено",
    "applying": "Применяется",
    "completed": "Применено полностью",
    "partial": "Применено частично",
    "ready": "Готово",
    "needs_review": "Нужно уточнить",
    "rejected": "Отклонено",
    "applied": "Применено",
    "replayed": "Уже было применено",
}


def list_import_batches(*, club_id, actor_user_id, page=1):
    from django.core.paginator import Paginator
    from django.db.models import Count

    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    rows = OpeningImportBatch.objects.for_club(club_id).annotate(item_count=Count("items")).order_by("-id")
    result = Paginator(rows, 20).get_page(page)
    for batch in result:
        batch.status_label = STATUS_LABELS.get(batch.status, batch.status)
    return result


def import_workspace(*, club_id, actor_user_id, batch_id, page=1):
    from decimal import Decimal

    from django.conf import settings
    from django.core.paginator import Paginator
    from django.utils import timezone

    from apps.attendance.models import TrainingGroup
    from apps.billing.models import Tariff
    from apps.clubs.models import ClubMembership
    from apps.students.models import OpeningImportPreview, Student
    from apps.trainers.models import Trainer

    batch = get_batch(club_id=club_id, actor_user_id=actor_user_id, batch_id=batch_id)
    preview = (
        OpeningImportPreview.objects.for_club(club_id)
        .filter(batch=batch, revision=batch.revision)
        .order_by("-id")
        .first()
    )
    if batch.accepted_preview_id:
        preview = batch.accepted_preview
    entries = {row["item_id"]: row for row in preview.selection} if preview else {}
    rows = batch.items.for_club(club_id).select_related("result_receipt").order_by("id")
    counts = Counter(rows.values_list("status", flat=True))
    items = Paginator(rows, 40).get_page(page)
    trainer_ids, tariff_ids, group_ids = set(), set(), set()
    for item in items:
        terms = entries.get(item.id, {}).get("terms", {})
        trainer_ids.update(
            terms.get(field)
            for field in ("trainer_id", "assigned_trainer_id", "package_owner_trainer_id", "sale_trainer_id")
        )
        student_scope = entries.get(item.id, {}).get("scope", {}).get("student") or {}
        trainer_ids.add(student_scope.get("assigned_trainer_id"))
        tariff_ids.add(terms.get("tariff_id"))
        group_ids.add(terms.get("training_group_id"))
    trainers = {row.id: str(row) for row in Trainer.objects.for_club(club_id).filter(id__in=trainer_ids - {None})}
    tariffs = dict(Tariff.objects.for_club(club_id).filter(id__in=tariff_ids - {None}).values_list("id", "name"))
    groups = dict(TrainingGroup.objects.for_club(club_id).filter(id__in=group_ids - {None}).values_list("id", "name"))
    for item in items:
        entry = entries.get(item.id, {})
        terms = entry.get("terms", {})
        item.status_label = STATUS_LABELS.get(item.status, item.status)
        item.person_name = " ".join(str(item.source_data.get(key) or "") for key in ("Имя", "Фамилия")).strip()
        item.preview_terms = terms
        item.preview_summary = entry.get("summary", {})
        item.tariff_name = tariffs.get(terms.get("tariff_id"), "")
        item.group_name = groups.get(terms.get("training_group_id"), "")
        item.trainer_name = trainers.get(terms.get("trainer_id"), "")
        scope = entry.get("scope", {})
        existing_student = scope.get("student")
        preserve_assignment = bool(existing_student and not terms.get("change_assigned_trainer"))
        assigned_id = (
            existing_student.get("assigned_trainer_id") if preserve_assignment else terms.get("assigned_trainer_id")
        )
        item.assigned_name = trainers.get(assigned_id, "")
        item.assignment_action = (
            "preserve" if preserve_assignment
            else "create_or_preserve" if not existing_student and not terms.get("change_assigned_trainer")
            else "set" if assigned_id else "clear"
        )
        item.old_status_label = dict(Student.Status.choices).get(item.preview_summary.get("old_status"), "")
        item.new_status_label = dict(Student.Status.choices).get(item.preview_summary.get("new_status"), "")
        item.payout_policy_label = {
            Tariff.PayoutPolicy.NONE: "Без начисления",
            Tariff.PayoutPolicy.ON_CHECKIN: "За занятие",
            Tariff.PayoutPolicy.ON_PAYMENT: "С оплаты",
        }.get(terms.get("payout_policy"), "")
        group_scope = scope.get("group") or {}
        membership = group_scope.get("membership")
        item.membership_effect = (
            "independent" if membership and membership[2] == "independent"
            else "existing_payment_owned" if membership
            else "new_payment_owned" if group_scope else "none"
        )
        item.package_owner_name = trainers.get(terms.get("package_owner_trainer_id"), "")
        item.sale_trainer_name = trainers.get(terms.get("sale_trainer_id"), "")
        item.result = item.result_receipt.domain_result if item.result_receipt_id else {}
    current_actor_allowed = (
        not batch.apply_actor_id
        or ClubMembership.objects.filter(
            club_id=club_id,
            user_id=batch.apply_actor_id,
            is_active=True,
            user__is_active=True,
            role__in=["owner", "admin"],
        ).exists()
    )
    ready_ids = set(rows.filter(status="ready").values_list("id", flat=True))
    ready_entries = [row for row in entries.values() if row["item_id"] in ready_ids]
    return {
        "batch": batch,
        "items_page": items,
        "preview": preview,
        "batch_status_label": STATUS_LABELS.get(batch.status, batch.status),
        "ready_count": counts["ready"],
        "review_count": counts["needs_review"] + counts["rejected"] + counts["draft"],
        "applied_count": counts["applied"],
        "replayed_count": counts["replayed"],
        "total_count": sum(counts.values()),
        "selected_ready_count": len(ready_entries),
        "source_paid_total": sum(
            (Decimal(row["terms"]["paid_amount"]) for row in ready_entries if row["kind"] == "entitlement"),
            Decimal("0"),
        ),
        "current_actor_allowed": current_actor_allowed,
        "can_poll": batch.status == "applying" and current_actor_allowed,
        "can_edit": batch.status != "applying" and batch.expires_at > timezone.now(),
        "preview_expired": bool(preview and not batch.accepted_preview_id and preview.expires_at <= timezone.now()),
        "import_enabled": settings.STUDENT_OPENING_IMPORT_ENABLED,
        "new_count": sum(not row.get("summary", {}).get("replay", False) for row in ready_entries),
    }
