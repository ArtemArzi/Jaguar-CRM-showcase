"""Atomic reviewed opening issuance. No ordinary sale or access side effects."""

import json
from dataclasses import dataclass
from hashlib import sha256

from django.utils import timezone

from apps.billing.models import OpeningEntitlementSnapshot, Payment, Tariff, TariffComponent
from apps.billing.service_modules.opening_terms import OpeningEntitlementTerms
from apps.clubs.models import Club, ClubMembership
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.students.identity_services import normalize_person_identity, resolve_staff_intake_person_identity
from apps.students.models import Student
from apps.trainers.models import Trainer
from apps.trainers.services import assert_trainer_payroll_date_open


@dataclass(frozen=True)
class OpeningPreview:
    fingerprint: str
    student_id: int | None
    history_only: bool
    old_status: str | None
    new_status: str
    scope: dict


def _authorize(*, club_id, actor_user_id):
    if not ClubMembership.objects.filter(
        club_id=club_id, user_id=actor_user_id, user__is_active=True, is_active=True,
        role__in=[ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN],
    ).exists():
        raise BusinessLogicError("Действие доступно владельцу или администратору клуба.", code="actor_not_authorized")


def _fail(message, code="opening_needs_review"):
    raise BusinessLogicError(message, code=code)


def _identity(*, club_id, terms):
    identity = normalize_person_identity(
        first_name=terms.first_name, last_name=terms.last_name, is_child=terms.is_child,
        phone=terms.phone, guardian_phone=terms.guardian_phone, date_of_birth=terms.date_of_birth,
    )
    resolution = resolve_staff_intake_person_identity(
        club_id=club_id, identity=identity, confirm_distinct_child=terms.confirm_distinct_child,
    )
    mapped_ids = set(OpeningEntitlementSnapshot.objects.for_club(club_id).filter(
        source_namespace=terms.source_namespace, student_source_key=terms.student_source_key,
    ).values_list("subscription__student_id", flat=True))
    if len(mapped_ids) > 1:
        _fail("Ключ ученика связан с несколькими карточками.", "identity_needs_review")
    selected_id = terms.student_id or next(iter(mapped_ids), None)
    if selected_id:
        student = Student.objects.for_club(club_id).filter(id=selected_id, deleted_at__isnull=True).first()
        if student is None:
            _fail("Карточка недоступна.", "target_not_available")
        if mapped_ids and mapped_ids != {selected_id}:
            _fail("Изменилось сопоставление ключа ученика.", "identity_needs_review")
        if resolution.duplicate and resolution.duplicate.id != student.id:
            _fail("Контакт связан с другой карточкой.", "identity_needs_review")
        stored_identity = normalize_person_identity(
            first_name=student.first_name, last_name=student.last_name, is_child=student.is_child,
            phone=student.phone, guardian_phone=student.guardian_phone, date_of_birth=student.date_of_birth,
        )
        if (
            stored_identity.first_name.strip().casefold() != identity.first_name.strip().casefold()
            or stored_identity.last_name.strip().casefold() != identity.last_name.strip().casefold()
            or stored_identity.is_child != identity.is_child
            or stored_identity.phone != identity.phone
            or stored_identity.guardian_phone != identity.guardian_phone
            or stored_identity.date_of_birth != identity.date_of_birth
        ):
            _fail("Исходные данные не совпадают с выбранной карточкой ученика.", "identity_needs_review")
        # Explicit reviewed selection resolves ambiguity, but never soft-delete.
        return student
    if resolution.soft_deleted or resolution.ambiguous or resolution.confirmation_required or resolution.duplicate:
        _fail("Подтвердите сопоставление ученика в предварительном просмотре.", "identity_needs_review")
    return None


def _resolve(*, club_id, terms):
    club = Club.objects.get(id=club_id)
    tariff = Tariff.objects.for_club(club_id).select_related("training_type", "location").filter(
        id=terms.tariff_id,
    ).first()
    if tariff is None:
        _fail("Тариф недоступен.", "target_not_available")
    kind = tariff.training_type.kind
    if kind not in {"group", "personal"}:
        _fail("Этот вид пакета требует отдельной сверки.")
    catalog_components = list(TariffComponent.objects.for_club(club_id).filter(
        tariff=tariff, is_active=True,
    ).order_by("id"))
    if len(catalog_components) > 1 or any(
        component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS
        or component.training_type_id != tariff.training_type_id
        or component.scope != tariff.scope or component.location_id != tariff.location_id
        for component in catalog_components
    ):
        _fail("Состав тарифа требует явной сверки с исходным конечным пакетом.")
    if kind == "group" and terms.payout_policy == Tariff.PayoutPolicy.ON_CHECKIN:
        _fail("Групповое начисление не может зависеть от нового посещения.")
    if kind == "personal" and terms.package_owner_trainer_id is None:
        _fail("Укажите владельца персонального пакета.")
    if kind == "group" and terms.package_owner_trainer_id is not None:
        _fail("Групповой пакет не имеет владельца персонального пакета.")
    history_only = terms.original_left == 0 or terms.expires_on < club_localdate(club)
    if terms.covered_through > timezone.now() or terms.effective_on > club_localdate(club):
        _fail("Сверка и подтверждённая историческая оплата не могут быть в будущем.")
    if terms.started_on > timezone.localtime(terms.covered_through, club_zoneinfo(club)).date():
        _fail("Исходный пакет должен начаться не позже границы сверки.")
    if not history_only and terms.operational_cutover <= timezone.now():
        _fail("Согласованное начало работы уже прошло. Обновите сверку.", "opening_cutover_needs_review")
    student = _identity(club_id=club_id, terms=terms)
    old_status = student.status if student else None
    new_status = old_status or Student.Status.CHURNED
    if not history_only:
        if old_status in {Student.Status.LEAD, Student.Status.TRIAL, Student.Status.LOST, Student.Status.CHURNED}:
            if not terms.confirm_student_transition:
                _fail("Подтвердите начало или возобновление занятий.", "identity_needs_review")
        new_status = (
            old_status if old_status in {Student.Status.ACTIVE, Student.Status.AT_RISK} else Student.Status.ACTIVE
        )
    trainers = sorted({value for value in (
        terms.assigned_trainer_id, terms.package_owner_trainer_id, terms.sale_trainer_id,
    ) if value is not None})
    trainer_rows = list(Trainer.objects.for_club(club_id).filter(id__in=trainers).order_by("id").values(
        "id", "is_active", "updated_at",
    ))
    if len(trainer_rows) != len(trainers) or any(not row["is_active"] for row in trainer_rows):
        _fail("Выбранный тренер недоступен.", "target_not_available")
    candidates = list(Payment.objects.for_club(club_id).filter(
        student_id=student.id if student else 0, amount=terms.paid_amount,
    ).order_by("id").values("id", "status", "subscription_id", "updated_at"))
    if candidates and not terms.distinct_payment_reference:
        _fail("Найдена похожая оплата. Сверьте её или подтвердите отдельный исходный платёж.",
              "payment_source_needs_review")
    if terms.payout_policy == Tariff.PayoutPolicy.ON_PAYMENT:
        assert_trainer_payroll_date_open(club_id=club_id, target_date=terms.effective_on)
    state = {
        "terms": terms.fingerprint(), "timezone": club.timezone, "history_only": history_only,
        "student": None if student is None else {
            "id": student.id, "updated_at": student.updated_at, "status": student.status,
            "assigned_trainer_id": student.assigned_trainer_id,
            "lead_status": student.lead_status, "became_student_at": student.became_student_at,
            "first_name": student.first_name, "last_name": student.last_name,
            "phone": student.phone, "guardian_phone": student.guardian_phone,
            "date_of_birth": student.date_of_birth, "is_child": student.is_child,
        },
        "tariff": {"id": tariff.id, "type": tariff.training_type_id, "kind": kind,
                   "scope": tariff.scope, "location": tariff.location_id, "updated_at": tariff.updated_at},
        "trainers": trainer_rows, "payments": candidates,
        "tariff_component_id": catalog_components[0].id if catalog_components else None,
    }
    _, _, _, _, group_state = _group_context(
        club=club, tariff=tariff, student=student, terms=terms, history_only=history_only,
    )
    state["group"] = group_state
    state["personal"] = _personal_cutover_context(
        club=club, tariff=tariff, student=student, terms=terms, history_only=history_only,
    )
    if state["personal"]:
        trainers = sorted(set(trainers) | {state["personal"]["occurrence"]["trainer_id"]})
    if group_state:
        trainers = sorted(set(trainers) | {group_state["group"][2]}
                          | {row["trainer_id"] for row in group_state["schedules"]})
    return club, tariff, student, trainers, state, new_status


def _fingerprint(state):
    return sha256(json.dumps(state, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def preview_opening_entitlement(*, club_id: int, actor_user_id: int, terms: OpeningEntitlementTerms) -> OpeningPreview:
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    terms = terms.normalized()
    _, _, student, _, state, new_status = _resolve(club_id=club_id, terms=terms)
    return OpeningPreview(
        fingerprint=_fingerprint(state), student_id=student.id if student else None,
        history_only=state["history_only"], old_status=student.status if student else None, new_status=new_status,
        scope=json.loads(json.dumps(state, default=str)),
    )


def _group_context(*, club, tariff, student, terms, history_only):
    from dataclasses import asdict
    from datetime import datetime, timedelta

    from django.conf import settings
    from django.utils import timezone

    from apps.attendance.models import Schedule, ScheduleEnrollment, TrainingGroupMembership, TrainingGroupRolloutState
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.attendance.services.checkin import _get_schedule_occurrence
    from apps.billing.service_modules.group_payments import _resolve_canonical_group_payment_target
    from apps.clubs.timezones import club_zoneinfo

    if tariff.training_type.kind != "group":
        if terms.training_group_id is not None or terms.schedule_id is not None:
            _fail("Персональный пакет не может создавать групповое членство.")
        return None, None, None, "", {}
    if history_only:
        # The original target remains in reviewed_input; no operational rights.
        return None, None, None, "", {}
    if terms.training_group_id is None or terms.schedule_id is None:
        _fail("Выберите каноническую группу и первое разрешённое занятие.")
    rollout = TrainingGroupRolloutState.objects.for_club(club).first()
    if not settings.TRAINING_GROUP_NEW_WRITES_ENABLED or not rollout or rollout.mode != "active":
        _fail("Новые групповые записи временно отключены.", "feature_disabled")
    schedule = Schedule.objects.for_club(club).select_related("trainer", "location", "training_type", "club").filter(
        id=terms.schedule_id, training_group_id=terms.training_group_id,
        is_active=True, one_time_date__isnull=True,
    ).first()
    if (
        schedule is None or schedule.training_type_id != tariff.training_type_id
        or (tariff.scope == Tariff.Scope.LOCATION and schedule.location_id != tariff.location_id)
    ):
        _fail("Группа или занятие недоступны для выбранного пакета.", "target_not_available")
    target_date = timezone.localtime(terms.operational_cutover, club_zoneinfo(club)).date()
    occurrence = _get_schedule_occurrence(schedule=schedule, checkin_date=target_date)
    start = timezone.make_aware(
        datetime.combine(occurrence.effective_date, occurrence.effective_start_time), club_zoneinfo(club),
    )
    if start != terms.operational_cutover or start <= timezone.now() or target_date > terms.expires_on:
        _fail("Начало работы должно совпадать с будущим занятием в пределах срока.", "opening_cutover_needs_review")
    cutoff_date = timezone.localtime(terms.covered_through, club_zoneinfo(club)).date()
    group_schedule_ids = set(Schedule.objects.for_club(club).filter(
        training_group_id=terms.training_group_id,
    ).values_list("id", flat=True))
    for item in get_schedule_occurrences_for_date(club=club, target_date=cutoff_date):
        if item.schedule_id not in group_schedule_ids:
            continue
        starts_at = timezone.make_aware(datetime.combine(item.effective_date, item.effective_start_time),
                                       club_zoneinfo(club))
        ends_at = timezone.make_aware(datetime.combine(item.effective_date, item.effective_end_time),
                                     club_zoneinfo(club))
        if ends_at <= starts_at:
            ends_at += timedelta(days=1)
        if starts_at <= terms.covered_through < ends_at:
            _fail("Граница сверки пересекает занятие. Уточните исходный остаток.", "opening_cutover_needs_review")
    group, membership, action = _resolve_canonical_group_payment_target(
        club_id=club.id, student_id=student.id if student else 0, schedule=schedule,
        target_start_date=target_date, requested_training_group_id=terms.training_group_id,
        rollout_state=rollout, lock=False,
    )
    if membership and membership.status != TrainingGroupMembership.Status.ACTIVE:
        _fail("Членство заморожено и требует отдельной сверки.")
    state = {
        "rollout": [rollout.id, rollout.mode, rollout.updated_at],
        "group": [group.id, group.updated_at, group.responsible_trainer_id],
        "group_terms": [group.name, group.status, group.training_type_id, group.location_id],
        "occurrence": asdict(occurrence),
        "schedules": list(Schedule.objects.for_club(club).filter(training_group=group).order_by("id").values(
            "id", "updated_at", "trainer_id", "location_id", "is_active", "one_time_date",
            "day_of_week", "start_time", "end_time", "training_type_id", "training_group_id",
        )),
        "membership": None if membership is None else [
            membership.id, membership.updated_at, membership.authority,
            membership.status, membership.starts_on, membership.ends_on,
        ],
        "enrollments": list(ScheduleEnrollment.objects.for_club(club).filter(
            student_id=student.id if student else 0, schedule__training_group=group,
        ).order_by("id").values("id", "updated_at", "status", "starts_on", "ends_on", "training_group_membership_id")),
    }
    return schedule, group, membership, action, state


def issue_opening_entitlement(
    *, club_id: int, actor_user_id: int, terms: OpeningEntitlementTerms,
    preview_fingerprint: str, channel: str,
) -> OpeningEntitlementSnapshot:
    from datetime import timedelta
    from decimal import Decimal

    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone

    from apps.attendance.models import Schedule, ScheduleEnrollment, TrainingGroup, TrainingGroupMembership
    from apps.attendance.services.training_group_memberships import lock_training_group_payment_scope
    from apps.billing.models import Subscription, SubscriptionComponent
    from apps.billing.service_modules.group_payments import (
        _link_payment_owned_group_membership,
        _snapshot_group_conversion_target,
    )
    from apps.billing.tasks import create_sale_earning
    from apps.clubs.timezones import club_local_day_start, club_zoneinfo
    from apps.trainers.models import TrainerPackageAllocation
    from apps.trainers.services import create_package_allocation_for_subscription, lock_trainer_payroll_mutation_scope

    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    terms = terms.normalized()
    if channel not in {"cli", "assistant_cli", "htmx", "worker", "test"}:
        _fail("Неизвестный канал переноса.")
    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        receipt = OpeningEntitlementSnapshot.objects.for_club(club_id).filter(
            source_namespace=terms.source_namespace, entitlement_source_key=terms.entitlement_source_key,
        ).first()
        if receipt:
            if receipt.payload_fingerprint != terms.fingerprint():
                _fail("Ключ уже принят с другими исходными данными.", "idempotency_conflict")
            return receipt
        if not settings.STUDENT_OPENING_IMPORT_ENABLED:
            _fail("Новый перенос временно отключён.", "feature_disabled")
        if Payment.objects.for_club(club_id).filter(
            opening_source_namespace=terms.source_namespace, opening_source_key=terms.payment_source_key,
            origin=Payment.Origin.OPENING,
        ).exists():
            _fail("Ключ оплаты уже связан с другим перенесённым пакетом.", "idempotency_conflict")
        club, tariff, student, trainer_ids, state, new_status = _resolve(club_id=club_id, terms=terms)
        if _fingerprint(state) != preview_fingerprint:
            _fail("Данные изменились после предварительного просмотра.", "preview_stale")
        if state["group"]:
            lock_training_group_payment_scope(club_id=club_id)
        list(Trainer.objects.for_club(club_id).select_for_update().filter(id__in=trainer_ids).order_by("id"))
        if student:
            Student.objects.for_club(club_id).select_for_update().get(id=student.id)
        if state["personal"]:
            Schedule.objects.for_club(club_id).select_for_update().get(id=terms.cutover_schedule_id)
        if state["group"]:
            TrainingGroup.objects.for_club(club_id).select_for_update().get(id=terms.training_group_id)
            list(Schedule.objects.for_club(club_id).select_for_update().filter(
                training_group_id=terms.training_group_id,
            ).order_by("id"))
            list(TrainingGroupMembership.objects.for_club(club_id).select_for_update().filter(
                student_id=student.id if student else 0, training_group_id=terms.training_group_id,
            ).order_by("id"))
            list(ScheduleEnrollment.objects.for_club(club_id).select_for_update().filter(
                id__in=[row["id"] for row in state["group"]["enrollments"]],
            ).order_by("id"))
        club, tariff, student, _, current_state, new_status = _resolve(club_id=club_id, terms=terms)
        if _fingerprint(current_state) != preview_fingerprint:
            _fail("Данные изменились во время проверки.", "preview_stale")
        old_status = student.status if student else None
        history_only = state["history_only"]
        now = timezone.now()
        if student is None:
            student = Student.objects.create(
                club_id=club_id, first_name=terms.first_name, last_name=terms.last_name,
                is_child=terms.is_child, phone=terms.phone, guardian_phone=terms.guardian_phone,
                date_of_birth=terms.date_of_birth, status=new_status, lead_status=None,
                assigned_trainer_id=terms.assigned_trainer_id,
                crm_entry_kind=Student.CrmEntryKind.EXISTING_STUDENT,
                crm_entered_by_id=actor_user_id, became_student_at=now,
            )
        else:
            student.status = new_status
            student.lead_status = None
            student.became_student_at = student.became_student_at or now
            if terms.change_assigned_trainer:
                student.assigned_trainer_id = terms.assigned_trainer_id
            student.save(update_fields=["status", "lead_status", "became_student_at", "assigned_trainer", "updated_at"])
        schedule, group, membership, action, _ = _group_context(
            club=club, tariff=tariff, student=student, terms=terms, history_only=history_only,
        )
        subscription = Subscription.objects.create(
            club_id=club_id, student=student, tariff=tariff,
            status=Subscription.Status.EXPIRED if history_only else Subscription.Status.ACTIVE,
            paid_amount=terms.paid_amount, trainings_left=terms.original_left, trainings_used=terms.original_used,
            expires_at=club_local_day_start(club, terms.expires_on + timedelta(days=1)),
            scope=tariff.scope, location_id=tariff.location_id, trainer_payout_policy_snapshot=terms.payout_policy,
        )
        component = SubscriptionComponent.objects.create(
            club_id=club_id, subscription=subscription, name_snapshot=tariff.name,
            tariff_component_id=state["tariff_component_id"],
            training_type_id=tariff.training_type_id, entitlement_kind="finite_credits",
            credits_total=terms.original_total, credits_used=terms.original_used, credits_left=terms.original_left,
            scope=tariff.scope, location_id=tariff.location_id, trainer_payout_policy_snapshot=terms.payout_policy,
            paid_amount_basis_snapshot=terms.paid_amount,
            unit_amount_basis_snapshot=(terms.paid_amount / terms.original_total).quantize(Decimal("0.01")),
            sale_trainer_id_snapshot=terms.sale_trainer_id, sale_rate_percent_snapshot=terms.sale_rate_percent,
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.OPENING_REVIEWED,
        )
        payment = Payment(
            club_id=club_id, student=student, tariff=tariff, subscription=subscription,
            origin=Payment.Origin.OPENING, opening_effective_on=terms.effective_on,
            opening_source_namespace=terms.source_namespace, opening_source_key=terms.payment_source_key,
            opening_provenance={"source_note": terms.source_note,
                                "distinct_payment_reference": terms.distinct_payment_reference,
                                "entitlement_source_key": terms.entitlement_source_key},
            amount=terms.paid_amount, original_amount=terms.paid_amount, payment_method=terms.payment_method,
            status=Payment.Status.CONFIRMED, recorded_by_id=actor_user_id, verified_by_id=actor_user_id,
            verified_at=now, seller_trainer_id=terms.sale_trainer_id,
            package_owner_trainer_id=terms.package_owner_trainer_id,
            sale_earning_snapshot_recorded=True, sale_trainer_id_snapshot=terms.sale_trainer_id,
            sale_training_type_id_snapshot=tariff.training_type_id,
            sale_training_type_kind_snapshot=tariff.training_type.kind,
            sale_rate_percent_snapshot=terms.sale_rate_percent, sale_amount_basis_snapshot=terms.paid_amount,
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.OPENING_REVIEWED,
        )
        if schedule:
            payment.target_schedule = schedule
            payment.target_training_group = group
            payment.target_group_membership = membership
            payment.group_membership_action_snapshot = action
            payment.target_start_date = timezone.localtime(terms.operational_cutover, club_zoneinfo(club)).date()
            _snapshot_group_conversion_target(payment, schedule, canonical_group=group)
            payment.sale_trainer_id_snapshot = terms.sale_trainer_id
            payment.sale_attribution_source = "opening_reviewed"
        payment.save()
        if schedule:
            _link_payment_owned_group_membership(
                payment=payment, club_id=club_id, actor_user_id=actor_user_id, scope_locked=True,
            )
        if terms.package_owner_trainer_id:
            create_package_allocation_for_subscription(
                club_id=club_id, subscription_id=subscription.id, payment_id=payment.id,
                owner_trainer_id=terms.package_owner_trainer_id, created_by_id=actor_user_id,
                source=TrainerPackageAllocation.Source.OPENING,
            )
        receipt = OpeningEntitlementSnapshot.objects.create(
            club_id=club_id, subscription=subscription, component=component, payment=payment,
            actor_id=actor_user_id, source_namespace=terms.source_namespace,
            student_source_key=terms.student_source_key, entitlement_source_key=terms.entitlement_source_key,
            payload_fingerprint=terms.fingerprint(), channel=channel, started_on=terms.started_on,
            expires_on=terms.expires_on, covered_through=terms.covered_through,
            operational_cutover=terms.operational_cutover, original_total=terms.original_total,
            original_used=terms.original_used, original_left=terms.original_left, history_only=history_only,
            reviewed_input=terms.as_payload(), student_transition={"from": old_status, "to": new_status},
        )
        if terms.payout_policy == Tariff.PayoutPolicy.ON_PAYMENT:
            create_sale_earning(payment.id, club_id)
        return receipt


def _personal_cutover_context(*, club, tariff, student, terms, history_only):
    from dataclasses import asdict
    from datetime import datetime, timedelta

    from django.db.models import Q

    from apps.attendance.models import Schedule
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.attendance.services.checkin import _get_schedule_occurrence

    if tariff.training_type.kind != "personal" or history_only:
        return {}
    if terms.cutover_schedule_id is None:
        _fail("Выберите первое разрешённое персональное занятие.", "opening_cutover_needs_review")
    schedule = Schedule.objects.for_club(club).filter(
        id=terms.cutover_schedule_id, is_active=True, training_type_id=tariff.training_type_id,
    ).first()
    if schedule is None or (tariff.scope == Tariff.Scope.LOCATION and schedule.location_id != tariff.location_id):
        _fail("Персональное занятие недоступно для этого пакета.", "target_not_available")
    target_date = timezone.localtime(terms.operational_cutover, club_zoneinfo(club)).date()
    occurrence = _get_schedule_occurrence(schedule=schedule, checkin_date=target_date)
    starts_at = timezone.make_aware(
        datetime.combine(occurrence.effective_date, occurrence.effective_start_time), club_zoneinfo(club),
    )
    if starts_at != terms.operational_cutover or target_date > terms.expires_on:
        _fail("Начало работы должно совпадать с выбранным персональным занятием.", "opening_cutover_needs_review")
    cutoff_date = timezone.localtime(terms.covered_through, club_zoneinfo(club)).date()
    # The anchor and known visits/bookings of the reused person are factual CRM
    # evidence; unrelated clients' personal lessons cannot invalidate this import.
    relevant_schedule_ids = set(Schedule.objects.for_club(club).filter(
        Q(id=schedule.id)
        | Q(enrollments__student_id=student.id if student else 0)
        | Q(checkins__student_id=student.id if student else 0),
        training_type_id=tariff.training_type_id,
    ).values_list("id", flat=True))
    for item in get_schedule_occurrences_for_date(club=club, target_date=cutoff_date):
        if item.schedule_id not in relevant_schedule_ids:
            continue
        start = timezone.make_aware(
            datetime.combine(item.effective_date, item.effective_start_time), club_zoneinfo(club),
        )
        end = timezone.make_aware(datetime.combine(item.effective_date, item.effective_end_time), club_zoneinfo(club))
        if end <= start:
            end += timedelta(days=1)
        if start <= terms.covered_through < end:
            _fail("Граница сверки пересекает персональное занятие.", "opening_cutover_needs_review")
    return {"schedule_id": schedule.id, "updated_at": schedule.updated_at, "occurrence": asdict(occurrence)}
