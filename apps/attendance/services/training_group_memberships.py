"""Canonical Training Group membership lifecycle and projection writes.

``ScheduleEnrollment`` remains the compatibility row for a concrete slot.  This
module is the only writer for the durable membership and its generated
``group_projection`` rows; callers must never use a projection as a second
lifecycle authority.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.attendance.models import (
    Schedule,
    ScheduleEnrollment,
    TrainingGroup,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.clubs.models import Club
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student


def training_group_new_writes_enabled() -> bool:
    """Return the global emergency gate for canonical TrainingGroup writes."""
    return bool(settings.TRAINING_GROUP_NEW_WRITES_ENABLED)


def assert_training_group_new_writes_enabled() -> None:
    """Fail before creating any new canonical group root or projection family.

    This deliberately is not part of ``lock_training_group_mutation_scope``:
    existing payment, provider, refund, cancellation, and membership lifecycle
    rows must remain operable while the emergency switch is off.
    """
    if not training_group_new_writes_enabled():
        raise BusinessLogicError(
            "Training group writes are temporarily disabled.",
            code="training_group_writes_disabled",
        )


def lock_training_group_payment_scope(*, club_id: int, no_key: bool = False) -> TrainingGroupRolloutState:
    """Acquire payroll and rollout locks before any canonical financial root."""
    from apps.trainers.services import lock_trainer_payroll_mutation_scope

    if no_key:
        # Same writer serialization, compatible with deferred Club FK checks
        # from existing subscription-locked freeze owners.
        Club.objects.select_for_update(no_key=True).only("id").get(id=club_id)
    else:
        lock_trainer_payroll_mutation_scope(club_id=club_id)
    rollout_state = (
        TrainingGroupRolloutState.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .first()
    )
    if rollout_state is None:
        raise BusinessLogicError(
            "Training group rollout state is missing for this club.",
            code="training_group_rollout_state_missing",
        )
    return rollout_state


def lock_training_group_mutation_scope(*, club_id: int, no_key: bool = False) -> TrainingGroupRolloutState:
    """Acquire the leading writer locks and reject lifecycle writes while reconciling."""
    rollout_state = lock_training_group_payment_scope(club_id=club_id, no_key=no_key)
    if rollout_state.mode == TrainingGroupRolloutState.Mode.RECONCILING:
        raise BusinessLogicError(
            "Training group changes are temporarily reconciling.",
            code="training_group_reconciling",
        )
    return rollout_state


def assert_training_group_expanding_write_allowed(
    *, rollout_state: TrainingGroupRolloutState
) -> None:
    """Permit eligibility-expanding writes only in the two canonical write modes."""
    if rollout_state.mode not in {
        TrainingGroupRolloutState.Mode.SHADOW,
        TrainingGroupRolloutState.Mode.ACTIVE,
    }:
        raise BusinessLogicError(
            "Training group writes are disabled for this rollout mode.",
            code="training_group_writes_disabled",
        )
    assert_training_group_new_writes_enabled()


def _membership_snapshot(membership: TrainingGroupMembership) -> dict[str, str | int | None]:
    return {
        "membership_id": membership.id,
        "training_group_id": membership.training_group_id,
        "student_id": membership.student_id,
        "status": membership.status,
        "starts_on": membership.starts_on.isoformat(),
        "ends_on": membership.ends_on.isoformat() if membership.ends_on else None,
        "source": membership.source,
        "authority": membership.authority,
    }


def _validate_event_inputs(*, rationale: str, idempotency_key: str) -> tuple[str, str]:
    normalized_rationale = rationale.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_rationale or len(normalized_rationale) > 500:
        raise BusinessLogicError(
            "A bounded membership rationale is required.",
            code="training_group_rationale_invalid",
        )
    if not normalized_key or len(normalized_key) > 120:
        raise BusinessLogicError(
            "A bounded membership idempotency key is required.",
            code="training_group_idempotency_invalid",
        )
    return normalized_rationale, normalized_key


def _save_membership(membership: TrainingGroupMembership) -> TrainingGroupMembership:
    try:
        membership.full_clean()
    except ValidationError as exc:
        raise BusinessLogicError(
            "; ".join(exc.messages) or "Invalid training group membership.",
            code="invalid_training_group_membership",
        ) from exc
    membership.save()
    return membership


def _save_projection(projection: ScheduleEnrollment) -> ScheduleEnrollment:
    try:
        projection.full_clean()
    except ValidationError as exc:
        raise BusinessLogicError(
            "; ".join(exc.messages) or "Invalid training group projection.",
            code="invalid_training_group_projection",
        ) from exc
    projection.save()
    return projection


def _lock_group_scope(
    *,
    club_id: int,
    training_group_ids: list[int],
    student_id: int,
) -> tuple[dict[int, TrainingGroup], Student, list[Schedule]]:
    """Lock student, groups, slots, memberships, then projections in plan order."""
    student = (
        Student.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=student_id, deleted_at__isnull=True)
        .first()
    )
    if student is None:
        raise BusinessLogicError("Student does not belong to this club.", code="student_club_mismatch")

    normalized_group_ids = sorted(set(training_group_ids))
    groups = {
        group.id: group
        for group in TrainingGroup.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=normalized_group_ids)
        .order_by("id")
    }
    if set(groups) != set(normalized_group_ids):
        raise BusinessLogicError("Training group does not belong to this club.", code="training_group_club_mismatch")

    schedules = list(
        Schedule.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            training_group_id__in=normalized_group_ids,
            one_time_date__isnull=True,
        )
        .order_by("id")
    )
    return groups, student, schedules


def _active_group_schedules(*, schedules: list[Schedule], training_group_id: int) -> list[Schedule]:
    return [
        schedule
        for schedule in schedules
        if schedule.training_group_id == training_group_id and schedule.is_active
    ]


def _backfilled_source_schedule_ids(*, membership: TrainingGroupMembership) -> set[int]:
    """Return immutable legacy source slots linked by S6 audit events."""
    source_enrollment_ids = TrainingGroupMembershipEvent.objects.for_club(membership.club_id).filter(
        membership_id=membership.id,
        action__in=["backfilled", "source_linked"],
        source_enrollment_id__isnull=False,
    ).values_list("source_enrollment_id", flat=True)
    return set(
        ScheduleEnrollment.objects.for_club(membership.club_id)
        .filter(id__in=source_enrollment_ids)
        .values_list("schedule_id", flat=True)
    )


def _sync_membership_projections(
    *,
    membership: TrainingGroupMembership,
    schedules: list[Schedule],
    create_missing: bool,
) -> list[ScheduleEnrollment]:
    """Synchronize immutable-slot projections while membership is lifecycle owner."""
    projections = list(
        ScheduleEnrollment.objects.for_club(membership.club_id)
        .select_for_update(of=("self",))
        .filter(training_group_membership_id=membership.id)
        .order_by("schedule_id", "id")
    )
    projections_by_schedule = {projection.schedule_id: projection for projection in projections}
    backfilled_source_schedule_ids = _backfilled_source_schedule_ids(membership=membership)

    if create_missing:
        for schedule in _active_group_schedules(
            schedules=schedules,
            training_group_id=membership.training_group_id,
        ):
            projection = projections_by_schedule.get(schedule.id)
            if projection is not None or schedule.id in backfilled_source_schedule_ids:
                continue
            conflicting = (
                ScheduleEnrollment.objects.for_club(membership.club_id)
                .select_for_update(of=("self",))
                .filter(
                    student_id=membership.student_id,
                    schedule_id=schedule.id,
                    ends_on__isnull=True,
                    status__in=[
                        ScheduleEnrollment.Status.ACTIVE,
                        ScheduleEnrollment.Status.TRIAL,
                        ScheduleEnrollment.Status.FROZEN,
                    ],
                )
                .exclude(training_group_membership_id=membership.id)
                .first()
            )
            if conflicting is not None:
                raise BusinessLogicError(
                    "An exact schedule roster row conflicts with this group membership.",
                    code="training_group_projection_conflict",
                )
            projection = ScheduleEnrollment(
                club_id=membership.club_id,
                student_id=membership.student_id,
                schedule_id=schedule.id,
                training_group_membership=membership,
                status=membership.status,
                starts_on=membership.starts_on,
                ends_on=membership.ends_on,
                created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
            _save_projection(projection)
            projections.append(projection)
            projections_by_schedule[schedule.id] = projection

    for projection in projections:
        if (
            projection.status != membership.status
            or projection.starts_on != membership.starts_on
            or projection.ends_on != membership.ends_on
        ):
            projection.status = membership.status
            projection.starts_on = membership.starts_on
            projection.ends_on = membership.ends_on
            _save_projection(projection)
    return projections


def _record_membership_event(
    *,
    membership: TrainingGroupMembership,
    action: str,
    effective_date: date,
    previous_snapshot: dict[str, str | int | None],
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
    source_enrollment_id: int | None = None,
    source_payment_id: int | None = None,
) -> TrainingGroupMembershipEvent:
    try:
        return TrainingGroupMembershipEvent.objects.create(
            club_id=membership.club_id,
            membership=membership,
            action=action,
            effective_date=effective_date,
            previous_state_snapshot=previous_snapshot,
            new_state_snapshot=_membership_snapshot(membership),
            actor_id=actor_user_id,
            rationale=rationale,
            idempotency_key=idempotency_key,
            source_enrollment_id=source_enrollment_id,
            source_payment_id=source_payment_id,
        )
    except IntegrityError as exc:
        raise BusinessLogicError(
            "Membership idempotency key conflicts with this lifecycle action.",
            code="training_group_membership_idempotency_conflict",
        ) from exc


def create_training_group_membership(
    *,
    club_id: int,
    student_id: int,
    training_group_id: int,
    starts_on: date,
    source: str,
    status: str = TrainingGroupMembership.Status.ACTIVE,
    authority: str = TrainingGroupMembership.Authority.INDEPENDENT,
    actor_user_id: int | None = None,
    rationale: str = "canonical_membership_create",
    idempotency_key: str,
    source_enrollment_id: int | None = None,
    source_payment_id: int | None = None,
) -> TrainingGroupMembership:
    """Create one open membership and one projection for every active mapped slot."""
    rationale, idempotency_key = _validate_event_inputs(
        rationale=rationale,
        idempotency_key=idempotency_key,
    )
    if status not in {TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN}:
        raise BusinessLogicError("Open membership status is invalid.", code="invalid_training_group_membership_status")
    if authority == TrainingGroupMembership.Authority.PAYMENT_OWNED:
        # Payment ownership needs the atomic payment-side linkage and event
        # snapshots supplied by the cutover workflow.  This generic public
        # lifecycle service must never create an audit-invalid orphan.
        raise BusinessLogicError(
            "Payment-owned memberships must be established by the payment lifecycle.",
            code="training_group_payment_ownership_required",
        )

    with transaction.atomic():
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        existing_event = (
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .select_related("membership")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_event is not None:
            membership = existing_event.membership
            if (
                existing_event.action != "created"
                or membership.student_id != student_id
                or membership.training_group_id != training_group_id
                or membership.starts_on != starts_on
                or membership.source != source
                or membership.status != status
                or membership.authority != authority
                or existing_event.source_enrollment_id != source_enrollment_id
                or existing_event.source_payment_id != source_payment_id
            ):
                raise BusinessLogicError(
                    "Membership idempotency key conflicts with this create action.",
                    code="training_group_membership_idempotency_conflict",
                )
            return membership

        assert_training_group_expanding_write_allowed(rollout_state=rollout_state)

        groups, _, schedules = _lock_group_scope(
            club_id=club_id,
            training_group_ids=[training_group_id],
            student_id=student_id,
        )
        group = groups[training_group_id]
        if group.status != TrainingGroup.Status.ACTIVE:
            raise BusinessLogicError("Training group is not active.", code="training_group_not_active")

        existing_membership = (
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                student_id=student_id,
                training_group_id=training_group_id,
                ends_on__isnull=True,
                status__in=[TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN],
            )
            .first()
        )
        if existing_membership is not None:
            raise BusinessLogicError(
                "Student already has an open training group membership.",
                code="training_group_membership_exists",
            )

        membership = TrainingGroupMembership(
            club_id=club_id,
            student_id=student_id,
            training_group=group,
            status=status,
            starts_on=starts_on,
            source=source,
            authority=authority,
            created_by_id=actor_user_id,
        )
        _save_membership(membership)
        _sync_membership_projections(
            membership=membership,
            schedules=schedules,
            create_missing=True,
        )
        _record_membership_event(
            membership=membership,
            action="created",
            effective_date=starts_on,
            previous_snapshot={},
            actor_user_id=actor_user_id,
            rationale=rationale,
            idempotency_key=idempotency_key,
            source_enrollment_id=source_enrollment_id,
            source_payment_id=source_payment_id,
        )
        return membership



def _payment_membership_source(*, club_id: int, payment_id: int) -> str:
    from apps.billing.models import Payment

    payment = Payment.objects.for_club(club_id).get(id=payment_id)
    return (
        TrainingGroupMembership.Source.IMPORT
        if payment.origin == Payment.Origin.OPENING
        else TrainingGroupMembership.Source.PAID_CONVERSION
    )


def create_payment_owned_training_group_membership(
    *,
    club_id: int,
    student_id: int,
    training_group_id: int,
    starts_on: date,
    payment_id: int,
    actor_user_id: int | None,
    scope_locked: bool = False,
) -> tuple[TrainingGroupMembership, list[ScheduleEnrollment]]:
    """Create the canonical membership/projection family owned by one payment."""

    idempotency_key = f"payment-owned-membership:{payment_id}"
    with transaction.atomic():
        if not scope_locked:
            lock_training_group_mutation_scope(club_id=club_id)
        existing_event = (
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .select_related("membership")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_event is not None:
            membership = existing_event.membership
            if (
                existing_event.action != "created"
                or existing_event.source_payment_id != payment_id
                or membership.student_id != student_id
                or membership.training_group_id != training_group_id
                or membership.starts_on != starts_on
                or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
            ):
                raise BusinessLogicError(
                    "Payment ownership does not match its canonical membership.",
                    code="training_group_payment_ownership_conflict",
                )
            projections = list(
                ScheduleEnrollment.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .filter(training_group_membership_id=membership.id)
                .order_by("schedule_id", "id")
            )
            return membership, projections

        groups, _, schedules = _lock_group_scope(
            club_id=club_id,
            training_group_ids=[training_group_id],
            student_id=student_id,
        )
        group = groups[training_group_id]
        if group.status != TrainingGroup.Status.ACTIVE:
            raise BusinessLogicError("Training group is not active.", code="training_group_not_active")

        existing_membership = (
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                student_id=student_id,
                training_group_id=training_group_id,
                ends_on__isnull=True,
                status__in=[TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN],
            )
            .first()
        )
        if existing_membership is not None:
            raise BusinessLogicError(
                "Student already has an open training group membership.",
                code="training_group_membership_exists",
            )

        membership = TrainingGroupMembership(
            club_id=club_id,
            student_id=student_id,
            training_group=group,
            status=TrainingGroupMembership.Status.ACTIVE,
            starts_on=starts_on,
            source=_payment_membership_source(club_id=club_id, payment_id=payment_id),
            authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
            created_by_id=actor_user_id,
        )
        _save_membership(membership)
        projections = _sync_membership_projections(
            membership=membership,
            schedules=schedules,
            create_missing=True,
        )
        _record_membership_event(
            membership=membership,
            action="created",
            effective_date=starts_on,
            previous_snapshot={},
            actor_user_id=actor_user_id,
            rationale="payment_owned_admission",
            idempotency_key=idempotency_key,
            source_payment_id=payment_id,
        )
        return membership, projections


def cancel_payment_owned_training_group_membership(
    *,
    club_id: int,
    membership_id: int,
    payment_id: int,
    actor_user_id: int | None,
    rationale: str,
) -> TrainingGroupMembership:
    """Close only the membership owned by a terminal payment lifecycle."""

    idempotency_key = f"payment-owned-membership-cancel:{payment_id}"
    with transaction.atomic():
        lock_training_group_mutation_scope(club_id=club_id)
        existing_event = (
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .select_related("membership")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_event is not None:
            if existing_event.membership_id != membership_id or existing_event.action != "payment_cancelled":
                raise BusinessLogicError(
                    "Payment cancellation conflicts with the membership lifecycle.",
                    code="training_group_payment_ownership_conflict",
                )
            return existing_event.membership

        membership, _, schedules = _get_membership_for_lifecycle(
            club_id=club_id,
            membership_id=membership_id,
        )
        if membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED:
            raise BusinessLogicError(
                "Only payment-owned memberships can be closed by payment lifecycle.",
                code="training_group_payment_ownership_required",
            )
        previous_snapshot = _membership_snapshot(membership)
        if membership.status not in {
            TrainingGroupMembership.Status.ACTIVE,
            TrainingGroupMembership.Status.FROZEN,
        }:
            return membership
        membership.status = TrainingGroupMembership.Status.CANCELLED
        membership.ends_on = membership.starts_on
        _save_membership(membership)
        _sync_membership_projections(
            membership=membership,
            schedules=schedules,
            create_missing=False,
        )
        _record_membership_event(
            membership=membership,
            action="payment_cancelled",
            effective_date=membership.ends_on,
            previous_snapshot=previous_snapshot,
            actor_user_id=actor_user_id,
            rationale=rationale[:500] or "payment_cancelled",
            idempotency_key=idempotency_key,
            source_payment_id=payment_id,
        )
        return membership


def _get_membership_for_lifecycle(
    *,
    club_id: int,
    membership_id: int,
    additional_group_id: int | None = None,
) -> tuple[TrainingGroupMembership, dict[int, TrainingGroup], list[Schedule]]:
    preview = (
        TrainingGroupMembership.objects.for_club(club_id)
        .filter(id=membership_id)
        .values("student_id", "training_group_id")
        .first()
    )
    if preview is None:
        raise BusinessLogicError("Training group membership was not found.", code="training_group_membership_not_found")
    group_ids = [preview["training_group_id"]]
    if additional_group_id is not None:
        group_ids.append(additional_group_id)
    groups, _, schedules = _lock_group_scope(
        club_id=club_id,
        training_group_ids=group_ids,
        student_id=preview["student_id"],
    )
    membership = (
        TrainingGroupMembership.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id=membership_id)
        .first()
    )
    if membership is None:
        raise BusinessLogicError("Training group membership was not found.", code="training_group_membership_not_found")
    return membership, groups, schedules


def _ensure_not_pending_payment_owned(*, membership: TrainingGroupMembership) -> None:
    from apps.billing.models import Payment

    if Payment.objects.for_club(membership.club_id).filter(
        conversion_group_membership_id=membership.id,
        status=Payment.Status.PENDING,
    ).exists():
        raise BusinessLogicError(
            "A payment-owned membership with pending payment must use payment lifecycle actions.",
            code="pending_payment_membership_action_forbidden",
        )


def _apply_membership_state(
    *,
    membership_id: int,
    club_id: int,
    status: str,
    effective_date: date,
    action: str,
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
) -> TrainingGroupMembership:
    rationale, idempotency_key = _validate_event_inputs(
        rationale=rationale,
        idempotency_key=idempotency_key,
    )
    with transaction.atomic():
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        existing_event = (
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .select_related("membership")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_event is not None:
            if existing_event.action != action or existing_event.membership_id != membership_id:
                raise BusinessLogicError(
                    "Membership idempotency key conflicts with this lifecycle action.",
                    code="training_group_membership_idempotency_conflict",
                )
            return existing_event.membership

        if action == "unfrozen":
            assert_training_group_expanding_write_allowed(rollout_state=rollout_state)

        membership, _, schedules = _get_membership_for_lifecycle(
            club_id=club_id,
            membership_id=membership_id,
        )
        _ensure_not_pending_payment_owned(membership=membership)
        previous_snapshot = _membership_snapshot(membership)
        if action in {"cancelled", "transferred"} and effective_date < membership.starts_on:
            raise BusinessLogicError(
                "Changing a membership before its start date is not supported.",
                code="training_group_pre_start_action_unsupported",
            )
        if membership.status in {
            TrainingGroupMembership.Status.CANCELLED,
            TrainingGroupMembership.Status.TRANSFERRED,
        }:
            raise BusinessLogicError(
                "This training group membership is already terminal.",
                code="training_group_membership_terminal",
            )
        if action == "frozen" and membership.status != TrainingGroupMembership.Status.ACTIVE:
            raise BusinessLogicError(
                "Only an active training group membership can be frozen.",
                code="training_group_membership_freeze_transition_invalid",
            )
        if action == "unfrozen" and membership.status != TrainingGroupMembership.Status.FROZEN:
            raise BusinessLogicError(
                "Only a frozen training group membership can be unfrozen.",
                code="training_group_membership_unfreeze_transition_invalid",
            )
        membership.status = status
        if status in {
            TrainingGroupMembership.Status.CANCELLED,
            TrainingGroupMembership.Status.TRANSFERRED,
        }:
            membership.ends_on = effective_date
        _save_membership(membership)
        _sync_membership_projections(
            membership=membership,
            schedules=schedules,
            create_missing=False,
        )
        _record_membership_event(
            membership=membership,
            action=action,
            effective_date=effective_date,
            previous_snapshot=previous_snapshot,
            actor_user_id=actor_user_id,
            rationale=rationale,
            idempotency_key=idempotency_key,
        )
        return membership


def freeze_training_group_membership(
    *, membership_id: int, club_id: int, actor_user_id: int | None, rationale: str, idempotency_key: str
) -> TrainingGroupMembership:
    club = Club.objects.only("id", "timezone").get(id=club_id)
    return _apply_membership_state(
        membership_id=membership_id,
        club_id=club_id,
        status=TrainingGroupMembership.Status.FROZEN,
        effective_date=club_localdate(club),
        action="frozen",
        actor_user_id=actor_user_id,
        rationale=rationale,
        idempotency_key=idempotency_key,
    )


def unfreeze_training_group_membership(
    *, membership_id: int, club_id: int, actor_user_id: int | None, rationale: str, idempotency_key: str
) -> TrainingGroupMembership:
    club = Club.objects.only("id", "timezone").get(id=club_id)
    return _apply_membership_state(
        membership_id=membership_id,
        club_id=club_id,
        status=TrainingGroupMembership.Status.ACTIVE,
        effective_date=club_localdate(club),
        action="unfrozen",
        actor_user_id=actor_user_id,
        rationale=rationale,
        idempotency_key=idempotency_key,
    )


def cancel_training_group_membership(
    *,
    membership_id: int,
    club_id: int,
    ends_on: date,
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
) -> TrainingGroupMembership:
    return _apply_membership_state(
        membership_id=membership_id,
        club_id=club_id,
        status=TrainingGroupMembership.Status.CANCELLED,
        effective_date=ends_on,
        action="cancelled",
        actor_user_id=actor_user_id,
        rationale=rationale,
        idempotency_key=idempotency_key,
    )


def transfer_training_group_membership(
    *,
    membership_id: int,
    club_id: int,
    target_training_group_id: int,
    ends_on: date,
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
) -> tuple[TrainingGroupMembership, TrainingGroupMembership]:
    """Transfer once, retaining old history and creating a future canonical membership."""
    rationale, idempotency_key = _validate_event_inputs(
        rationale=rationale,
        idempotency_key=idempotency_key,
    )
    with transaction.atomic():
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        existing_event = (
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .select_related("membership")
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_event is not None:
            if existing_event.action != "transferred" or existing_event.membership_id != membership_id:
                raise BusinessLogicError(
                    "Membership idempotency key conflicts with this transfer.",
                    code="training_group_membership_idempotency_conflict",
                )
            target = (
                TrainingGroupMembership.objects.for_club(club_id)
                .filter(
                    student_id=existing_event.membership.student_id,
                    training_group_id=target_training_group_id,
                    starts_on=ends_on + timedelta(days=1),
                    source=TrainingGroupMembership.Source.TRANSFER,
                )
                .order_by("id")
                .first()
            )
            if target is None:
                raise BusinessLogicError(
                    "Transfer retry does not have a canonical target membership.",
                    code="training_group_transfer_incomplete",
                )
            return existing_event.membership, target

        assert_training_group_expanding_write_allowed(rollout_state=rollout_state)

        membership, groups, schedules = _get_membership_for_lifecycle(
            club_id=club_id,
            membership_id=membership_id,
            additional_group_id=target_training_group_id,
        )
        if membership.training_group_id == target_training_group_id:
            raise BusinessLogicError("Target training group must differ.", code="same_training_group_transfer")
        _ensure_not_pending_payment_owned(membership=membership)
        if ends_on < membership.starts_on:
            raise BusinessLogicError(
                "Changing a membership before its start date is not supported.",
                code="training_group_pre_start_action_unsupported",
            )
        if membership.status not in {
            TrainingGroupMembership.Status.ACTIVE,
            TrainingGroupMembership.Status.FROZEN,
        }:
            raise BusinessLogicError(
                "This training group membership is terminal.",
                code="training_group_membership_terminal",
            )
        target_group = groups[target_training_group_id]
        if target_group.status != TrainingGroup.Status.ACTIVE:
            raise BusinessLogicError("Target training group is not active.", code="training_group_not_active")
        existing_target = (
            TrainingGroupMembership.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                student_id=membership.student_id,
                training_group_id=target_training_group_id,
                ends_on__isnull=True,
                status__in=[TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN],
            )
            .first()
        )
        if existing_target is not None:
            raise BusinessLogicError(
                "Student already has the target group membership.",
                code="training_group_membership_exists",
            )

        previous_snapshot = _membership_snapshot(membership)
        membership.status = TrainingGroupMembership.Status.TRANSFERRED
        membership.ends_on = ends_on
        _save_membership(membership)
        _sync_membership_projections(membership=membership, schedules=schedules, create_missing=False)

        target = TrainingGroupMembership(
            club_id=club_id,
            student_id=membership.student_id,
            training_group=target_group,
            status=TrainingGroupMembership.Status.ACTIVE,
            starts_on=ends_on + timedelta(days=1),
            source=TrainingGroupMembership.Source.TRANSFER,
            authority=TrainingGroupMembership.Authority.INDEPENDENT,
            created_by_id=actor_user_id,
        )
        _save_membership(target)
        _sync_membership_projections(membership=target, schedules=schedules, create_missing=True)
        _record_membership_event(
            membership=membership,
            action="transferred",
            effective_date=ends_on,
            previous_snapshot=previous_snapshot,
            actor_user_id=actor_user_id,
            rationale=rationale,
            idempotency_key=idempotency_key,
        )
        return membership, target


def fan_out_training_group_membership_projections(*, club_id: int, training_group_id: int) -> int:
    """Create missing active-slot projections with bounded fan-out queries."""
    with transaction.atomic():
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        assert_training_group_expanding_write_allowed(rollout_state=rollout_state)
        group = (
            TrainingGroup.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(id=training_group_id)
            .first()
        )
        if group is None:
            raise BusinessLogicError(
                "Training group does not belong to this club.",
                code="training_group_club_mismatch",
            )
        schedules = list(
            Schedule.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(training_group_id=group.id, one_time_date__isnull=True, is_active=True)
            .order_by("id")
        )
        memberships = list(
            TrainingGroupMembership.objects.for_club(club_id)
            .select_related("student")
            .select_for_update(of=("self",))
            .filter(
                training_group_id=group.id,
                ends_on__isnull=True,
                status__in=[TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN],
            )
            .order_by("id")
        )
        if not schedules or not memberships:
            return len(memberships)

        membership_ids = [membership.id for membership in memberships]
        schedule_ids = [schedule.id for schedule in schedules]
        existing_pairs = set(
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(training_group_membership_id__in=membership_ids)
            .values_list("training_group_membership_id", "schedule_id")
        )
        source_enrollment_pairs = list(
            TrainingGroupMembershipEvent.objects.for_club(club_id)
            .filter(
                membership_id__in=membership_ids,
                action__in=["backfilled", "source_linked"],
                source_enrollment_id__isnull=False,
            )
            .values_list("membership_id", "source_enrollment_id")
        )
        if source_enrollment_pairs:
            source_schedule_ids = dict(
                ScheduleEnrollment.objects.for_club(club_id)
                .filter(id__in=[source_enrollment_id for _, source_enrollment_id in source_enrollment_pairs])
                .values_list("id", "schedule_id")
            )
            existing_pairs.update(
                (membership_id, source_schedule_ids[source_enrollment_id])
                for membership_id, source_enrollment_id in source_enrollment_pairs
                if source_enrollment_id in source_schedule_ids
            )
        missing_pairs = [
            (membership, schedule)
            for membership in memberships
            for schedule in schedules
            if (membership.id, schedule.id) not in existing_pairs
        ]
        if not missing_pairs:
            return len(memberships)

        conflicting_pairs = set(
            ScheduleEnrollment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(
                student_id__in=[membership.student_id for membership in memberships],
                schedule_id__in=schedule_ids,
                ends_on__isnull=True,
                status__in=[
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.TRIAL,
                    ScheduleEnrollment.Status.FROZEN,
                ],
            )
            .exclude(training_group_membership_id__in=membership_ids)
            .values_list("student_id", "schedule_id")
        )
        if any((membership.student_id, schedule.id) in conflicting_pairs for membership, schedule in missing_pairs):
            raise BusinessLogicError(
                "An exact schedule roster row conflicts with this group membership.",
                code="training_group_projection_conflict",
            )

        # The locked group, slots, memberships, and conflict rows establish all
        # ScheduleEnrollment.clean invariants for this canonical projection family.
        projections = [
            ScheduleEnrollment(
                club_id=club_id,
                student=membership.student,
                schedule=schedule,
                training_group_membership=membership,
                status=membership.status,
                starts_on=membership.starts_on,
                ends_on=membership.ends_on,
                created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
            for membership, schedule in missing_pairs
        ]
        try:
            ScheduleEnrollment.objects.bulk_create(projections)
        except IntegrityError as exc:
            raise BusinessLogicError(
                "An exact schedule roster row conflicts with this group membership.",
                code="training_group_projection_conflict",
            ) from exc
        return len(memberships)
