from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q

from apps.billing.models import Payment, Subscription, Tariff, TrainingType
from apps.billing.service_modules.group_contracts import (
    validate_trainer_group_payment_contract as _validate_trainer_group_payment_contract_core,
)
from apps.clubs.timezones import club_localdate_by_id
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student

if TYPE_CHECKING:
    from apps.attendance.models import ScheduleEnrollment


GROUP_SALE_OFFER_PROTOCOL = "group-sale-offer-v2"


@dataclass(frozen=True)
class GroupSaleOffer:
    """The locked/read-only commercial terms for one canonical new admission."""

    payload: dict
    rollout_state: object


def _offer_digest(*, canonical: dict) -> str:
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    signature = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"), encoded, hashlib.sha256
    ).hexdigest()
    return f"v2.{signature}"


def _offer_component_snapshot(component) -> dict:
    return {
        "id": component.id,
        "updated_at": component.updated_at.isoformat() if component.pk else None,
        "training_type_id": component.training_type_id,
        "entitlement_kind": component.entitlement_kind,
        "credits_total": component.credits_total,
        "weekly_limit": component.weekly_limit,
        "scope": component.scope,
        "location_id": component.location_id,
        "trainer_payout_policy": component.trainer_payout_policy,
        "paid_amount_basis": str(component.paid_amount_basis),
        "sort_order": component.sort_order,
    }


def _raise_group_offer_changed() -> None:
    raise BusinessLogicError(
        "Коммерческие условия группы изменились, запросите предложение заново",
        code="group_offer_changed",
    )


def resolve_v2_group_sale_offer(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
    lock: bool,
) -> GroupSaleOffer:
    """Resolve one canonical lead admission and sign every mutable input.

    Preview callers pass ``lock=False``; a command holds its outer transaction
    and passes ``lock=True``.  Neither path creates catalog, membership, or
    payment rows, so a rejected/stale offer has zero commercial artifacts.
    """

    from apps.attendance.models import Schedule, TrainingGroupRolloutState
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.attendance.services.training_group_memberships import (
        lock_training_group_mutation_scope,
    )
    from apps.billing.service_modules.tariff_components import _payment_preflight_tariff_components
    from apps.clubs.capabilities import get_commercial_journey_capability
    from apps.clubs.models import ClubSettings

    if lock:
        # Match the payment path's writer ordering: Club -> payroll/rollout ->
        # Student -> target.  This is deliberately before all financial rows.
        from apps.clubs.models import Club

        Club.objects.select_for_update(of=("self",)).get(id=club_id)
        ClubSettings.objects.select_for_update(of=("self",)).filter(club_id=club_id).first()
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
    else:
        rollout_state = TrainingGroupRolloutState.objects.for_club(club_id).first()

    capability = get_commercial_journey_capability(club=club_id)
    if (
        not capability.v2_commercial_journey_enabled
        or rollout_state is None
        or rollout_state.mode != TrainingGroupRolloutState.Mode.ACTIVE
        or not bool(settings.TRAINING_GROUP_NEW_WRITES_ENABLED)
    ):
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code="commercial_journey_unavailable",
        )

    students = Student.objects.for_club(club_id)
    if lock:
        students = students.select_for_update()
    student = students.filter(id=student_id, deleted_at__isnull=True).first()
    if student is None:
        raise BusinessLogicError("Контекст клиента изменился", code="person_context_changed")
    if (
        student.status not in {Student.Status.LEAD, Student.Status.TRIAL}
        or student.lead_status is None
    ):
        raise BusinessLogicError("Клиент уже не является новой заявкой", code="person_context_changed")

    tariffs = Tariff.objects.for_club(club_id).select_related("training_type", "location")
    if lock:
        tariffs = tariffs.select_for_update(of=("self",))
    tariff = tariffs.filter(id=tariff_id, is_active=True).first()
    if tariff is None or tariff.training_type.kind != TrainingType.Kind.GROUP:
        _raise_group_offer_changed()
    if tariff.price <= 0:
        _raise_group_offer_changed()
    components = _payment_preflight_tariff_components(tariff, club_id=club_id)

    try:
        schedule = _validate_group_conversion_target(
            club_id=club_id,
            tariff=tariff,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            lock_schedule=lock,
        )
    except BusinessLogicError as exc:
        if exc.code in {
            "target_schedule_not_found",
            "target_schedule_invalid",
            "target_schedule_trainer_inactive",
            "target_schedule_not_group",
            "target_schedule_training_type_mismatch",
            "target_schedule_location_mismatch",
            "target_schedule_occurrence_not_found",
        }:
            _raise_group_offer_changed()
        raise
    if schedule is None:
        _raise_group_offer_changed()

    canonical_group, membership, action = _resolve_canonical_group_payment_target(
        club_id=club_id,
        student_id=student.id,
        schedule=schedule,
        target_start_date=target_start_date,
        requested_training_group_id=target_training_group_id,
        rollout_state=rollout_state,
        lock=lock,
    )
    if (
        canonical_group is None
        or membership is not None
        or action != Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    ):
        raise BusinessLogicError(
            "Для этой группы доступно только продление в карточке ученика",
            code="group_sale_renewal_conflict",
        )

    slots = (
        Schedule.objects.for_club(club_id)
        .filter(
            training_group_id=canonical_group.id,
            is_active=True,
            one_time_date__isnull=True,
            training_type_id=tariff.training_type_id,
            trainer__is_active=True,
        )
        .select_related("trainer", "location")
        .order_by("day_of_week", "start_time", "id")
    )
    if lock:
        slots = slots.select_for_update(of=("self",))
    slot_list = list(slots)
    if not slot_list or target_schedule_id not in {slot.id for slot in slot_list}:
        _raise_group_offer_changed()

    selected_occurrence = next(
        (
            occurrence
            for occurrence in get_schedule_occurrences_for_date(
                club=club_id,
                target_date=target_start_date,
            )
            if occurrence.schedule_id == target_schedule_id
        ),
        None,
    )
    if selected_occurrence is None:
        _raise_group_offer_changed()

    weekly_schedule = [
        {
            "schedule_id": slot.id,
            "updated_at": slot.updated_at.isoformat(),
            "day_of_week": slot.day_of_week,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
            "trainer_id": slot.trainer_id,
            "trainer_name": _trainer_snapshot_name(slot.trainer),
            "location_id": slot.location_id,
            "location_name": slot.location.name,
        }
        for slot in slot_list
    ]
    selected_occurrence_payload = {
        "schedule_id": selected_occurrence.schedule_id,
        "date": selected_occurrence.effective_date.isoformat(),
        "start_time": selected_occurrence.effective_start_time.isoformat(),
        "end_time": selected_occurrence.effective_end_time.isoformat(),
        "trainer_id": selected_occurrence.trainer_id,
        "trainer_name": selected_occurrence.trainer_name,
        "location_id": selected_occurrence.location_id,
        "location_name": selected_occurrence.location_name,
        "is_rescheduled": selected_occurrence.is_rescheduled,
        "is_substitute": selected_occurrence.is_substitute,
    }
    buyer_email_required = bool(
        getattr(settings, "PAYMENT_PROVIDER", "") == "tochka"
        and getattr(settings, "TOCHKA_RECEIPT_MODE", "") == "tochka_receipt"
    )
    canonical = {
        "protocol": GROUP_SALE_OFFER_PROTOCOL,
        "club_id": club_id,
        "capability": {
            "protocol_version": capability.protocol_version,
            "unified": capability.unified_client_journey_enabled,
            "rollout_mode": rollout_state.mode,
            "training_group_new_writes_enabled": bool(settings.TRAINING_GROUP_NEW_WRITES_ENABLED),
        },
        "student": {
            "id": student.id,
            "status": student.status,
            "lead_status": student.lead_status,
            "became_student_at": student.became_student_at.isoformat() if student.became_student_at else None,
        },
        "tariff": {
            "id": tariff.id,
            "updated_at": tariff.updated_at.isoformat(),
            "price": str(tariff.price),
            "trainings_limit": tariff.trainings_limit,
            "duration_days": tariff.duration_days,
            "scope": tariff.scope,
            "location_id": tariff.location_id,
            "training_type_id": tariff.training_type_id,
            "components": [_offer_component_snapshot(component) for component in components],
        },
        "group": {
            "id": canonical_group.id,
            "updated_at": canonical_group.updated_at.isoformat(),
            "name": canonical_group.name,
            "responsible_trainer_id": canonical_group.responsible_trainer_id,
            "location_id": canonical_group.location_id,
            "weekly_schedule": weekly_schedule,
        },
        "selected_occurrence": selected_occurrence_payload,
        "expected_action": Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
        "buyer_email_required": buyer_email_required,
    }
    digest = _offer_digest(canonical=canonical)
    return GroupSaleOffer(
        rollout_state=rollout_state,
        payload={
            "protocol_version": "v2",
            "student": {"id": student.id, "display_name": str(student)},
            "tariff": {
                "id": tariff.id,
                "name": tariff.name,
                "price": f"{tariff.price:.2f}",
                "trainings_limit": tariff.trainings_limit,
                "duration_days": tariff.duration_days,
            },
            "group": {
                "id": canonical_group.id,
                "name": canonical_group.name,
                "responsible_trainer_id": canonical_group.responsible_trainer_id,
                "responsible_trainer_name": _trainer_snapshot_name(canonical_group.responsible_trainer),
                "location_id": canonical_group.location_id,
                "location_name": canonical_group.location.name,
                "weekly_schedule": weekly_schedule,
            },
            "selected_occurrence": selected_occurrence_payload,
            "expected_action": Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
            "buyer_email_required": buyer_email_required,
            "offer_digest": digest,
        },
    )


def assert_v2_group_sale_offer_digest(*, offer: GroupSaleOffer, expected_offer_digest: str) -> None:
    actual = str(offer.payload["offer_digest"])
    if not hmac.compare_digest(actual, (expected_offer_digest or "").strip()):
        _raise_group_offer_changed()


def _v2_group_sale_command_fingerprint(
    *,
    student_id: int,
    tariff_id: int,
    payment_method: str,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
    expected_offer_digest: str,
    buyer_email: str | None = None,
) -> str:
    from apps.billing.service_modules.renewals import build_subscription_command_fingerprint

    return build_subscription_command_fingerprint(
        student_id=student_id,
        tariff_id=tariff_id,
        payment_method=payment_method,
        discount_ids=[],
        debt_ids=[],
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        offer_digest=expected_offer_digest,
        buyer_email_hash=(
            hashlib.sha256(buyer_email.strip().lower().encode("utf-8")).hexdigest()
            if buyer_email and buyer_email.strip()
            else None
        ),
    )


def _replay_v2_group_sale_command(
    *,
    club_id: int,
    command_idempotency_key: str,
    command_fingerprint: str,
):
    existing = (
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .select_related("subscription")
        .filter(command_idempotency_key=command_idempotency_key)
        .first()
    )
    if existing is None:
        return None
    if existing.command_fingerprint != command_fingerprint:
        raise BusinessLogicError(
            "Idempotency key was already used for another command",
            code="idempotency_conflict",
        )
    existing._command_replayed = True
    return existing


def _trainer_snapshot_name(trainer) -> str:
    return f"{trainer.first_name} {trainer.last_name}"


def _validate_trainer_group_payment_contract(
    *,
    club_id: int,
    student: Student,
    tariff: Tariff,
    target_schedule_id: int | None,
    target_start_date: date | None,
    lock_enrollments: bool = False,
) -> None:
    """Compatibility owner for callers while the reusable contract stays acyclic."""

    _validate_trainer_group_payment_contract_core(
        club_id=club_id,
        student=student,
        tariff=tariff,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        lock_enrollments=lock_enrollments,
    )


def _validate_group_conversion_target(
    *,
    club_id: int,
    tariff: Tariff,
    target_schedule_id: int | None,
    target_start_date: date | None,
    seller_trainer_id: int | None = None,
    for_confirmation: bool = False,
    lock_schedule: bool = False,
    allow_inactive_target_trainer: bool = False,
) -> object | None:
    if target_schedule_id is None and target_start_date is None:
        return None

    if tariff.training_type.kind != TrainingType.Kind.GROUP:
        raise BusinessLogicError(
            "Целевую группу можно указать только для групповой оплаты",
            code="target_schedule_not_allowed",
        )
    if target_schedule_id is None or target_start_date is None:
        raise BusinessLogicError(
            "Для групповой конверсии укажите целевую группу и дату старта",
            code="target_schedule_required",
        )
    if not for_confirmation and target_start_date < club_localdate_by_id(club_id):
        raise BusinessLogicError(
            "Дата старта группы не может быть в прошлом",
            code="target_start_date_in_past",
        )

    from apps.attendance.models import Schedule
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    schedules = Schedule.objects.for_club(club_id)
    if lock_schedule:
        schedules = schedules.select_for_update(of=("self",))
    schedule = (
        schedules.select_related("trainer", "location", "training_type")
        .filter(id=target_schedule_id)
        .first()
    )
    if schedule is None:
        raise BusinessLogicError(
            "Целевая группа не найдена",
            code="target_schedule_not_found",
        )
    if not schedule.is_active or schedule.one_time_date is not None:
        raise BusinessLogicError(
            "Целевая группа недоступна для постоянной записи",
            code="target_schedule_invalid",
        )
    if not allow_inactive_target_trainer and not schedule.trainer.is_active:
        raise BusinessLogicError(
            "Тренер целевой группы неактивен",
            code="target_schedule_trainer_inactive",
        )
    if (
        schedule.training_type_id is None
        or schedule.training_type.kind != TrainingType.Kind.GROUP
    ):
        raise BusinessLogicError(
            "Целевая группа должна быть групповой тренировкой",
            code="target_schedule_not_group",
        )
    if schedule.training_type_id != tariff.training_type_id:
        raise BusinessLogicError(
            "Тариф не соответствует типу целевой группы",
            code="target_schedule_training_type_mismatch",
        )
    if (
        tariff.scope == Tariff.Scope.LOCATION
        and tariff.location_id != schedule.location_id
    ):
        raise BusinessLogicError(
            "Тариф не действует в зале целевой группы",
            code="target_schedule_location_mismatch",
        )
    if seller_trainer_id is not None and seller_trainer_id != schedule.trainer_id:
        raise BusinessLogicError(
            "Тренер-продавец должен совпадать с тренером целевой группы",
            code="target_schedule_seller_mismatch",
        )

    occurrence_exists = any(
        occurrence.schedule_id == schedule.id
        for occurrence in get_schedule_occurrences_for_date(
            club=club_id,
            target_date=target_start_date,
        )
    )
    if not occurrence_exists:
        raise BusinessLogicError(
            "На выбранную дату у целевой группы нет занятия",
            code="target_schedule_occurrence_not_found",
        )
    return schedule


def _resolve_canonical_group_payment_target(
    *,
    club_id: int,
    student_id: int,
    schedule,
    target_start_date: date,
    requested_training_group_id: int | None,
    rollout_state=None,
    lock: bool,
):
    """Resolve a mapped slot to its canonical group under the rollout matrix."""

    if schedule.training_group_id is None:
        if requested_training_group_id is not None:
            raise BusinessLogicError(
                "The selected schedule is not linked to the requested training group.",
                code="target_training_group_mismatch",
            )
        return None, None, ""

    from apps.attendance.models import (
        TrainingGroup,
        TrainingGroupMembership,
        TrainingGroupRolloutState,
    )
    from apps.attendance.services.training_group_memberships import (
        lock_training_group_mutation_scope,
    )

    if rollout_state is None:
        rollout_state = (
            lock_training_group_mutation_scope(club_id=club_id)
            if lock
            else TrainingGroupRolloutState.objects.for_club(club_id).first()
        )
    if rollout_state is None:
        return None, None, ""
    if rollout_state.mode == TrainingGroupRolloutState.Mode.CONTAINMENT:
        raise BusinessLogicError(
            "Canonical training-group sales are temporarily disabled.",
            code="training_group_writes_disabled",
        )
    if (
        requested_training_group_id is not None
        and rollout_state.mode != TrainingGroupRolloutState.Mode.ACTIVE
    ):
        # An explicit canonical target is a client contract, not a hint. It
        # must never fall through to the legacy paid-conversion branch when a
        # rollout flips or the state is only shadow/reconciling/off.
        raise BusinessLogicError(
            "Canonical training-group sales are temporarily disabled.",
            code="training_group_writes_disabled",
        )
    if rollout_state.mode == TrainingGroupRolloutState.Mode.OFF:
        return None, None, ""

    groups = TrainingGroup.objects.for_club(club_id)
    if lock:
        groups = groups.select_for_update(of=("self",))
    group = (
        groups.select_related("responsible_trainer")
        .filter(id=schedule.training_group_id, status=TrainingGroup.Status.ACTIVE)
        .first()
    )
    if group is None or not group.responsible_trainer.is_active:
        raise BusinessLogicError(
            "The selected training group is unavailable for admission.",
            code="target_training_group_invalid",
        )
    if (
        requested_training_group_id is not None
        and requested_training_group_id != group.id
    ):
        raise BusinessLogicError(
            "The selected schedule does not belong to the requested training group.",
            code="target_training_group_mismatch",
        )

    memberships = TrainingGroupMembership.objects.for_club(club_id)
    if lock:
        memberships = memberships.select_for_update(of=("self",))
    membership = (
        memberships.filter(
            student_id=student_id,
            training_group_id=group.id,
            starts_on__lte=target_start_date,
            status__in=[
                TrainingGroupMembership.Status.ACTIVE,
                TrainingGroupMembership.Status.FROZEN,
            ],
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=target_start_date))
        .first()
    )
    if membership is None:
        future_open_membership = (
            memberships.filter(
                student_id=student_id,
                training_group_id=group.id,
                starts_on__gt=target_start_date,
                ends_on__isnull=True,
                status__in=[
                    TrainingGroupMembership.Status.ACTIVE,
                    TrainingGroupMembership.Status.FROZEN,
                ],
            )
            .first()
        )
        if future_open_membership is not None:
            raise BusinessLogicError(
                "The student already has a future membership for this training group.",
                code="training_group_membership_exists",
            )
    action = (
        Payment.GroupMembershipActionSnapshot.RENEWAL
        if membership is not None
        else Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    )
    return group, membership, action


def _link_payment_owned_group_membership(
    *,
    payment: Payment,
    club_id: int,
    actor_user_id: int | None,
    scope_locked: bool = False,
):
    """Create/replay the one payment-owned membership and preserve its anchor."""

    if (
        payment.target_training_group_id is None
        or payment.group_membership_action_snapshot
        != Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    ):
        return None
    if payment.target_start_date is None or payment.target_schedule_id is None:
        raise BusinessLogicError(
            "A group admission requires an exact schedule and start date.",
            code="payment_conversion_target_required",
        )

    from apps.attendance.services.training_group_memberships import (
        create_payment_owned_training_group_membership,
    )

    membership, projections = create_payment_owned_training_group_membership(
        club_id=club_id,
        student_id=payment.student_id,
        training_group_id=payment.target_training_group_id,
        starts_on=payment.target_start_date,
        payment_id=payment.id,
        actor_user_id=actor_user_id,
        scope_locked=scope_locked,
    )
    conversion_enrollment = next(
        (
            projection
            for projection in projections
            if projection.schedule_id == payment.target_schedule_id
        ),
        None,
    )
    if conversion_enrollment is None:
        raise BusinessLogicError(
            "The selected group anchor was not projected by the membership.",
            code="payment_conversion_enrollment_invalid",
        )
    if payment.conversion_group_membership_id is None:
        payment.target_group_membership = membership
        payment.conversion_group_membership = membership
        payment.conversion_enrollment = conversion_enrollment
        payment.full_clean()
        payment.save(
            update_fields=[
                "target_group_membership",
                "conversion_group_membership",
                "conversion_enrollment",
                "updated_at",
            ]
        )
    elif (
        payment.target_group_membership_id != membership.id
        or payment.conversion_group_membership_id != membership.id
        or payment.conversion_enrollment_id != conversion_enrollment.id
    ):
        raise BusinessLogicError(
            "Payment ownership does not match the canonical group family.",
            code="training_group_payment_ownership_conflict",
        )
    return membership


def _close_payment_owned_group_membership(
    *,
    payment: Payment,
    club_id: int,
    actor_user_id: int | None,
    rationale: str,
) -> None:
    if payment.conversion_group_membership_id is None:
        return
    from apps.attendance.services.training_group_memberships import (
        cancel_payment_owned_training_group_membership,
    )

    cancel_payment_owned_training_group_membership(
        club_id=club_id,
        membership_id=payment.conversion_group_membership_id,
        payment_id=payment.id,
        actor_user_id=actor_user_id,
        rationale=rationale,
    )


def _snapshot_group_conversion_target(
    payment: Payment,
    schedule,
    *,
    canonical_group=None,
) -> None:
    """Freeze the canonical group sale owner separately from its chosen slot."""
    if canonical_group is None:
        payment.target_group_name_snapshot = schedule.group_name
        payment.sale_trainer_id_snapshot = schedule.trainer_id
        payment.sale_attribution_source = "target_group_regular_trainer"
    else:
        payment.target_group_name_snapshot = canonical_group.name
        payment.sale_trainer_id_snapshot = canonical_group.responsible_trainer_id
        payment.sale_attribution_source = "training_group_responsible_trainer"
    payment.target_location_id_snapshot = schedule.location_id
    payment.target_location_name_snapshot = schedule.location.name
    payment.target_trainer_id_snapshot = schedule.trainer_id
    payment.target_trainer_name_snapshot = _trainer_snapshot_name(schedule.trainer)
    payment.target_training_type_id_snapshot = schedule.training_type_id
    payment.target_training_type_kind_snapshot = schedule.training_type.kind


def _validate_payment_conversion_target_for_confirm(
    *,
    payment: Payment,
    subscription: Subscription,
    club_id: int,
):
    if not payment.target_schedule_id:
        return None
    canonical_group_target = payment.target_training_group_id is not None
    schedule = _validate_group_conversion_target(
        club_id=club_id,
        tariff=subscription.tariff,
        target_schedule_id=payment.target_schedule_id,
        target_start_date=payment.target_start_date,
        seller_trainer_id=None,
        for_confirmation=True,
        allow_inactive_target_trainer=canonical_group_target,
    )
    if (
        canonical_group_target
        and schedule.training_group_id != payment.target_training_group_id
    ):
        raise BusinessLogicError(
            "Целевая группа изменилась, выберите группу заново",
            code="payment_conversion_target_reselect_required",
        )
    if (
        schedule.location_id != payment.target_location_id_snapshot
        or schedule.training_type_id != payment.target_training_type_id_snapshot
    ):
        raise BusinessLogicError(
            "Целевая группа изменилась, выберите группу заново",
            code="payment_conversion_target_reselect_required",
        )
    return schedule


def _lock_and_validate_payment_conversion_enrollment_for_confirm(
    *,
    payment: Payment,
    club_id: int,
) -> None:
    """Keep pending payment-owned admission ownership immutable through confirmation."""
    if payment.conversion_enrollment_id is None:
        return

    from apps.attendance.models import ScheduleEnrollment

    if payment.conversion_group_membership_id is not None:
        from apps.attendance.models import TrainingGroupMembership

        membership = (
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=payment.conversion_group_membership_id)
            .first()
        )
        if (
            membership is None
            or payment.target_group_membership_id != membership.id
            or payment.target_training_group_id != membership.training_group_id
            or membership.student_id != payment.student_id
            or membership.starts_on != payment.target_start_date
            or membership.authority
            != TrainingGroupMembership.Authority.PAYMENT_OWNED
        ):
            raise BusinessLogicError(
                "Payment ownership does not match the canonical group membership.",
                code="payment_conversion_enrollment_invalid",
            )

    enrollment = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=payment.conversion_enrollment_id)
        .first()
    )
    if (
        enrollment is None
        or payment.target_schedule_id is None
        or payment.target_start_date is None
        or enrollment.club_id != club_id
        or enrollment.student_id != payment.student_id
        or enrollment.schedule_id != payment.target_schedule_id
        or enrollment.starts_on != payment.target_start_date
        or enrollment.status != ScheduleEnrollment.Status.ACTIVE
        or (
            payment.conversion_group_membership_id is None
            and enrollment.created_from
            != ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        )
        or (
            payment.conversion_group_membership_id is not None
            and (
                enrollment.created_from
                != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
                or enrollment.training_group_membership_id
                != payment.conversion_group_membership_id
            )
        )
    ):
        raise BusinessLogicError(
            "Операционная запись оплаты изменилась и требует ручной коррекции",
            code="payment_conversion_enrollment_invalid",
        )


def _enroll_paid_conversion_target(
    *,
    club_id: int,
    payment: Payment,
) -> ScheduleEnrollment | None:
    if not payment.target_schedule_id:
        return None

    from apps.attendance.models import ScheduleEnrollment
    from apps.attendance.services.enrollment import enroll_student_in_schedule

    existing = (
        ScheduleEnrollment.objects.for_club(club_id)
        .select_for_update()
        .filter(
            student_id=payment.student_id,
            schedule_id=payment.target_schedule_id,
            ends_on__isnull=True,
            status__in=[
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.Status.TRIAL,
                ScheduleEnrollment.Status.FROZEN,
            ],
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        return None

    return enroll_student_in_schedule(
        club_id=club_id,
        student_id=payment.student_id,
        schedule_id=payment.target_schedule_id,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=payment.target_start_date,
        ends_on=None,
        created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    )
