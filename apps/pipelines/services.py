from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.pipelines.models import Pipeline, PipelineExecution, PipelineStep

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")


def _safe_pipeline_message(value: object) -> str:
    message = str(value or "Follow-up").strip()
    message = _EMAIL_RE.sub("[redacted-email]", message)
    message = _PHONE_RE.sub("[redacted-phone]", message)
    return message[:500]


def _task_type_for_execution(*, execution: PipelineExecution) -> str:
    from apps.retention.models import RetentionTask

    # Follow-up executions are seeded as "after trial", so their durable task is post-trial.
    if execution.pipeline.pipeline_type == Pipeline.PipelineType.FOLLOW_UP:
        return RetentionTask.TaskType.POST_TRIAL
    return RetentionTask.TaskType.RETENTION


def seed_default_pipelines(*, club_id: int) -> None:
    """Create default follow-up and win-back pipelines for a club. Idempotent."""
    _seed_follow_up(club_id=club_id)
    _seed_win_back(club_id=club_id)


def _seed_follow_up(*, club_id: int) -> None:
    pipeline, created = Pipeline.objects.get_or_create(
        club_id=club_id,
        pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
        is_active=True,
        defaults={"name": "Follow-up after trial"},
    )
    if not created:
        return

    PipelineStep.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        order=1,
        delay_hours=2,
        action_type=PipelineStep.ActionType.CREATE_TASK,
        action_config={"message": "Позвонить, спросить впечатления"},
    )
    PipelineStep.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        order=2,
        delay_hours=72,
        action_type=PipelineStep.ActionType.CREATE_TASK,
        action_config={"message": "Follow-up: интересно?"},
    )
    PipelineStep.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        order=3,
        delay_hours=168,
        action_type=PipelineStep.ActionType.CHANGE_STATUS,
        action_config={"status": "lost", "loss_reason": "changed_mind"},
        is_terminal=True,
    )
    logger.info("pipeline_seeded", extra={"club_id": club_id, "type": "follow_up"})


def _seed_win_back(*, club_id: int) -> None:
    pipeline, created = Pipeline.objects.get_or_create(
        club_id=club_id,
        pipeline_type=Pipeline.PipelineType.WIN_BACK,
        is_active=True,
        defaults={"name": "Win-back churned"},
    )
    if not created:
        return

    PipelineStep.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        order=1,
        delay_hours=336,
        action_type=PipelineStep.ActionType.CREATE_TASK,
        action_config={"message": "Позвонить ушедшему ученику"},
    )
    PipelineStep.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        order=2,
        delay_hours=1440,
        action_type=PipelineStep.ActionType.CREATE_TASK,
        action_config={"message": "Финальный контакт"},
        is_terminal=True,
    )
    logger.info("pipeline_seeded", extra={"club_id": club_id, "type": "win_back"})


def trigger_pipeline(
    *, club_id: int, student_id: int, pipeline_type: str
) -> PipelineExecution | None:
    """Trigger a pipeline for a student. Returns None if no pipeline or duplicate."""
    pipeline = (
        Pipeline.objects.for_club(club_id)
        .filter(pipeline_type=pipeline_type, is_active=True)
        .first()
    )
    if not pipeline:
        return None

    # Skip if active execution already exists
    if PipelineExecution.objects.for_club(club_id).filter(
        pipeline__pipeline_type=pipeline_type,
        student_id=student_id,
        completed_at__isnull=True,
        cancelled_at__isnull=True,
    ).exists():
        return None

    first_step = pipeline.steps.order_by("order").first()
    if not first_step:
        return None

    execution = PipelineExecution.objects.create(
        club_id=club_id,
        pipeline=pipeline,
        student_id=student_id,
        next_step_at=timezone.now() + timedelta(hours=first_step.delay_hours),
    )
    logger.info(
        "pipeline_triggered",
        extra={
            "execution_id": execution.id,
            "pipeline_type": pipeline_type,
            "student_id": student_id,
            "club_id": club_id,
        },
    )
    return execution


def advance_due_pipelines(*, club_id: int) -> dict:
    """Advance all due pipeline executions for a club. Race-condition safe."""
    now = timezone.now()
    results = {"advanced": 0, "completed": 0}

    with transaction.atomic():
        due_executions = (
            PipelineExecution.objects.for_club(club_id)
            .filter(
                next_step_at__lte=now,
                completed_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .select_for_update(of=("self",), skip_locked=True)
            .select_related("pipeline", "current_step")
        )

        for execution in due_executions:
            _advance_single(execution=execution, now=now)
            results["advanced"] += 1
            if execution.completed_at:
                results["completed"] += 1

    return results


def _advance_single(*, execution: PipelineExecution, now) -> None:
    """Advance a single execution to its next step."""
    if execution.current_step is None:
        next_order = 1
    else:
        next_order = execution.current_step.order + 1

    step = execution.pipeline.steps.filter(order=next_order).first()
    if not step:
        # No more steps -- complete
        execution.completed_at = now
        execution.next_step_at = None
        execution.save(update_fields=["completed_at", "next_step_at", "updated_at"])
        return

    _execute_step(execution=execution, step=step)

    execution.current_step = step
    if step.is_terminal:
        execution.completed_at = now
        execution.next_step_at = None
    else:
        next_step = execution.pipeline.steps.filter(order=next_order + 1).first()
        if next_step:
            execution.next_step_at = now + timedelta(hours=next_step.delay_hours)
        else:
            execution.completed_at = now
            execution.next_step_at = None
    execution.save(
        update_fields=["current_step", "completed_at", "next_step_at", "updated_at"]
    )


def _execute_step(*, execution: PipelineExecution, step: PipelineStep) -> None:
    """Execute a pipeline step action."""
    if step.action_type == PipelineStep.ActionType.CREATE_TASK:
        _action_create_task(execution=execution, config=step.action_config)
    elif step.action_type == PipelineStep.ActionType.SEND_PUSH:
        _action_send_push(execution=execution, config=step.action_config)
    elif step.action_type == PipelineStep.ActionType.CHANGE_STATUS:
        _action_change_status(execution=execution, config=step.action_config)

    logger.info(
        "pipeline_step_executed",
        extra={
            "execution_id": execution.id,
            "step_order": step.order,
            "action_type": step.action_type,
            "club_id": execution.club_id,
        },
    )


def _action_create_task(*, execution: PipelineExecution, config: dict) -> None:
    """Create a follow-up task for the assigned trainer."""
    from apps.students.models import Student

    student = Student.objects.for_club(execution.club_id).select_related("assigned_trainer").get(
        id=execution.student_id
    )
    trainer = student.assigned_trainer
    if not trainer or not trainer.user_id:
        logger.warning(
            "pipeline_task_no_assigned_trainer",
            extra={"student_id": student.id, "club_id": execution.club_id},
        )
        return

    message = _safe_pipeline_message(config.get("message", "Follow-up"))
    task_notes = f"Pipeline step: {message}"

    from apps.retention.services import create_or_reuse_retention_task

    task = create_or_reuse_retention_task(
        club_id=execution.club_id,
        student_id=student.id,
        trainer_id=trainer.id,
        task_type=_task_type_for_execution(execution=execution),
        due_date=timezone.localdate(),
        notes=task_notes,
    )

    from apps.notifications.routes import trainer_task_url
    from apps.notifications.services import send_push_to_user

    send_push_to_user(
        user_id=trainer.user_id,
        title="Pipeline task",
        body="Open the follow-up task in the trainer app.",
        url=trainer_task_url(task.id),
    )


def _action_send_push(*, execution: PipelineExecution, config: dict) -> None:
    """Send push notification to the student's assigned trainer."""
    from apps.notifications.routes import trainer_tasks_url
    from apps.notifications.services import send_push_to_user
    from apps.students.models import Student

    student = Student.objects.for_club(execution.club_id).select_related("assigned_trainer").get(
        id=execution.student_id
    )
    trainer = student.assigned_trainer
    if not trainer or not trainer.user_id:
        logger.warning(
            "pipeline_push_no_assigned_trainer",
            extra={"student_id": student.id, "club_id": execution.club_id},
        )
        return

    message = _safe_pipeline_message(config.get("message", "Pipeline notification"))
    send_push_to_user(
        user_id=trainer.user_id,
        title="Pipeline",
        body=message,
        url=trainer_tasks_url(),
    )


def _action_change_status(*, execution: PipelineExecution, config: dict) -> None:
    """Change student status (e.g., mark as LOST)."""
    from apps.students.models import Student

    new_status = config.get("status")
    if not new_status:
        return

    student = Student.objects.for_club(execution.club_id).get(id=execution.student_id)
    update_fields = ["status", "updated_at"]
    updates = {"status": new_status}

    loss_reason = config.get("loss_reason")
    if new_status == Student.Status.LOST and student.lead_status is not None:
        from apps.leads.services import lose_lead

        lose_lead(
            club_id=execution.club_id,
            student_id=execution.student_id,
            loss_reason=loss_reason or Student.LossReason.OTHER,
        )
        return

    if loss_reason:
        updates["loss_reason"] = loss_reason
        update_fields.append("loss_reason")

    Student.objects.for_club(execution.club_id).filter(id=execution.student_id).update(
        **updates
    )
    logger.info(
        "pipeline_status_changed",
        extra={
            "student_id": execution.student_id,
            "new_status": new_status,
            "club_id": execution.club_id,
        },
    )


def cancel_pipeline(
    *, club_id: int, student_id: int, pipeline_type: str | None = None
) -> int:
    """Cancel active pipeline executions for a student."""
    qs = PipelineExecution.objects.for_club(club_id).filter(
        student_id=student_id,
        completed_at__isnull=True,
        cancelled_at__isnull=True,
    )
    if pipeline_type:
        qs = qs.filter(pipeline__pipeline_type=pipeline_type)

    count = qs.update(cancelled_at=timezone.now())
    if count:
        logger.info(
            "pipeline_cancelled",
            extra={"student_id": student_id, "club_id": club_id, "count": count},
        )
    return count
