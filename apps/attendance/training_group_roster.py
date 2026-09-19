"""One membership-aware expected-roster resolver for all attendance consumers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256

from django.db.models import Count

from apps.attendance.models import (
    Checkin,
    GroupSession,
    Schedule,
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
)
from apps.billing.models import Debt, Payment
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student

OPEN_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.ACTIVE,
    ScheduleEnrollment.Status.TRIAL,
    ScheduleEnrollment.Status.FROZEN,
)
TERMINAL_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.TRANSFERRED,
    ScheduleEnrollment.Status.CANCELLED,
)
_AUDIT_HORIZON_MAX_SCHEDULES = 200
_AUDIT_HORIZON_WEEKS = 8


@dataclass(frozen=True)
class ExpectedRosterEntry:
    student_id: int
    schedule_id: int
    source: str
    enrollment_id: int | None = None
    membership_id: int | None = None
    enrollment_status: str | None = None
    created_from: str = ""
    starts_on: date | None = None
    ends_on: date | None = None
    blocked_reason: str | None = None


def _is_cancelled_dated_booking(enrollment: ScheduleEnrollment) -> bool:
    return (
        enrollment.status == ScheduleEnrollment.Status.CANCELLED
        and enrollment.starts_on is not None
        and enrollment.starts_on == enrollment.ends_on
        and enrollment.created_from
        in {
            ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
            ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
            ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
            ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        }
    )


def _enrollment_is_effective(enrollment: ScheduleEnrollment, *, target_date: date) -> bool:
    if _is_cancelled_dated_booking(enrollment):
        return False
    # A terminal projection is historical evidence only.  It can never regain
    # roster authority from an implicit FK check or terminal-date fallback.
    if (
        enrollment.created_from == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        and enrollment.status in TERMINAL_ENROLLMENT_STATUSES
    ):
        return False
    if (
        enrollment.status in TERMINAL_ENROLLMENT_STATUSES
        and enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
    ):
        return False
    if enrollment.status not in OPEN_ENROLLMENT_STATUSES and not (
        enrollment.status in TERMINAL_ENROLLMENT_STATUSES
        and enrollment.ends_on is not None
        and enrollment.ends_on >= target_date
    ):
        return False
    if enrollment.starts_on is not None and enrollment.starts_on > target_date:
        return False
    if enrollment.ends_on is not None and enrollment.ends_on < target_date:
        return False
    return True


def _membership_is_effective(membership: TrainingGroupMembership, *, target_date: date) -> bool:
    return (
        membership.status
        in {TrainingGroupMembership.Status.ACTIVE, TrainingGroupMembership.Status.FROZEN}
        and membership.starts_on <= target_date
        and (membership.ends_on is None or membership.ends_on >= target_date)
    )


def resolve_expected_roster_by_schedule_date(
    *,
    club,
    schedule_ids: Iterable[int],
    target_date: date,
    legacy_window_days: int = 30,
    legacy_min_checkins: int = 1,
    legacy_include_target_date: bool = False,
) -> dict[int, dict[int, ExpectedRosterEntry]]:
    """Resolve membership, exact booking, unlinked legacy, then legacy fallback.

    The result is indexed by exact schedule and student.  A group membership is
    deliberately inserted first and therefore wins over its compatibility row
    and any dated booking for the same student/slot.  Frozen memberships remain
    visible with a stable blocked reason.
    """
    unique_schedule_ids = list(dict.fromkeys(schedule_ids))
    if not unique_schedule_ids:
        return {}
    schedules = list(
        Schedule.objects.for_club(club)
        .filter(id__in=unique_schedule_ids)
        .only("id", "training_group_id")
        .order_by("id")
    )
    roster: dict[int, dict[int, ExpectedRosterEntry]] = {schedule_id: {} for schedule_id in unique_schedule_ids}
    schedules_by_group: dict[int, list[int]] = {}
    for schedule in schedules:
        if schedule.training_group_id:
            schedules_by_group.setdefault(schedule.training_group_id, []).append(schedule.id)

    if schedules_by_group:
        memberships = list(
            TrainingGroupMembership.objects.for_club(club)
            .filter(training_group_id__in=schedules_by_group)
            .order_by("training_group_id", "student_id", "id")
        )
        for membership in memberships:
            if not _membership_is_effective(membership, target_date=target_date):
                continue
            for schedule_id in schedules_by_group[membership.training_group_id]:
                roster[schedule_id][membership.student_id] = ExpectedRosterEntry(
                    student_id=membership.student_id,
                    schedule_id=schedule_id,
                    source="training_group_membership",
                    membership_id=membership.id,
                    enrollment_status=membership.status,
                    created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
                    starts_on=membership.starts_on,
                    ends_on=membership.ends_on,
                    blocked_reason=(
                        "training_group_membership_frozen"
                        if membership.status == TrainingGroupMembership.Status.FROZEN
                        else None
                    ),
                )

        # S6 preserves legacy source rows exactly and links them through an
        # append-only membership event instead of rewriting their source/date
        # fields.  Those rows must never regain independent roster authority
        # after a canonical membership later freezes or becomes terminal.
        migrated_source_enrollment_ids = set(
            TrainingGroupMembershipEvent.objects.for_club(club)
            .filter(
                membership__training_group_id__in=schedules_by_group,
                action__in=["backfilled", "source_linked"],
                source_enrollment_id__isnull=False,
            )
            .values_list("source_enrollment_id", flat=True)
        )
    else:
        migrated_source_enrollment_ids = set()

    enrollments = list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(schedule_id__in=unique_schedule_ids)
        .order_by("schedule_id", "student_id", "-starts_on", "-id")
    )
    pairs_with_any_enrollment = {(row.schedule_id, row.student_id) for row in enrollments}
    for enrollment in enrollments:
        if enrollment.id in migrated_source_enrollment_ids:
            continue
        if not _enrollment_is_effective(enrollment, target_date=target_date):
            continue
        schedule_roster = roster[enrollment.schedule_id]
        # Membership state is canonical, and a projection is never an independent
        # participant in legacy or dated-booking precedence.
        if enrollment.student_id in schedule_roster or enrollment.training_group_membership_id:
            continue
        schedule_roster[enrollment.student_id] = ExpectedRosterEntry(
            student_id=enrollment.student_id,
            schedule_id=enrollment.schedule_id,
            source="exact_booking" if enrollment.starts_on == enrollment.ends_on else "legacy_enrollment",
            enrollment_id=enrollment.id,
            enrollment_status=enrollment.status,
            created_from=enrollment.created_from,
            starts_on=enrollment.starts_on,
            ends_on=enrollment.ends_on,
            blocked_reason=(
                "enrollment_frozen" if enrollment.status == ScheduleEnrollment.Status.FROZEN else None
            ),
        )

    legacy_cutoff = target_date - timedelta(days=legacy_window_days)
    legacy_rows = (
        Checkin.objects.for_club(club)
        .filter(
            schedule_id__in=unique_schedule_ids,
            date__gte=legacy_cutoff,
            **({"date__lte": target_date} if legacy_include_target_date else {"date__lt": target_date}),
            deleted_at__isnull=True,
        )
        .values("schedule_id", "student_id")
        .annotate(checkin_count=Count("id"))
        .filter(checkin_count__gte=legacy_min_checkins)
        .order_by("schedule_id", "student_id")
    )
    for row in legacy_rows:
        schedule_id = row["schedule_id"]
        student_id = row["student_id"]
        if (schedule_id, student_id) in pairs_with_any_enrollment or student_id in roster[schedule_id]:
            continue
        roster[schedule_id][student_id] = ExpectedRosterEntry(
            student_id=student_id,
            schedule_id=schedule_id,
            source="legacy_attended_checkin",
        )

    expected_student_ids = {student_id for rows in roster.values() for student_id in rows}
    if not expected_student_ids:
        return roster
    valid_student_ids = set(
        Student.objects.for_club(club)
        .filter(id__in=expected_student_ids, deleted_at__isnull=True)
        .exclude(status__in=[Student.Status.CHURNED, Student.Status.LOST])
        .values_list("id", flat=True)
    )
    explicit_roster_student_ids = {
        entry.student_id
        for rows in roster.values()
        for entry in rows.values()
        if entry.source != "legacy_attended_checkin"
    }
    # Preserve the existing roster policy: a current explicit enrollment can
    # still make a churned student visible, while the legacy fallback cannot.
    valid_student_ids |= set(
        Student.objects.for_club(club)
        .filter(id__in=explicit_roster_student_ids, status=Student.Status.CHURNED, deleted_at__isnull=True)
        .values_list("id", flat=True)
    )
    return {
        schedule_id: {
            student_id: entry for student_id, entry in rows.items() if student_id in valid_student_ids
        }
        for schedule_id, rows in roster.items()
    }


def resolve_expected_roster_for_schedule_date(
    *,
    club,
    schedule_id: int,
    target_date: date,
    legacy_window_days: int = 30,
    legacy_min_checkins: int = 1,
    legacy_include_target_date: bool = False,
) -> dict[int, ExpectedRosterEntry]:
    return resolve_expected_roster_by_schedule_date(
        club=club,
        schedule_ids=[schedule_id],
        target_date=target_date,
        legacy_window_days=legacy_window_days,
        legacy_min_checkins=legacy_min_checkins,
        legacy_include_target_date=legacy_include_target_date,
    ).get(schedule_id, {})


def _redacted_roster_digest(*, student_ids: set[int]) -> str:
    return sha256(",".join(str(student_id) for student_id in sorted(student_ids)).encode("ascii")).hexdigest()


def _adjacent_schedule_occurrences(*, schedule: Schedule, boundary: date) -> set[date]:
    """Return the previous/current-or-next/next exact occurrence for a boundary."""
    if schedule.one_time_date is not None:
        return {schedule.one_time_date}
    days_until = (schedule.day_of_week - boundary.weekday()) % 7
    following = boundary + timedelta(days=days_until)
    previous = following - timedelta(days=7)
    return {previous, following, following + timedelta(days=7)}


def build_training_group_roster_audit_horizon(
    *,
    club,
    schedule_ids: Iterable[int],
    as_of_date: date,
) -> dict[int, tuple[date, ...]]:
    """Build a bounded, deterministic, non-PII roster-comparison horizon.

    Historical affected dates are retained exactly.  Lifecycle/payment
    boundaries contribute the adjacent concrete slot occurrences, and every
    active mapped slot of the selected group contributes its next eight weeks
    from ``as_of_date``.
    """
    selected_schedule_ids = list(dict.fromkeys(schedule_ids))
    if len(selected_schedule_ids) > _AUDIT_HORIZON_MAX_SCHEDULES:
        raise BusinessLogicError(
            "Too many schedules were requested for a roster audit horizon.",
            code="training_group_audit_horizon_too_large",
        )
    if not selected_schedule_ids:
        return {}
    selected_schedules = list(
        Schedule.objects.for_club(club)
        .filter(id__in=selected_schedule_ids)
        .only("id", "is_active", "day_of_week", "one_time_date", "training_group_id")
        .order_by("id")
    )
    selected_group_ids = {schedule.training_group_id for schedule in selected_schedules if schedule.training_group_id}
    mapped_schedules = list(
        Schedule.objects.for_club(club)
        .filter(
            training_group_id__in=selected_group_ids,
            is_active=True,
            one_time_date__isnull=True,
        )
        .only("id", "is_active", "day_of_week", "one_time_date", "training_group_id")
        .order_by("id")
    )
    schedules_by_id = {schedule.id: schedule for schedule in [*selected_schedules, *mapped_schedules]}
    horizon: dict[int, set[date]] = {schedule_id: set() for schedule_id in schedules_by_id}
    if not schedules_by_id:
        return {}
    active_schedules = [
        schedule
        for schedule in schedules_by_id.values()
        if schedule.is_active and schedule.training_group_id and schedule.one_time_date is None
    ]

    for schedule_id, occurred_on in Checkin.objects.for_club(club).filter(
        schedule_id__in=schedules_by_id
    ).values_list("schedule_id", "date"):
        horizon[schedule_id].add(occurred_on)
    for schedule_id, occurred_on in GroupSession.objects.for_club(club).filter(
        schedule_id__in=schedules_by_id
    ).values_list("schedule_id", "date"):
        horizon[schedule_id].add(occurred_on)
    for schedule_id, occurred_on in Debt.objects.for_club(club).filter(
        checkin__schedule_id__in=schedules_by_id
    ).values_list("checkin__schedule_id", "checkin__date"):
        horizon[schedule_id].add(occurred_on)

    boundaries_by_schedule: dict[int, set[date]] = {schedule_id: set() for schedule_id in schedules_by_id}
    for schedule_id, starts_on, ends_on in ScheduleEnrollment.objects.for_club(club).filter(
        schedule_id__in=schedules_by_id
    ).values_list("schedule_id", "starts_on", "ends_on"):
        boundaries_by_schedule[schedule_id].update(boundary for boundary in (starts_on, ends_on) if boundary)
    for schedule_id, target_start_date in Payment.objects.for_club(club).filter(
        target_schedule_id__in=schedules_by_id,
        target_start_date__isnull=False,
    ).values_list("target_schedule_id", "target_start_date"):
        boundaries_by_schedule[schedule_id].add(target_start_date)

    group_ids = {schedule.training_group_id for schedule in active_schedules if schedule.training_group_id}
    if group_ids:
        boundaries_by_group: dict[int, set[date]] = {group_id: set() for group_id in group_ids}
        for group_id, starts_on, ends_on in TrainingGroupMembership.objects.for_club(club).filter(
            training_group_id__in=group_ids
        ).values_list("training_group_id", "starts_on", "ends_on"):
            boundaries_by_group[group_id].update(boundary for boundary in (starts_on, ends_on) if boundary)
        for schedule in active_schedules:
            if schedule.training_group_id:
                boundaries_by_schedule[schedule.id].update(boundaries_by_group[schedule.training_group_id])

    eight_week_end = as_of_date + timedelta(days=(_AUDIT_HORIZON_WEEKS * 7) - 1)
    for schedule in active_schedules:
        for boundary in boundaries_by_schedule[schedule.id]:
            horizon[schedule.id].update(_adjacent_schedule_occurrences(schedule=schedule, boundary=boundary))
        if schedule.one_time_date is not None:
            if as_of_date <= schedule.one_time_date <= eight_week_end:
                horizon[schedule.id].add(schedule.one_time_date)
            continue
        for offset in range((schedule.day_of_week - as_of_date.weekday()) % 7, _AUDIT_HORIZON_WEEKS * 7, 7):
            horizon[schedule.id].add(as_of_date + timedelta(days=offset))
    return {schedule_id: tuple(sorted(dates)) for schedule_id, dates in sorted(horizon.items())}


def assert_expected_roster_comparison(
    *,
    club,
    schedule_id: int,
    target_date: date,
    expected_student_ids: set[int],
    mode: str,
) -> dict[str, str | int]:
    """Fail closed with count/digest evidence, never student contacts or payloads."""
    if mode not in {"legacy_parity", "owner_delta"}:
        raise BusinessLogicError(
            "Unsupported expected-roster comparison mode.",
            code="training_group_roster_comparison_mode_invalid",
        )
    actual_student_ids = set(
        resolve_expected_roster_for_schedule_date(
            club=club,
            schedule_id=schedule_id,
            target_date=target_date,
        )
    )
    if actual_student_ids != expected_student_ids:
        raise BusinessLogicError(
            "Expected roster comparison failed.",
            code=(
                "training_group_legacy_parity_mismatch"
                if mode == "legacy_parity"
                else "training_group_owner_delta_mismatch"
            ),
        )
    return {
        "mode": mode,
        "schedule_id": schedule_id,
        "target_date": target_date.isoformat(),
        "student_count": len(actual_student_ids),
        "student_ids_digest": _redacted_roster_digest(student_ids=actual_student_ids),
    }


def assert_expected_roster_horizon_comparison(
    *,
    club,
    schedule_ids: Iterable[int],
    as_of_date: date,
    expected_student_ids_by_slot: dict[tuple[int, date], set[int]],
    mode: str,
) -> dict[str, int | str]:
    """Compare every deterministic horizon slot and return redacted evidence."""
    if mode not in {"legacy_parity", "owner_delta"}:
        raise BusinessLogicError(
            "Unsupported expected-roster comparison mode.",
            code="training_group_roster_comparison_mode_invalid",
        )
    horizon = build_training_group_roster_audit_horizon(
        club=club,
        schedule_ids=schedule_ids,
        as_of_date=as_of_date,
    )
    evidence_parts: list[str] = []
    for schedule_id, dates in horizon.items():
        for target_date in dates:
            evidence = assert_expected_roster_comparison(
                club=club,
                schedule_id=schedule_id,
                target_date=target_date,
                expected_student_ids=expected_student_ids_by_slot.get((schedule_id, target_date), set()),
                mode=mode,
            )
            evidence_parts.append(
                f"{schedule_id}:{target_date.isoformat()}:{evidence['student_ids_digest']}"
            )
    return {
        "mode": mode,
        "slot_count": len(evidence_parts),
        "horizon_digest": sha256("|".join(evidence_parts).encode("ascii")).hexdigest(),
    }


def snapshot_expected_roster_horizon(
    *,
    club,
    schedule_ids: Iterable[int],
    as_of_date: date,
) -> dict[str, int | str]:
    """Freeze a redacted digest of the current roster over the deterministic horizon."""
    horizon = build_training_group_roster_audit_horizon(
        club=club,
        schedule_ids=schedule_ids,
        as_of_date=as_of_date,
    )
    expected = {
        (schedule_id, target_date): set(
            resolve_expected_roster_for_schedule_date(
                club=club,
                schedule_id=schedule_id,
                target_date=target_date,
            )
        )
        for schedule_id, dates in horizon.items()
        for target_date in dates
    }
    return assert_expected_roster_horizon_comparison(
        club=club,
        schedule_ids=schedule_ids,
        as_of_date=as_of_date,
        expected_student_ids_by_slot=expected,
        mode="owner_delta",
    )
