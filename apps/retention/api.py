from ninja import Router
from ninja.pagination import LimitOffsetPagination, paginate

from apps.common.permissions import role_required
from apps.retention.schemas import (
    CloseTaskIn,
    RetentionTaskOut,
    SnoozeTaskIn,
    TaskCommentIn,
    TaskCommentOut,
    UpdateTaskStatusIn,
)
from apps.retention.selectors import (
    get_overdue_tasks,
    get_task_by_id,
    get_task_comments,
    get_tasks_for_trainer,
)
from apps.retention.services import (
    add_task_comment,
    close_retention_task,
    snooze_task,
    update_task_status,
)

router = Router(tags=["retention"])


def _get_trainer_id(request) -> int | None:
    """Return trainer ID if request is from a trainer role, None for owner/admin."""
    if request._membership.role == "trainer":
        from apps.trainers.models import Trainer
        from apps.trainers.selectors import get_trainer_for_user

        try:
            trainer = get_trainer_for_user(club=request.club, user=request.user)
        except Trainer.DoesNotExist:
            from ninja.errors import HttpError
            raise HttpError(403, "No trainer profile found for this user")
        return trainer.id
    return None


def _check_task_ownership(request, task_id: int) -> None:
    """Raise 403 if trainer tries to access another trainer's task.

    For trainer role, also verifies the task exists and belongs to them.
    Owner/admin can access all tasks (no DB hit here).
    """
    trainer_id = _get_trainer_id(request)
    if trainer_id is None:
        return  # owner/admin can access all tasks
    from apps.retention.models import RetentionTask

    task = RetentionTask.objects.for_club(request.club).get(id=task_id)
    if task.trainer_id != trainer_id:
        from ninja.errors import HttpError

        raise HttpError(403, "Access denied: task belongs to another trainer")


@router.get("/tasks/", response=list[RetentionTaskOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_tasks(request, trainer_id: int | None = None, resolved: bool = False, resolved_today: bool = False):
    effective_trainer_id = trainer_id
    own_trainer_id = _get_trainer_id(request)
    if own_trainer_id is not None:
        effective_trainer_id = own_trainer_id

    if effective_trainer_id:
        return get_tasks_for_trainer(
            club=request.club, trainer_id=effective_trainer_id,
            resolved=resolved, resolved_today=resolved_today,
        )
    return get_overdue_tasks(club=request.club)


@router.get("/tasks/{task_id}/", response=RetentionTaskOut)
@role_required("owner", "admin", "trainer")
def get_task(request, task_id: int):
    _check_task_ownership(request, task_id)
    return get_task_by_id(club=request.club, task_id=task_id)


@router.get("/tasks/{task_id}/comments/", response=list[TaskCommentOut])
@role_required("owner", "admin", "trainer")
def list_comments(request, task_id: int):
    _check_task_ownership(request, task_id)
    return list(get_task_comments(club=request.club, task_id=task_id))


@router.post("/tasks/{task_id}/comments/", response={201: TaskCommentOut})
@role_required("owner", "admin", "trainer")
def add_comment(request, task_id: int, data: TaskCommentIn):
    _check_task_ownership(request, task_id)
    comment = add_task_comment(
        task_id=task_id,
        club_id=request.club.id,
        author_id=request.user.id,
        text=data.text,
    )
    return 201, comment


@router.post("/tasks/{task_id}/close/", response=RetentionTaskOut)
@role_required("owner", "admin", "trainer")
def close_task(request, task_id: int, data: CloseTaskIn):
    _check_task_ownership(request, task_id)
    return close_retention_task(
        task_id=task_id,
        club_id=request.club.id,
        resolution=data.resolution,
        notes=data.notes,
    )


@router.post("/tasks/{task_id}/snooze/", response=RetentionTaskOut)
@role_required("owner", "admin", "trainer")
def snooze_task_endpoint(request, task_id: int, data: SnoozeTaskIn):
    _check_task_ownership(request, task_id)
    return snooze_task(
        task_id=task_id,
        club_id=request.club.id,
        new_due_date=data.new_due_date,
        increment_attempt=data.increment_attempt,
    )


@router.post("/tasks/{task_id}/status/", response=RetentionTaskOut)
@role_required("owner", "admin", "trainer")
def update_status(request, task_id: int, data: UpdateTaskStatusIn):
    _check_task_ownership(request, task_id)
    return update_task_status(
        task_id=task_id,
        club_id=request.club.id,
        status=data.status,
    )
