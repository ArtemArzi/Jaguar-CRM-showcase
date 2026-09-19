from __future__ import annotations

from datetime import date as date_type

from django.utils import timezone

from apps.common.exceptions import BusinessLogicError

MASS_NOTIFICATION_SEGMENT_TYPES = frozenset({"club", "status", "location", "group"})


def get_students_for_training_reminder(
    *, club, schedule_id: int, target_date: date_type | None = None
) -> list:
    """Return enrolled or habitual students who should get a reminder."""
    from apps.attendance.selectors import get_expected_student_ids_for_schedule_date
    from apps.students.models import Student

    expected_student_ids = get_expected_student_ids_for_schedule_date(
        club=club,
        schedule_id=schedule_id,
        target_date=target_date or timezone.now().date(),
        legacy_window_days=28,
        legacy_min_checkins=2,
    )
    return list(
        Student.objects.for_club(club)
        .filter(
            id__in=expected_student_ids,
            status__in=[Student.Status.ACTIVE, Student.Status.AT_RISK],
            deleted_at__isnull=True,
        )
        .select_related("club")
        .order_by("first_name", "last_name", "id")
    )


def _positive_int_segment_value(value, *, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BusinessLogicError(message, code="invalid_mass_notification_segment_filter")
    return value


def validate_mass_notification_segment(
    *,
    club_id: int,
    segment_type: str,
    segment_filter: dict,
) -> dict:
    from apps.attendance.models import Schedule
    from apps.clubs.models import Location
    from apps.students.models import Student

    if segment_type not in MASS_NOTIFICATION_SEGMENT_TYPES:
        raise BusinessLogicError(
            "Неизвестный тип сегмента",
            code="invalid_mass_notification_segment_type",
        )
    if not isinstance(segment_filter, dict):
        raise BusinessLogicError(
            "Некорректный фильтр сегмента",
            code="invalid_mass_notification_segment_filter",
        )

    if segment_type == "club":
        if segment_filter:
            raise BusinessLogicError(
                "Для сегмента «Все ученики» дополнительный фильтр не нужен",
                code="invalid_mass_notification_segment_filter",
            )
        return {}

    if segment_type == "group":
        if set(segment_filter) not in ({"training_group_id"}, {"schedule_id"}):
            raise BusinessLogicError(
                "Укажите корректный ID группы",
                code="mass_notification_segment_value_required",
            )
        if "training_group_id" in segment_filter:
            from apps.attendance.models import TrainingGroup

            training_group_id = _positive_int_segment_value(
                segment_filter["training_group_id"],
                message="Укажите корректный ID группы",
            )
            if not TrainingGroup.objects.for_club(club_id).filter(id=training_group_id).exists():
                raise BusinessLogicError(
                    "Группа не найдена",
                    code="mass_notification_segment_not_found",
                )
            return {"training_group_id": training_group_id}

        schedule_id = _positive_int_segment_value(
            segment_filter["schedule_id"],
            message="Укажите корректный ID группы",
        )
        schedule = Schedule.objects.for_club(club_id).only("id", "training_group_id").filter(id=schedule_id).first()
        if schedule is None:
            raise BusinessLogicError("Группа не найдена", code="mass_notification_segment_not_found")
        return (
            {"training_group_id": schedule.training_group_id}
            if schedule.training_group_id
            else {"schedule_id": schedule.id}
        )

    expected_key = {"status": "status", "location": "location_id"}[segment_type]
    if set(segment_filter) != {expected_key}:
        messages = {
            "status": "Выберите статус ученика",
            "location": "Укажите корректный ID локации",
        }
        raise BusinessLogicError(
            messages[segment_type],
            code="mass_notification_segment_value_required",
        )

    if segment_type == "status":
        status = segment_filter["status"]
        if not isinstance(status, str) or status not in Student.Status.values:
            raise BusinessLogicError(
                "Выберите корректный статус ученика",
                code="invalid_mass_notification_status",
            )
        return {"status": status}

    if segment_type == "location":
        location_id = _positive_int_segment_value(
            segment_filter["location_id"],
            message="Укажите корректный ID локации",
        )
        if not Location.objects.filter(club_id=club_id, id=location_id).exists():
            raise BusinessLogicError(
                "Локация не найдена",
                code="mass_notification_segment_not_found",
            )
        return {"location_id": location_id}



def get_recipients_for_segment(*, club_id: int, segment_type: str, segment_filter: dict) -> list[int]:
    """Return list of user IDs matching the given segment within the club.

    Segments that target students return linked student user IDs inside the
    current club. Students without portal users are not push recipients.
    """
    from apps.attendance.models import ScheduleEnrollment
    from apps.attendance.selectors import schedule_enrollment_active_on_date_q
    from apps.clubs.models import Club, ClubMembership
    from apps.clubs.timezones import club_localdate
    from apps.students.models import Student

    normalized_filter = validate_mass_notification_segment(
        club_id=club_id,
        segment_type=segment_type,
        segment_filter=segment_filter,
    )
    students = (
        Student.objects.for_club(club_id)
        .filter(
            deleted_at__isnull=True,
            user_id__isnull=False,
            user__club_memberships__club_id=club_id,
            user__club_memberships__role=ClubMembership.Role.STUDENT,
            user__club_memberships__is_active=True,
        )
        .distinct()
    )

    if segment_type == "status":
        students = students.filter(status=normalized_filter["status"])
    elif segment_type in {"location", "group"}:
        club = Club.objects.only("id", "timezone").get(id=club_id)
        enrollments = ScheduleEnrollment.objects.for_club(club_id).filter(
            schedule_enrollment_active_on_date_q(club_localdate(club)),
            schedule__club_id=club_id,
        )
        if segment_type == "location":
            enrollments = enrollments.filter(
                schedule__location_id=normalized_filter["location_id"],
            )
            students = students.filter(id__in=enrollments.values("student_id"))
        else:
            from apps.attendance.models import Schedule
            from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date

            schedule_ids = list(
                Schedule.objects.for_club(club_id)
                .filter(
                    training_group_id=normalized_filter.get("training_group_id"),
                    is_active=True,
                    one_time_date__isnull=True,
                )
                .values_list("id", flat=True)
            ) if "training_group_id" in normalized_filter else [normalized_filter["schedule_id"]]
            roster = resolve_expected_roster_by_schedule_date(
                club=club,
                schedule_ids=schedule_ids,
                target_date=club_localdate(club),
            )
            students = students.filter(
                id__in={student_id for rows in roster.values() for student_id in rows}
            )

    return list(students.order_by("id").values_list("user_id", flat=True))
