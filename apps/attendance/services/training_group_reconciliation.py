"""Training-group reconciliation preview, owner-confirmed apply, and audit.

Preview remains deterministic and mutation-free.  Apply is deliberately a
separate, digest-bound reconciliation-only operation so an owner cannot turn a
visual suggestion into an inferred or partially committed group mapping.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from hashlib import sha256
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q

from apps.attendance.models import (
    Checkin,
    GroupSession,
    Schedule,
    ScheduleEnrollment,
    TrainingGroup,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.training_group_selectors import PERMANENT_ENROLLMENT_SOURCES
from apps.billing.models import BankPaymentOrder, Debt, Payment, PaymentRefundCase, Subscription
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import Trainer


class TrainingGroupPreviewError(Exception):
    def __init__(self, *, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _stable_digest(payload: dict) -> str:
    canonical_payload = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical_payload.encode("utf-8")).hexdigest()


def _conflict(*, code: str, scope: str, ids: list[int], detail: str) -> dict:
    return {"code": code, "scope": scope, "ids": sorted(ids), "detail": detail}


def _get_selected_schedules(*, club, schedule_ids: list[int]) -> list[Schedule]:
    normalized_ids = sorted(set(schedule_ids))
    if not normalized_ids:
        raise TrainingGroupPreviewError(
            code="schedule_ids_required",
            message="Select at least one exact recurring group schedule.",
        )
    if len(normalized_ids) != len(schedule_ids):
        raise TrainingGroupPreviewError(
            code="duplicate_schedule_id",
            message="Each selected schedule ID must be unique.",
        )

    schedules = list(
        Schedule.objects.for_club(club)
        .filter(id__in=normalized_ids)
        .select_related("training_type", "location", "trainer", "training_group")
        .order_by("id")
    )
    found_ids = {schedule.id for schedule in schedules}
    missing_ids = sorted(set(normalized_ids) - found_ids)
    if missing_ids:
        raise TrainingGroupPreviewError(
            code="unknown_schedule_ids",
            message=f"Selected schedules are not available in this club: {missing_ids}.",
        )
    invalid_ids = [
        schedule.id
        for schedule in schedules
        if schedule.one_time_date is not None or schedule.training_type.kind != "group"
    ]
    if invalid_ids:
        raise TrainingGroupPreviewError(
            code="non_recurring_group_schedule",
            message=f"Only recurring group schedules can be previewed: {sorted(invalid_ids)}.",
        )
    mapped_group_ids = {schedule.training_group_id for schedule in schedules if schedule.training_group_id}
    if mapped_group_ids and (
        len(mapped_group_ids) != 1 or any(schedule.training_group_id is None for schedule in schedules)
    ):
        raise TrainingGroupPreviewError(
            code="schedule_mapping_scope_conflict",
            message="Selected schedules must be all unmapped or already belong to one exact canonical group.",
        )
    return schedules


def _validate_schedule_scope(*, schedules: list[Schedule]) -> None:
    training_type_ids = {schedule.training_type_id for schedule in schedules}
    location_ids = {schedule.location_id for schedule in schedules}
    if len(training_type_ids) != 1:
        raise TrainingGroupPreviewError(
            code="mixed_training_type",
            message="Selected schedules must have one exact training type.",
        )
    if len(location_ids) != 1:
        raise TrainingGroupPreviewError(
            code="mixed_location",
            message="Selected schedules must have one exact location.",
        )


def _responsible_trainer_preview(
    *,
    club,
    schedules: list[Schedule],
    responsible_trainer_id: int | None,
) -> tuple[int | None, str, list[dict]]:
    trainer_ids = sorted({schedule.trainer_id for schedule in schedules})
    conflicts: list[dict] = []
    if responsible_trainer_id is not None:
        trainer = Trainer.objects.for_club(club).filter(id=responsible_trainer_id).only("id", "is_active").first()
        if trainer is None:
            raise TrainingGroupPreviewError(
                code="responsible_trainer_not_in_club",
                message="The responsible trainer must belong to this club.",
            )
        if not trainer.is_active:
            raise TrainingGroupPreviewError(
                code="responsible_trainer_inactive",
                message="The responsible trainer must be active.",
            )
        return trainer.id, "explicit", conflicts
    if len(trainer_ids) == 1:
        trainer = Trainer.objects.for_club(club).filter(id=trainer_ids[0]).only("id", "is_active").first()
        if trainer is not None and trainer.is_active:
            return trainer.id, "same_slot_trainer_default", conflicts
        conflicts.append(
            _conflict(
                code="responsible_trainer_inactive",
                scope="group",
                ids=trainer_ids,
                detail="The common slot trainer is inactive; choose an active responsible trainer explicitly.",
            )
        )
        return None, "unresolved_inactive_common_trainer", conflicts
    conflicts.append(
        _conflict(
            code="responsible_trainer_required",
            scope="group",
            ids=trainer_ids,
            detail="Selected slots have mixed trainers; choose one same-club responsible trainer explicitly.",
        )
    )
    return None, "unresolved_mixed_trainers", conflicts


def _normalize_start_dates(*, start_dates: list[dict]) -> dict[int, date]:
    result: dict[int, date] = {}
    for item in start_dates:
        student_id = item["student_id"]
        if student_id in result:
            raise TrainingGroupPreviewError(
                code="duplicate_student_start_date",
                message=f"Student {student_id} has more than one chosen start date.",
            )
        result[student_id] = item["starts_on"]
    return result


def _is_exact_terminal_payment_conversion_history(*, payment: Payment) -> bool:
    """Allow only immutable terminal conversion history to remain on the legacy path."""
    conversion_enrollment = payment.conversion_enrollment
    return (
        conversion_enrollment is not None
        and payment.club_id == conversion_enrollment.club_id
        and payment.student_id == conversion_enrollment.student_id
        and payment.target_schedule_id == conversion_enrollment.schedule_id
        and payment.status == Payment.Status.CONFIRMED
        and payment.target_start_date == conversion_enrollment.starts_on
        and conversion_enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        and conversion_enrollment.status
        in {
            ScheduleEnrollment.Status.TRANSFERRED,
            ScheduleEnrollment.Status.CANCELLED,
        }
        and conversion_enrollment.ends_on is not None
    )


def build_training_group_reconciliation_preview(
    *,
    club,
    schedule_ids: list[int],
    canonical_name: str,
    responsible_trainer_id: int | None = None,
    start_dates: list[dict] | None = None,
) -> dict:
    """Build a deterministic preview for an explicit set of schedule IDs.

    The returned digest covers only explicit selections and server-derived
    metadata. No model save, update, delete, transaction, or event creation is
    performed here.
    """
    normalized_name = canonical_name.strip()
    if not normalized_name:
        raise TrainingGroupPreviewError(
            code="canonical_name_required",
            message="A canonical group name is required for preview.",
        )
    schedules = _get_selected_schedules(club=club, schedule_ids=schedule_ids)
    _validate_schedule_scope(schedules=schedules)
    normalized_start_dates = _normalize_start_dates(start_dates=start_dates or [])
    selected_schedule_ids = [schedule.id for schedule in schedules]
    trainer_id, trainer_source, conflicts = _responsible_trainer_preview(
        club=club,
        schedules=schedules,
        responsible_trainer_id=responsible_trainer_id,
    )
    mapped_group_ids = {schedule.training_group_id for schedule in schedules if schedule.training_group_id}
    if mapped_group_ids:
        mapped_group = schedules[0].training_group
        if (
            mapped_group is None
            or mapped_group.name != normalized_name
            or mapped_group.training_type_id != schedules[0].training_type_id
            or mapped_group.location_id != schedules[0].location_id
            or mapped_group.responsible_trainer_id != trainer_id
        ):
            raise TrainingGroupPreviewError(
                code="mapped_schedule_preview_mismatch",
                message="The selected canonical slots do not match this preview decision.",
            )

    enrollments = list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            schedule_id__in=selected_schedule_ids,
            created_from__in=PERMANENT_ENROLLMENT_SOURCES,
            ends_on__isnull=True,
            status__in=[ScheduleEnrollment.Status.ACTIVE, ScheduleEnrollment.Status.FROZEN],
        )
        .order_by("student_id", "schedule_id", "id")
        .only("id", "student_id", "schedule_id", "status", "starts_on", "created_from")
    )
    enrollment_ids = [enrollment.id for enrollment in enrollments]
    payment_ids_by_enrollment: dict[int, list[int]] = defaultdict(list)
    if enrollment_ids:
        for enrollment_id, payment_id in (
            Payment.objects.for_club(club)
            .filter(conversion_enrollment_id__in=enrollment_ids)
            .order_by("conversion_enrollment_id", "id")
            .values_list("conversion_enrollment_id", "id")
        ):
            payment_ids_by_enrollment[enrollment_id].append(payment_id)

    enrollments_by_student: dict[int, list[ScheduleEnrollment]] = defaultdict(list)
    for enrollment in enrollments:
        enrollments_by_student[enrollment.student_id].append(enrollment)

    extra_start_date_ids = sorted(set(normalized_start_dates) - set(enrollments_by_student))
    if extra_start_date_ids:
        raise TrainingGroupPreviewError(
            code="unknown_student_start_date",
            message=f"Start dates were supplied for students outside the selected roster: {extra_start_date_ids}.",
        )

    proposed_memberships: list[dict] = []
    projections: list[dict] = []
    roster_deltas: list[dict] = []
    for student_id in sorted(enrollments_by_student):
        source_rows = enrollments_by_student[student_id]
        source_ids = [row.id for row in source_rows]
        sources = sorted({row.created_from for row in source_rows})
        statuses = sorted({row.status for row in source_rows})
        existing_schedule_ids = {row.schedule_id for row in source_rows}
        row_conflicts: list[dict] = []
        if len(statuses) != 1:
            row_conflicts.append(
                _conflict(
                    code="conflicting_enrollment_status",
                    scope="student",
                    ids=source_ids,
                    detail="Selected permanent enrollments disagree on active or frozen status.",
                )
            )

        source_start_dates = sorted({row.starts_on for row in source_rows if row.starts_on is not None})
        earliest_legacy_start_date = source_start_dates[0] if source_start_dates else None
        chosen_start_date = normalized_start_dates.get(student_id)
        if chosen_start_date is None and len(source_start_dates) == 1:
            chosen_start_date = source_start_dates[0]
            start_date_source = "legacy_exact"
        elif chosen_start_date is not None:
            start_date_source = "owner_explicit_preview"
        else:
            start_date_source = "unresolved"
            row_conflicts.append(
                _conflict(
                    code="conflicting_or_missing_start_date",
                    scope="student",
                    ids=source_ids,
                    detail="Choose one membership start date explicitly; legacy schedule starts do not agree.",
                )
            )

        payment_source_ids = [
            row.id
            for row in source_rows
            if row.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        ]
        payment_ids = sorted(
            payment_id
            for enrollment_id in payment_source_ids
            for payment_id in payment_ids_by_enrollment[enrollment_id]
        )
        independent_sources = {
            ScheduleEnrollment.CreatedFrom.MANUAL,
            ScheduleEnrollment.CreatedFrom.IMPORT,
        }
        has_paid_provenance_contradiction = bool(payment_source_ids) and (
            len(payment_source_ids) != 1 or len(payment_ids) != 1
        )
        if has_paid_provenance_contradiction:
            row_conflicts.append(
                _conflict(
                    code="mixed_or_multi_payment_ownership",
                    scope="student",
                    ids=source_ids + payment_ids,
                    detail=(
                        "Multiple paid-conversion sources or payments require an "
                        "explicit unresolved decision before any mapping can apply."
                    ),
                )
            )
        if any(source in independent_sources for source in sources):
            authority = TrainingGroupMembership.Authority.INDEPENDENT
        elif not has_paid_provenance_contradiction and len(payment_source_ids) == 1 and len(payment_ids) == 1:
            authority = TrainingGroupMembership.Authority.PAYMENT_OWNED
        else:
            authority = "unresolved"
            if not has_paid_provenance_contradiction:
                row_conflicts.append(
                    _conflict(
                        code="mixed_or_multi_payment_ownership",
                        scope="student",
                        ids=source_ids + payment_ids,
                        detail=(
                            "Payment-owned authority requires exactly one paid-conversion "
                            "enrollment and one exact payment."
                        ),
                    )
                )

        for schedule_id in selected_schedule_ids:
            action = "link_existing" if schedule_id in existing_schedule_ids else "add_compatibility_projection"
            projections.append({"student_id": student_id, "schedule_id": schedule_id, "action": action})
            roster_deltas.append(
                {
                    "student_id": student_id,
                    "schedule_id": schedule_id,
                    "effective_starts_on": chosen_start_date.isoformat() if chosen_start_date else None,
                    "earliest_legacy_starts_on": (
                        earliest_legacy_start_date.isoformat() if earliest_legacy_start_date else None
                    ),
                    "action": action,
                    "requires_explicit_starts_on": chosen_start_date is None,
                }
            )
        proposed_memberships.append(
            {
                "student_id": student_id,
                "source_enrollment_ids": source_ids,
                "source_payment_ids": payment_ids,
                "source_kinds": sources,
                "status": statuses[0] if len(statuses) == 1 else None,
                "starts_on": chosen_start_date.isoformat() if chosen_start_date else None,
                "earliest_legacy_starts_on": (
                    earliest_legacy_start_date.isoformat() if earliest_legacy_start_date else None
                ),
                "start_date_source": start_date_source,
                "authority": authority,
                "projection_schedule_ids": selected_schedule_ids,
                "conflicts": row_conflicts,
            }
        )
        conflicts.extend(row_conflicts)

    target_payment_conflicts: list[dict] = []
    for payment in (
        Payment.objects.for_club(club)
        .filter(target_schedule_id__in=selected_schedule_ids)
        .exclude(conversion_enrollment_id__isnull=True)
        .select_related("conversion_enrollment")
        .order_by("id")
        .only(
            "id",
            "club_id",
            "student_id",
            "status",
            "target_schedule_id",
            "target_start_date",
            "conversion_enrollment_id",
            "conversion_enrollment__id",
            "conversion_enrollment__club_id",
            "conversion_enrollment__student_id",
            "conversion_enrollment__schedule_id",
            "conversion_enrollment__status",
            "conversion_enrollment__starts_on",
            "conversion_enrollment__ends_on",
            "conversion_enrollment__created_from",
        )
    ):
        if (
            payment.conversion_enrollment_id not in enrollment_ids
            and not _is_exact_terminal_payment_conversion_history(payment=payment)
        ):
            target_payment_conflicts.append(
                _conflict(
                    code="payment_conversion_outside_selected_roster",
                    scope="payment",
                    ids=[payment.id, payment.target_schedule_id, payment.conversion_enrollment_id],
                    detail=(
                        "A selected schedule payment points to an enrollment outside "
                        "this explicit roster selection."
                    ),
                )
            )
    conflicts.extend(target_payment_conflicts)

    payload = {
        "version": 1,
        "club_id": club.id,
        "canonical_group": {
            "name": normalized_name,
            "training_type_id": schedules[0].training_type_id,
            "location_id": schedules[0].location_id,
            "responsible_trainer_id": trainer_id,
            "responsible_trainer_source": trainer_source,
        },
        "selected_schedule_ids": selected_schedule_ids,
        "proposed_memberships": proposed_memberships,
        "compatibility_projections": projections,
        "proposed_roster_deltas": roster_deltas,
        "conflicts": conflicts,
    }
    return {**payload, "digest": _stable_digest(payload)}


def _derived_idempotency_key(*, root_key: str, action: str, target_id: int | None = None) -> str:
    material = f"{root_key}:{action}:{target_id if target_id is not None else ''}"
    return sha256(material.encode("utf-8")).hexdigest()


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


def _lock_reconciliation_financial_roots(
    *, club, schedule_ids: list[int], source_enrollment_ids: list[int]
) -> dict[int, Payment]:
    """Lock the financial half of an explicit mapping before identity rows.

    The preview has already bounded the candidate set.  We nevertheless lock
    every payment targeted at a selected slot, not just paid-conversion sources,
    so an unpreviewed target conflict is detected under the same transaction.
    """
    payment_ids = list(
        Payment.objects.for_club(club)
        .filter(
            Q(conversion_enrollment_id__in=source_enrollment_ids)
            | Q(target_schedule_id__in=schedule_ids)
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    if not payment_ids:
        return {}
    list(
        PaymentRefundCase.objects.for_club(club)
        .select_for_update(of=("self",))
        .filter(order__payment_id__in=payment_ids)
        .order_by("id")
    )
    list(
        BankPaymentOrder.objects.for_club(club)
        .select_for_update(of=("self",))
        .filter(payment_id__in=payment_ids)
        .order_by("id")
    )
    payments = list(
        Payment.objects.for_club(club)
        .select_for_update(of=("self",))
        .select_related("tariff", "target_schedule", "conversion_enrollment")
        .filter(id__in=payment_ids)
        .order_by("id")
    )
    subscription_ids = sorted({payment.subscription_id for payment in payments if payment.subscription_id})
    if subscription_ids:
        list(
            Subscription.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id__in=subscription_ids)
            .order_by("id")
        )
    return {payment.id: payment for payment in payments}


def _record_backfill_source_events(
    *,
    membership: TrainingGroupMembership,
    source_enrollments: list[ScheduleEnrollment],
    source_payment_id: int | None,
    actor_user_id: int | None,
    rationale: str,
    root_idempotency_key: str,
) -> None:
    snapshot = _membership_snapshot(membership)
    for index, enrollment in enumerate(source_enrollments):
        if TrainingGroupMembershipEvent.objects.for_club(membership.club_id).filter(
            membership=membership,
            action__in=["backfilled", "source_linked"],
            source_enrollment_id=enrollment.id,
        ).exists():
            continue
        TrainingGroupMembershipEvent.objects.create(
            club_id=membership.club_id,
            membership=membership,
            action="backfilled" if index == 0 else "source_linked",
            effective_date=membership.starts_on,
            previous_state_snapshot={},
            new_state_snapshot=snapshot,
            actor_id=actor_user_id,
            rationale=rationale,
            idempotency_key=_derived_idempotency_key(
                root_key=root_idempotency_key,
                action="backfill-source",
                target_id=enrollment.id,
            ),
            source_enrollment_id=enrollment.id,
            source_payment_id=(
                source_payment_id if enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION else None
            ),
        )


def _link_backfill_sources_to_membership(
    *,
    membership: TrainingGroupMembership,
    source_enrollments: list[ScheduleEnrollment],
) -> None:
    """Route preserved permanent rows to canonical lifecycle without rewriting provenance."""
    for enrollment in source_enrollments:
        enrollment.training_group_membership = membership
        try:
            enrollment.full_clean()
        except ValidationError as exc:
            raise BusinessLogicError(
                "; ".join(exc.messages) or "Legacy enrollment cannot link to canonical membership.",
                code="training_group_reconciliation_scope_changed",
            ) from exc
        enrollment.save(update_fields=["training_group_membership", "updated_at"])


def _create_missing_backfill_projections(
    *,
    membership: TrainingGroupMembership,
    active_schedules: list[Schedule],
    source_enrollments: list[ScheduleEnrollment],
) -> int:
    """Add only genuinely absent compatibility rows without rewriting sources."""
    source_schedule_ids = {enrollment.schedule_id for enrollment in source_enrollments}
    created = 0
    for schedule in active_schedules:
        if schedule.id in source_schedule_ids:
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
            .first()
        )
        if conflicting is not None:
            if (
                conflicting.training_group_membership_id == membership.id
                and conflicting.created_from == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
                and conflicting.status == membership.status
                and conflicting.starts_on == membership.starts_on
            ):
                continue
            raise BusinessLogicError(
                "An exact schedule roster row conflicts with the approved backfill.",
                code="training_group_projection_conflict",
            )
        projection = ScheduleEnrollment(
            club_id=membership.club_id,
            student_id=membership.student_id,
            schedule=schedule,
            training_group_membership=membership,
            status=membership.status,
            starts_on=membership.starts_on,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )
        try:
            projection.full_clean()
        except ValidationError as exc:
            raise BusinessLogicError(
                "; ".join(exc.messages) or "Invalid training group compatibility projection.",
                code="invalid_training_group_projection",
            ) from exc
        projection.save()
        created += 1
    return created


def _link_exact_legacy_payment(
    *,
    payment: Payment,
    membership: TrainingGroupMembership,
    training_group: TrainingGroup,
    source_enrollment: ScheduleEnrollment,
) -> None:
    """Perform only the accepted null-to-canonical-link migration update."""
    if (
        payment.club_id != membership.club_id
        or payment.student_id != membership.student_id
        or payment.conversion_enrollment_id != source_enrollment.id
        or payment.target_schedule_id != source_enrollment.schedule_id
    ):
        raise BusinessLogicError(
            "Payment does not exactly own the approved legacy conversion source.",
            code="training_group_payment_backfill_mismatch",
        )
    if any(
        value not in (None, "")
        for value in (
            payment.target_training_group_id,
            payment.target_group_membership_id,
            payment.conversion_group_membership_id,
            payment.group_membership_action_snapshot,
        )
    ):
        raise BusinessLogicError(
            "Payment already has immutable canonical group ownership data.",
            code="training_group_payment_backfill_mismatch",
        )
    payment.target_training_group = training_group
    payment.target_group_membership = membership
    if membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED:
        payment.conversion_group_membership = membership
    # ``payment`` was locked before the selected schedule was mapped.  Refresh
    # only that protected anchor relation so model validation observes the
    # transaction-local canonical FK rather than a stale select_related cache.
    payment.target_schedule = Schedule.objects.for_club(membership.club_id).get(id=payment.target_schedule_id)
    try:
        payment.full_clean()
        payment.save()
    except ValidationError as exc:
        raise BusinessLogicError(
            "; ".join(exc.messages) or "Payment cannot be linked to the approved canonical membership.",
            code="training_group_payment_backfill_mismatch",
        ) from exc


def _legacy_payment_is_already_linked(
    *,
    payment: Payment,
    membership: TrainingGroupMembership,
    training_group: TrainingGroup,
    source_enrollment: ScheduleEnrollment,
) -> bool:
    """Recognize only the exact immutable result of a prior accepted backfill."""
    return (
        payment.club_id == membership.club_id
        and payment.student_id == membership.student_id
        and payment.conversion_enrollment_id == source_enrollment.id
        and payment.target_schedule_id == source_enrollment.schedule_id
        and payment.target_training_group_id == training_group.id
        and payment.target_group_membership_id == membership.id
        and payment.conversion_group_membership_id
        == (
            membership.id
            if membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
            else None
        )
        and payment.group_membership_action_snapshot == ""
    )


def apply_training_group_reconciliation(
    *,
    club,
    schedule_ids: list[int],
    canonical_name: str,
    responsible_trainer_id: int | None,
    start_dates: list[dict],
    preview_digest: str,
    actor_user_id: int | None,
    rationale: str,
    idempotency_key: str,
) -> dict:
    """Atomically apply an owner-confirmed reconciliation preview.

    This is intentionally not a generic group-creation service: it can only
    consume the exact deterministic S2 preview while a club is quiesced in
    ``reconciling``.  Every visible result is committed together or rolled back.
    """
    clean_rationale = rationale.strip()
    clean_key = idempotency_key.strip()
    if not clean_rationale or len(clean_rationale) > 500:
        raise BusinessLogicError(
            "A bounded reconciliation rationale is required.", code="training_group_rationale_invalid"
        )
    if not clean_key or len(clean_key) > 120:
        raise BusinessLogicError(
            "A bounded reconciliation idempotency key is required.", code="training_group_idempotency_invalid"
        )
    if len(preview_digest) != 64:
        raise BusinessLogicError(
            "A canonical preview digest is required.", code="training_group_preview_digest_invalid"
        )

    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import (
            assert_training_group_new_writes_enabled,
            lock_training_group_payment_scope,
        )

        rollout_state = lock_training_group_payment_scope(club_id=club.id)
        if rollout_state.mode != TrainingGroupRolloutState.Mode.RECONCILING:
            raise BusinessLogicError(
                "Training group mapping apply is allowed only during reconciliation.",
                code="training_group_reconciliation_required",
            )
        existing = (
            TrainingGroupMappingEvent.objects.for_club(club)
            .select_related("training_group")
            .filter(idempotency_key=clean_key, schedule_id__isnull=True)
            .first()
        )
        if existing is not None:
            snapshot = existing.new_group_snapshot
            if (
                existing.action != "apply_completed"
                or snapshot.get("preview_digest") != preview_digest
                or snapshot.get("selected_schedule_ids") != sorted(set(schedule_ids))
            ):
                raise BusinessLogicError(
                    "Reconciliation idempotency key conflicts with this apply request.",
                    code="training_group_reconciliation_idempotency_conflict",
                )
            return snapshot

        assert_training_group_new_writes_enabled()

        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=schedule_ids,
            canonical_name=canonical_name,
            responsible_trainer_id=responsible_trainer_id,
            start_dates=start_dates,
        )
        if preview["digest"] != preview_digest:
            raise BusinessLogicError(
                "The owner-confirmed preview is stale or does not match this apply request.",
                code="training_group_preview_digest_mismatch",
            )
        if preview["conflicts"]:
            raise BusinessLogicError(
                "Resolve every reconciliation conflict before apply.",
                code="training_group_reconciliation_conflicts_unresolved",
            )

        selected_schedule_ids = preview["selected_schedule_ids"]
        proposed_memberships = preview["proposed_memberships"]
        source_enrollment_ids = sorted(
            source_id
            for proposal in proposed_memberships
            for source_id in proposal["source_enrollment_ids"]
        )
        payments_by_id = _lock_reconciliation_financial_roots(
            club=club,
            schedule_ids=selected_schedule_ids,
            source_enrollment_ids=source_enrollment_ids,
        )
        proposal_student_ids = sorted({proposal["student_id"] for proposal in proposed_memberships})
        locked_student_ids = list(
            Student.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id__in=proposal_student_ids, deleted_at__isnull=True)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if locked_student_ids != proposal_student_ids:
            raise BusinessLogicError(
                "A reconciliation student changed before canonical membership backfill.",
                code="training_group_reconciliation_scope_changed",
            )

        preview_group_id = next(
            (
                schedule.training_group_id
                for schedule in _get_selected_schedules(
                    club=club,
                    schedule_ids=selected_schedule_ids,
                )
                if schedule.training_group_id is not None
            ),
            None,
        )
        group = (
            TrainingGroup.objects.for_club(club)
            .select_for_update(of=("self",))
            .select_related("training_type", "location", "responsible_trainer")
            .filter(id=preview_group_id)
            .first()
            if preview_group_id is not None
            else None
        )
        if preview_group_id is not None and group is None:
            raise BusinessLogicError(
                "The canonical training group changed before repair apply.",
                code="training_group_reconciliation_scope_changed",
            )

        locked_schedules = list(
            Schedule.objects.for_club(club)
            .select_for_update(of=("self",))
            .select_related("training_type", "location", "trainer", "training_group")
            .filter(id__in=selected_schedule_ids)
            .order_by("id")
        )
        mapped_group_ids = {
            schedule.training_group_id
            for schedule in locked_schedules
            if schedule.training_group_id is not None
        }
        if (
            [schedule.id for schedule in locked_schedules] != selected_schedule_ids
            or len(mapped_group_ids) > 1
            or (mapped_group_ids and any(schedule.training_group_id is None for schedule in locked_schedules))
            or mapped_group_ids != ({preview_group_id} if preview_group_id is not None else set())
        ):
            raise BusinessLogicError(
                "Selected schedule mapping changed before reconciliation apply.",
                code="training_group_reconciliation_scope_changed",
            )
        locked_sources = list(
            ScheduleEnrollment.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id__in=source_enrollment_ids)
            .order_by("id")
        )
        sources_by_id = {source.id: source for source in locked_sources}
        if sorted(sources_by_id) != source_enrollment_ids:
            raise BusinessLogicError(
                "Legacy enrollment sources changed before reconciliation apply.",
                code="training_group_reconciliation_scope_changed",
            )
        # Lock every operational history row whose meaning depends on a slot
        # before moving the slot under canonical group identity.  The rows are
        # preserved; the locks only serialize reconciliation against check-in,
        # debt, and session writers using the documented hierarchy.
        list(
            Checkin.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(schedule_id__in=selected_schedule_ids)
            .order_by("id")
        )
        list(
            Debt.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(checkin__schedule_id__in=selected_schedule_ids)
            .order_by("id")
        )
        list(
            GroupSession.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(schedule_id__in=selected_schedule_ids)
            .order_by("id")
        )

        trainer = (
            Trainer.objects.for_club(club)
            .select_for_update(of=("self",))
            .filter(id=preview["canonical_group"]["responsible_trainer_id"], is_active=True)
            .first()
        )
        if trainer is None:
            raise BusinessLogicError(
                "The responsible trainer changed before reconciliation apply.",
                code="training_group_reconciliation_scope_changed",
            )
        repairing_existing_group = group is not None
        if repairing_existing_group:
            if (
                group.status != TrainingGroup.Status.ACTIVE
                or group.name != preview["canonical_group"]["name"]
                or group.training_type_id != preview["canonical_group"]["training_type_id"]
                or group.location_id != preview["canonical_group"]["location_id"]
                or group.responsible_trainer_id != trainer.id
            ):
                raise BusinessLogicError(
                    "The canonical training group changed before repair apply.",
                    code="training_group_reconciliation_scope_changed",
                )
        else:
            group = TrainingGroup(
                club=club,
                name=preview["canonical_group"]["name"],
                training_type_id=preview["canonical_group"]["training_type_id"],
                location_id=preview["canonical_group"]["location_id"],
                responsible_trainer=trainer,
                status=TrainingGroup.Status.DRAFT,
                created_by_id=actor_user_id,
            )
            try:
                group.full_clean()
                group.save()
            except ValidationError as exc:
                raise BusinessLogicError(
                    "; ".join(exc.messages) or "Invalid training group reconciliation mapping.",
                    code="training_group_reconciliation_scope_changed",
                ) from exc

            for schedule in locked_schedules:
                schedule.training_group = group
                try:
                    schedule.full_clean()
                    schedule.save()
                except ValidationError as exc:
                    raise BusinessLogicError(
                        "; ".join(exc.messages) or "Schedule cannot join the canonical training group.",
                        code="training_group_reconciliation_scope_changed",
                    ) from exc

            # The draft group is still transaction-private.  Model validation
            # correctly requires an active group for an open membership; any later
            # error rolls back this status together with every mapping and link.
            group.status = TrainingGroup.Status.ACTIVE
            try:
                group.full_clean()
                group.save()
            except ValidationError as exc:
                raise BusinessLogicError(
                    "; ".join(exc.messages) or "Training group activation failed.",
                    code="training_group_reconciliation_scope_changed",
                ) from exc

        active_schedules = [schedule for schedule in locked_schedules if schedule.is_active]
        membership_count = 0
        projection_count = 0
        linked_payment_count = 0
        for proposal in proposed_memberships:
            source_rows = [sources_by_id[source_id] for source_id in proposal["source_enrollment_ids"]]
            if (
                any(
                    source.student_id != proposal["student_id"]
                    or source.schedule_id not in selected_schedule_ids
                    or source.created_from not in PERMANENT_ENROLLMENT_SOURCES
                    or source.ends_on is not None
                    or source.status != proposal["status"]
                    for source in source_rows
                )
                or proposal["starts_on"] is None
                or proposal["authority"] not in {
                    TrainingGroupMembership.Authority.INDEPENDENT,
                    TrainingGroupMembership.Authority.PAYMENT_OWNED,
                }
            ):
                raise BusinessLogicError(
                    "Legacy membership authority changed before reconciliation apply.",
                    code="training_group_reconciliation_scope_changed",
                )
            existing_membership = (
                TrainingGroupMembership.objects.for_club(club)
                .select_for_update(of=("self",))
                .filter(
                    student_id=proposal["student_id"],
                    training_group=group,
                    ends_on__isnull=True,
                    status__in=[TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN],
                )
                .first()
            )
            if existing_membership is not None:
                if (
                    not repairing_existing_group
                    or existing_membership.status != proposal["status"]
                    or existing_membership.starts_on != date.fromisoformat(proposal["starts_on"])
                    or existing_membership.source != TrainingGroupMembership.Source.MIGRATION
                    or existing_membership.authority != proposal["authority"]
                ):
                    raise BusinessLogicError(
                        "An open canonical membership conflicts with this repair.",
                        code="training_group_reconciliation_scope_changed",
                    )
                membership = existing_membership
            else:
                membership = TrainingGroupMembership(
                    club=club,
                    student_id=proposal["student_id"],
                    training_group=group,
                    status=proposal["status"],
                    starts_on=date.fromisoformat(proposal["starts_on"]),
                    source=TrainingGroupMembership.Source.MIGRATION,
                    authority=proposal["authority"],
                    created_by_id=actor_user_id,
                )
                try:
                    membership.full_clean()
                    membership.save()
                except ValidationError as exc:
                    raise BusinessLogicError(
                        "; ".join(exc.messages) or "Invalid reconciled training group membership.",
                        code="training_group_reconciliation_scope_changed",
                    ) from exc
            source_payment_ids = proposal["source_payment_ids"]
            if (
                proposal["authority"] == TrainingGroupMembership.Authority.PAYMENT_OWNED
                and len(source_payment_ids) != 1
            ):
                raise BusinessLogicError(
                    "Payment-owned backfill must have one exact payment source.",
                    code="training_group_reconciliation_scope_changed",
                )
            if any(
                source.training_group_membership_id not in {None, membership.id}
                for source in source_rows
            ):
                raise BusinessLogicError(
                    "A legacy enrollment source is linked to another canonical membership.",
                    code="training_group_reconciliation_scope_changed",
                )
            _record_backfill_source_events(
                membership=membership,
                source_enrollments=source_rows,
                source_payment_id=source_payment_ids[0] if source_payment_ids else None,
                actor_user_id=actor_user_id,
                rationale=clean_rationale,
                root_idempotency_key=clean_key,
            )
            _link_backfill_sources_to_membership(
                membership=membership,
                source_enrollments=source_rows,
            )
            projection_count += _create_missing_backfill_projections(
                membership=membership,
                active_schedules=active_schedules,
                source_enrollments=source_rows,
            )
            for payment_id in source_payment_ids:
                payment = payments_by_id.get(payment_id)
                paid_source = next(
                    (source for source in source_rows if source.id == payment.conversion_enrollment_id),
                    None,
                ) if payment is not None else None
                if payment is None or paid_source is None:
                    raise BusinessLogicError(
                        "Payment source changed before reconciliation apply.",
                        code="training_group_reconciliation_scope_changed",
                    )
                if _legacy_payment_is_already_linked(
                    payment=payment,
                    membership=membership,
                    training_group=group,
                    source_enrollment=paid_source,
                ):
                    continue
                _link_exact_legacy_payment(
                    payment=payment,
                    membership=membership,
                    training_group=group,
                    source_enrollment=paid_source,
                )
                linked_payment_count += 1
            membership_count += 1

        batch_id = uuid4()
        for schedule in locked_schedules:
            TrainingGroupMappingEvent.objects.create(
                club=club,
                batch_id=batch_id,
                training_group=group,
                schedule=schedule,
                action=(
                    "schedule_reconciled"
                    if repairing_existing_group
                    else "schedule_mapped"
                ),
                previous_group_snapshot={
                    "training_group_id": group.id if repairing_existing_group else None
                },
                new_group_snapshot={"training_group_id": group.id},
                actor_id=actor_user_id,
                rationale=clean_rationale,
                idempotency_key=_derived_idempotency_key(
                    root_key=clean_key,
                    action=(
                        "schedule-reconciled"
                        if repairing_existing_group
                        else "schedule-mapped"
                    ),
                    target_id=schedule.id,
                ),
            )
        roster_delta_digest = _stable_digest({"deltas": preview["proposed_roster_deltas"]})
        from apps.attendance.training_group_roster import snapshot_expected_roster_horizon
        from apps.clubs.timezones import club_localdate

        roster_horizon_as_of = club_localdate(club)
        roster_horizon = snapshot_expected_roster_horizon(
            club=club,
            schedule_ids=selected_schedule_ids,
            as_of_date=roster_horizon_as_of,
        )
        from apps.attendance.services.training_groups import approved_reconciliation_rollout_gate_digest

        rollout_gate_digest = approved_reconciliation_rollout_gate_digest(
            club_id=club.id,
            additional_roster_delta_digests=[roster_delta_digest],
            additional_roster_horizon_digests=[roster_horizon["horizon_digest"]],
        )
        result = {
            "training_group_id": group.id,
            "status": group.status,
            "preview_digest": preview_digest,
            "selected_schedule_ids": selected_schedule_ids,
            "membership_count": membership_count,
            "projection_count": projection_count,
            "linked_payment_count": linked_payment_count,
            "repaired_existing_group": repairing_existing_group,
            "roster_delta_digest": roster_delta_digest,
            "approved_roster_horizon_as_of": roster_horizon_as_of.isoformat(),
            "approved_roster_horizon_digest": roster_horizon["horizon_digest"],
            "approved_roster_horizon_slot_count": roster_horizon["slot_count"],
            "rollout_gate_digest": rollout_gate_digest,
            "batch_id": str(batch_id),
        }
        TrainingGroupMappingEvent.objects.create(
            club=club,
            batch_id=batch_id,
            training_group=group,
            schedule=None,
            action="apply_completed",
            previous_group_snapshot={},
            new_group_snapshot=result,
            actor_id=actor_user_id,
            rationale=clean_rationale,
            idempotency_key=clean_key,
        )
        return result


def audit_training_groups(*, club) -> dict:
    """Return aggregate, redacted invariant failures for one club."""
    invalid: list[str] = []
    rollout_state_count = TrainingGroupRolloutState.objects.for_club(club).count()
    if rollout_state_count == 0:
        invalid.append("missing_rollout_state")
    elif rollout_state_count != 1:
        invalid.append("duplicate_rollout_state")

    duplicate_rollout_clubs = (
        TrainingGroupRolloutState.objects.for_club(club)
        .values("club_id")
        .annotate(row_count=Count("id"))
        .filter(club_id=club.id, row_count__gt=1)
        .exists()
    )
    if duplicate_rollout_clubs and "duplicate_rollout_state" not in invalid:
        invalid.append("duplicate_rollout_state")

    linked_schedules = Schedule.objects.for_club(club).filter(training_group_id__isnull=False)
    if linked_schedules.filter(
        Q(one_time_date__isnull=False)
        | ~Q(training_type_id=F("training_group__training_type_id"))
        | ~Q(location_id=F("training_group__location_id"))
    ).exists():
        invalid.append("invalid_schedule_group_link")

    projections = ScheduleEnrollment.objects.for_club(club).filter(
        Q(created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION)
        | Q(training_group_membership_id__isnull=False)
    )
    projection_count = projections.count()
    valid_link_source = Q(created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION) | Q(
        created_from__in=[
            ScheduleEnrollment.CreatedFrom.MANUAL,
            ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            ScheduleEnrollment.CreatedFrom.IMPORT,
        ],
        training_group_membership__source=TrainingGroupMembership.Source.MIGRATION,
    )
    invalid_projection = projections.filter(
        Q(created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION, training_group_membership_id__isnull=True)
        | Q(training_group_membership_id__isnull=False)
        & (
            ~valid_link_source
            | ~Q(schedule__training_group_id=F("training_group_membership__training_group_id"))
            | ~Q(student_id=F("training_group_membership__student_id"))
        )
    ).exists()
    if invalid_projection:
        invalid.append("invalid_membership_projection")

    migration_source_events = TrainingGroupMembershipEvent.objects.for_club(club).filter(
        action__in=["backfilled", "source_linked"],
        source_enrollment_id__isnull=False,
    )
    source_enrollment_ids = list(migration_source_events.values_list("source_enrollment_id", flat=True))
    source_rows = {
        enrollment.id: enrollment
        for enrollment in ScheduleEnrollment.objects.for_club(club)
        .filter(id__in=source_enrollment_ids)
        .select_related("schedule")
    }
    for event in migration_source_events.select_related("membership"):
        source = source_rows.get(event.source_enrollment_id)
        if (
            source is None
            or source.student_id != event.membership.student_id
            or source.schedule.training_group_id != event.membership.training_group_id
            or source.training_group_membership_id != event.membership_id
            or source.created_from not in PERMANENT_ENROLLMENT_SOURCES
            or event.membership.source != TrainingGroupMembership.Source.MIGRATION
        ):
            invalid.append("invalid_migration_source_link")
            break
    if TrainingGroupMembership.objects.for_club(club).filter(
        source=TrainingGroupMembership.Source.MIGRATION
    ).exclude(id__in=migration_source_events.values_list("membership_id", flat=True)).exists():
        invalid.append("missing_migration_source_link")

    open_memberships = TrainingGroupMembership.objects.for_club(club).filter(
        ends_on__isnull=True,
        status__in=[
            TrainingGroupMembership.Status.ACTIVE,
            TrainingGroupMembership.Status.FROZEN,
        ],
    )
    active_slots = Schedule.objects.for_club(club).filter(
        training_group_id__isnull=False,
        one_time_date__isnull=True,
        is_active=True,
    )
    for membership in open_memberships.only("id", "training_group_id", "student_id"):
        slot_ids = list(
            active_slots.filter(training_group_id=membership.training_group_id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        linked_slot_ids = list(
            ScheduleEnrollment.objects.for_club(club)
            .filter(
                training_group_membership_id=membership.id,
                schedule_id__in=slot_ids,
                ends_on__isnull=True,
                status__in=[
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.FROZEN,
                ],
            )
            .order_by("schedule_id")
            .values_list("schedule_id", flat=True)
        )
        if linked_slot_ids != slot_ids:
            invalid.append("incomplete_membership_projection_coverage")
            break
    if (
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            schedule__training_group_id__isnull=False,
            training_group_membership_id__isnull=True,
            created_from__in=PERMANENT_ENROLLMENT_SOURCES,
            ends_on__isnull=True,
            status__in=[
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.Status.FROZEN,
            ],
        )
        .exists()
    ):
        invalid.append("unlinked_mapped_permanent_enrollment")

    payment_owned_memberships = TrainingGroupMembership.objects.for_club(club).filter(
        authority=TrainingGroupMembership.Authority.PAYMENT_OWNED
    )
    for membership in payment_owned_memberships.only("id"):
        if Payment.objects.for_club(club).filter(conversion_group_membership_id=membership.id).count() != 1:
            invalid.append("invalid_payment_owned_membership")
            break
    if (
        Payment.objects.for_club(club)
        .filter(conversion_group_membership_id__isnull=False)
        .exclude(conversion_group_membership__authority=TrainingGroupMembership.Authority.PAYMENT_OWNED)
        .exists()
    ):
        invalid.append("invalid_conversion_group_ownership")

    dirty_mapping_events = TrainingGroupMappingEvent.objects.for_club(club).filter(
        schedule_id__isnull=False
    ).exclude(schedule__training_group_id=F("training_group_id")).exists()
    if dirty_mapping_events:
        invalid.append("dirty_mapping_event")

    return {
        "club_id": club.id,
        "rollout_state_count": rollout_state_count,
        "training_group_count": TrainingGroup.objects.for_club(club).count(),
        "linked_schedule_count": linked_schedules.count(),
        "membership_count": TrainingGroupMembership.objects.for_club(club).count(),
        "projection_count": projection_count,
        "mapping_event_count": TrainingGroupMappingEvent.objects.for_club(club).count(),
        "invalid": sorted(set(invalid)),
        "valid": not invalid,
    }
