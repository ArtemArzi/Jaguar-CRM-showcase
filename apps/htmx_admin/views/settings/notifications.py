import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.billing.services import update_club_settings
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.notifications.models import NotificationTemplate
from apps.notifications.services import update_notification_template

from ._helpers import _parse_int, _parse_time, _settings_context

logger = logging.getLogger(__name__)


@management_view_required
def settings_notifications_view(request: HttpRequest) -> HttpResponse:
    """Notifications tab: push settings + templates."""
    saved = False
    error = None

    if request.method == "POST":
        fields, error = _parse_notification_settings_fields(request.POST)
        if error is None:
            try:
                update_club_settings(club_id=request.club.id, **fields)
            except BusinessLogicError as e:
                error = str(e)
            else:
                saved = True

    return _render_notifications_settings(request, saved=saved, error=error)


def _parse_notification_settings_fields(post_data) -> tuple[dict, str | None]:
    fields: dict = {}

    int_fields = (
        ("max_push_per_week", "Максимум уведомлений в неделю", 0, 50),
        ("feedback_delay_hours", "Запрос обратной связи", 0, 72),
    )
    for field_name, label, min_val, max_val in int_fields:
        raw_value = post_data.get(field_name, "").strip()
        if not raw_value:
            continue
        parsed_value = _parse_int(raw_value, min_val=min_val, max_val=max_val)
        if parsed_value is None:
            return {}, f"{label} должен быть числом от {min_val} до {max_val}"
        fields[field_name] = parsed_value

    time_fields = (
        ("quiet_hours_start", "Тихие часы — начало"),
        ("quiet_hours_end", "Тихие часы — конец"),
    )
    for field_name, label in time_fields:
        raw_value = post_data.get(field_name, "").strip()
        if not raw_value:
            continue
        parsed_value = _parse_time(raw_value)
        if parsed_value is None:
            return {}, f"{label} укажите в формате ЧЧ:ММ"
        fields[field_name] = parsed_value

    return fields, None


def _render_notifications_settings(
    request: HttpRequest,
    *,
    saved: bool = False,
    error: str | None = None,
) -> HttpResponse:
    ctx = _settings_context("notifications", request)

    _seed_default_templates(request.club)

    templates_qs = list(
        NotificationTemplate.objects.for_club(request.club).order_by("trigger_type")
    )

    # Group templates by category
    _cat_triggers = {
        "Абонементы": [
            "sub_expiry_7d", "sub_expiry_3d", "sub_expiry_1d",
            "trainings_left_2", "trainings_last",
        ],
        "Родители": ["parent_checkin", "parent_sub_expiry", "parent_grade_up"],
        "Вовлечение": [
            "trial_feedback", "training_reminder", "training_reminder_24h", "missed_training",
            "follow_up", "churned_survey",
        ],
    }
    tmpl_by_trigger = {t.trigger_type: t for t in templates_qs}
    grouped_templates = []
    for cat_name, triggers in _cat_triggers.items():
        cat_tmpls = [tmpl_by_trigger[t] for t in triggers if t in tmpl_by_trigger]
        if cat_tmpls:
            grouped_templates.append((cat_name, cat_tmpls))

    ctx["grouped_templates"] = grouped_templates
    ctx["saved"] = saved
    ctx["error"] = error
    template = "dashboard/settings/notifications.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


_DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    "sub_expiry_7d": (
        "{name}, напоминание",
        "{name}, ваш абонемент истекает через 7 дней. Продлите, чтобы не потерять место!",
    ),
    "sub_expiry_3d": ("{name}, напоминание", "{name}, до конца абонемента осталось 3 дня!"),
    "sub_expiry_1d": ("{name}, напоминание", "{name}, абонемент заканчивается завтра!"),
    "trainings_left_2": ("{name}, напоминание", "{name}, осталось 2 занятия по абонементу."),
    "trainings_last": ("{name}, напоминание", "{name}, это последнее занятие по абонементу!"),
    "parent_checkin": ("{name} на тренировке", "{name} отмечен(а) на тренировке."),
    "parent_sub_expiry": ("Абонемент {name}", "Абонемент {name} скоро истечёт."),
    "parent_grade_up": ("{name} — новый грейд!", "{name} получил(а) новый грейд!"),
    "trial_feedback": ("Как вам тренировка?", "{name}, как вам пробная тренировка?"),
    "training_reminder": ("Тренировка скоро", "{name}, скоро тренировка. Проверьте расписание."),
    "training_reminder_24h": (
        "Тренировка завтра",
        "{name}, завтра тренировка {group} в {time}.",
    ),
    "missed_training": ("Пропущена тренировка", "{name}, вы пропустили привычную тренировку."),
    "follow_up": ("Напоминание", "Напоминание: {name} — запланированная задача."),
    "churned_survey": (
        "Расскажите нам",
        "{name}, расскажите, почему вы перестали заниматься?",
    ),
}


def _seed_default_templates(club) -> None:
    """Ensure every club has the current default notification templates."""
    created_any = False
    for trigger_type, (title, body) in _DEFAULT_TEMPLATES.items():
        _, created = NotificationTemplate.objects.get_or_create(
            club=club,
            trigger_type=trigger_type,
            defaults={
                "title_template": title,
                "body_template": body,
                "is_enabled": True,
            },
        )
        created_any = created_any or created
    if created_any:
        logger.info("notification_templates_seeded", extra={"club_id": club.id})


_TRIGGER_TYPE_LABELS: dict[str, str] = {
    "sub_expiry_7d": "Абонемент истекает (7 дней)",
    "sub_expiry_3d": "Абонемент истекает (3 дня)",
    "sub_expiry_1d": "Абонемент истекает (1 день)",
    "trainings_left_2": "Осталось 2 занятия",
    "trainings_last": "Последнее занятие",
    "parent_checkin": "Родитель: ребёнок на тренировке",
    "parent_sub_expiry": "Родитель: абонемент ребёнка",
    "parent_grade_up": "Родитель: новый грейд",
    "trial_feedback": "Фидбэк после пробной",
    "training_reminder": "Напоминание о тренировке",
    "training_reminder_24h": "Напоминание о тренировке за 24 часа",
    "missed_training": "Пропущенная тренировка",
    "follow_up": "Напоминание по задаче",
    "churned_survey": "Опрос ушедших",
}

_PLACEHOLDER_HINTS: dict[str, str] = {
    "sub_expiry_7d": "{name} — имя ученика, {days} — дней до окончания",
    "sub_expiry_3d": "{name} — имя ученика, {days} — дней до окончания",
    "sub_expiry_1d": "{name} — имя ученика, {days} — дней до окончания",
    "trainings_left_2": "{name} — имя ученика, {trainings_left} — осталось занятий",
    "trainings_last": "{name} — имя ученика",
    "parent_checkin": "{name} — имя ребёнка",
    "parent_sub_expiry": "{name} — имя ребёнка, {days} — дней до окончания",
    "parent_grade_up": "{name} — имя ребёнка, {grade} — название грейда",
    "trial_feedback": "{name} — имя ученика",
    "training_reminder": "{name} — имя ученика, {time} — время тренировки",
    "training_reminder_24h": (
        "{name} — имя ученика, {group} — группа, {time} — время тренировки"
    ),
    "missed_training": "{name} — имя ученика",
    "follow_up": "{name} — имя ученика",
    "churned_survey": "{name} — имя ученика",
}

_DAYS_BEFORE_DEFAULTS: dict[str, int] = {
    "sub_expiry_7d": 7,
    "sub_expiry_3d": 3,
    "sub_expiry_1d": 1,
}


@management_view_required
def notification_template_form(
    request: HttpRequest, template_id: int,
) -> HttpResponse:
    """GET: show edit form in slide-over. POST: update template."""
    try:
        tmpl = NotificationTemplate.objects.for_club(request.club).get(
            id=template_id,
        )
    except NotificationTemplate.DoesNotExist:
        return HttpResponse("Шаблон не найден", status=404)

    error = None

    if request.method == "POST":
        title_template = request.POST.get("title_template", "").strip()
        body_template = request.POST.get("body_template", "").strip()
        is_enabled = request.POST.get("is_enabled") == "on"
        supports_days_before = tmpl.trigger_type in _DAYS_BEFORE_DEFAULTS
        days_before = None

        if not title_template or not body_template:
            error = "Заголовок и текст обязательны"
        elif supports_days_before:
            raw_days_before = request.POST.get("days_before", "").strip()
            days_before = _parse_int(raw_days_before, min_val=1, max_val=365)
            if days_before is None:
                error = "Дней до окончания должно быть числом от 1 до 365"
        if error is None:
            fields = {
                "title_template": title_template,
                "body_template": body_template,
                "is_enabled": is_enabled,
            }
            if supports_days_before:
                fields["days_before"] = days_before
            try:
                update_notification_template(
                    template_id=tmpl.id,
                    club_id=request.club.id,
                    **fields,
                )
            except BusinessLogicError as e:
                error = str(e)
            else:
                response = HttpResponse(status=204)
                response["HX-Redirect"] = "/dashboard/settings/notifications/"
                return response

    supports_days_before = tmpl.trigger_type in _DAYS_BEFORE_DEFAULTS
    days_before_value = tmpl.days_before
    if days_before_value is None and supports_days_before:
        days_before_value = _DAYS_BEFORE_DEFAULTS[tmpl.trigger_type]

    ctx = {
        "tmpl": tmpl,
        "trigger_label": _TRIGGER_TYPE_LABELS.get(
            tmpl.trigger_type, tmpl.get_trigger_type_display(),
        ),
        "placeholder_hint": _PLACEHOLDER_HINTS.get(
            tmpl.trigger_type, "{name} — имя ученика",
        ),
        "supports_days_before": supports_days_before,
        "days_before_value": days_before_value,
        "error": error,
    }
    return render(request, "dashboard/settings/_template_form.html", ctx)


@management_view_required
@require_POST
def notification_template_toggle(
    request: HttpRequest, template_id: int,
) -> HttpResponse:
    """Toggle is_enabled for a notification template."""
    try:
        tmpl = NotificationTemplate.objects.for_club(request.club).get(
            id=template_id,
        )
    except NotificationTemplate.DoesNotExist:
        return HttpResponse("Шаблон не найден", status=404)

    update_notification_template(
        template_id=tmpl.id,
        club_id=request.club.id,
        is_enabled=not tmpl.is_enabled,
    )
    # Re-render the full notifications tab (no redirect → no scroll jump)
    return _render_notifications_settings(request)
