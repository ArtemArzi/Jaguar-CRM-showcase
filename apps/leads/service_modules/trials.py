from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.leads.service_modules._shared import (
    _ensure_active_lead,
    _ensure_expected_trainer_assignment,
    _get_active_trainer,
    _get_lead_for_update,
    _record_lifecycle_event,
)
from apps.students.models import Student

logger = logging.getLogger("apps.leads.services")


def _trigger_trial_done_side_effects(*, club_id: int, student_id: int) -> None:
    from apps.feedback.services import schedule_trial_feedback

    schedule_trial_feedback(club_id=club_id, student_id=student_id)

    from apps.pipelines.services import trigger_pipeline

    trigger_pipeline(club_id=club_id, student_id=student_id, pipeline_type="follow_up")


def is_exact_booked_trial_checkin(*, club_id: int, student: Student, checkin, lock: bool = False) -> bool:
    """Whether this check-in is the lead's dated booked-trial occurrence."""
    if (
        checkin is None
        or checkin.club_id != club_id
        or checkin.student_id != student.id
        or student.status != Student.Status.TRIAL
        or student.lead_status != Student.LeadStatus.TRIAL_BOOKED
    ):
        return False

    from apps.attendance.models import ScheduleEnrollment

    enrollments = ScheduleEnrollment.objects.for_club(club_id)
    if lock:
        enrollments = enrollments.select_for_update(of=("self",))
    return enrollments.filter(
        student_id=student.id,
        schedule_id=checkin.schedule_id,
        status=ScheduleEnrollment.Status.TRIAL,
        created_from__in=(
            ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
            ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
        ),
        starts_on__lte=checkin.date,
        ends_on__gte=checkin.date,
    ).exists()


def complete_booked_trial_after_checkin(
    *,
    club_id: int,
    student_id: int,
    checkin=None,
    trigger_trial_done_side_effects: Callable[..., None],
) -> bool:
    """Complete only the exact dated group-trial enrollment that was checked in."""
    if checkin is None:
        return False
    with transaction.atomic():
        student = (
            Student.objects.for_club(club_id)
            .select_for_update()
            .get(id=student_id, deleted_at__isnull=True)
        )
        if not is_exact_booked_trial_checkin(
            club_id=club_id,
            student=student,
            checkin=checkin,
            lock=True,
        ):
            return False

        old_status = student.lead_status
        student.lead_status = Student.LeadStatus.TRIAL_DONE
        student.save(update_fields=["lead_status", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.TRIAL_DONE,
            old_lead_status=old_status,
            new_lead_status=Student.LeadStatus.TRIAL_DONE,
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
        )

    trigger_trial_done_side_effects(club_id=club_id, student_id=student.id)
    logger.info("trial_completed_from_checkin", extra={"student_id": student.id, "club_id": club_id})
    return True


def book_trial(
    *,
    club_id: int,
    student_id: int,
    trial_date=None,
    schedule_id: int | None = None,
    occurrence_date=None,
    required_trainer_id: int | None = None,
    mode: str = "group",
    starts_at=None,
    ends_at=None,
    trainer_id: int | None = None,
    location_id: int | None = None,
    training_type_id: int | None = None,
    actor_user_id: int | None = None,
    required_assigned_trainer_id: int | None = None,
    now: Callable[[], datetime],
) -> Student:
    if mode not in {"group", "personal"}:
        raise BusinessLogicError("Invalid trial booking mode", code="invalid_trial_booking_mode")
    if mode == "personal":
        raise BusinessLogicError(
            "New personal trials are not supported; create a paid personal booking instead",
            code="personal_trial_not_supported",
        )

    if schedule_id is None or occurrence_date is None:
        raise BusinessLogicError(
            "Group trial requires schedule and occurrence date",
            code="trial_schedule_required",
        )

    with transaction.atomic():
        from apps.attendance.models import Schedule, ScheduleEnrollment, ScheduleException
        from apps.attendance.selectors import get_schedule_occurrence
        from apps.attendance.services import enroll_student_in_schedule
        from apps.clubs.timezones import club_zoneinfo

        student = _get_lead_for_update(club_id=club_id, student_id=student_id)

        _ensure_active_lead(student)
        _ensure_expected_trainer_assignment(
            student,
            required_assigned_trainer_id=required_assigned_trainer_id,
        )
        if student.lead_status not in ("new", "contacted", "thinking"):
            raise BusinessLogicError(
                f"Cannot book trial from status '{student.lead_status}'",
                code="invalid_transition",
            )

        schedule = (
            Schedule.objects.for_club(club_id)
            .select_for_update()
            .select_related("club")
            .filter(id=schedule_id)
            .first()
        )
        if schedule is None:
            raise BusinessLogicError(
                "Schedule does not belong to this club",
                code="schedule_club_mismatch",
            )
        list(
            ScheduleException.objects.for_club(club_id)
            .select_for_update()
            .filter(schedule_id=schedule.id)
        )
        occurrence = get_schedule_occurrence(
            club=schedule.club,
            schedule_id=schedule.id,
            occurrence_date=occurrence_date,
        )
        if occurrence is None:
            raise BusinessLogicError(
                "No schedule occurrence exists for trial date",
                code="schedule_occurrence_not_found",
            )
        if required_trainer_id is not None and occurrence.trainer_id != required_trainer_id:
            raise BusinessLogicError(
                "Schedule occurrence does not belong to this trainer",
                code="schedule_trainer_mismatch",
            )

        club_tz = club_zoneinfo(schedule.club)
        canonical_start = timezone.make_aware(
            datetime.combine(
                occurrence.effective_date,
                occurrence.effective_start_time,
            ),
            club_tz,
        )
        if trial_date is not None:
            supplied_start = _as_club_aware(
                trial_date,
                club_tz=club_tz,
                field="trial_date",
            )
            if supplied_start != canonical_start:
                raise BusinessLogicError(
                    "Trial time does not match the schedule occurrence",
                    code="trial_time_mismatch",
                )
        if canonical_start <= now():
            raise BusinessLogicError(
                "Trial occurrence must start in the future",
                code="trial_start_not_future",
            )

        old_status = student.lead_status
        student.lead_status = Student.LeadStatus.TRIAL_BOOKED
        student.status = Student.Status.TRIAL
        student.trial_date = canonical_start
        student.save(update_fields=["lead_status", "status", "trial_date", "updated_at"])
        _record_lifecycle_event(
            club_id=club_id,
            student_id=student.id,
            event_type=LeadLifecycleEvent.EventType.TRIAL_BOOKED,
            old_lead_status=old_status,
            new_lead_status=Student.LeadStatus.TRIAL_BOOKED,
            old_trainer_id=student.assigned_trainer_id,
            new_trainer_id=student.assigned_trainer_id,
            actor_user_id=actor_user_id,
            metadata={
                "mode": "group",
                "schedule_id": schedule.id,
                "occurrence_date": occurrence.occurrence_date.isoformat(),
                "effective_date": occurrence.effective_date.isoformat(),
                "trainer_id": occurrence.trainer_id,
            },
        )

        enroll_student_in_schedule(
            club_id=club_id,
            student_id=student.id,
            schedule_id=schedule.id,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=occurrence.effective_date,
            ends_on=occurrence.effective_date,
            trial_at=canonical_start,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
    logger.info(
        "trial_booked",
        extra={
            "student_id": student.id,
            "club_id": club_id,
            "schedule_id": schedule.id,
            "occurrence_date": occurrence.effective_date.isoformat(),
        },
    )

    return student


def _as_club_aware(value, *, club_tz, field: str):
    if value is None:
        raise BusinessLogicError(f"{field} is required", code=f"{field}_required")
    if timezone.is_naive(value):
        return timezone.make_aware(value, club_tz)
    return value.astimezone(club_tz)


def _booking_time(value):
    return value.time().replace(tzinfo=None, microsecond=0)


def _personal_trial_group_name(student: Student) -> str:
    display_name = f"{student.last_name} {student.first_name}".strip() or f"#{student.id}"
    return f"Пробная персоналка: {display_name}"[:100]


def _personal_trial_slot_conflicts(
    *,
    club_id: int,
    trainer_id: int,
    target_date,
    starts_at,
    ends_at,
    lock: bool = True,
) -> bool:
    from apps.attendance.models import Schedule

    qs = Schedule.objects.for_club(club_id)
    if lock:
        qs = qs.select_for_update()
    return (
        qs.filter(
            trainer_id=trainer_id,
            is_active=True,
            start_time__lt=_booking_time(ends_at),
            end_time__gt=_booking_time(starts_at),
        )
        .filter(
            models.Q(one_time_date=target_date)
            | models.Q(one_time_date__isnull=True, day_of_week=target_date.weekday())
        )
        .exists()
    )


def _book_personal_trial(
    *,
    club_id: int,
    student_id: int,
    trial_date,
    starts_at,
    ends_at,
    trainer_id: int | None,
    location_id: int | None,
    training_type_id: int | None,
    actor_user_id: int | None,
) -> Student:
    from apps.clubs.models import Club
    from apps.clubs.timezones import club_zoneinfo

    club = Club.objects.get(id=club_id)
    club_tz = club_zoneinfo(club)
    starts_at = _as_club_aware(
        starts_at or trial_date,
        club_tz=club_tz,
        field="starts_at",
    )
    ends_at = _as_club_aware(ends_at, club_tz=club_tz, field="ends_at")
    if starts_at <= timezone.now():
        raise BusinessLogicError(
            "Personal trial must start in the future",
            code="trial_start_not_future",
        )
    if ends_at <= starts_at:
        raise BusinessLogicError(
            "Personal trial end time must be after start time",
            code="invalid_personal_trial_time",
        )
    if starts_at.date() != ends_at.date():
        raise BusinessLogicError(
            "Personal trial must start and end on the same date",
            code="personal_trial_crosses_date",
        )
    if location_id is None:
        raise BusinessLogicError("Location is required", code="location_required")
    if training_type_id is None:
        raise BusinessLogicError("Training type is required", code="training_type_required")

    try:
        with transaction.atomic():
            return _book_personal_trial_locked(
                club_id=club_id,
                student_id=student_id,
                trial_date=trial_date,
                starts_at=starts_at,
                ends_at=ends_at,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                actor_user_id=actor_user_id,
            )
    except IntegrityError:
        effective_trainer_id = trainer_id
        if effective_trainer_id is None:
            effective_trainer_id = (
                Student.objects.for_club(club_id)
                .filter(id=student_id)
                .values_list("assigned_trainer_id", flat=True)
                .first()
            )
        if _personal_trial_slot_conflicts(
            club_id=club_id,
            trainer_id=effective_trainer_id,
            target_date=starts_at.date(),
            starts_at=starts_at,
            ends_at=ends_at,
            lock=False,
        ):
            raise BusinessLogicError(
                "Trainer already has a schedule in this time slot",
                code="personal_trial_slot_conflict",
            )
        raise


def _book_personal_trial_locked(
    *,
    club_id: int,
    student_id: int,
    trial_date,
    starts_at,
    ends_at,
    trainer_id: int | None,
    location_id: int,
    training_type_id: int,
    actor_user_id: int | None,
) -> Student:
    from apps.attendance.models import Schedule, ScheduleBookingEvent, ScheduleEnrollment
    from apps.billing.models import TrainingType
    from apps.clubs.models import Location
    from apps.trainers.models import TrainerLocation, TrainerRate

    student = _get_lead_for_update(club_id=club_id, student_id=student_id)
    _ensure_active_lead(student)
    if student.lead_status not in ("new", "contacted", "thinking"):
        raise BusinessLogicError(
            f"Cannot book trial from status '{student.lead_status}'",
            code="invalid_transition",
        )

    effective_trainer_id = trainer_id or student.assigned_trainer_id
    if effective_trainer_id is None:
        raise BusinessLogicError("Trainer is required", code="trainer_required")
    trainer = _get_active_trainer(
        club_id=club_id,
        trainer_id=effective_trainer_id,
        lock_for_update=True,
    )
    if not Location.objects.filter(id=location_id, club_id=club_id).exists():
        raise BusinessLogicError(
            "Location does not belong to this club",
            code="location_club_mismatch",
        )
    if not TrainerLocation.objects.for_club(club_id).filter(
        trainer=trainer,
        location_id=location_id,
    ).exists():
        raise BusinessLogicError(
            "Trainer is not assigned to this location",
            code="trainer_location_required",
        )

    training_type = (
        TrainingType.objects.for_club(club_id)
        .select_for_update()
        .filter(id=training_type_id, is_active=True)
        .first()
    )
    if training_type is None:
        raise BusinessLogicError(
            "Training type does not belong to this club",
            code="training_type_club_mismatch",
        )
    if training_type.kind not in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}:
        raise BusinessLogicError(
            "Personal trial requires a personal or mini-group training type",
            code="personal_trial_requires_personal_type",
        )
    if not TrainerRate.objects.for_club(club_id).filter(
        trainer=trainer,
        location_id=location_id,
        training_type=training_type,
    ).exists():
        raise BusinessLogicError(
            "Trainer rate is required for this personal trial",
            code="trainer_rate_required",
        )

    target_date = starts_at.date()
    if _personal_trial_slot_conflicts(
        club_id=club_id,
        trainer_id=trainer.id,
        target_date=target_date,
        starts_at=starts_at,
        ends_at=ends_at,
    ):
        raise BusinessLogicError(
            "Trainer already has a schedule in this time slot",
            code="personal_trial_slot_conflict",
        )

    schedule = Schedule.objects.create(
        club_id=club_id,
        day_of_week=target_date.weekday(),
        start_time=_booking_time(starts_at),
        end_time=_booking_time(ends_at),
        group_name=_personal_trial_group_name(student),
        trainer=trainer,
        location_id=location_id,
        training_type=training_type,
        one_time_date=target_date,
    )
    enrollment = ScheduleEnrollment(
        club_id=club_id,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=target_date,
        ends_on=target_date,
        trial_at=starts_at,
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    enrollment.full_clean()
    enrollment.save()

    old_status = student.lead_status
    student.lead_status = Student.LeadStatus.TRIAL_BOOKED
    student.status = Student.Status.TRIAL
    student.trial_date = starts_at
    student.save(update_fields=["lead_status", "status", "trial_date", "updated_at"])
    _record_lifecycle_event(
        club_id=club_id,
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.TRIAL_BOOKED,
        old_lead_status=old_status,
        new_lead_status=Student.LeadStatus.TRIAL_BOOKED,
        old_trainer_id=student.assigned_trainer_id,
        new_trainer_id=student.assigned_trainer_id,
        actor_user_id=actor_user_id,
        metadata={
            "mode": "personal",
            "schedule_id": schedule.id,
            "trainer_id": trainer.id,
            "location_id": location_id,
            "training_type_id": training_type.id,
        },
    )
    event = ScheduleBookingEvent(
        club_id=club_id,
        enrollment=enrollment,
        schedule=schedule,
        student=student,
        actor_id=actor_user_id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        effective_date=target_date,
        metadata={
            "mode": "personal",
            "lead_id": student.id,
            "trainer_id": trainer.id,
            "location_id": location_id,
            "training_type_id": training_type.id,
        },
    )
    event.full_clean()
    event.save()

    logger.info(
        "personal_trial_booked",
        extra={
            "student_id": student.id,
            "club_id": club_id,
            "schedule_id": schedule.id,
            "trial_date": target_date.isoformat(),
        },
    )
    return student
