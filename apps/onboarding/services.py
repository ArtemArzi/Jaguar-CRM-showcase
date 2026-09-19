from __future__ import annotations

import logging
from copy import deepcopy
from datetime import time
from uuid import uuid4

from django.db import transaction
from pydantic import ValidationError

from apps.common.exceptions import BusinessLogicError
from apps.grades.services import GRADE_TEMPLATES, add_grade, create_grade_system
from apps.onboarding.models import OnboardingDraft
from apps.onboarding.schemas import (
    GradesStepIn,
    ScheduleStepIn,
    StudentsStepIn,
    TariffsStepIn,
    TrainersStepIn,
)

logger = logging.getLogger(__name__)

DRAFT_SCHEMA_VERSION = 2
SCHEMA_VERSION_KEY = "_schema_version"
SKIPPED_STEPS_KEY = "_skipped_steps"
ONBOARDING_STEP_SEQUENCE = (1, 2, 3, 4, 5)

STEP_SCHEMAS = {
    OnboardingDraft.Step.GRADES: GradesStepIn,
    OnboardingDraft.Step.TRAINERS: TrainersStepIn,
    OnboardingDraft.Step.SCHEDULE: ScheduleStepIn,
    OnboardingDraft.Step.STUDENTS: StudentsStepIn,
    OnboardingDraft.Step.TARIFFS: TariffsStepIn,
}


def next_onboarding_step(step: int) -> int | None:
    try:
        index = ONBOARDING_STEP_SEQUENCE.index(step)
    except ValueError as exc:
        raise BusinessLogicError(f"Invalid step: {step}", code="invalid_step") from exc
    if index == len(ONBOARDING_STEP_SEQUENCE) - 1:
        return None
    return ONBOARDING_STEP_SEQUENCE[index + 1]


def _new_draft_data() -> dict:
    return {
        SCHEMA_VERSION_KEY: DRAFT_SCHEMA_VERSION,
        SKIPPED_STEPS_KEY: [],
    }


def start_onboarding(*, club_id: int) -> OnboardingDraft:
    draft, created = OnboardingDraft.objects.get_or_create(
        club_id=club_id,
        is_completed=False,
        defaults={"current_step": OnboardingDraft.Step.GRADES, "data": _new_draft_data()},
    )
    if not created:
        _upgrade_legacy_draft(draft=draft)
    if created:
        logger.info("onboarding_started", extra={"draft_id": draft.id, "club_id": club_id})
    return draft


def save_step(*, club_id: int, draft_id: int, step: int, data: dict) -> OnboardingDraft:
    draft = OnboardingDraft.objects.for_club(club_id).get(id=draft_id, is_completed=False)
    _upgrade_legacy_draft(draft=draft)

    parsed = _validate_step(step=step, data=data)
    if step == OnboardingDraft.Step.TRAINERS:
        _validate_unique_draft_trainer_refs(parsed=parsed)

    draft_data = deepcopy(draft.data)
    draft_data[str(step)] = parsed.model_dump(mode="json")
    skipped_steps = set(draft_data.get(SKIPPED_STEPS_KEY, []))
    skipped_steps.discard(step)
    draft_data[SKIPPED_STEPS_KEY] = sorted(skipped_steps)
    draft.data = draft_data

    next_step = next_onboarding_step(step)
    draft.current_step = max(draft.current_step, next_step or step)
    draft.save(update_fields=["data", "current_step", "updated_at"])

    logger.info(
        "onboarding_step_saved",
        extra={"draft_id": draft.id, "step": step, "club_id": club_id},
    )
    return draft


def skip_step(*, club_id: int, draft_id: int, step: int) -> OnboardingDraft:
    if step not in ONBOARDING_STEP_SEQUENCE:
        raise BusinessLogicError(f"Invalid step: {step}", code="invalid_step")

    draft = OnboardingDraft.objects.for_club(club_id).get(id=draft_id, is_completed=False)
    _upgrade_legacy_draft(draft=draft)
    draft_data = deepcopy(draft.data)
    draft_data.pop(str(step), None)
    skipped_steps = set(draft_data.get(SKIPPED_STEPS_KEY, []))
    skipped_steps.add(step)
    draft_data[SKIPPED_STEPS_KEY] = sorted(skipped_steps)
    draft.data = draft_data

    next_step = next_onboarding_step(step)
    draft.current_step = max(draft.current_step, next_step or step)
    draft.save(update_fields=["data", "current_step", "updated_at"])
    logger.info(
        "onboarding_step_skipped",
        extra={"draft_id": draft.id, "step": step, "club_id": club_id},
    )
    return draft


def finish_onboarding(*, club_id: int, draft_id: int) -> OnboardingDraft:
    # A legacy-to-current draft upgrade is a recoverability mutation of the
    # draft itself. Commit it before the all-or-nothing object creation
    # transaction so a validation error can return the owner to populated v2
    # inputs instead of rolling the upgrade back with business side effects.
    with transaction.atomic():
        draft_to_normalize = (
            OnboardingDraft.objects.for_club(club_id)
            .select_for_update()
            .get(id=draft_id)
        )
        if not draft_to_normalize.is_completed:
            _upgrade_legacy_draft(draft=draft_to_normalize)

    first_completion = False

    with transaction.atomic():
        draft = (
            OnboardingDraft.objects.for_club(club_id)
            .select_for_update()
            .get(id=draft_id)
        )
        if draft.is_completed:
            return draft

        completion = _prevalidate_completion(club_id=club_id, data=draft.data)

        draft_trainer_ids = _apply_trainers(
            club_id=club_id,
            parsed=completion["trainers"],
        )
        _apply_grades(club_id=club_id, data=draft.data)
        _apply_schedule(
            club_id=club_id,
            parsed=completion["schedules"],
            draft_trainer_ids=draft_trainer_ids,
        )
        _apply_students(club_id=club_id, data=draft.data)
        _apply_tariffs(club_id=club_id, data=draft.data)

        draft.is_completed = True
        draft.current_step = ONBOARDING_STEP_SEQUENCE[-1]
        draft.save(update_fields=["is_completed", "current_step", "updated_at"])
        first_completion = True
        transaction.on_commit(lambda: _enqueue_default_pipeline_seed(club_id=club_id))

    if first_completion:
        logger.info("onboarding_completed", extra={"draft_id": draft.id, "club_id": club_id})
    return draft


def _validate_step(*, step: int, data: dict):
    schema_cls = STEP_SCHEMAS.get(step)
    if schema_cls is None:
        raise BusinessLogicError(f"Invalid step: {step}", code="invalid_step")
    try:
        return schema_cls.model_validate(data)
    except ValidationError as exc:
        raise BusinessLogicError(
            "Onboarding step data is invalid",
            code="invalid_onboarding_step",
        ) from exc


def _validate_unique_draft_trainer_refs(*, parsed: TrainersStepIn) -> None:
    refs = [str(item.client_ref) for item in parsed.trainers]
    if len(refs) != len(set(refs)):
        raise BusinessLogicError(
            "Draft trainer references must be unique",
            code="onboarding_trainer_reference_duplicate",
        )


def _prevalidate_completion(*, club_id: int, data: dict) -> dict:
    trainers = _validated_optional_step(
        data=data,
        step=OnboardingDraft.Step.TRAINERS,
        schema_cls=TrainersStepIn,
    )
    _validate_unique_draft_trainer_refs(parsed=trainers)
    _validate_draft_trainer_phones(club_id=club_id, parsed=trainers)

    skipped_steps = set(data.get(SKIPPED_STEPS_KEY, []))
    raw_schedule = data.get(str(OnboardingDraft.Step.SCHEDULE))
    if raw_schedule is None:
        if OnboardingDraft.Step.SCHEDULE not in skipped_steps:
            raise BusinessLogicError(
                "Choose a schedule or explicitly skip the schedule step",
                code="onboarding_schedule_incomplete",
            )
        schedules = ScheduleStepIn(schedules=[])
    else:
        schedules = _validated_optional_step(
            data=data,
            step=OnboardingDraft.Step.SCHEDULE,
            schema_cls=ScheduleStepIn,
        )
        if not schedules.schedules:
            raise BusinessLogicError(
                "Choose a schedule or explicitly skip the schedule step",
                code="onboarding_schedule_incomplete",
            )

    if not schedules.schedules:
        return {"trainers": trainers, "schedules": schedules}

    from apps.clubs.models import Location
    from apps.trainers.models import Trainer

    draft_refs = {str(item.client_ref) for item in trainers.trainers}
    existing_ids = {item.trainer_id for item in schedules.schedules if item.trainer_id is not None}
    valid_existing_ids = set(
        Trainer.objects.for_club(club_id)
        .filter(id__in=existing_ids, is_active=True)
        .values_list("id", flat=True)
    )

    location_ids = {item.location_id for item in schedules.schedules if item.location_id is not None}
    valid_location_ids = set(
        Location.objects.filter(club_id=club_id, id__in=location_ids).values_list("id", flat=True)
    )
    club_has_location = Location.objects.filter(club_id=club_id).exists()

    for item in schedules.schedules:
        has_existing = item.trainer_id is not None
        has_draft = item.trainer_ref is not None
        if has_existing == has_draft:
            raise BusinessLogicError(
                "Select one trainer for every submitted schedule",
                code="onboarding_schedule_trainer_unresolved",
            )
        if has_existing and item.trainer_id not in valid_existing_ids:
            raise BusinessLogicError(
                "Selected trainer is unavailable for this club",
                code="onboarding_schedule_trainer_unresolved",
            )
        if has_draft and str(item.trainer_ref) not in draft_refs:
            raise BusinessLogicError(
                "Selected draft trainer is unavailable",
                code="onboarding_schedule_trainer_unresolved",
            )

        if item.location_id is None:
            code = (
                "onboarding_schedule_location_required"
                if not club_has_location
                else "onboarding_schedule_location_unresolved"
            )
            raise BusinessLogicError("Select a location for every schedule", code=code)
        if item.location_id not in valid_location_ids:
            raise BusinessLogicError(
                "Selected location is unavailable for this club",
                code="onboarding_schedule_location_unresolved",
            )
        _parse_onboarding_time(item.start)
        _parse_onboarding_time(item.end)

    return {"trainers": trainers, "schedules": schedules}


def _validated_optional_step(*, data: dict, step: int, schema_cls):
    raw = data.get(str(step), {})
    try:
        return schema_cls.model_validate(raw)
    except ValidationError as exc:
        raise BusinessLogicError(
            "Saved onboarding data is invalid",
            code="invalid_onboarding_step",
        ) from exc


def _validate_draft_trainer_phones(*, club_id: int, parsed: TrainersStepIn) -> None:
    from apps.trainers.models import Trainer

    phones = [item.phone for item in parsed.trainers if item.phone]
    if len(phones) != len(set(phones)) or Trainer.objects.for_club(club_id).filter(phone__in=phones).exists():
        raise BusinessLogicError(
            "A trainer with this phone already exists",
            code="onboarding_trainer_phone_exists",
        )


def _apply_grades(*, club_id: int, data: dict) -> None:
    step_data = data.get(str(OnboardingDraft.Step.GRADES))
    if not step_data:
        return

    parsed = GradesStepIn.model_validate(step_data)
    if not parsed.use_templates:
        return

    for discipline in parsed.disciplines:
        if discipline not in GRADE_TEMPLATES:
            logger.warning("unknown_discipline_template", extra={"discipline": discipline, "club_id": club_id})
            continue
        grade_system = create_grade_system(club_id=club_id, discipline=discipline)
        for name, order, min_trainings in GRADE_TEMPLATES[discipline]:
            add_grade(
                club_id=club_id,
                grade_system_id=grade_system.id,
                name=name,
                order=order,
                min_trainings=min_trainings,
            )


def _apply_schedule(
    *,
    club_id: int,
    parsed: ScheduleStepIn,
    draft_trainer_ids: dict[str, int],
) -> None:
    if not parsed.schedules:
        return

    from apps.attendance.services.schedule import create_schedule

    training_type = _get_default_group_training_type(club_id=club_id)
    for item in parsed.schedules:
        trainer_id = item.trainer_id or draft_trainer_ids[str(item.trainer_ref)]
        create_schedule(
            club_id=club_id,
            day_of_week=item.day,
            start_time=_parse_onboarding_time(item.start),
            end_time=_parse_onboarding_time(item.end),
            group_name=item.group,
            trainer_id=trainer_id,
            location_id=item.location_id,
            training_type_id=training_type.id,
        )


def _apply_students(*, club_id: int, data: dict) -> None:
    step_data = data.get(str(OnboardingDraft.Step.STUDENTS))
    if not step_data:
        return

    from apps.students.services import create_student

    parsed = StudentsStepIn.model_validate(step_data)
    for item in parsed.students:
        try:
            create_student(
                club_id=club_id,
                first_name=item.first_name,
                last_name=item.last_name,
                phone=item.phone,
                source="onboarding",
            )
        except BusinessLogicError as exc:
            if exc.code == "duplicate_phone":
                continue
            raise


def _apply_tariffs(*, club_id: int, data: dict) -> None:
    step_data = data.get(str(OnboardingDraft.Step.TARIFFS))
    if not step_data:
        return

    from apps.billing.services import create_tariff

    parsed = TariffsStepIn.model_validate(step_data)
    training_type = _get_default_group_training_type(club_id=club_id)
    for item in parsed.tariffs:
        create_tariff(
            club_id=club_id,
            name=item.name,
            price=item.price,
            trainings_limit=item.training_limit,
            duration_days=item.duration_days,
            training_type_id=training_type.id,
        )


def _get_default_group_training_type(*, club_id: int):
    from apps.billing.models import TrainingType

    training_type, _ = TrainingType.objects.get_or_create(
        club_id=club_id,
        slug="group",
        defaults={
            "name": "Group",
            "is_active": True,
            "kind": TrainingType.Kind.GROUP,
        },
    )
    return training_type


def _apply_trainers(*, club_id: int, parsed: TrainersStepIn) -> dict[str, int]:
    from apps.trainers.services import create_trainer

    trainer_ids: dict[str, int] = {}
    for item in parsed.trainers:
        trainer = create_trainer(
            club_id=club_id,
            first_name=item.first_name,
            last_name=item.last_name,
            phone=item.phone,
        )
        trainer_ids[str(item.client_ref)] = trainer.id
    return trainer_ids


def _parse_onboarding_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BusinessLogicError(
            "Schedule time is invalid",
            code="invalid_onboarding_schedule_time",
        ) from exc


def _enqueue_default_pipeline_seed(*, club_id: int) -> None:
    try:
        from django_q.tasks import async_task

        async_task("apps.pipelines.tasks.seed_default_pipelines_task", club_id=club_id)
    except ImportError:
        logger.warning("django-q2 not configured; pipeline seeding skipped")


def _upgrade_legacy_draft(*, draft: OnboardingDraft) -> None:
    if draft.data.get(SCHEMA_VERSION_KEY) == DRAFT_SCHEMA_VERSION:
        if SKIPPED_STEPS_KEY not in draft.data:
            draft.data[SKIPPED_STEPS_KEY] = []
            draft.save(update_fields=["data", "updated_at"])
        return

    from apps.clubs.models import Location
    from apps.trainers.models import Trainer

    legacy = deepcopy(draft.data)
    raw_trainers = legacy.get("5", {}).get("trainers", [])
    trainers: list[dict] = []
    for raw in raw_trainers:
        trainers.append(
            {
                "client_ref": str(raw.get("client_ref") or uuid4()),
                "first_name": raw.get("first_name", ""),
                "last_name": raw.get("last_name", ""),
                "phone": raw.get("phone", ""),
            }
        )

    locations = list(Location.objects.filter(club_id=draft.club_id).order_by("id").values_list("id", flat=True))
    legacy_location_id = locations[0] if len(locations) == 1 else None
    existing_trainers = list(
        Trainer.objects.for_club(draft.club_id)
        .filter(is_active=True)
        .only("id", "first_name", "last_name")
    )

    schedules: list[dict] = []
    has_unresolved_schedule = False
    for raw in legacy.get("2", {}).get("schedules", []):
        trainer_name = str(raw.get("trainer_name", "")).strip()
        matches = _legacy_trainer_matches(
            trainer_name=trainer_name,
            existing_trainers=existing_trainers,
            draft_trainers=trainers,
        )
        upgraded = {
            "day": raw.get("day"),
            "start": raw.get("start"),
            "end": raw.get("end"),
            "group": raw.get("group", ""),
            "trainer_id": None,
            "trainer_ref": None,
            "location_id": legacy_location_id,
            "legacy_trainer_name": "",
        }
        if len(matches) == 1:
            kind, value = matches[0]
            upgraded["trainer_id" if kind == "existing" else "trainer_ref"] = value
        else:
            upgraded["legacy_trainer_name"] = trainer_name
            has_unresolved_schedule = True
        schedules.append(upgraded)

    data = _new_draft_data()
    if "1" in legacy:
        data["1"] = legacy["1"]
    if "5" in legacy:
        data["2"] = {"trainers": trainers}
    if "2" in legacy:
        data["3"] = {"schedules": schedules}
    if "3" in legacy:
        data["4"] = legacy["3"]
    if "4" in legacy:
        data["5"] = legacy["4"]

    skipped_steps: list[int] = []
    if draft.current_step > 1 and "1" not in legacy:
        skipped_steps.append(OnboardingDraft.Step.GRADES)
    if draft.current_step >= 5 and "5" not in legacy:
        skipped_steps.append(OnboardingDraft.Step.TRAINERS)
    if draft.current_step >= 2 and "2" not in legacy:
        skipped_steps.append(OnboardingDraft.Step.SCHEDULE)
    if draft.current_step >= 3 and "3" not in legacy:
        skipped_steps.append(OnboardingDraft.Step.STUDENTS)
    if draft.current_step >= 4 and "4" not in legacy:
        skipped_steps.append(OnboardingDraft.Step.TARIFFS)
    data[SKIPPED_STEPS_KEY] = sorted(skipped_steps)

    legacy_step_map = {1: 1, 2: 3, 3: 4, 4: 5, 5: 5}
    draft.current_step = (
        OnboardingDraft.Step.SCHEDULE
        if has_unresolved_schedule
        else legacy_step_map.get(draft.current_step, OnboardingDraft.Step.GRADES)
    )
    draft.data = data
    draft.save(update_fields=["data", "current_step", "updated_at"])


def _legacy_trainer_matches(
    *,
    trainer_name: str,
    existing_trainers: list,
    draft_trainers: list[dict],
) -> list[tuple[str, int | str]]:
    normalized = _normalize_person_name(trainer_name)
    if not normalized:
        return []

    matches: list[tuple[str, int | str]] = []
    for trainer in existing_trainers:
        aliases = {
            _normalize_person_name(trainer.first_name),
            _normalize_person_name(f"{trainer.first_name} {trainer.last_name}"),
        }
        if normalized in aliases:
            matches.append(("existing", trainer.id))
    for trainer in draft_trainers:
        aliases = {
            _normalize_person_name(trainer["first_name"]),
            _normalize_person_name(f"{trainer['first_name']} {trainer['last_name']}"),
        }
        if normalized in aliases:
            matches.append(("draft", trainer["client_ref"]))
    return matches


def _normalize_person_name(value: str) -> str:
    return " ".join(value.casefold().split())
