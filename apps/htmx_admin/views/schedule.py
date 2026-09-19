import logging
from datetime import date, timedelta
from datetime import time as time_type

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.attendance.models import (
    PersonalDropInBooking,
    Schedule,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupRolloutState,
)
from apps.attendance.selectors import (
    get_personal_drop_in_attendance_correction_preview,
    get_schedule_by_id,
    get_schedule_occurrences_for_date,
    get_schedule_occurrences_for_range,
    get_schedules,
    get_students_for_schedule,
    list_schedule_enrollments,
)
from apps.attendance.services import (
    cancel_schedule_enrollment,
    cancel_session,
    cancel_training_group_membership,
    create_schedule,
    delete_exception,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    freeze_training_group_membership,
    record_personal_drop_in_attendance_correction,
    reschedule_session,
    substitute_trainer,
    transfer_schedule_enrollment,
    transfer_training_group_membership,
    unfreeze_schedule_enrollment,
    unfreeze_training_group_membership,
    update_schedule,
)
from apps.attendance.services.training_group_reconciliation import (
    TrainingGroupPreviewError,
    apply_training_group_reconciliation,
    build_training_group_reconciliation_preview,
)
from apps.attendance.services.training_groups import archive_training_group
from apps.attendance.training_group_selectors import (
    get_training_group_management_summary,
    get_training_group_reconciliation_inventory,
)
from apps.billing.models import TrainingType
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.students.models import Student
from apps.trainers.selectors import get_trainers

logger = logging.getLogger(__name__)

DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

MONTH_NAMES_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

DAY_NAMES_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
}

TRAINER_COLORS = [
    "#C45A3B", "#5B9BD5", "#6B8E5E", "#E8C84B", "#9B59B6",
    "#E67E22", "#1ABC9C", "#E74C3C", "#3498DB", "#2ECC71",
]

ENROLLMENT_STATUS_LABELS = {
    ScheduleEnrollment.Status.ACTIVE: "Активен",
    ScheduleEnrollment.Status.TRIAL: "Пробное",
    ScheduleEnrollment.Status.FROZEN: "Заморожен",
}

CHECKIN_BLOCKED_BADGE_LABELS = {
    "enrollment_frozen": "Заморожен",
    "training_group_membership_frozen": "Заморожен",
}

CHECKIN_BLOCKED_REASON_LABELS = {
    "enrollment_frozen": "Нельзя отметить: заморозка",
    "training_group_membership_frozen": "Нельзя отметить: заморозка",
}

OPEN_ENROLLMENT_STATUSES = (
    ScheduleEnrollment.Status.ACTIVE,
    ScheduleEnrollment.Status.TRIAL,
    ScheduleEnrollment.Status.FROZEN,
)


def _trainer_color(trainer_id: int) -> str:
    return TRAINER_COLORS[trainer_id % len(TRAINER_COLORS)]


DAY_CHOICES_RU = [
    (0, "Понедельник"), (1, "Вторник"), (2, "Среда"),
    (3, "Четверг"), (4, "Пятница"), (5, "Суббота"), (6, "Воскресенье"),
]


def _format_date_ru(d: date) -> str:
    return f"{d.day} {MONTH_NAMES_RU[d.month]}"


def _format_schedule_transfer_label(schedule: Schedule) -> str:
    if schedule.one_time_date:
        day_label = schedule.one_time_date.strftime("%d.%m.%Y")
    else:
        day_label = DAY_NAMES_RU.get(schedule.day_of_week, str(schedule.day_of_week))
    group_name = schedule.training_group.name if schedule.training_group_id else schedule.group_name
    return (
        f"{group_name} · {schedule.trainer} · {schedule.location.name} · "
        f"{day_label} {schedule.start_time:%H:%M}-{schedule.end_time:%H:%M}"
    )


def _schedule_transfer_error_message(exc: BusinessLogicError) -> str:
    if exc.code == "same_schedule_transfer":
        return "Выберите другую группу"
    if exc.code == "schedule_club_mismatch":
        return "Выберите расписание этого клуба"
    return str(exc) or getattr(exc, "message", "Не удалось перевести ученика")


def _parse_schedule_post(post) -> dict | None:
    """Parse schedule form POST data. Returns dict or None on error."""
    try:
        s = post.get("start_time", "").split(":")
        e = post.get("end_time", "").split(":")
        start = time_type(int(s[0]), int(s[1]))
        end = time_type(int(e[0]), int(e[1]))
        if start >= end:
            return None

        one_time_date_str = post.get("one_time_date", "").strip()
        one_time_date = None
        if one_time_date_str:
            one_time_date = date.fromisoformat(one_time_date_str)
            day_int = one_time_date.weekday()
        else:
            day_int = int(post.get("day_of_week", ""))

        return {
            "day_of_week": day_int,
            "start_time": start,
            "end_time": end,
            "trainer_id": int(post.get("trainer_id", "")),
            "location_id": int(post.get("location_id", "")),
            "training_type_id": int(post.get("training_type_id", "")),
            "one_time_date": one_time_date,
        }
    except (ValueError, IndexError):
        return None


def _parse_session_date_from_post(request: HttpRequest) -> date:
    date_str = request.POST.get("date", "")
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise Http404


def _enrollment_matches_session_date(enrollment: ScheduleEnrollment, session_date: date) -> bool:
    if enrollment.status not in OPEN_ENROLLMENT_STATUSES:
        return False
    if enrollment.starts_on and enrollment.starts_on > session_date:
        return False
    if enrollment.ends_on and enrollment.ends_on < session_date:
        return False
    return True


def _render_session_detail(
    request: HttpRequest,
    *,
    schedule_id: int,
    session_date: date,
    error: str = "",
    success: str = "",
) -> HttpResponse:
    context = _get_session_detail_context(
        club=request.club,
        schedule_id=schedule_id,
        session_date=session_date,
    )
    if error:
        context["error"] = error
    if success:
        context["success"] = success
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
def schedule_create(request: HttpRequest) -> HttpResponse:
    trainers = get_trainers(club=request.club)
    locations = Location.objects.filter(club=request.club)
    training_types = TrainingType.objects.for_club(request.club).filter(is_active=True)
    training_groups = (
        TrainingGroup.objects.for_club(request.club)
        .filter(status=TrainingGroup.Status.ACTIVE)
        .select_related("training_type", "location", "responsible_trainer")
        .order_by("name", "id")
    )

    if request.method == "GET":
        return render(request, "dashboard/schedule/_create_form.html", {
            "days": DAY_CHOICES_RU,
            "trainers": trainers,
            "locations": locations,
            "training_types": training_types,
            "training_groups": training_groups,
        })

    training_group_value = (request.POST.get("training_group_id") or "").strip()
    try:
        training_group_id = int(training_group_value) if training_group_value else None
    except ValueError:
        training_group_id = None
    group_name = (request.POST.get("group_name") or "").strip()
    selected_group = (
        training_groups.filter(id=training_group_id).first()
        if training_group_id is not None
        else None
    )
    if training_group_value and selected_group is None:
        return render(request, "dashboard/schedule/_create_form.html", {
            "days": DAY_CHOICES_RU,
            "trainers": trainers,
            "locations": locations,
            "training_types": training_types,
            "training_groups": training_groups,
            "error": "Выберите активную тренировочную группу этого клуба",
        })
    if selected_group is not None:
        group_name = selected_group.name
    elif not group_name:
        return render(request, "dashboard/schedule/_create_form.html", {
            "days": DAY_CHOICES_RU,
            "trainers": trainers,
            "locations": locations,
            "training_types": training_types,
            "training_groups": training_groups,
            "error": "Название группы обязательно",
        })

    parsed = _parse_schedule_post(request.POST)
    if parsed is None:
        return render(request, "dashboard/schedule/_create_form.html", {
            "days": DAY_CHOICES_RU,
            "trainers": trainers,
            "locations": locations,
            "training_types": training_types,
            "training_groups": training_groups,
            "error": "Заполните все поля корректно (время начала должно быть раньше окончания)",
        })

    try:
        create_schedule(
            club_id=request.club.id,
            day_of_week=parsed["day_of_week"],
            start_time=parsed["start_time"],
            end_time=parsed["end_time"],
            group_name=group_name,
            trainer_id=parsed["trainer_id"],
            location_id=parsed["location_id"],
            training_type_id=parsed["training_type_id"],
            one_time_date=parsed["one_time_date"],
            training_group_id=training_group_id,
            actor_user_id=request.user.id,
        )
    except BusinessLogicError as e:
        return render(request, "dashboard/schedule/_create_form.html", {
            "days": DAY_CHOICES_RU,
            "trainers": trainers,
            "locations": locations,
            "training_types": training_types,
            "training_groups": training_groups,
            "error": str(e),
        })

    logger.info("schedule_created_via_admin", extra={"club_id": request.club.id})
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "closeSlideOver, scheduleUpdated"
    return response


@management_view_required
def schedule_view(request: HttpRequest) -> HttpResponse:
    today = date.today()
    try:
        week_offset = int(request.GET.get("week_offset", 0))
    except (ValueError, TypeError):
        week_offset = 0
    week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=5)
    occurrences_by_date = get_schedule_occurrences_for_range(
        club=request.club,
        date_from=week_start,
        date_to=week_end,
    )

    columns = []
    for i in range(6):
        day_date = week_start + timedelta(days=i)
        day_sessions = [
            {
                "schedule_id": occurrence.schedule_id,
                "group_name": occurrence.group_name,
                "effective_start_time": occurrence.effective_start_time,
                "effective_end_time": occurrence.effective_end_time,
                "trainer_name": occurrence.trainer_name,
                "location_name": occurrence.location_name,
                "one_time_date": occurrence.one_time_date,
                "is_rescheduled": occurrence.is_rescheduled,
                "is_substitute": occurrence.is_substitute,
                "trainer_color": _trainer_color(occurrence.trainer_id),
            }
            for occurrence in occurrences_by_date.get(day_date, [])
        ]
        columns.append({
            "label": DAY_LABELS[i],
            "date": day_date,
            "sessions": day_sessions,
            "is_today": day_date == today,
        })

    if week_start.month != week_end.month:
        week_label = (
            f"{week_start.day} {MONTH_NAMES_RU[week_start.month]} — "
            f"{week_end.day} {MONTH_NAMES_RU[week_end.month]}"
        )
    else:
        week_label = f"{week_start.day} — {week_end.day} {MONTH_NAMES_RU[week_end.month]}"

    context = {
        "page_title": "Расписание",
        "columns": columns,
        "week_start": week_start,
        "week_end": week_end,
        "week_label": week_label,
        "prev_week_offset": week_offset - 1,
        "next_week_offset": week_offset + 1,
    }
    if request.htmx:
        return render(request, "dashboard/schedule/week.html#content", context)
    return render(request, "dashboard/schedule/week.html", context)


@management_view_required
def training_group_reconciliation(request: HttpRequest) -> HttpResponse:
    """Render and apply one owner-confirmed, digest-bound reconciliation."""
    inventory = get_training_group_reconciliation_inventory(club=request.club)
    rollout_state = TrainingGroupRolloutState.objects.for_club(request.club).first()
    context = {
        "inventory": inventory,
        "group_management": get_training_group_management_summary(club=request.club),
        "rollout_state": rollout_state,
        "preview": None,
        "preview_error": "",
        "group_error": "",
        "apply_result": None,
        "trainers": get_trainers(club=request.club),
    }
    if request.method != "POST":
        return render(request, "dashboard/training_groups/reconciliation.html", context)

    try:
        schedule_ids = [int(value) for value in request.POST.getlist("schedule_ids")]
        start_dates = [
            {"student_id": int(key.removeprefix("start_date_")), "starts_on": date.fromisoformat(value)}
            for key, value in request.POST.items()
            if key.startswith("start_date_") and value
        ]
        responsible_trainer_value = request.POST.get("responsible_trainer_id", "").strip()
        preview = build_training_group_reconciliation_preview(
            club=request.club,
            schedule_ids=schedule_ids,
            canonical_name=request.POST.get("canonical_name", ""),
            responsible_trainer_id=int(responsible_trainer_value) if responsible_trainer_value else None,
            start_dates=start_dates,
        )
        context["preview"] = preview
        if request.POST.get("reconciliation_action") == "apply":
            context["apply_result"] = apply_training_group_reconciliation(
                club=request.club,
                schedule_ids=schedule_ids,
                canonical_name=request.POST.get("canonical_name", ""),
                responsible_trainer_id=int(responsible_trainer_value) if responsible_trainer_value else None,
                start_dates=start_dates,
                preview_digest=request.POST.get("preview_digest", ""),
                actor_user_id=request.user.id,
                rationale=request.POST.get("rationale", ""),
                idempotency_key=request.POST.get("idempotency_key", ""),
            )
    except (BusinessLogicError, TrainingGroupPreviewError, ValueError) as exc:
        context["preview_error"] = getattr(exc, "message", str(exc))

    template_name = (
        "dashboard/training_groups/_reconciliation_preview.html"
        if request.headers.get("HX-Request")
        else "dashboard/training_groups/reconciliation.html"
    )
    return render(request, template_name, context)


@management_view_required
def training_group_archive(request: HttpRequest, training_group_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        archive_training_group(
            club_id=request.club.id,
            training_group_id=training_group_id,
            actor_user_id=request.user.id,
            rationale=request.POST.get("rationale", ""),
            idempotency_key=f"dashboard-training-group-archive-{training_group_id}",
        )
    except BusinessLogicError as exc:
        context = {
            "inventory": get_training_group_reconciliation_inventory(club=request.club),
            "group_management": get_training_group_management_summary(club=request.club),
            "rollout_state": TrainingGroupRolloutState.objects.for_club(request.club).first(),
            "preview": None,
            "preview_error": "",
            "group_error": exc.message,
            "apply_result": None,
            "trainers": get_trainers(club=request.club),
        }
        return render(
            request,
            "dashboard/training_groups/reconciliation.html",
            context,
            status=400,
        )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/training-groups/reconciliation/"},
    )


def _get_session_detail_context(*, club, schedule_id: int, session_date: date) -> dict:
    from apps.attendance.models import Checkin

    try:
        schedule = get_schedule_by_id(club=club, schedule_id=schedule_id)
    except Schedule.DoesNotExist:
        raise Http404

    same_day_exception = ScheduleException.objects.for_club(club).filter(
        schedule=schedule, date=session_date,
    ).select_related("substitute_trainer").first()
    moved_here_reschedule = ScheduleException.objects.for_club(club).filter(
        schedule=schedule,
        exception_type=ScheduleException.ExceptionType.RESCHEDULED,
        new_date=session_date,
    ).first()
    occurrence = next(
        (
            item
            for item in get_schedule_occurrences_for_date(club=club, target_date=session_date)
            if item.schedule_id == schedule_id
        ),
        None,
    )

    display_start_time = occurrence.effective_start_time if occurrence else schedule.start_time
    display_end_time = occurrence.effective_end_time if occurrence else schedule.end_time
    display_trainer = occurrence.trainer_name if occurrence else str(schedule.trainer)
    display_location = occurrence.location_name if occurrence else schedule.location.name

    exception_display_type = None
    exception_display_message = None
    exception_action_date = session_date
    session_date_display = (
        f"{DAY_NAMES_RU[session_date.weekday()]}, "
        f"{_format_date_ru(session_date)} {session_date.year}"
    )

    if same_day_exception and same_day_exception.exception_type != ScheduleException.ExceptionType.SUBSTITUTE:
        exception_display_type = same_day_exception.exception_type
        if same_day_exception.exception_type == ScheduleException.ExceptionType.CANCELLED:
            exception_display_message = "Занятие отменено"
            if same_day_exception.reason:
                exception_display_message += f": {same_day_exception.reason}"
        elif same_day_exception.exception_type == ScheduleException.ExceptionType.RESCHEDULED:
            exception_display_message = (
                f"Перенесено на {DAY_NAMES_RU[same_day_exception.new_date.weekday()]}, "
                f"{_format_date_ru(same_day_exception.new_date)}"
            )
            if same_day_exception.new_start_time:
                exception_display_message += f" в {same_day_exception.new_start_time.strftime('%H:%M')}"
            if same_day_exception.reason:
                exception_display_message += f" ({same_day_exception.reason})"
    elif moved_here_reschedule:
        old_date_display = (
            f"{DAY_NAMES_RU[moved_here_reschedule.date.weekday()]}, "
            f"{_format_date_ru(moved_here_reschedule.date)}"
        )
        exception_display_type = ScheduleException.ExceptionType.RESCHEDULED
        exception_display_message = f"Перенесено с {old_date_display} на {session_date_display}"
        if display_start_time:
            exception_display_message += f" в {display_start_time.strftime('%H:%M')}"
        if same_day_exception and same_day_exception.exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
            exception_display_message += f". Замена тренера: {same_day_exception.substitute_trainer}"
            if same_day_exception.reason:
                exception_display_message += f" ({same_day_exception.reason})"
        elif moved_here_reschedule.reason:
            exception_display_message += f" ({moved_here_reschedule.reason})"
        exception_action_date = moved_here_reschedule.date
    elif same_day_exception:
        exception_display_type = same_day_exception.exception_type
        if same_day_exception.exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
            exception_display_message = f"Замена тренера: {same_day_exception.substitute_trainer}"
            if same_day_exception.reason:
                exception_display_message += f" ({same_day_exception.reason})"
            display_trainer = str(same_day_exception.substitute_trainer or schedule.trainer)

    has_exception = exception_display_type is not None

    trainers = get_trainers(club=club)

    checkins = list(
        Checkin.objects.for_club(club)
        .filter(schedule_id=schedule_id, date=session_date, deleted_at__isnull=True)
        .select_related("student")
        .order_by("student__last_name", "student__first_name")
    )
    checked_in_student_ids = {checkin.student_id for checkin in checkins}
    roster_rows_by_student_id = {
        row["id"]: row
        for row in get_students_for_schedule(
            club=club,
            schedule_id=schedule_id,
            reference_date=session_date,
        )
    }
    effective_enrollment_ids = {
        row["enrollment_id"]
        for row in roster_rows_by_student_id.values()
        if row.get("enrollment_id") is not None
    }
    enrollments = [
        enrollment
        for enrollment in list_schedule_enrollments(club=club, schedule_id=schedule_id)
        if _enrollment_matches_session_date(enrollment, session_date)
        and enrollment.id in effective_enrollment_ids
    ]
    enrolled_student_ids = {enrollment.student_id for enrollment in enrollments}
    enrollment_rows = []
    for enrollment in enrollments:
        roster_row = roster_rows_by_student_id.get(enrollment.student_id, {})
        enrollment_status = enrollment.status
        checkin_blocked_reason = roster_row.get("checkin_blocked_reason") or (
            "enrollment_frozen"
            if enrollment_status == ScheduleEnrollment.Status.FROZEN
            else None
        )
        enrollment_rows.append(
            {
                "enrollment": enrollment,
                "enrollment_status": enrollment_status,
                "status_label": ENROLLMENT_STATUS_LABELS.get(enrollment_status, enrollment_status),
                "checkin_blocked_reason": checkin_blocked_reason,
                "checkin_blocked_badge_label": CHECKIN_BLOCKED_BADGE_LABELS.get(
                    checkin_blocked_reason,
                ),
                "checkin_blocked_label": CHECKIN_BLOCKED_REASON_LABELS.get(
                    checkin_blocked_reason,
                    "Нельзя отметить",
                ) if checkin_blocked_reason else "",
                "alerts": [alert for alert in roster_row.get("alerts", []) if alert.get("message")],
                "is_checked_in": enrollment.student_id in checked_in_student_ids,
                "can_freeze": enrollment.status != ScheduleEnrollment.Status.FROZEN,
                "can_unfreeze": enrollment.status == ScheduleEnrollment.Status.FROZEN,
                "can_transfer": enrollment.status in OPEN_ENROLLMENT_STATUSES,
            },
        )
    personal_drop_in_blocked_reason = next(
        (
            row["checkin_blocked_reason"]
            for row in enrollment_rows
            if row["enrollment"].created_from == ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN
        ),
        "",
    )
    personal_drop_in_attendance = get_personal_drop_in_attendance_correction_preview(
        club=club,
        schedule_id=schedule_id,
        session_date=session_date,
        effective_end_time=display_end_time,
        has_exception=has_exception,
        checkin_blocked_reason=personal_drop_in_blocked_reason,
    )
    available_students = (
        Student.objects.for_club(club)
        .filter(deleted_at__isnull=True)
        .exclude(id__in=enrolled_student_ids)
        .order_by("last_name", "first_name", "id")
    )
    transfer_schedules = [
        {
            "id": target_schedule.id,
            "label": _format_schedule_transfer_label(target_schedule),
        }
        for target_schedule in (
            get_schedules(club=club)
            .exclude(id=schedule_id)
            .order_by("day_of_week", "start_time", "group_name", "id")
        )
    ]

    ctx: dict = {
        "schedule": schedule,
        "session_date": session_date,
        "session_date_display": session_date_display,
        "trainers": trainers,
        "checkins": checkins,
        "display_start_time": display_start_time,
        "display_end_time": display_end_time,
        "display_trainer": display_trainer,
        "display_location": display_location,
        "has_exception": has_exception,
        "exception_display_type": exception_display_type,
        "exception_display_message": exception_display_message,
        "exception_action_date": exception_action_date,
        "enrollment_rows": enrollment_rows,
        "personal_drop_in_attendance": personal_drop_in_attendance,
        "available_students": available_students,
        "transfer_schedules": transfer_schedules,
    }
    return ctx


@management_view_required
def session_detail(request: HttpRequest, schedule_id: int) -> HttpResponse:
    date_str = request.GET.get("date", "")
    try:
        session_date = date.fromisoformat(date_str)
    except ValueError:
        session_date = date.today()

    context = _get_session_detail_context(
        club=request.club, schedule_id=schedule_id, session_date=session_date,
    )
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
@require_POST
def personal_drop_in_attendance_correction(
    request: HttpRequest,
    booking_id: int,
) -> HttpResponse:
    booking = (
        PersonalDropInBooking.objects.for_club(request.club)
        .select_related("enrollment", "enrollment__schedule")
        .filter(id=booking_id)
        .first()
    )
    if booking is None:
        raise Http404

    schedule_id = booking.enrollment.schedule_id
    session_date = booking.enrollment.schedule.one_time_date
    if session_date is None:
        raise Http404

    try:
        result = record_personal_drop_in_attendance_correction(
            club_id=request.club.id,
            booking_id=booking.id,
            actor_user_id=request.user.id,
            reason=request.POST.get("reason", ""),
        )
    except BusinessLogicError as exc:
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=str(exc),
        )

    message = (
        "Посещение зачтено: абонемент или задолженность обновлены по действующим правилам."
        if result.created
        else "Посещение уже было зачтено ранее; повторных списаний нет."
    )
    return _render_session_detail(
        request,
        schedule_id=schedule_id,
        session_date=session_date,
        success=message,
    )


@management_view_required
def session_enroll(request: HttpRequest, schedule_id: int) -> HttpResponse:
    session_date = _parse_session_date_from_post(request)
    student_id_raw = request.POST.get("student_id", "")
    status = request.POST.get("status") or ScheduleEnrollment.Status.ACTIVE
    try:
        student_id = int(student_id_raw)
    except (ValueError, TypeError):
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error="Выберите ученика для записи",
        )

    try:
        enroll_student_in_schedule(
            club_id=request.club.id,
            student_id=student_id,
            schedule_id=schedule_id,
            status=status,
            starts_on=session_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
            actor_user_id=request.user.id,
        )
    except BusinessLogicError as e:
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=str(e),
        )

    return _render_session_detail(
        request,
        schedule_id=schedule_id,
        session_date=session_date,
    )


@management_view_required
def schedule_enrollment_cancel_admin(request: HttpRequest, enrollment_id: int) -> HttpResponse:
    session_date = _parse_session_date_from_post(request)
    try:
        enrollment = ScheduleEnrollment.objects.for_club(request.club).select_related(
            "training_group_membership"
        ).get(id=enrollment_id)
        if enrollment.training_group_membership_id:
            cancel_training_group_membership(
                membership_id=enrollment.training_group_membership_id,
                club_id=request.club.id,
                ends_on=session_date - timedelta(days=1),
                actor_user_id=request.user.id,
                rationale="Owner session detail cancellation.",
                idempotency_key=f"htmx-membership-cancel-{enrollment.training_group_membership_id}-{session_date.isoformat()}",
            )
        else:
            enrollment = cancel_schedule_enrollment(
                club_id=request.club.id,
                enrollment_id=enrollment_id,
                ends_on=session_date - timedelta(days=1),
            )
    except BusinessLogicError as e:
        schedule_id = int(request.POST.get("schedule_id", "0") or 0)
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=str(e),
        )

    return _render_session_detail(
        request,
        schedule_id=enrollment.schedule_id,
        session_date=session_date,
    )


@management_view_required
def schedule_enrollment_freeze_admin(request: HttpRequest, enrollment_id: int) -> HttpResponse:
    session_date = _parse_session_date_from_post(request)
    try:
        enrollment = ScheduleEnrollment.objects.for_club(request.club).select_related(
            "training_group_membership"
        ).get(id=enrollment_id)
        if enrollment.training_group_membership_id:
            freeze_training_group_membership(
                membership_id=enrollment.training_group_membership_id,
                club_id=request.club.id,
                actor_user_id=request.user.id,
                rationale="Owner session detail freeze.",
                idempotency_key=f"htmx-membership-freeze-{enrollment.training_group_membership_id}",
            )
        else:
            enrollment = freeze_schedule_enrollment(
                club_id=request.club.id,
                enrollment_id=enrollment_id,
            )
    except BusinessLogicError as e:
        schedule_id = int(request.POST.get("schedule_id", "0") or 0)
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=str(e),
        )

    return _render_session_detail(
        request,
        schedule_id=enrollment.schedule_id,
        session_date=session_date,
    )


@management_view_required
def schedule_enrollment_unfreeze_admin(request: HttpRequest, enrollment_id: int) -> HttpResponse:
    session_date = _parse_session_date_from_post(request)
    try:
        enrollment = ScheduleEnrollment.objects.for_club(request.club).select_related(
            "training_group_membership"
        ).get(id=enrollment_id)
        if enrollment.training_group_membership_id:
            unfreeze_training_group_membership(
                membership_id=enrollment.training_group_membership_id,
                club_id=request.club.id,
                actor_user_id=request.user.id,
                rationale="Owner session detail unfreeze.",
                idempotency_key=f"htmx-membership-unfreeze-{enrollment.training_group_membership_id}",
            )
        else:
            enrollment = unfreeze_schedule_enrollment(
                club_id=request.club.id,
                enrollment_id=enrollment_id,
            )
    except BusinessLogicError as e:
        schedule_id = int(request.POST.get("schedule_id", "0") or 0)
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=str(e),
        )

    return _render_session_detail(
        request,
        schedule_id=enrollment.schedule_id,
        session_date=session_date,
    )


@management_view_required
def schedule_enrollment_transfer_admin(request: HttpRequest, enrollment_id: int) -> HttpResponse:
    session_date = _parse_session_date_from_post(request)
    schedule_id = int(request.POST.get("schedule_id", "0") or 0)
    target_schedule_raw = request.POST.get("target_schedule_id", "")
    transfer_date_raw = request.POST.get("transfer_date") or request.POST.get("effective_from", "")
    try:
        target_schedule_id = int(target_schedule_raw)
    except (ValueError, TypeError):
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error="Выберите группу для перевода",
        )
    try:
        transfer_date = date.fromisoformat(transfer_date_raw)
    except ValueError:
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error="Укажите дату перехода",
        )

    try:
        enrollment = ScheduleEnrollment.objects.for_club(request.club).select_related(
            "training_group_membership"
        ).get(id=enrollment_id)
        if enrollment.training_group_membership_id:
            target_schedule = Schedule.objects.for_club(request.club).only(
                "id", "training_group_id"
            ).get(id=target_schedule_id)
            if target_schedule.training_group_id is None:
                raise BusinessLogicError(
                    "Выберите связанный слот целевой группы.",
                    code="training_group_transfer_target_required",
                )
            transfer_training_group_membership(
                membership_id=enrollment.training_group_membership_id,
                club_id=request.club.id,
                target_training_group_id=target_schedule.training_group_id,
                ends_on=transfer_date - timedelta(days=1),
                actor_user_id=request.user.id,
                rationale="Owner session detail transfer.",
                idempotency_key=(
                    f"htmx-membership-transfer-{enrollment.training_group_membership_id}-"
                    f"{target_schedule.training_group_id}-{transfer_date.isoformat()}"
                ),
            )
            closed_enrollment = enrollment
        else:
            closed_enrollment, _new_enrollment = transfer_schedule_enrollment(
                club_id=request.club.id,
                enrollment_id=enrollment_id,
                target_schedule_id=target_schedule_id,
                ends_on=transfer_date - timedelta(days=1),
            )
    except BusinessLogicError as e:
        return _render_session_detail(
            request,
            schedule_id=schedule_id,
            session_date=session_date,
            error=_schedule_transfer_error_message(e),
        )

    return _render_session_detail(
        request,
        schedule_id=closed_enrollment.schedule_id,
        session_date=session_date,
    )


@management_view_required
def session_cancel(request: HttpRequest, schedule_id: int) -> HttpResponse:
    date_str = request.POST.get("date", "")
    reason = request.POST.get("reason", "")
    try:
        session_date = date.fromisoformat(date_str)
    except ValueError:
        raise Http404

    try:
        cancel_session(
            club_id=request.club.id,
            schedule_id=schedule_id,
            date=session_date,
            reason=reason,
        )
    except BusinessLogicError as e:
        context = _get_session_detail_context(
            club=request.club, schedule_id=schedule_id, session_date=session_date,
        )
        context["error"] = str(e)
        return render(request, "dashboard/schedule/_session_detail.html", context)

    context = _get_session_detail_context(
        club=request.club, schedule_id=schedule_id, session_date=session_date,
    )
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
def session_reschedule(request: HttpRequest, schedule_id: int) -> HttpResponse:
    date_str = request.POST.get("date", "")
    new_date_str = request.POST.get("new_date", "")
    new_time_str = request.POST.get("new_time", "")
    try:
        session_date = date.fromisoformat(date_str)
        new_session_date = date.fromisoformat(new_date_str)
    except ValueError:
        raise Http404

    from apps.attendance.models import Schedule

    try:
        schedule = get_schedule_by_id(club=request.club, schedule_id=schedule_id)
    except Schedule.DoesNotExist:
        raise Http404

    if new_time_str:
        parts = new_time_str.split(":")
        new_start = time_type(int(parts[0]), int(parts[1]))
        duration = (
            schedule.end_time.hour * 60 + schedule.end_time.minute
        ) - (schedule.start_time.hour * 60 + schedule.start_time.minute)
        end_minutes = new_start.hour * 60 + new_start.minute + duration
        new_end = time_type(end_minutes // 60, end_minutes % 60)
    else:
        new_start = schedule.start_time
        new_end = schedule.end_time

    try:
        reschedule_session(
            club_id=request.club.id,
            schedule_id=schedule_id,
            date=session_date,
            new_date=new_session_date,
            new_start_time=new_start,
            new_end_time=new_end,
        )
    except BusinessLogicError as e:
        context = _get_session_detail_context(
            club=request.club, schedule_id=schedule_id, session_date=session_date,
        )
        context["error"] = str(e)
        return render(request, "dashboard/schedule/_session_detail.html", context)

    context = _get_session_detail_context(
        club=request.club, schedule_id=schedule_id, session_date=session_date,
    )
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
def schedule_edit(request: HttpRequest, schedule_id: int) -> HttpResponse:
    from apps.attendance.models import Schedule

    try:
        schedule = get_schedule_by_id(club=request.club, schedule_id=schedule_id)
    except Schedule.DoesNotExist:
        raise Http404

    trainers = get_trainers(club=request.club)
    locations = Location.objects.filter(club=request.club)
    training_types = TrainingType.objects.for_club(request.club).filter(is_active=True)

    if request.method == "GET":
        return render(request, "dashboard/schedule/_edit_form.html", {
            "schedule": schedule, "days": DAY_CHOICES_RU,
            "trainers": trainers, "locations": locations, "training_types": training_types,
        })

    # Deactivation action
    if request.POST.get("action") == "deactivate":
        update_schedule(schedule_id=schedule_id, club_id=request.club.id, is_active=False)
        logger.info("schedule_deactivated_via_admin", extra={"schedule_id": schedule_id, "club_id": request.club.id})
        response = HttpResponse(status=204)
        response["HX-Trigger"] = "closeSlideOver, scheduleUpdated"
        return response

    group_name = (request.POST.get("group_name") or "").strip()
    if not group_name:
        return render(request, "dashboard/schedule/_edit_form.html", {
            "schedule": schedule, "days": DAY_CHOICES_RU, "trainers": trainers,
            "locations": locations, "training_types": training_types, "error": "Название группы обязательно",
        })

    parsed = _parse_schedule_post(request.POST)
    if parsed is None:
        return render(request, "dashboard/schedule/_edit_form.html", {
            "schedule": schedule, "days": DAY_CHOICES_RU, "trainers": trainers,
            "locations": locations, "training_types": training_types,
            "error": "Заполните все поля корректно (время начала должно быть раньше окончания)",
        })

    try:
        update_schedule(
            schedule_id=schedule_id, club_id=request.club.id,
            day_of_week=parsed["day_of_week"],
            start_time=parsed["start_time"],
            end_time=parsed["end_time"],
            group_name=group_name,
            trainer_id=parsed["trainer_id"],
            location_id=parsed["location_id"],
            training_type_id=parsed["training_type_id"],
        )
    except BusinessLogicError as e:
        return render(request, "dashboard/schedule/_edit_form.html", {
            "schedule": schedule, "days": DAY_CHOICES_RU, "trainers": trainers,
            "locations": locations, "training_types": training_types, "error": str(e),
        })

    logger.info("schedule_edited_via_admin", extra={"schedule_id": schedule_id, "club_id": request.club.id})
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "closeSlideOver, scheduleUpdated"
    return response


@management_view_required
def exception_revert(request: HttpRequest, schedule_id: int) -> HttpResponse:
    date_str = request.POST.get("date", "")
    try:
        session_date = date.fromisoformat(date_str)
    except ValueError:
        raise Http404

    try:
        delete_exception(
            club_id=request.club.id,
            schedule_id=schedule_id,
            exception_date=session_date,
        )
    except BusinessLogicError as e:
        context = _get_session_detail_context(
            club=request.club, schedule_id=schedule_id, session_date=session_date,
        )
        context["error"] = str(e)
        return render(request, "dashboard/schedule/_session_detail.html", context)

    context = _get_session_detail_context(
        club=request.club, schedule_id=schedule_id, session_date=session_date,
    )
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
def session_substitute(request: HttpRequest, schedule_id: int) -> HttpResponse:
    date_str = request.POST.get("date", "")
    substitute_trainer_id = request.POST.get("substitute_trainer_id", "")
    try:
        session_date = date.fromisoformat(date_str)
        sub_id = int(substitute_trainer_id)
    except (ValueError, TypeError):
        raise Http404

    try:
        substitute_trainer(
            club_id=request.club.id,
            schedule_id=schedule_id,
            date=session_date,
            substitute_trainer_id=sub_id,
        )
    except BusinessLogicError as e:
        context = _get_session_detail_context(
            club=request.club, schedule_id=schedule_id, session_date=session_date,
        )
        context["error"] = str(e)
        return render(request, "dashboard/schedule/_session_detail.html", context)

    context = _get_session_detail_context(
        club=request.club, schedule_id=schedule_id, session_date=session_date,
    )
    response = render(request, "dashboard/schedule/_session_detail.html", context)
    response["HX-Trigger"] = "scheduleUpdated"
    return response


@management_view_required
def checkin_cancel_admin(request: HttpRequest, checkin_id: int) -> HttpResponse:
    from apps.attendance.models import Checkin
    from apps.attendance.services.checkin import cancel_checkin

    schedule_id = request.POST.get("schedule_id", "")
    date_str = request.POST.get("date", "")
    try:
        session_date = date.fromisoformat(date_str)
        sid = int(schedule_id)
    except (ValueError, TypeError):
        raise Http404

    error = ""
    try:
        cancel_checkin(
            checkin_id=checkin_id,
            club_id=request.club.id,
            cancelled_by_user_id=request.user.id,
            user_role=request._membership.role,
        )
    except BusinessLogicError as e:
        logger = logging.getLogger(__name__)
        logger.warning("checkin_cancel_failed", extra={"checkin_id": checkin_id, "error": str(e)})
        error = str(e)

    # Return updated checkins partial
    checkins = list(
        Checkin.objects.for_club(request.club)
        .filter(schedule_id=sid, date=session_date, deleted_at__isnull=True)
        .select_related("student")
        .order_by("student__last_name", "student__first_name")
    )
    response = render(request, "dashboard/trainers/_session_checkins.html", {
        "checkins": checkins,
        "schedule_id": sid,
        "session_date": session_date,
        "error": error,
    })
    response["HX-Trigger"] = "scheduleUpdated"
    return response
