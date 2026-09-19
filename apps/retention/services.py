from __future__ import annotations

import logging
import zoneinfo
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.retention.models import RetentionTask

logger = logging.getLogger(__name__)

YELLOW_MULTIPLIER = 2.0
RED_MULTIPLIER = 3.5
CHURNED_DAYS = 30
MIN_YELLOW_DAYS = 4
MAX_YELLOW_DAYS = 10
MIN_RED_DAYS = 7
MAX_RED_DAYS = 21
MIN_CHECKINS_FOR_BASELINE = 3


def calculate_student_thresholds(*, checkins_last_30: int) -> tuple[int, int]:
    """Returns (yellow_days, red_days) based on visit frequency."""
    if checkins_last_30 < MIN_CHECKINS_FOR_BASELINE:
        return MAX_YELLOW_DAYS, MAX_RED_DAYS

    avg_gap = 30.0 / checkins_last_30
    yellow = max(MIN_YELLOW_DAYS, min(int(avg_gap * YELLOW_MULTIPLIER), MAX_YELLOW_DAYS))
    red = max(MIN_RED_DAYS, min(int(avg_gap * RED_MULTIPLIER), MAX_RED_DAYS))
    return yellow, red


def check_retention_triggers(*, club) -> dict:
    """Check all active/at_risk students in a club for retention triggers.
    Uses batch annotated query (no per-student queries).
    Returns {"tasks_created": N, "status_changes": N}.
    """
    from apps.students.models import Student

    club_tz = zoneinfo.ZoneInfo(club.timezone)
    today = timezone.now().astimezone(club_tz).date()
    thirty_days_ago = today - timedelta(days=30)

    # Batch: annotate all active/at_risk students with checkins in last 30 days
    students = (
        Student.objects.for_club(club)
        .filter(
            status__in=[Student.Status.ACTIVE, Student.Status.AT_RISK],
            deleted_at__isnull=True,
        )
        .annotate(
            checkins_30d=Count(
                "checkins",
                filter=Q(
                    checkins__date__gte=thirty_days_ago,
                    checkins__deleted_at__isnull=True,
                ),
            )
        )
    )

    # Pre-load open tasks and last checkins to avoid N+1 in the loop
    from apps.attendance.models import Checkin

    open_tasks: dict = {}
    for task in RetentionTask.objects.for_club(club).filter(resolved_at__isnull=True).order_by("-created_at"):
        # Keep only the most recent task per student
        if task.student_id not in open_tasks:
            open_tasks[task.student_id] = task

    # Pre-load last checkin per student: 1) find max date, 2) fetch objects
    from django.db.models import Max

    last_checkin_objs: dict = {}
    student_ids = [s.id for s in students]
    if student_ids:
        # Subquery: latest checkin id per student
        latest_ids = (
            Checkin.objects.for_club(club)
            .filter(student_id__in=student_ids, deleted_at__isnull=True)
            .values("student_id")
            .annotate(last_id=Max("id"))
            .values_list("last_id", flat=True)
        )
        for checkin in Checkin.objects.for_club(club).filter(id__in=latest_ids).select_related("trainer"):
            last_checkin_objs[checkin.student_id] = checkin

    tasks_created = 0
    status_changes = 0

    for student in students:
        result = _process_student_retention(
            student=student,
            club=club,
            today=today,
            existing_task=open_tasks.get(student.id),
            last_checkin=last_checkin_objs.get(student.id),
        )
        tasks_created += result["tasks_created"]
        status_changes += result["status_changes"]

    logger.info(
        "retention_check_complete",
        extra={"club_id": club.id, "tasks_created": tasks_created, "status_changes": status_changes},
    )
    return {"tasks_created": tasks_created, "status_changes": status_changes}


_LEVEL_PRIORITY = {
    RetentionTask.Level.YELLOW: 0,
    RetentionTask.Level.RED: 1,
    RetentionTask.Level.CHURNED: 2,
}


def _process_student_retention(
    *,
    student,
    club,
    today: date,
    existing_task=None,
    last_checkin=None,
) -> dict:
    from apps.students.models import Student

    result = {"tasks_created": 0, "status_changes": 0}

    if not student.last_visit_date:
        return result

    days_inactive = (today - student.last_visit_date).days
    if days_inactive <= 0:
        return result

    yellow_days, red_days = calculate_student_thresholds(checkins_last_30=student.checkins_30d)

    # Determine level
    if days_inactive >= CHURNED_DAYS:
        level = RetentionTask.Level.CHURNED
        new_status = Student.Status.CHURNED
    elif days_inactive >= red_days:
        level = RetentionTask.Level.RED
        new_status = Student.Status.AT_RISK
    elif days_inactive >= yellow_days:
        level = RetentionTask.Level.YELLOW
        new_status = Student.Status.AT_RISK
    else:
        return result

    # Update student status if needed
    if student.status != new_status:
        Student.objects.for_club(club).filter(id=student.id).update(status=new_status)
        result["status_changes"] = 1

        # Trigger win-back pipeline and churned survey when student becomes churned
        if new_status == Student.Status.CHURNED:
            from apps.pipelines.services import trigger_pipeline

            trigger_pipeline(
                club_id=club.id, student_id=student.id, pipeline_type="win_back"
            )
            from django_q.tasks import async_task as q_async_task

            q_async_task(
                "apps.feedback.tasks.send_churned_survey_push",
                student.id,
                club.id,
            )

    # Update existing task level if escalated, or create new
    if existing_task:
        existing_priority = _LEVEL_PRIORITY.get(existing_task.level, -1)
        new_priority = _LEVEL_PRIORITY.get(level, -1)
        if new_priority > existing_priority:
            old_level = existing_task.level
            existing_task.level = level
            existing_task.save(update_fields=["level", "updated_at"])
            logger.info(
                "retention_task_level_updated",
                extra={"task_id": existing_task.id, "old_level": old_level, "new_level": level, "club_id": club.id},
            )
        return result

    if not last_checkin:
        return result

    task = RetentionTask.objects.create(
        club=club,
        student=student,
        trainer=last_checkin.trainer,
        level=level,
        due_date=today,
    )
    try:
        from apps.notifications.services import send_trainer_retention_task_notification

        send_trainer_retention_task_notification(club=club, task=task)
    except Exception:
        logger.exception(
            "retention_trainer_notification_failed",
            extra={"task_id": task.id, "club_id": club.id, "trainer_id": task.trainer_id},
        )
    result["tasks_created"] = 1
    return result


def check_renewal_triggers(*, club) -> dict:
    """Scan active subscriptions for expiring ones. Create renewal tasks."""
    from apps.attendance.models import Checkin
    from apps.billing.models import Subscription

    today = date.today()
    seven_days = today + timedelta(days=7)

    expiring_student_ids = list(
        Subscription.objects.for_club(club)
        .filter(status=Subscription.Status.ACTIVE, deleted_at__isnull=True)
        .filter(
            Q(trainings_left__isnull=False, trainings_left__lte=2) |
            Q(expires_at__lte=seven_days)
        )
        .values_list("student_id", flat=True)
        .distinct()
    )

    existing = set(
        RetentionTask.objects.for_club(club)
        .filter(task_type=RetentionTask.TaskType.RENEWAL, resolved_at__isnull=True)
        .values_list("student_id", flat=True)
    )

    new_student_ids = [sid for sid in expiring_student_ids if sid not in existing]
    if not new_student_ids:
        return {"tasks_created": 0}

    # Batch: find last trainer for each student in one query
    from django.db.models import Max

    last_checkin_dates = (
        Checkin.objects.for_club(club)
        .filter(student_id__in=new_student_ids, deleted_at__isnull=True)
        .values("student_id")
        .annotate(last_date=Max("date"))
    )
    # Build {student_id: trainer_id} map
    trainer_by_student: dict[int, int] = {}
    for row in last_checkin_dates:
        checkin = (
            Checkin.objects.for_club(club)
            .filter(student_id=row["student_id"], date=row["last_date"], deleted_at__isnull=True)
            .values_list("trainer_id", flat=True)
            .first()
        )
        if checkin:
            trainer_by_student[row["student_id"]] = checkin

    tasks_created = 0
    for student_id in new_student_ids:
        trainer_id = trainer_by_student.get(student_id)
        if not trainer_id:
            continue
        RetentionTask.objects.create(
            club=club,
            student_id=student_id,
            trainer_id=trainer_id,
            task_type=RetentionTask.TaskType.RENEWAL,
            level="",
            due_date=today,
        )
        tasks_created += 1

    if tasks_created:
        logger.info("renewal_tasks_created", extra={"club_id": club.id, "count": tasks_created})
    return {"tasks_created": tasks_created}


def create_lead_task(*, student_id: int, club_id: int, trainer_id: int) -> RetentionTask | None:
    """Create a new_lead task. Returns None if task already exists."""
    if RetentionTask.objects.for_club(club_id).filter(
        student_id=student_id, task_type=RetentionTask.TaskType.NEW_LEAD, resolved_at__isnull=True,
    ).exists():
        return None

    task = RetentionTask.objects.create(
        club_id=club_id,
        student_id=student_id,
        trainer_id=trainer_id,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        level="",
        due_date=date.today(),
    )
    logger.info("lead_task_created", extra={"student_id": student_id, "club_id": club_id})
    return task


def _merge_task_notes(existing: str, addition: str) -> str:
    if not addition:
        return existing
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}\n\n{addition}"


def _validate_task_scope(*, club_id: int, student_id: int, trainer_id: int) -> None:
    from apps.students.models import Student
    from apps.trainers.models import Trainer

    Student.objects.for_club(club_id).only("id").get(id=student_id)
    Trainer.objects.for_club(club_id).only("id").get(id=trainer_id)


def _update_reused_task(
    *,
    task: RetentionTask,
    trainer_id: int,
    due_date: date,
    notes: str,
) -> RetentionTask:
    update_fields: list[str] = []
    if task.trainer_id != trainer_id:
        task.trainer_id = trainer_id
        update_fields.append("trainer")
    if task.status != RetentionTask.TaskStatus.SNOOZED and task.due_date != due_date:
        task.due_date = due_date
        update_fields.append("due_date")

    merged_notes = _merge_task_notes(task.notes, notes)
    if merged_notes != task.notes:
        task.notes = merged_notes
        update_fields.append("notes")

    if update_fields:
        task.full_clean()
        task.save(update_fields=[*update_fields, "updated_at"])
    return task


def create_or_reuse_retention_task(
    *,
    student_id: int,
    club_id: int,
    trainer_id: int,
    task_type: str,
    due_date: date,
    notes: str = "",
) -> RetentionTask:
    """Create or reuse the unresolved task for this student/type."""
    _validate_task_scope(club_id=club_id, student_id=student_id, trainer_id=trainer_id)

    try:
        with transaction.atomic():
            task = (
                RetentionTask.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    student_id=student_id,
                    task_type=task_type,
                    resolved_at__isnull=True,
                )
                .first()
            )
            if task:
                return _update_reused_task(
                    task=task,
                    trainer_id=trainer_id,
                    due_date=due_date,
                    notes=notes,
                )

            task = RetentionTask(
                club_id=club_id,
                student_id=student_id,
                trainer_id=trainer_id,
                task_type=task_type,
                level="",
                due_date=due_date,
                notes=notes,
            )
            task.full_clean()
            task.save()
            logger.info(
                "retention_task_created",
                extra={"task_id": task.id, "club_id": club_id, "task_type": task_type},
            )
            return task
    except IntegrityError:
        with transaction.atomic():
            task = (
                RetentionTask.objects.for_club(club_id)
                .select_for_update()
                .get(
                    student_id=student_id,
                    task_type=task_type,
                    resolved_at__isnull=True,
                )
            )
            return _update_reused_task(
                task=task,
                trainer_id=trainer_id,
                due_date=due_date,
                notes=notes,
            )


def create_post_trial_task(*, student_id: int, club_id: int, trainer_id: int) -> RetentionTask | None:
    """Create post_trial task if student is lead/trial with no active subscription."""
    from apps.billing.models import Subscription
    from apps.students.models import Student

    student = Student.objects.for_club(club_id).get(id=student_id)
    if student.status not in (Student.Status.LEAD, Student.Status.TRIAL):
        return None

    has_active_sub = Subscription.objects.for_club(club_id).filter(
        student_id=student_id, status=Subscription.Status.ACTIVE,
    ).exists()
    if has_active_sub:
        return None

    if RetentionTask.objects.for_club(club_id).filter(
        student_id=student_id, task_type=RetentionTask.TaskType.POST_TRIAL, resolved_at__isnull=True,
    ).exists():
        return None

    task = RetentionTask.objects.create(
        club_id=club_id,
        student_id=student_id,
        trainer_id=trainer_id,
        task_type=RetentionTask.TaskType.POST_TRIAL,
        level="",
        due_date=date.today() + timedelta(days=1),
    )
    logger.info("post_trial_task_created", extra={"student_id": student_id, "club_id": club_id})
    return task


def close_retention_task(*, task_id: int, club_id: int, resolution: str, notes: str = "") -> RetentionTask:
    from django.db import transaction

    with transaction.atomic():
        task = RetentionTask.objects.for_club(club_id).select_for_update().get(id=task_id, resolved_at__isnull=True)
        task.resolved_at = timezone.now()
        task.resolution = resolution
        task.notes = notes
        task.status = RetentionTask.TaskStatus.CLOSED
        task.save(update_fields=["resolved_at", "resolution", "notes", "status", "updated_at"])
    logger.info("retention_task_closed", extra={"task_id": task.id, "resolution": resolution})
    return task


def add_task_comment(*, task_id: int, club_id: int, author_id: int, text: str):
    from apps.retention.models import TaskComment

    task = RetentionTask.objects.for_club(club_id).get(id=task_id)
    comment = TaskComment.objects.create(
        club_id=club_id, task=task, author_id=author_id, text=text
    )
    logger.info("task_comment_added", extra={"task_id": task_id, "author_id": author_id})
    return comment


def snooze_task(
    *, task_id: int, club_id: int, new_due_date: date, increment_attempt: bool = False,
) -> RetentionTask:
    from django.db import transaction

    with transaction.atomic():
        task = (
            RetentionTask.objects.for_club(club_id)
            .select_for_update()
            .get(id=task_id, resolved_at__isnull=True)
        )
        task.due_date = new_due_date
        task.status = RetentionTask.TaskStatus.SNOOZED
        update_fields = ["due_date", "status", "updated_at"]
        if increment_attempt:
            task.attempt_count += 1
            update_fields.append("attempt_count")
        task.save(update_fields=update_fields)
    logger.info(
        "task_snoozed",
        extra={"task_id": task_id, "new_due_date": str(new_due_date), "attempt_count": task.attempt_count},
    )
    return task


def update_task_status(*, task_id: int, club_id: int, status: str) -> RetentionTask:
    task = RetentionTask.objects.for_club(club_id).get(id=task_id)
    task.status = status
    update_fields = ["status", "updated_at"]
    if status == "closed" and not task.resolved_at:
        task.resolved_at = timezone.now()
        task.resolution = RetentionTask.Resolution.MANUAL_OTHER
        update_fields.extend(["resolved_at", "resolution"])
    task.save(update_fields=update_fields)
    logger.info("task_status_updated", extra={"task_id": task_id, "status": status})
    return task


def auto_close_retention_tasks(*, student_id: int, club_id: int) -> int:
    """Close retention + new_lead tasks when student checks in."""
    updated = (
        RetentionTask.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            resolved_at__isnull=True,
            task_type__in=[RetentionTask.TaskType.RETENTION, RetentionTask.TaskType.NEW_LEAD],
        )
        .update(
            resolved_at=timezone.now(),
            resolution=RetentionTask.Resolution.AUTO_CHECKIN,
            status=RetentionTask.TaskStatus.CLOSED,
        )
    )
    if updated:
        logger.info(
            "retention_tasks_auto_closed",
            extra={"student_id": student_id, "club_id": club_id, "count": updated},
        )
    return updated


def auto_close_tasks_on_subscription(*, student_id: int, club_id: int) -> int:
    """Close post_trial, new_lead, and renewal tasks when subscription is created."""
    updated = (
        RetentionTask.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            resolved_at__isnull=True,
            task_type__in=[
                RetentionTask.TaskType.POST_TRIAL,
                RetentionTask.TaskType.NEW_LEAD,
                RetentionTask.TaskType.RENEWAL,
            ],
        )
        .update(
            resolved_at=timezone.now(),
            resolution=RetentionTask.Resolution.AUTO_SUBSCRIPTION,
            status=RetentionTask.TaskStatus.CLOSED,
        )
    )
    if updated:
        logger.info(
            "tasks_auto_closed_on_subscription",
            extra={"student_id": student_id, "club_id": club_id, "count": updated},
        )
    return updated


def auto_close_tasks_on_personal_admission(*, student_id: int, club_id: int) -> int:
    """Close lead follow-ups from an exact attended personal admission."""

    updated = (
        RetentionTask.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            resolved_at__isnull=True,
            task_type__in=[
                RetentionTask.TaskType.POST_TRIAL,
                RetentionTask.TaskType.NEW_LEAD,
                RetentionTask.TaskType.RENEWAL,
            ],
        )
        .update(
            resolved_at=timezone.now(),
            resolution=RetentionTask.Resolution.AUTO_ADMISSION,
            status=RetentionTask.TaskStatus.CLOSED,
        )
    )
    if updated:
        logger.info(
            "tasks_auto_closed_on_personal_admission",
            extra={"student_id": student_id, "club_id": club_id, "count": updated},
        )
    return updated


def reopen_retention_tasks_auto_closed(
    *, student_id: int, club_id: int, after,
) -> int:
    """T4: re-open retention tasks that were auto-closed by a (now-cancelled) checkin.

    Targets only AUTO_CHECKIN-resolved tasks closed at-or-after `after`,
    so we don't disturb tasks closed by other paths or older checkins.
    """
    qs = RetentionTask.objects.for_club(club_id).filter(
        student_id=student_id,
        resolution=RetentionTask.Resolution.AUTO_CHECKIN,
        resolved_at__gte=after,
    )
    count = qs.update(
        resolved_at=None,
        resolution="",
        notes="",
        status=RetentionTask.TaskStatus.OPEN,
    )
    if count:
        logger.info(
            "retention_tasks_reopened",
            extra={"student_id": student_id, "club_id": club_id, "count": count},
        )
    return count
