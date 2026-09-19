from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.models import (
    _TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN,
    Schedule,
    TrainingGroup,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupRolloutEvent,
    TrainingGroupRolloutState,
)
from apps.billing.models import BankPaymentOrder, Payment, PaymentRefundCase, Subscription, TrainingType
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer

_ALLOWED_TRANSITIONS = {
    (TrainingGroupRolloutState.Mode.OFF, TrainingGroupRolloutState.Mode.RECONCILING),
    (TrainingGroupRolloutState.Mode.SHADOW, TrainingGroupRolloutState.Mode.RECONCILING),
    (TrainingGroupRolloutState.Mode.ACTIVE, TrainingGroupRolloutState.Mode.CONTAINMENT),
    (TrainingGroupRolloutState.Mode.CONTAINMENT, TrainingGroupRolloutState.Mode.RECONCILING),
    (TrainingGroupRolloutState.Mode.RECONCILING, TrainingGroupRolloutState.Mode.SHADOW),
    (TrainingGroupRolloutState.Mode.RECONCILING, TrainingGroupRolloutState.Mode.OFF),
    (TrainingGroupRolloutState.Mode.RECONCILING, TrainingGroupRolloutState.Mode.CONTAINMENT),
    (TrainingGroupRolloutState.Mode.SHADOW, TrainingGroupRolloutState.Mode.ACTIVE),
    (TrainingGroupRolloutState.Mode.SHADOW, TrainingGroupRolloutState.Mode.CONTAINMENT),
    (TrainingGroupRolloutState.Mode.CONTAINMENT, TrainingGroupRolloutState.Mode.SHADOW),
}


def _save_training_group(group: TrainingGroup) -> TrainingGroup:
    try:
        group.full_clean()
    except ValidationError as exc:
        raise BusinessLogicError(
            "; ".join(exc.messages) or "Invalid training group.",
            code="invalid_training_group",
        ) from exc
    group.save()
    return group


def create_training_group(
    *,
    club_id: int,
    name: str,
    training_type_id: int,
    location_id: int,
    responsible_trainer_id: int,
    actor_user_id: int | None,
) -> TrainingGroup:
    """Create one active canonical group with explicit same-club identity."""
    normalized_name = name.strip()
    if not normalized_name:
        raise BusinessLogicError("Training group name is required.", code="training_group_name_required")

    from apps.attendance.services.training_group_memberships import (
        assert_training_group_new_writes_enabled,
        lock_training_group_mutation_scope,
    )

    with transaction.atomic():
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        assert_training_group_new_writes_enabled()
        if rollout_state.mode not in {
            TrainingGroupRolloutState.Mode.SHADOW,
            TrainingGroupRolloutState.Mode.ACTIVE,
        }:
            raise BusinessLogicError(
                "Training group writes are disabled for this rollout mode.",
                code="training_group_writes_disabled",
            )
        training_type = (
            TrainingType.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=training_type_id, kind=TrainingType.Kind.GROUP)
            .first()
        )
        if training_type is None:
            raise BusinessLogicError(
                "Training group requires a club group training type.",
                code="training_group_training_type_invalid",
            )
        location = Location.objects.filter(id=location_id, club_id=club_id).select_for_update(of=("self",)).first()
        if location is None:
            raise BusinessLogicError("Location does not belong to this club.", code="location_club_mismatch")
        trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=responsible_trainer_id, is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError(
                "An active responsible trainer is required.",
                code="training_group_responsible_trainer_invalid",
            )
        return _save_training_group(
            TrainingGroup(
                club_id=club_id,
                name=normalized_name,
                training_type=training_type,
                location=location,
                responsible_trainer=trainer,
                status=TrainingGroup.Status.ACTIVE,
                created_by_id=actor_user_id,
            )
        )


def reassign_training_group_responsibility(
    *,
    club_id: int,
    training_group_id: int,
    responsible_trainer_id: int,
) -> TrainingGroup:
    """Change future group responsibility without touching financial snapshots."""
    from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

    with transaction.atomic():
        lock_training_group_mutation_scope(club_id=club_id)
        trainer = (
            Trainer.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=responsible_trainer_id, is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError(
                "An active responsible trainer is required.",
                code="training_group_responsible_trainer_invalid",
            )
        group = (
            TrainingGroup.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=training_group_id)
            .first()
        )
        if group is None:
            raise BusinessLogicError("Training group was not found.", code="training_group_not_found")
        if group.status == TrainingGroup.Status.ARCHIVED:
            raise BusinessLogicError(
                "An archived training group cannot be reassigned.",
                code="training_group_archived",
            )
        group.responsible_trainer = trainer
        return _save_training_group(group)


_OPEN_ARCHIVE_BANK_ORDER_STATUSES = (
    BankPaymentOrder.Status.CREATED,
    BankPaymentOrder.Status.PENDING,
    BankPaymentOrder.Status.AUTHORIZED,
    BankPaymentOrder.Status.MANUAL_REVIEW,
)


def _validate_archive_event_inputs(*, rationale: str, idempotency_key: str) -> tuple[str, str]:
    clean_rationale = rationale.strip()
    clean_key = idempotency_key.strip()
    if not clean_rationale or len(clean_rationale) > 500:
        raise BusinessLogicError(
            "A bounded archive rationale is required.", code="training_group_rationale_invalid"
        )
    if not clean_key or len(clean_key) > 120:
        raise BusinessLogicError(
            "A bounded archive idempotency key is required.", code="training_group_idempotency_invalid"
        )
    return clean_rationale, clean_key


def _lock_training_group_archive_financial_roots(
    *, club_id: int, training_group_id: int
) -> tuple[list[Payment], list[BankPaymentOrder]]:
    """Lock the archive financial roots before group, slot, or membership rows.

    Archive has no financial side effect, but it must serialize with pending
    payment/order completion before deciding that the group has no open
    financial state.  This mirrors the documented refund -> order -> payment
    -> subscription hierarchy used by reconciliation and payment lifecycle
    writers.
    """
    payment_ids = list(
        Payment.objects.for_club(club_id)
        .filter(target_training_group_id=training_group_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if not payment_ids:
        return [], []
    list(
        PaymentRefundCase.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(order__payment_id__in=payment_ids)
        .order_by("id")
    )
    orders = list(
        BankPaymentOrder.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(payment_id__in=payment_ids)
        .order_by("id")
    )
    payments = list(
        Payment.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=payment_ids)
        .order_by("id")
    )
    subscription_ids = sorted({payment.subscription_id for payment in payments if payment.subscription_id})
    if subscription_ids:
        list(
            Subscription.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id__in=subscription_ids)
            .order_by("id")
        )
    return payments, orders


def archive_training_group(
    *,
    club_id: int,
    training_group_id: int,
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
) -> TrainingGroup:
    """Archive a fully closed group through one explicit, audited operation.

    Archive never closes slots, memberships, orders, payments, or any other
    lifecycle row.  Its read checks are performed under the same leading and
    financial locks as writers so a concurrent payment/order completion cannot
    turn a passed precondition into an implicit financial effect.
    """
    clean_rationale, clean_key = _validate_archive_event_inputs(
        rationale=rationale,
        idempotency_key=idempotency_key,
    )
    from apps.attendance.services.training_group_memberships import lock_training_group_payment_scope

    with transaction.atomic():
        lock_training_group_payment_scope(club_id=club_id)
        payments, orders = _lock_training_group_archive_financial_roots(
            club_id=club_id,
            training_group_id=training_group_id,
        )
        group = (
            TrainingGroup.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=training_group_id)
            .first()
        )
        if group is None:
            raise BusinessLogicError("Training group was not found.", code="training_group_not_found")

        existing_event = (
            TrainingGroupMappingEvent.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(idempotency_key=clean_key)
            .first()
        )
        if existing_event is not None:
            if (
                existing_event.action != "archived"
                or existing_event.training_group_id != group.id
                or group.status != TrainingGroup.Status.ARCHIVED
            ):
                raise BusinessLogicError(
                    "Archive idempotency key conflicts with this request.",
                    code="training_group_archive_idempotency_conflict",
                )
            return group
        if group.status == TrainingGroup.Status.ARCHIVED:
            raise BusinessLogicError(
                "Training group is already archived; retry its original archive key.",
                code="training_group_archive_already_archived",
            )

        active_slot_ids = list(
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(training_group_id=group.id, is_active=True)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if active_slot_ids:
            raise BusinessLogicError(
                "Deactivate every group slot before archive.",
                code="training_group_archive_active_slots",
            )
        open_membership_ids = list(
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                training_group_id=group.id,
                ends_on__isnull=True,
                status__in=[
                    TrainingGroupMembership.Status.ACTIVE,
                    TrainingGroupMembership.Status.FROZEN,
                ],
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        if open_membership_ids:
            raise BusinessLogicError(
                "Close every group membership before archive.",
                code="training_group_archive_open_memberships",
            )
        if any(payment.status == Payment.Status.PENDING for payment in payments):
            raise BusinessLogicError(
                "Pending payments must be closed before archive.",
                code="training_group_archive_pending_payments",
            )
        if any(order.status in _OPEN_ARCHIVE_BANK_ORDER_STATUSES for order in orders):
            raise BusinessLogicError(
                "Pending bank payment orders must be closed before archive.",
                code="training_group_archive_pending_orders",
            )

        previous_snapshot = {
            "training_group_id": group.id,
            "status": group.status,
            "active_slot_ids": active_slot_ids,
            "open_membership_ids": open_membership_ids,
            "pending_payment_ids": [payment.id for payment in payments if payment.status == Payment.Status.PENDING],
            "open_bank_order_ids": [
                order.id for order in orders if order.status in _OPEN_ARCHIVE_BANK_ORDER_STATUSES
            ],
        }
        group.status = TrainingGroup.Status.ARCHIVED
        group = _save_training_group(group)
        event = TrainingGroupMappingEvent(
            club_id=club_id,
            batch_id=uuid4(),
            training_group=group,
            schedule=None,
            action="archived",
            previous_group_snapshot=previous_snapshot,
            new_group_snapshot={"training_group_id": group.id, "status": group.status},
            actor_id=actor_user_id,
            rationale=clean_rationale,
            idempotency_key=clean_key,
        )
        try:
            event.full_clean()
            event.save()
        except ValidationError as exc:
            raise BusinessLogicError(
                "; ".join(exc.messages) or "Training group archive audit could not be recorded.",
                code="training_group_archive_audit_invalid",
            ) from exc
        return group


def approved_reconciliation_rollout_gate_digest(
    *,
    club_id: int,
    additional_roster_delta_digests: list[str] | None = None,
    additional_roster_horizon_digests: list[str] | None = None,
) -> str:
    """Return the owner-approved digest for the current reconciling epoch only."""
    epoch = (
        TrainingGroupRolloutEvent.objects.for_club(club_id)
        .filter(new_mode=TrainingGroupRolloutState.Mode.RECONCILING)
        .order_by("-id")
        .values_list("created_at", flat=True)
        .first()
    )
    if epoch is None:
        return ""
    snapshots = list(
        TrainingGroupMappingEvent.objects.for_club(club_id)
        .filter(action="apply_completed", schedule_id__isnull=True, created_at__gte=epoch)
        .order_by("id")
        .values_list("new_group_snapshot", flat=True)
    )
    if (
        not snapshots
        and not additional_roster_delta_digests
        and not additional_roster_horizon_digests
    ):
        return ""
    material = {
        "roster_delta_digests": [snapshot.get("roster_delta_digest", "") for snapshot in snapshots]
        + (additional_roster_delta_digests or []),
        "roster_horizon_digests": [
            snapshot.get("approved_roster_horizon_digest", "") for snapshot in snapshots
        ]
        + (additional_roster_horizon_digests or []),
    }
    canonical = json.dumps(material, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _operational_audit_is_deferred_provider_queue_only(*, operational_audit: dict[str, object]) -> bool:
    """Recognize the replay queue that can only drain after a shadow exit commits."""

    deferred_count = operational_audit.get("deferred_provider_event_count")
    invalid_state_counts = operational_audit.get("invalid_state_counts")
    return (
        isinstance(deferred_count, int)
        and deferred_count > 0
        and isinstance(invalid_state_counts, dict)
        and invalid_state_counts == {"deferred_provider_event": deferred_count}
        and operational_audit.get("legacy_unlinked_pending_manual_count") == 0
    )


def _assert_forward_rollout_gates(
    *,
    club_id: int,
    approved_roster_delta_digest: str,
    allow_deferred_provider_queue: bool = False,
) -> None:
    from apps.attendance.services.training_group_reconciliation import audit_training_groups
    from apps.billing.management.commands.audit_operational_admissions import _audit_club
    from apps.clubs.models import Club

    club = Club.objects.get(id=club_id)
    group_audit = audit_training_groups(club=club)
    operational_audit = _audit_club(club=club)
    operational_audit_allowed = bool(operational_audit["clean"]) or (
        allow_deferred_provider_queue
        and _operational_audit_is_deferred_provider_queue_only(operational_audit=operational_audit)
    )
    if not group_audit["valid"] or not operational_audit_allowed:
        raise BusinessLogicError(
            "Fail-closed rollout audits must pass before forward activation.",
            code="training_group_rollout_prerequisite_missing",
        )
    epoch = (
        TrainingGroupRolloutEvent.objects.for_club(club_id)
        .filter(new_mode=TrainingGroupRolloutState.Mode.RECONCILING)
        .order_by("-id")
        .values_list("created_at", flat=True)
        .first()
    )
    snapshots = (
        list(
            TrainingGroupMappingEvent.objects.for_club(club_id)
            .filter(
                action="apply_completed",
                schedule_id__isnull=True,
                created_at__gte=epoch,
            )
            .order_by("id")
            .values_list("new_group_snapshot", flat=True)
        )
        if epoch is not None
        else []
    )
    from apps.attendance.training_group_roster import snapshot_expected_roster_horizon

    for snapshot in snapshots:
        try:
            as_of_date = date.fromisoformat(snapshot["approved_roster_horizon_as_of"])
            schedule_ids = [int(value) for value in snapshot["selected_schedule_ids"]]
            approved_digest = snapshot["approved_roster_horizon_digest"]
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessLogicError(
                "Approved roster horizon evidence is missing.",
                code="training_group_owner_delta_mismatch",
            ) from exc
        current = snapshot_expected_roster_horizon(
            club=club,
            schedule_ids=schedule_ids,
            as_of_date=as_of_date,
        )
        if current["horizon_digest"] != approved_digest:
            raise BusinessLogicError(
                "Current roster differs from the owner-approved reconciliation horizon.",
                code="training_group_owner_delta_mismatch",
            )
    has_apply = bool(approved_reconciliation_rollout_gate_digest(club_id=club_id))
    if has_apply and approved_roster_delta_digest != approved_reconciliation_rollout_gate_digest(club_id=club_id):
        raise BusinessLogicError(
            "Owner-approved reconciliation roster delta is missing or stale.",
            code="training_group_rollout_prerequisite_missing",
        )


def _register_deferred_provider_event_replay(*, club_id: int) -> None:
    """Register the exit replay so an idempotent retry can recover enqueue failure."""

    def enqueue_deferred_provider_event_replay() -> None:
        from django_q.tasks import async_task

        async_task(
            "apps.billing.tasks.replay_deferred_bank_payment_provider_events_task",
            club_id=club_id,
        )

    transaction.on_commit(enqueue_deferred_provider_event_replay)


def transition_training_group_rollout(
    *,
    club_id: int,
    target_mode: str,
    actor_id: int | None,
    rationale: str,
    idempotency_key: str,
    forward_audit_passed: bool = False,
    forward_audit_failed: bool = False,
    canonical_mutations_committed: bool = False,
    repair_audits_passed: bool = False,
    approved_roster_delta_digest: str = "",
) -> TrainingGroupRolloutState:
    """Apply one audited, row-locked rollout transition without group writes."""
    clean_rationale = rationale.strip()
    clean_key = idempotency_key.strip()
    if not clean_rationale or len(clean_rationale) > 500:
        raise BusinessLogicError("A bounded rollout rationale is required.", code="training_group_rationale_invalid")
    if not clean_key or len(clean_key) > 120:
        raise BusinessLogicError("A bounded idempotency key is required.", code="training_group_idempotency_invalid")
    try:
        target = TrainingGroupRolloutState.Mode(target_mode)
    except ValueError as exc:
        raise BusinessLogicError("Unknown rollout mode.", code="training_group_rollout_mode_invalid") from exc

    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_payment_scope

        state = lock_training_group_payment_scope(club_id=club_id)
        existing = (
            TrainingGroupRolloutEvent.objects.for_club(club_id)
            .filter(idempotency_key=clean_key)
            .first()
        )
        if existing:
            if existing.new_mode != target or state.mode != existing.new_mode:
                raise BusinessLogicError(
                    "Rollout idempotency key conflicts with this transition.",
                    code="training_group_rollout_idempotency_conflict",
                )
            if (
                existing.previous_mode == TrainingGroupRolloutState.Mode.RECONCILING
                and existing.new_mode
                in {
                    TrainingGroupRolloutState.Mode.OFF,
                    TrainingGroupRolloutState.Mode.SHADOW,
                    TrainingGroupRolloutState.Mode.CONTAINMENT,
                }
            ):
                _register_deferred_provider_event_replay(club_id=club_id)
            return state

        previous = state.mode
        if (previous, target) not in _ALLOWED_TRANSITIONS:
            raise BusinessLogicError(
                "Rollout transition is not allowed.", code="training_group_rollout_transition_invalid"
            )
        if previous == TrainingGroupRolloutState.Mode.RECONCILING:
            applied_batches_exist = TrainingGroupMappingEvent.objects.for_club(club_id).filter(
                action="apply_completed", schedule_id__isnull=True
            ).exists()
            if target == TrainingGroupRolloutState.Mode.SHADOW:
                if not forward_audit_passed:
                    raise BusinessLogicError(
                        "Reconciliation requires a passing forward audit before shadow.",
                        code="training_group_rollout_prerequisite_missing",
                    )
                _assert_forward_rollout_gates(
                    club_id=club_id,
                    approved_roster_delta_digest=approved_roster_delta_digest,
                    allow_deferred_provider_queue=True,
                )
            if target == TrainingGroupRolloutState.Mode.OFF and (
                state.reconciling_from_mode != TrainingGroupRolloutState.Mode.OFF
                or canonical_mutations_committed
                or applied_batches_exist
            ):
                raise BusinessLogicError(
                    "Only an uncommitted off reconciliation may abort to off.",
                    code="training_group_rollout_prerequisite_missing",
                )
            if target == TrainingGroupRolloutState.Mode.CONTAINMENT and not (
                (canonical_mutations_committed or applied_batches_exist) and forward_audit_failed
            ):
                raise BusinessLogicError(
                    "Containment requires committed canonical work and a failed forward audit.",
                    code="training_group_rollout_prerequisite_missing",
                )
        if (
            previous == TrainingGroupRolloutState.Mode.CONTAINMENT
            and target == TrainingGroupRolloutState.Mode.SHADOW
            and not repair_audits_passed
        ):
            raise BusinessLogicError(
                "Containment requires repaired audits before shadow.",
                code="training_group_rollout_prerequisite_missing",
            )
        if previous == TrainingGroupRolloutState.Mode.CONTAINMENT and target == TrainingGroupRolloutState.Mode.SHADOW:
            _assert_forward_rollout_gates(
                club_id=club_id,
                approved_roster_delta_digest=approved_roster_delta_digest,
            )
        if previous == TrainingGroupRolloutState.Mode.SHADOW and target == TrainingGroupRolloutState.Mode.ACTIVE:
            _assert_forward_rollout_gates(
                club_id=club_id,
                approved_roster_delta_digest=approved_roster_delta_digest,
            )

        reconciling_from_mode = previous if target == TrainingGroupRolloutState.Mode.RECONCILING else ""
        TrainingGroupRolloutState.objects.for_club(club_id).filter(pk=state.pk)._update_for_transition(
            transition_token=_TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN,
            mode=target,
            reconciling_from_mode=reconciling_from_mode,
            updated_at=timezone.now(),
        )
        try:
            TrainingGroupRolloutEvent.objects.create(
                club_id=club_id,
                previous_mode=previous,
                new_mode=target,
                actor_id=actor_id,
                rationale=clean_rationale,
                idempotency_key=clean_key,
            )
        except IntegrityError as exc:
            raise BusinessLogicError(
                "Rollout idempotency key conflicts with this transition.",
                code="training_group_rollout_idempotency_conflict",
            ) from exc
        state.mode = target
        state.reconciling_from_mode = reconciling_from_mode
        if previous == TrainingGroupRolloutState.Mode.RECONCILING and target in {
            TrainingGroupRolloutState.Mode.OFF,
            TrainingGroupRolloutState.Mode.SHADOW,
            TrainingGroupRolloutState.Mode.CONTAINMENT,
        }:
            _register_deferred_provider_event_replay(club_id=club_id)
        return state


def transition_training_group_rollout_for_owner(
    *,
    club_id: int,
    target_mode: str,
    actor_id: int,
    rationale: str,
    idempotency_key: str,
    rollout_gate_digest: str,
) -> TrainingGroupRolloutState:
    """Owner adapter derives transition intent from actual audits, not payload flags."""
    from apps.attendance.services.training_group_reconciliation import audit_training_groups
    from apps.billing.management.commands.audit_operational_admissions import _audit_club
    from apps.clubs.models import Club

    group_audit = audit_training_groups(club=Club.objects.get(id=club_id))
    operational_audit = _audit_club(club=Club.objects.get(id=club_id))
    audits_clean = group_audit["valid"] and operational_audit["clean"]
    target_is_reconciling_shadow = (
        TrainingGroupRolloutState.objects.for_club(club_id)
        .filter(mode=TrainingGroupRolloutState.Mode.RECONCILING)
        .exists()
        and target_mode == TrainingGroupRolloutState.Mode.SHADOW
    )
    deferred_queue_only = group_audit["valid"] and _operational_audit_is_deferred_provider_queue_only(
        operational_audit=operational_audit
    )
    deferred_queue_shadow_exit = target_is_reconciling_shadow and deferred_queue_only
    return transition_training_group_rollout(
        club_id=club_id,
        target_mode=target_mode,
        actor_id=actor_id,
        rationale=rationale,
        idempotency_key=idempotency_key,
        approved_roster_delta_digest=rollout_gate_digest,
        forward_audit_passed=audits_clean or deferred_queue_shadow_exit,
        forward_audit_failed=not audits_clean and not deferred_queue_only,
        canonical_mutations_committed=bool(
            approved_reconciliation_rollout_gate_digest(club_id=club_id)
        ),
        repair_audits_passed=audits_clean,
    )


def get_training_group_rollout_state(*, club_id: int) -> TrainingGroupRolloutState:
    """Return the per-club state or fail closed for affected future writers."""
    state = TrainingGroupRolloutState.objects.for_club(club_id).first()
    if state is None:
        raise BusinessLogicError(
            "Training group rollout state is missing for this club.",
            code="training_group_rollout_state_missing",
        )
    return state
