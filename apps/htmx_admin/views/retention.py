from datetime import date

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.template.defaultfilters import truncatechars

from apps.common.permissions import management_view_required
from apps.retention.models import RetentionTask
from apps.retention.selectors import get_overdue_tasks
from apps.trainers.selectors import get_trainers

TASK_LEVEL_COLORS = {
    "yellow": "bg-amber-100 text-amber-800",
    "red": "bg-red-100 text-red-800",
    "churned": "bg-gray-300 text-gray-600",
}

TASK_STATUS_COLORS = {
    "open": "bg-yellow-100 text-yellow-800",
    "resolved": "bg-green-100 text-green-800",
    "overdue": "bg-red-100 text-red-800",
}

TASK_STATUS_LABELS = {
    "open": ("Новая", "bg-yellow-100 text-yellow-800"),
    "in_progress": ("В работе", "bg-blue-100 text-blue-800"),
    "snoozed": ("Отложена", "bg-gray-100 text-gray-800"),
    "closed": ("Закрыта", "bg-green-100 text-green-800"),
}


@management_view_required
def retention_tasks(request: HttpRequest) -> HttpResponse:
    status_filter = request.GET.get("status", "")
    trainer_id = request.GET.get("trainer_id", "")
    page_num = request.GET.get("page", "1")

    tasks = (
        RetentionTask.objects.for_club(request.club)
        .select_related("student", "trainer")
        .prefetch_related("comments__author")
        .order_by("-due_date")
    )

    if status_filter == "open":
        tasks = tasks.filter(resolved_at__isnull=True)
    elif status_filter == "resolved":
        tasks = tasks.filter(resolved_at__isnull=False)
    elif status_filter == "overdue":
        tasks = get_overdue_tasks(club=request.club)

    if trainer_id:
        tasks = tasks.filter(trainer_id=int(trainer_id))

    paginator = Paginator(tasks, 20)
    page_obj = paginator.get_page(page_num)

    today = date.today()
    for task in page_obj:
        comments = list(task.comments.all())
        if comments:
            # comments are ordered by -created_at, so first is the latest
            latest = comments[0]
            task.last_activity = latest.created_at
            task.last_comment_text = truncatechars(latest.text, 60)
        else:
            task.last_activity = task.updated_at
            task.last_comment_text = ""
        task.inactive_days = (today - task.last_activity.date()).days
        label, color = TASK_STATUS_LABELS.get(task.status, ("—", "bg-gray-100 text-gray-800"))
        task.status_label = label
        task.status_color = color

    trainers = get_trainers(club=request.club)

    context = {
        "page_title": "Retention Tasks",
        "page_obj": page_obj,
        "trainers": trainers,
        "current_status": status_filter,
        "current_trainer_id": trainer_id,
        "level_colors": TASK_LEVEL_COLORS,
        "status_choices": [("", "All"), ("open", "Open"), ("resolved", "Resolved"), ("overdue", "Overdue")],
    }
    if request.htmx:
        return render(request, "dashboard/retention/tasks.html#content", context)
    return render(request, "dashboard/retention/tasks.html", context)


@management_view_required
def retention_task_comments(request: HttpRequest, task_id: int) -> HttpResponse:
    from apps.retention.models import TaskComment

    comments = (
        TaskComment.objects.for_club(request.club)
        .filter(task_id=task_id)
        .select_related("author")
        .order_by("created_at")
    )
    return render(request, "dashboard/retention/_task_comments.html", {"comments": comments})
