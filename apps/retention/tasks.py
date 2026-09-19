from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def daily_retention_check() -> dict:
    from apps.clubs.models import Club
    from apps.retention.services import check_renewal_triggers, check_retention_triggers

    results = {"clubs_checked": 0, "tasks_created": 0, "status_changes": 0, "renewal_tasks": 0}
    for club in Club.objects.filter(is_active=True):
        club_result = check_retention_triggers(club=club)
        renewal_result = check_renewal_triggers(club=club)
        results["clubs_checked"] += 1
        results["tasks_created"] += club_result["tasks_created"]
        results["status_changes"] += club_result["status_changes"]
        results["renewal_tasks"] += renewal_result["tasks_created"]
    logger.info("daily_retention_check_complete", extra=results)
    return results


def create_post_trial_task(
    student_id: int,
    club_id: int,
    trainer_id: int,
    checkin_id: int | None = None,
) -> None:
    """Thin wrapper for django-q2. Business logic in services.create_post_trial_task."""
    if checkin_id is not None:
        from apps.attendance.models import Checkin

        if not Checkin.objects.for_club(club_id).filter(
            id=checkin_id,
            student_id=student_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).exists():
            return

    from apps.retention.services import create_post_trial_task as _create

    _create(student_id=student_id, club_id=club_id, trainer_id=trainer_id)


def auto_close_retention_on_checkin(checkin_id: int, club_id: int) -> None:
    from apps.attendance.models import Checkin
    from apps.clubs.timezones import club_localdate
    from apps.retention.services import auto_close_retention_tasks
    from apps.students.models import Student

    checkin = (
        Checkin.objects.for_club(club_id)
        .filter(
            id=checkin_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
    )
    if checkin is None:
        return
    if checkin.date < club_localdate(checkin.club, checkin.created_at):
        return

    auto_close_retention_tasks(student_id=checkin.student_id, club_id=club_id)
    # Restore student status to active if was at_risk
    Student.objects.for_club(club_id).filter(
        id=checkin.student_id,
        status=Student.Status.AT_RISK,
    ).update(status=Student.Status.ACTIVE)


def reverse_auto_close_retention(checkin_id: int, club_id: int) -> None:
    """T4: when a checkin is cancelled, re-open retention tasks it auto-closed.

    Reads the (possibly soft-deleted) Checkin to get its created_at, then
    re-opens AUTO_CHECKIN tasks for the student that resolved at-or-after
    that timestamp.
    """
    from apps.attendance.models import Checkin
    from apps.clubs.timezones import club_localdate
    from apps.retention.services import reopen_retention_tasks_auto_closed

    # Read soft-deleted rows: cancel_checkin soft-deletes the Checkin
    # before scheduling this task, so the default manager would miss it.
    checkin = Checkin._base_manager.filter(
        id=checkin_id, club_id=club_id,
    ).first()
    if checkin is None:
        logger.warning(
            "reverse_auto_close_retention_no_checkin",
            extra={"checkin_id": checkin_id, "club_id": club_id},
        )
        return
    if checkin.date < club_localdate(checkin.club, checkin.created_at):
        return
    reopen_retention_tasks_auto_closed(
        student_id=checkin.student_id,
        club_id=club_id,
        after=checkin.created_at,
    )
