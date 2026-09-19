from __future__ import annotations

from django.db.models import OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.retention.models import RetentionTask


def _annotate_last_activity(qs: QuerySet[RetentionTask]) -> QuerySet[RetentionTask]:
    from apps.retention.models import TaskComment

    latest_comment = (
        TaskComment.objects.filter(task=OuterRef("pk"), club_id=OuterRef("club_id"))
        .order_by("-created_at")
        .values("created_at")[:1]
    )
    return qs.annotate(
        _last_comment_date=Subquery(latest_comment),
        _last_activity_date=Greatest("updated_at", Coalesce(Subquery(latest_comment), "updated_at")),
    )


def get_tasks_for_trainer(
    *, club, trainer_id: int, resolved: bool = False, resolved_today: bool = False
) -> QuerySet[RetentionTask]:
    qs = RetentionTask.objects.for_club(club).filter(trainer_id=trainer_id)
    if resolved:
        qs = qs.filter(resolved_at__isnull=False)
        if resolved_today:
            qs = qs.filter(resolved_at__date=timezone.now().date())
    else:
        qs = qs.filter(resolved_at__isnull=True)
    qs = qs.select_related("student").order_by("-due_date")
    return _annotate_last_activity(qs)


def get_overdue_tasks(*, club) -> QuerySet[RetentionTask]:
    qs = (
        RetentionTask.objects.for_club(club)
        .filter(resolved_at__isnull=True, due_date__lt=timezone.now().date())
        .select_related("student", "trainer")
        .order_by("due_date")
    )
    return _annotate_last_activity(qs)


def get_task_by_id(*, club, task_id: int) -> RetentionTask:
    return (
        RetentionTask.objects.for_club(club)
        .select_related("student", "trainer")
        .get(id=task_id)
    )


def get_task_comments(*, club, task_id: int) -> QuerySet:
    from apps.retention.models import TaskComment

    return TaskComment.objects.for_club(club).filter(task_id=task_id).select_related("author")
