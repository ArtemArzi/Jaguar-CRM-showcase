from __future__ import annotations

import html
import json
import logging
import urllib.request

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import LeadIntakeEvent
from apps.students.models import Student

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 10


def send_lead_intake_telegram_task(event_id: int, club_id: int) -> None:
    event = (
        LeadIntakeEvent.objects.for_club(club_id)
        .select_related("student", "club")
        .filter(id=event_id)
        .first()
    )
    if event is None:
        logger.warning("lead_intake_telegram_event_missing", extra={"event_id": event_id, "club_id": club_id})
        return

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_LEAD_CHAT_ID", "")
    if not token or not chat_id:
        event.telegram_status = LeadIntakeEvent.TelegramStatus.SKIPPED
        event.telegram_error_code = "telegram_not_configured"
        event.save(update_fields=["telegram_status", "telegram_error_code", "updated_at"])
        logger.info("lead_intake_telegram_skipped", extra={"event_id": event.id, "club_id": club_id})
        return

    event.telegram_attempt_count += 1
    try:
        _send_telegram_message(
            token=token,
            chat_id=chat_id,
            message_thread_id=getattr(settings, "TELEGRAM_LEAD_MESSAGE_THREAD_ID", 0) or None,
            text=_render_telegram_message(event),
        )
    except Exception as exc:
        event.telegram_status = LeadIntakeEvent.TelegramStatus.FAILED
        event.telegram_error_code = "telegram_delivery_failed"
        event.save(
            update_fields=[
                "telegram_status",
                "telegram_attempt_count",
                "telegram_error_code",
                "updated_at",
            ]
        )
        logger.warning(
            "lead_intake_telegram_failed",
            extra={
                "event_id": event.id,
                "club_id": club_id,
                "error_type": type(exc).__name__,
            },
        )
        return

    event.telegram_status = LeadIntakeEvent.TelegramStatus.SENT
    event.telegram_sent_at = timezone.now()
    event.telegram_error_code = ""
    event.save(
        update_fields=[
            "telegram_status",
            "telegram_attempt_count",
            "telegram_sent_at",
            "telegram_error_code",
            "updated_at",
        ]
    )
    logger.info("lead_intake_telegram_sent", extra={"event_id": event.id, "club_id": club_id})


def _send_telegram_message(
    *,
    token: str,
    chat_id: str,
    message_thread_id: int | None = None,
    text: str,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
        response.read()


def _render_telegram_message(event: LeadIntakeEvent) -> str:
    mode = getattr(settings, "TELEGRAM_LEAD_MESSAGE_MODE", "full")
    student = event.student
    lines = [
        f"Новая заявка с сайта #{event.id}",
        "",
        f"Формат: {_format_label(event.preferred_format)}",
        f"Источник: {event.source_page or 'сайт'}",
    ]
    if event.is_repeat_submission:
        lines.append("Повторная заявка: да")
        status_context = _crm_status_context(student)
        if status_context:
            lines.append(f"Текущий статус CRM: {status_context}")
    if event.requires_owner_review:
        lines.append("Требуется проверка руководителем: удалённая карточка")

    if mode == "full":
        lines[2:2] = [
            f"Имя: {student.first_name}",
            f"Телефон: {student.phone}",
            f"Зачем хочет прийти: {event.goal}",
        ]

    crm_url = _crm_event_url(event)
    if crm_url:
        lines.extend(["", f"Открыть в CRM: {crm_url}"])

    return "\n".join(html.escape(line) for line in lines)


def _format_label(value: str) -> str:
    return dict(LeadIntakeEvent.PreferredFormat.choices).get(value, value)


def _crm_status_context(student: Student) -> str:
    parts = [student.get_status_display()]
    if student.lead_status:
        parts.append(student.get_lead_status_display())
    if student.status == Student.Status.LOST and student.loss_reason:
        parts.append(f"причина: {student.get_loss_reason_display()}")
    return " · ".join(part for part in parts if part)


def _crm_event_url(event: LeadIntakeEvent) -> str:
    if event.requires_owner_review:
        return ""
    base_url = getattr(settings, "CRM_PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}{reverse('student-card', kwargs={'student_id': event.student_id})}"
