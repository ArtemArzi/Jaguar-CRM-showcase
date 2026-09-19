"""Read-only inventory queries for the Training Group reconciliation preview."""

from __future__ import annotations

from collections import defaultdict

from django.conf import settings

from apps.attendance.models import (
    Checkin,
    GroupSession,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMembership,
    TrainingGroupRolloutState,
)
from apps.billing.models import BankPaymentOrder, Payment, PaymentRefundCase

PERMANENT_ENROLLMENT_SOURCES = (
    ScheduleEnrollment.CreatedFrom.MANUAL,
    ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    ScheduleEnrollment.CreatedFrom.IMPORT,
)


def get_training_group_payment_selection_capability(*, club) -> dict[str, str | bool]:
    """Read the current server-owned semantic mode for payment selection."""

    rollout_state = TrainingGroupRolloutState.objects.for_club(club).only("mode").first()
    mode = rollout_state.mode if rollout_state is not None else "missing"
    if mode == TrainingGroupRolloutState.Mode.OFF:
        selection_mode = "legacy"
    elif (
        mode == TrainingGroupRolloutState.Mode.SHADOW
        and settings.TRAINING_GROUP_NEW_WRITES_ENABLED
    ):
        selection_mode = "legacy"
    elif (
        mode == TrainingGroupRolloutState.Mode.ACTIVE
        and settings.TRAINING_GROUP_NEW_WRITES_ENABLED
    ):
        selection_mode = "canonical"
    else:
        selection_mode = "disabled"

    return {
        "training_group_rollout_mode": mode,
        "training_group_payment_selection_mode": selection_mode,
        # Retained for clients that only need card presentation. It must not be
        # interpreted as permission to fall back to legacy schedule selection.
        "canonical_group_selection_enabled": selection_mode == "canonical",
    }


def get_training_group_management_summary(*, club) -> list[dict]:
    """Return read-only canonical lifecycle and archive-precondition facts."""

    groups = list(
        TrainingGroup.objects.for_club(club)
        .select_related("training_type", "location", "responsible_trainer")
        .order_by("name", "id")
    )
    group_ids = [group.id for group in groups]
    if not group_ids:
        return []

    slots_by_group: dict[int, list[Schedule]] = defaultdict(list)
    for schedule in (
        Schedule.objects.for_club(club)
        .filter(training_group_id__in=group_ids)
        .select_related("trainer")
        .order_by("training_group_id", "day_of_week", "start_time", "id")
    ):
        slots_by_group[schedule.training_group_id].append(schedule)

    active_slots_by_group: dict[int, int] = defaultdict(int)
    for group_id in (
        Schedule.objects.for_club(club)
        .filter(training_group_id__in=group_ids, is_active=True)
        .order_by("training_group_id", "id")
        .values_list("training_group_id", flat=True)
    ):
        active_slots_by_group[group_id] += 1

    open_memberships_by_group: dict[int, int] = defaultdict(int)
    for group_id in (
        TrainingGroupMembership.objects.for_club(club)
        .filter(
            training_group_id__in=group_ids,
            ends_on__isnull=True,
            status__in=[
                TrainingGroupMembership.Status.ACTIVE,
                TrainingGroupMembership.Status.FROZEN,
            ],
        )
        .order_by("training_group_id", "id")
        .values_list("training_group_id", flat=True)
    ):
        open_memberships_by_group[group_id] += 1

    pending_payments_by_group: dict[int, int] = defaultdict(int)
    for group_id in (
        Payment.objects.for_club(club)
        .filter(target_training_group_id__in=group_ids, status=Payment.Status.PENDING)
        .order_by("target_training_group_id", "id")
        .values_list("target_training_group_id", flat=True)
    ):
        pending_payments_by_group[group_id] += 1

    open_orders_by_group: dict[int, int] = defaultdict(int)
    for group_id in (
        BankPaymentOrder.objects.for_club(club)
        .filter(
            payment__target_training_group_id__in=group_ids,
            status__in=[
                BankPaymentOrder.Status.CREATED,
                BankPaymentOrder.Status.PENDING,
                BankPaymentOrder.Status.AUTHORIZED,
                BankPaymentOrder.Status.MANUAL_REVIEW,
            ],
        )
        .order_by("payment__target_training_group_id", "id")
        .values_list("payment__target_training_group_id", flat=True)
    ):
        open_orders_by_group[group_id] += 1

    result = []
    for group in groups:
        slots = slots_by_group[group.id]
        active_slot_ids = [slot.id for slot in slots if slot.is_active]
        open_membership_ids = list(
            TrainingGroupMembership.objects.for_club(club)
            .filter(
                training_group_id=group.id,
                ends_on__isnull=True,
                status__in=[
                    TrainingGroupMembership.Status.ACTIVE,
                    TrainingGroupMembership.Status.FROZEN,
                ],
            )
            .values_list("id", flat=True)
        )
        linked_open_row_count = (
            ScheduleEnrollment.objects.for_club(club)
            .filter(
                schedule_id__in=active_slot_ids,
                training_group_membership_id__in=open_membership_ids,
                ends_on__isnull=True,
                status__in=[
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.FROZEN,
                ],
            )
            .count()
        )
        missing_projection_count = max(
            0,
            len(active_slot_ids) * len(open_membership_ids) - linked_open_row_count,
        )
        unlinked_permanent_count = (
            ScheduleEnrollment.objects.for_club(club)
            .filter(
                schedule__training_group_id=group.id,
                training_group_membership_id__isnull=True,
                created_from__in=PERMANENT_ENROLLMENT_SOURCES,
                ends_on__isnull=True,
                status__in=[
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.FROZEN,
                ],
            )
            .count()
        )
        mapping_ready = (
            group.status == TrainingGroup.Status.ACTIVE
            and bool(slots)
            and all(
                slot.one_time_date is None
                and slot.training_type_id == group.training_type_id
                and slot.location_id == group.location_id
                for slot in slots
            )
            and missing_projection_count == 0
            and unlinked_permanent_count == 0
        )
        result.append({
            "group": group,
            "slots": slots,
            "active_slot_count": active_slots_by_group[group.id],
            "open_membership_count": open_memberships_by_group[group.id],
            "pending_payment_count": pending_payments_by_group[group.id],
            "open_bank_order_count": open_orders_by_group[group.id],
            "mapping_ready": mapping_ready,
            "missing_projection_count": missing_projection_count,
            "unlinked_permanent_count": unlinked_permanent_count,
            "archive_preconditions_clear": (
                group.status == TrainingGroup.Status.ACTIVE
                and active_slots_by_group[group.id] == 0
                and open_memberships_by_group[group.id] == 0
                and pending_payments_by_group[group.id] == 0
                and open_orders_by_group[group.id] == 0
            ),
        })
    return result


def get_training_group_reconciliation_inventory(*, club) -> list[dict]:
    """Return redacted, tenant-scoped facts for explicit schedule selection.

    This selector deliberately does not group schedules by their legacy display
    name. Callers must provide every exact schedule ID to the preview service.
    """
    schedules = list(
        Schedule.objects.for_club(club)
        .filter(one_time_date__isnull=True, training_type__kind="group")
        .select_related("training_type", "location", "trainer", "training_group")
        .order_by("id")
    )
    schedule_ids = [schedule.id for schedule in schedules]
    if not schedule_ids:
        return []

    enrollment_ids_by_schedule: dict[int, list[int]] = defaultdict(list)
    for enrollment in (
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            schedule_id__in=schedule_ids,
            created_from__in=PERMANENT_ENROLLMENT_SOURCES,
            ends_on__isnull=True,
            status__in=[ScheduleEnrollment.Status.ACTIVE, ScheduleEnrollment.Status.FROZEN],
        )
        .order_by("schedule_id", "id")
        .only("id", "schedule_id")
    ):
        enrollment_ids_by_schedule[enrollment.schedule_id].append(enrollment.id)

    payment_ids_by_schedule: dict[int, list[int]] = defaultdict(list)
    for payment in (
        Payment.objects.for_club(club)
        .filter(target_schedule_id__in=schedule_ids)
        .order_by("target_schedule_id", "id")
        .only("id", "target_schedule_id")
    ):
        payment_ids_by_schedule[payment.target_schedule_id].append(payment.id)

    refund_counts_by_schedule: dict[int, int] = defaultdict(int)
    for target_schedule_id in (
        PaymentRefundCase.objects.for_club(club)
        .filter(order__payment__target_schedule_id__in=schedule_ids)
        .order_by("order__payment__target_schedule_id", "id")
        .values_list("order__payment__target_schedule_id", flat=True)
    ):
        refund_counts_by_schedule[target_schedule_id] += 1

    history_counts: dict[str, dict[int, int]] = {
        "checkins": defaultdict(int),
        "sessions": defaultdict(int),
        "exceptions": defaultdict(int),
        "booking_events": defaultdict(int),
    }
    for key, queryset in (
        ("checkins", Checkin.objects.for_club(club).filter(schedule_id__in=schedule_ids)),
        ("sessions", GroupSession.objects.for_club(club).filter(schedule_id__in=schedule_ids)),
        ("exceptions", ScheduleException.objects.for_club(club).filter(schedule_id__in=schedule_ids)),
        ("booking_events", ScheduleBookingEvent.objects.for_club(club).filter(schedule_id__in=schedule_ids)),
    ):
        for schedule_id in queryset.order_by("schedule_id", "id").values_list("schedule_id", flat=True):
            history_counts[key][schedule_id] += 1

    return [
        {
            "schedule_id": schedule.id,
            "group_name": schedule.group_name,
            "day_of_week": schedule.day_of_week,
            "start_time": schedule.start_time.isoformat(),
            "end_time": schedule.end_time.isoformat(),
            "is_active": schedule.is_active,
            "training_type_id": schedule.training_type_id,
            "training_type_name": schedule.training_type.name,
            "location_id": schedule.location_id,
            "location_name": schedule.location.name,
            "trainer_id": schedule.trainer_id,
            "training_group_id": schedule.training_group_id,
            "permanent_enrollment_ids": enrollment_ids_by_schedule[schedule.id],
            "payment_target_ids": payment_ids_by_schedule[schedule.id],
            "refund_case_count": refund_counts_by_schedule[schedule.id],
            "history_counts": {
                "checkins": history_counts["checkins"][schedule.id],
                "sessions": history_counts["sessions"][schedule.id],
                "exceptions": history_counts["exceptions"][schedule.id],
                "booking_events": history_counts["booking_events"][schedule.id],
            },
        }
        for schedule in schedules
    ]
