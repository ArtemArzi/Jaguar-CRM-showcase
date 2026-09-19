from ninja import Query, Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.common.permissions import role_required
from apps.pipelines.models import PipelineExecution, PipelineStep
from apps.pipelines.schemas import (
    PipelineExecutionOut,
    PipelineOut,
    PipelineStepOut,
    UpdatePipelineStepIn,
)
from apps.pipelines.selectors import (
    get_pipeline_executions,
    get_pipelines,
    get_trainer_pipeline_tasks,
)
from apps.trainers.models import Trainer
from apps.trainers.selectors import get_trainer_for_user

router = Router(tags=["pipelines"])


def _get_current_trainer_id(request) -> int:
    try:
        trainer = get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")
    return trainer.id


@router.get("/", response=list[PipelineOut])
@role_required("owner", "admin")
def list_pipelines(request):
    return get_pipelines(club=request.club)


@router.put("/{pipeline_id}/steps/{step_id}", response=PipelineStepOut)
@role_required("owner", "admin")
def update_step(request, pipeline_id: int, step_id: int, payload: UpdatePipelineStepIn):
    step = PipelineStep.objects.for_club(request.club).get(
        id=step_id, pipeline_id=pipeline_id
    )
    step.delay_hours = payload.delay_hours
    step.save(update_fields=["delay_hours", "updated_at"])
    return step


@router.get("/executions", response=list[PipelineExecutionOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_executions(
    request,
    student_id: int | None = Query(None),
    is_active: bool = Query(True),
):
    membership = getattr(request, "_membership", None)
    trainer_id = None
    if membership and membership.role == "trainer":
        trainer_id = _get_current_trainer_id(request)

    return get_pipeline_executions(
        club=request.club,
        student_id=student_id,
        is_active=is_active,
        assigned_trainer_id=trainer_id,
    )


@router.post("/executions/{execution_id}/cancel", response={200: dict})
@role_required("owner", "admin")
def cancel_execution(request, execution_id: int):
    from django.utils import timezone

    updated = (
        PipelineExecution.objects.for_club(request.club)
        .filter(id=execution_id, completed_at__isnull=True, cancelled_at__isnull=True)
        .update(cancelled_at=timezone.now())
    )
    return {"cancelled": updated}


@router.get("/my-tasks", response=list[PipelineExecutionOut])
@role_required("trainer")
@paginate(LimitOffsetPagination)
def my_tasks(request):
    return get_trainer_pipeline_tasks(club=request.club, trainer_id=_get_current_trainer_id(request))
