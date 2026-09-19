import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.notifications.models import MassNotification
from apps.notifications.services import get_mass_notification_recipient_ids, send_mass_notification

logger = logging.getLogger(__name__)

SEGMENT_CHOICES = [
    ("club", "All members"),
    ("status", "By status"),
    ("group", "By group"),
    ("location", "By location"),
]


@management_view_required
def push_notifications(request: HttpRequest) -> HttpResponse:
    error = None
    success = None

    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        segment_type = request.POST.get("segment_type", "club")
        segment_value = request.POST.get("segment_value", "").strip()

        if not text:
            error = "Message text is required"
        else:
            try:
                segment_filter = _build_segment_filter(segment_type, segment_value)
                notif = send_mass_notification(
                    club_id=request.club.id,
                    text=text,
                    segment_type=segment_type,
                    segment_filter=segment_filter,
                    sent_by_id=request.user.id,
                )
                success = f"Notification sent to {notif.recipient_count} recipients"
                logger.info(
                    "push_sent_via_admin",
                    extra={"club_id": request.club.id, "notif_id": notif.id},
                )
            except BusinessLogicError as e:
                error = e.message

    recent = (
        MassNotification.objects.for_club(request.club)
        .order_by("-created_at")[:10]
    )

    context = {
        "page_title": "Push Notifications",
        "segment_choices": SEGMENT_CHOICES,
        "recent_notifications": recent,
        "error": error,
        "success": success,
    }
    if request.htmx:
        return render(request, "dashboard/notifications/push.html#content", context)
    return render(request, "dashboard/notifications/push.html", context)


@management_view_required
def push_preview(request: HttpRequest) -> HttpResponse:
    segment_type = request.POST.get("segment_type", "club")
    segment_value = request.POST.get("segment_value", "").strip()
    try:
        segment_filter = _build_segment_filter(segment_type, segment_value)
        user_ids = get_mass_notification_recipient_ids(
            club_id=request.club.id,
            segment_type=segment_type,
            segment_filter=segment_filter,
        )
    except BusinessLogicError as exc:
        return render(request, "dashboard/notifications/_preview_count.html", {
            "error": exc.message,
        })
    return render(request, "dashboard/notifications/_preview_count.html", {
        "recipient_count": len(user_ids),
    })


def _build_segment_filter(segment_type: str, segment_value: str) -> dict:
    """Build segment_filter dict from form values."""
    if segment_type == "club":
        return {}
    if segment_type == "group":
        if not segment_value.isdigit() or int(segment_value) < 1:
            raise BusinessLogicError(
                "Укажите корректный ID группы",
                code="invalid_mass_notification_segment_filter",
            )
        return {"schedule_id": int(segment_value)}
    if segment_type == "location":
        if not segment_value.isdigit() or int(segment_value) < 1:
            raise BusinessLogicError(
                "Укажите корректный ID локации",
                code="invalid_mass_notification_segment_filter",
            )
        return {"location_id": int(segment_value)}
    if segment_type == "status":
        if not segment_value:
            raise BusinessLogicError(
                "Выберите статус ученика",
                code="mass_notification_segment_value_required",
            )
        return {"status": segment_value}
    raise BusinessLogicError(
        "Неизвестный тип сегмента",
        code="invalid_mass_notification_segment_type",
    )
