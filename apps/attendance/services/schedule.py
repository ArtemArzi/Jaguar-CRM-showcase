from __future__ import annotations

import logging
from datetime import date, datetime, time

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    PersonalDropInBooking,
    Schedule,
    ScheduleException,
    TrainingGroup,
    TrainingGroupRolloutState,
)
from apps.billing.models import TrainingType
from apps.clubs.models import Location
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer

logger = logging.getLogger(__name__)


def _validate_schedule_time_range(*, start_time: time, end_time: time) -> None:
    if end_time <= start_time:
        raise BusinessLogicError(
            "Время окончания должно быть позже времени начала",
            code="invalid_schedule_time_range",
        )


def _one_time_slot_conflict_exists(
    *,
    club_id: int,
    trainer_id: int,
    one_time_date: date | None,
    start_time: time,
    end_time: time,
    exclude_schedule_id: int | None = None,
) -> bool:
    if one_time_date is None:
        return False
    qs = Schedule.objects.for_club(club_id).filter(
        trainer_id=trainer_id,
        one_time_date=one_time_date,
        start_time=start_time,
        end_time=end_time,
    )
    if exclude_schedule_id is not None:
        qs = qs.exclude(id=exclude_schedule_id)
    return qs.exists()


def _raise_schedule_slot_conflict() -> None:
    raise BusinessLogicError(
        "Trainer already has a one-time schedule in this time slot",
        code="schedule_slot_conflict",
    )


def _assert_no_live_personal_drop_in(*, club_id: int, schedule_id: int) -> None:
    if PersonalDropInBooking.objects.for_club(club_id).filter(
        enrollment__schedule_id=schedule_id,
        state__in=[
            PersonalDropInBooking.State.SCHEDULED,
            PersonalDropInBooking.State.ATTENDED,
        ],
    ).exists():
        raise BusinessLogicError(
            "Use the personal drop-in booking action for this session",
            code="personal_drop_in_use_booking_action",
        )


def _lock_schedule_exception_mutation_scope(
    *,
    club_id: int,
    schedule_id: int,
    substitute_trainer_id: int | None = None,
) -> Schedule:
    """Use the shared trainer -> schedule -> exception mutation order."""
    preview = Schedule.objects.for_club(club_id).only("trainer_id").get(id=schedule_id)
    trainer_ids = sorted({preview.trainer_id, *({substitute_trainer_id} if substitute_trainer_id else set())})
    locked_trainer_ids = set(
        Trainer.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(id__in=trainer_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if locked_trainer_ids != set(trainer_ids):
        raise BusinessLogicError("Тренер не относится к этому клубу", code="trainer_club_mismatch")
    return Schedule.objects.for_club(club_id).select_for_update(of=("self",)).get(id=schedule_id)


def _assert_no_personal_drop_in_slot_conflict(
    *,
    club_id: int,
    trainer_id: int,
    target_date: date,
    start_time: time,
    end_time: time,
) -> None:
    has_conflict = (
        PersonalDropInBooking.objects.for_club(club_id)
        .select_for_update(of=("self",))
        .filter(
            state__in=[
                PersonalDropInBooking.State.SCHEDULED,
                PersonalDropInBooking.State.ATTENDED,
            ],
            enrollment__schedule__trainer_id=trainer_id,
            enrollment__schedule__one_time_date=target_date,
            enrollment__schedule__start_time__lt=end_time,
            enrollment__schedule__end_time__gt=start_time,
        )
        .exists()
    )
    if has_conflict:
        raise BusinessLogicError(
            "Нельзя создать пересечение с активной разовой персоналкой",
            code="personal_drop_in_slot_conflict",
        )


def create_schedule(
    *,
    club_id: int,
    day_of_week: int,
    start_time: time,
    end_time: time,
    group_name: str,
    trainer_id: int,
    location_id: int,
    training_type_id: int,
    one_time_date: date | None = None,
    training_group_id: int | None = None,
    actor_user_id: int | None = None,
) -> Schedule:
    _validate_schedule_time_range(start_time=start_time, end_time=end_time)

    if not Trainer.objects.for_club(club_id).filter(id=trainer_id).exists():
        raise BusinessLogicError(
            "Trainer does not belong to this club",
            code="trainer_club_mismatch",
        )

    if not Location.objects.filter(id=location_id, club_id=club_id).exists():
        raise BusinessLogicError(
            "Location does not belong to this club",
            code="location_club_mismatch",
        )

    if not TrainingType.objects.for_club(club_id).filter(id=training_type_id, is_active=True).exists():
        raise BusinessLogicError(
            "Training type does not belong to this club",
            code="training_type_club_mismatch",
        )

    # Auto-derive day_of_week from one_time_date
    if one_time_date is not None:
        day_of_week = one_time_date.weekday()

    if _one_time_slot_conflict_exists(
        club_id=club_id,
        trainer_id=trainer_id,
        one_time_date=one_time_date,
        start_time=start_time,
        end_time=end_time,
    ):
        _raise_schedule_slot_conflict()

    try:
        with transaction.atomic():
            training_type = TrainingType.objects.for_club(club_id).only("kind").get(id=training_type_id)
            if one_time_date is None and training_type.kind == TrainingType.Kind.GROUP:
                from apps.attendance.services.training_group_memberships import (
                    assert_training_group_new_writes_enabled,
                    lock_training_group_mutation_scope,
                )

                rollout_state = lock_training_group_mutation_scope(club_id=club_id)
                if training_group_id is not None or rollout_state.mode in {
                    TrainingGroupRolloutState.Mode.SHADOW,
                    TrainingGroupRolloutState.Mode.ACTIVE,
                }:
                    assert_training_group_new_writes_enabled()
                if rollout_state.mode == TrainingGroupRolloutState.Mode.OFF:
                    if training_group_id is not None:
                        raise BusinessLogicError(
                            "Training group writes are disabled for this rollout mode.",
                            code="training_group_writes_disabled",
                        )
                elif rollout_state.mode in {
                    TrainingGroupRolloutState.Mode.SHADOW,
                    TrainingGroupRolloutState.Mode.ACTIVE,
                }:
                    if training_group_id is None:
                        from apps.attendance.services.training_groups import create_training_group

                        training_group_id = create_training_group(
                            club_id=club_id,
                            name=group_name,
                            training_type_id=training_type_id,
                            location_id=location_id,
                            responsible_trainer_id=trainer_id,
                            actor_user_id=actor_user_id,
                        ).id
                else:
                    raise BusinessLogicError(
                        "Training group writes are disabled for this rollout mode.",
                        code="training_group_writes_disabled",
                    )
            if training_group_id is not None:
                from apps.attendance.services.training_group_memberships import (
                    fan_out_training_group_membership_projections,
                    lock_training_group_mutation_scope,
                )

                lock_training_group_mutation_scope(club_id=club_id)
                training_group = (
                    TrainingGroup.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .filter(id=training_group_id)
                    .first()
                )
                if training_group is None:
                    raise BusinessLogicError(
                        "Training group does not belong to this club",
                        code="training_group_club_mismatch",
                    )
                if training_group.status != TrainingGroup.Status.ACTIVE:
                    raise BusinessLogicError(
                        "Only an active training group may receive a recurring slot.",
                        code="training_group_not_active",
                    )
            schedule = Schedule(
                club_id=club_id,
                day_of_week=day_of_week,
                start_time=start_time,
                end_time=end_time,
                group_name=group_name,
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
                one_time_date=one_time_date,
                training_group_id=training_group_id,
            )
            schedule.full_clean()
            schedule.save()
            if training_group_id is not None:
                # The new active slot is not observable outside this transaction
                # until every open membership has its compatibility projection.
                fan_out_training_group_membership_projections(
                    club_id=club_id,
                    training_group_id=training_group_id,
                )
    except IntegrityError:
        if _one_time_slot_conflict_exists(
            club_id=club_id,
            trainer_id=trainer_id,
            one_time_date=one_time_date,
            start_time=start_time,
            end_time=end_time,
        ):
            _raise_schedule_slot_conflict()
        raise
    logger.info("schedule_created", extra={"schedule_id": schedule.id, "club_id": club_id})
    return schedule


_UPDATE_SCHEDULE_FIELDS = frozenset(
    {
        "day_of_week",
        "start_time",
        "end_time",
        "group_name",
        "trainer_id",
        "location_id",
        "training_type_id",
        "is_active",
        "one_time_date",
    }
)


@transaction.atomic
def update_schedule(*, schedule_id: int, club_id: int, **fields) -> Schedule:
    bad = set(fields) - _UPDATE_SCHEDULE_FIELDS
    if bad:
        raise BusinessLogicError(f"Fields not allowed: {bad}", code="invalid_fields")
    preview = Schedule.objects.for_club(club_id).only(
        "is_active",
        "training_group_id",
        "group_name",
        "training_type_id",
        "training_type__kind",
        "location_id",
        "one_time_date",
    ).select_related("training_type").get(id=schedule_id)
    is_recurring_group_schedule = (
        preview.one_time_date is None and preview.training_type.kind == TrainingType.Kind.GROUP
    )
    protected_identity_changed = any(
        getattr(preview, field) != fields[field]
        for field in ("group_name", "training_type_id", "location_id", "one_time_date")
        if field in fields
    )
    if preview.training_group_id and protected_identity_changed:
        raise BusinessLogicError(
            "Use a future replacement slot for a linked training group schedule identity change.",
            code="training_group_linked_schedule_identity_protected",
        )
    is_group_reactivation = (
        preview.training_group_id is not None
        and not preview.is_active
        and fields.get("is_active") is True
    )
    if is_recurring_group_schedule:
        from apps.attendance.services.training_group_memberships import (
            assert_training_group_new_writes_enabled,
            lock_training_group_mutation_scope,
        )

        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        if preview.training_group_id is not None:
            safe_deactivation = (
                preview.is_active
                and fields == {"is_active": False}
            )
            if not safe_deactivation:
                if rollout_state.mode not in {
                    TrainingGroupRolloutState.Mode.SHADOW,
                    TrainingGroupRolloutState.Mode.ACTIVE,
                }:
                    raise BusinessLogicError(
                        "Training group writes are disabled for this rollout mode.",
                        code="training_group_writes_disabled",
                    )
                assert_training_group_new_writes_enabled()
    if is_group_reactivation:
        group = TrainingGroup.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=preview.training_group_id
        )
        if group.status != TrainingGroup.Status.ACTIVE:
            raise BusinessLogicError(
                "Only an active training group may reactivate a recurring slot.",
                code="training_group_not_active",
            )
    schedule = Schedule.objects.for_club(club_id).select_for_update().get(id=schedule_id)
    _assert_no_live_personal_drop_in(club_id=club_id, schedule_id=schedule.id)
    # Auto-derive day_of_week when one_time_date changes
    if "one_time_date" in fields and fields["one_time_date"] is not None:
        fields["day_of_week"] = fields["one_time_date"].weekday()
    if "trainer_id" in fields and not Trainer.objects.for_club(club_id).filter(id=fields["trainer_id"]).exists():
        raise BusinessLogicError(
            "Trainer does not belong to this club",
            code="trainer_club_mismatch",
        )
    if "location_id" in fields and not Location.objects.filter(id=fields["location_id"], club_id=club_id).exists():
        raise BusinessLogicError(
            "Location does not belong to this club",
            code="location_club_mismatch",
        )
    if "training_type_id" in fields and not TrainingType.objects.for_club(club_id).filter(
        id=fields["training_type_id"],
        is_active=True,
    ).exists():
        raise BusinessLogicError(
            "Training type does not belong to this club",
            code="training_type_club_mismatch",
        )
    next_trainer_id = fields.get("trainer_id", schedule.trainer_id)
    next_one_time_date = fields.get("one_time_date", schedule.one_time_date)
    next_start_time = fields.get("start_time", schedule.start_time)
    next_end_time = fields.get("end_time", schedule.end_time)
    _validate_schedule_time_range(
        start_time=next_start_time,
        end_time=next_end_time,
    )
    if _one_time_slot_conflict_exists(
        club_id=club_id,
        trainer_id=next_trainer_id,
        one_time_date=next_one_time_date,
        start_time=next_start_time,
        end_time=next_end_time,
        exclude_schedule_id=schedule.id,
    ):
        _raise_schedule_slot_conflict()
    for field, value in fields.items():
        setattr(schedule, field, value)
    try:
        schedule.save(update_fields=[*fields.keys(), "updated_at"])
    except IntegrityError:
        if _one_time_slot_conflict_exists(
            club_id=club_id,
            trainer_id=next_trainer_id,
            one_time_date=next_one_time_date,
            start_time=next_start_time,
            end_time=next_end_time,
            exclude_schedule_id=schedule.id,
        ):
            _raise_schedule_slot_conflict()
        raise
    if is_group_reactivation:
        from apps.attendance.services.training_group_memberships import fan_out_training_group_membership_projections

        fan_out_training_group_membership_projections(
            club_id=club_id,
            training_group_id=schedule.training_group_id,
        )
    return schedule


def _create_schedule_exception(
    *,
    club_id: int,
    schedule_id: int,
    exception_date: date,
    exception_type: str,
    reason: str = "",
    **extra_fields,
) -> ScheduleException:
    try:
        with transaction.atomic():
            schedule = _lock_schedule_exception_mutation_scope(
                club_id=club_id,
                schedule_id=schedule_id,
                substitute_trainer_id=(
                    extra_fields.get("substitute_trainer_id")
                    if exception_type == ScheduleException.ExceptionType.SUBSTITUTE
                    else None
                ),
            )
            _assert_no_live_personal_drop_in(club_id=club_id, schedule_id=schedule.id)
            _ensure_schedule_exception_target_is_not_past(
                schedule=schedule,
                exception_date=exception_date,
            )
            _assert_schedule_exception_target_exists(
                schedule=schedule,
                exception_date=exception_date,
                exception_type=exception_type,
            )
            if exception_type == ScheduleException.ExceptionType.RESCHEDULED:
                _assert_reschedule_destination_available(
                    schedule=schedule,
                    exception_date=exception_date,
                    new_date=extra_fields["new_date"],
                )
                _assert_no_personal_drop_in_slot_conflict(
                    club_id=club_id,
                    trainer_id=schedule.trainer_id,
                    target_date=extra_fields["new_date"],
                    start_time=extra_fields["new_start_time"],
                    end_time=extra_fields["new_end_time"],
                )
            if exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
                start_time, end_time = _effective_occurrence_times(
                    schedule=schedule,
                    target_date=exception_date,
                )
                _assert_no_personal_drop_in_slot_conflict(
                    club_id=club_id,
                    trainer_id=extra_fields["substitute_trainer_id"],
                    target_date=exception_date,
                    start_time=start_time,
                    end_time=end_time,
                )
            if exception_type in {
                ScheduleException.ExceptionType.CANCELLED,
                ScheduleException.ExceptionType.RESCHEDULED,
            }:
                _assert_schedule_exception_target_has_no_live_checkins(
                    club_id=club_id,
                    schedule=schedule,
                    exception_date=exception_date,
                )
            exc = ScheduleException.objects.create(
                club_id=club_id,
                schedule=schedule,
                date=exception_date,
                exception_type=exception_type,
                reason=reason,
                **extra_fields,
            )
    except IntegrityError:
        # Verify it's actually the unique constraint (schedule, date) and not something else
        if ScheduleException.objects.for_club(schedule.club_id).filter(schedule=schedule, date=exception_date).exists():
            raise BusinessLogicError(
                "Exception already exists for this schedule and date",
                code="duplicate_exception",
            )
        raise  # re-raise unexpected IntegrityError as-is

    return exc


def _schedule_base_occurs_on_date(*, schedule: Schedule, target_date: date) -> bool:
    if schedule.one_time_date is not None:
        return schedule.one_time_date == target_date
    return schedule.day_of_week == target_date.weekday()


def _schedule_has_rescheduled_occurrence_on_date(*, schedule: Schedule, target_date: date) -> bool:
    return ScheduleException.objects.for_club(schedule.club_id).filter(
        schedule=schedule,
        exception_type=ScheduleException.ExceptionType.RESCHEDULED,
        new_date=target_date,
    ).exists()


def _effective_occurrence_times(*, schedule: Schedule, target_date: date) -> tuple[time, time]:
    """Return the effective time of a base or rescheduled schedule occurrence."""
    rescheduled_exception = (
        ScheduleException.objects.for_club(schedule.club_id)
        .select_for_update(of=("self",))
        .filter(
            schedule=schedule,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=target_date,
        )
        .order_by("id")
        .first()
    )
    if rescheduled_exception is not None:
        return rescheduled_exception.new_start_time, rescheduled_exception.new_end_time
    return schedule.start_time, schedule.end_time


def _assert_reschedule_destination_available(
    *,
    schedule: Schedule,
    exception_date: date,
    new_date: date,
) -> None:
    if new_date == exception_date:
        return

    destination_conflicts = _schedule_base_occurs_on_date(
        schedule=schedule,
        target_date=new_date,
    ) or _schedule_has_rescheduled_occurrence_on_date(
        schedule=schedule,
        target_date=new_date,
    )
    if destination_conflicts:
        raise BusinessLogicError(
            "На выбранную дату уже существует занятие этого расписания",
            code="schedule_occurrence_conflict",
        )


def _assert_schedule_exception_target_exists(
    *,
    schedule: Schedule,
    exception_date: date,
    exception_type: str,
) -> None:
    if not schedule.is_active:
        raise BusinessLogicError(
            "Нет подходящей тренировки на выбранную дату",
            code="schedule_occurrence_not_found",
        )

    if _schedule_base_occurs_on_date(schedule=schedule, target_date=exception_date):
        return

    if (
        exception_type == ScheduleException.ExceptionType.SUBSTITUTE
        and _schedule_has_rescheduled_occurrence_on_date(
            schedule=schedule,
            target_date=exception_date,
        )
    ):
        return

    raise BusinessLogicError(
        "Нет подходящей тренировки на выбранную дату",
        code="schedule_occurrence_not_found",
    )


def _assert_schedule_exception_target_has_no_live_checkins(
    *,
    club_id: int,
    schedule: Schedule,
    exception_date: date,
) -> None:
    has_checkins = (
        Checkin.objects.for_club(club_id)
        .select_for_update()
        .filter(
            schedule=schedule,
            date=exception_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .exists()
    )
    if has_checkins:
        raise BusinessLogicError(
            "Нельзя отменить или перенести занятие: уже есть отметки посещения",
            code="schedule_session_has_checkins",
        )


def _ensure_schedule_exception_target_is_not_past(
    *,
    schedule: Schedule,
    exception_date: date,
) -> None:
    session_end = datetime.combine(exception_date, schedule.end_time)
    if timezone.is_naive(session_end):
        session_end = timezone.make_aware(session_end, club_zoneinfo(schedule.club))
    if session_end <= timezone.now():
        raise BusinessLogicError(
            "Past session cannot be changed",
            code="schedule_session_past",
        )


def cancel_session(
    *,
    club_id: int,
    schedule_id: int,
    date: date,
    reason: str = "",
) -> ScheduleException:
    exc = _create_schedule_exception(
        club_id=club_id,
        schedule_id=schedule_id,
        exception_date=date,
        exception_type=ScheduleException.ExceptionType.CANCELLED,
        reason=reason,
    )
    logger.info(
        "session_cancelled",
        extra={"schedule_id": schedule_id, "date": str(date), "club_id": club_id},
    )
    return exc


def reschedule_session(
    *,
    club_id: int,
    schedule_id: int,
    date: date,
    new_date: date,
    new_start_time: time,
    new_end_time: time,
    reason: str = "",
) -> ScheduleException:
    _validate_schedule_time_range(
        start_time=new_start_time,
        end_time=new_end_time,
    )
    exc = _create_schedule_exception(
        club_id=club_id,
        schedule_id=schedule_id,
        exception_date=date,
        exception_type=ScheduleException.ExceptionType.RESCHEDULED,
        reason=reason,
        new_date=new_date,
        new_start_time=new_start_time,
        new_end_time=new_end_time,
    )
    logger.info(
        "session_rescheduled",
        extra={"schedule_id": schedule_id, "date": str(date), "new_date": str(new_date), "club_id": club_id},
    )
    return exc


def substitute_trainer(
    *,
    club_id: int,
    schedule_id: int,
    date: date,
    substitute_trainer_id: int,
    reason: str = "",
) -> ScheduleException:
    if not Trainer.objects.for_club(club_id).filter(id=substitute_trainer_id).exists():
        raise BusinessLogicError(
            "Substitute trainer does not belong to this club",
            code="trainer_club_mismatch",
        )

    exc = _create_schedule_exception(
        club_id=club_id,
        schedule_id=schedule_id,
        exception_date=date,
        exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
        reason=reason,
        substitute_trainer_id=substitute_trainer_id,
    )
    logger.info(
        "trainer_substituted",
        extra={
            "schedule_id": schedule_id,
            "date": str(date),
            "substitute_id": substitute_trainer_id,
            "club_id": club_id,
        },
    )
    return exc


def delete_exception(*, club_id: int, schedule_id: int, exception_date: date) -> None:
    """Revert a session exception — restore original schedule for this date."""
    with transaction.atomic():
        schedule = _lock_schedule_exception_mutation_scope(
            club_id=club_id,
            schedule_id=schedule_id,
        )
        exc = (
            ScheduleException.objects.for_club(club_id)
            .select_for_update()
            .filter(
                schedule=schedule,
                date=exception_date,
            )
            .first()
        )

        if exc is None:
            raise BusinessLogicError(
                "Исключение не найдено для этой даты",
                code="exception_not_found",
            )

        if schedule.is_active:
            start_time, end_time = schedule.start_time, schedule.end_time
            if exc.exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
                start_time, end_time = _effective_occurrence_times(
                    schedule=schedule,
                    target_date=exception_date,
                )
            _assert_no_personal_drop_in_slot_conflict(
                club_id=club_id,
                trainer_id=schedule.trainer_id,
                target_date=exception_date,
                start_time=start_time,
                end_time=end_time,
            )

        # Safety: can't revert substitute if checkins already recorded for this date
        if exc.exception_type == ScheduleException.ExceptionType.SUBSTITUTE:
            has_checkins = (
                Checkin.objects.for_club(club_id)
                .select_for_update()
                .filter(
                    schedule=schedule,
                    date=exception_date,
                    deleted_at__isnull=True,
                )
                .exists()
            )
            if has_checkins:
                raise BusinessLogicError(
                    "Нельзя отменить замену: за эту дату уже записаны посещения",
                    code="exception_has_checkins",
                )

        exc.delete()

    logger.info(
        "exception_deleted",
        extra={"schedule_id": schedule_id, "date": str(exception_date), "club_id": club_id},
    )
