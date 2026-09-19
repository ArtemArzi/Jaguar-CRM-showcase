from __future__ import annotations

from django.db.models import QuerySet

from apps.pipelines.models import Pipeline, PipelineExecution


def get_pipelines(*, club) -> QuerySet[Pipeline]:
    return Pipeline.objects.for_club(club).prefetch_related("steps")


def get_pipeline_executions(
    *,
    club,
    student_id: int | None = None,
    is_active: bool = True,
    assigned_trainer_id: int | None = None,
) -> QuerySet[PipelineExecution]:
    qs = PipelineExecution.objects.for_club(club).select_related(
        "pipeline", "current_step", "student"
    )
    if is_active:
        qs = qs.filter(completed_at__isnull=True, cancelled_at__isnull=True)
    if student_id is not None:
        qs = qs.filter(student_id=student_id)
    if assigned_trainer_id is not None:
        qs = qs.filter(student__assigned_trainer_id=assigned_trainer_id)
    return qs.order_by("next_step_at", "started_at", "id")


def get_trainer_pipeline_tasks(
    *, club, trainer_id: int
) -> QuerySet[PipelineExecution]:
    return (
        PipelineExecution.objects.for_club(club)
        .filter(
            student__assigned_trainer_id=trainer_id,
            completed_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .select_related("pipeline", "current_step", "student")
        .order_by("next_step_at", "started_at", "id")
    )
