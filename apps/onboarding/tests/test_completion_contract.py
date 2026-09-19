from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection

from apps.attendance.models import Schedule
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.onboarding.models import OnboardingDraft
from apps.onboarding.services import finish_onboarding, save_step, start_onboarding
from apps.trainers.models import Trainer
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


def _save_trainers(*, draft: OnboardingDraft, trainers: list[dict]) -> OnboardingDraft:
    return save_step(
        club_id=draft.club_id,
        draft_id=draft.id,
        step=2,
        data={"trainers": trainers},
    )


def _save_schedule(*, draft: OnboardingDraft, schedules: list[dict]) -> OnboardingDraft:
    return save_step(
        club_id=draft.club_id,
        draft_id=draft.id,
        step=3,
        data={"schedules": schedules},
    )


def _schedule_payload(
    *,
    trainer_id: int | None = None,
    trainer_ref: str | None = None,
    location_id: int | None = None,
) -> dict:
    return {
        "day": 0,
        "start": "10:00",
        "end": "11:00",
        "group": "Adults",
        "trainer_id": trainer_id,
        "trainer_ref": trainer_ref,
        "location_id": location_id,
    }


def test_finish_uses_exact_existing_trainer_id_when_names_are_duplicated(club):
    location = LocationFactory(club=club)
    TrainerFactory(club=club, first_name="Ivan", last_name="One")
    selected = TrainerFactory(club=club, first_name="Ivan", last_name="Two")
    draft = start_onboarding(club_id=club.id)
    _save_schedule(
        draft=draft,
        schedules=[_schedule_payload(trainer_id=selected.id, location_id=location.id)],
    )

    completed = finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert completed.id == draft.id
    assert completed.is_completed is True
    assert Schedule.objects.for_club(club).get().trainer_id == selected.id


def test_finish_maps_schedule_to_exact_draft_trainer_reference(club):
    location = LocationFactory(club=club)
    existing = TrainerFactory(club=club, first_name="Existing")
    draft_ref = str(uuid4())
    draft = start_onboarding(club_id=club.id)
    _save_trainers(
        draft=draft,
        trainers=[
            {
                "client_ref": draft_ref,
                "first_name": "Draft",
                "last_name": "Trainer",
                "phone": "+79001112233",
            }
        ],
    )
    _save_schedule(
        draft=draft,
        schedules=[_schedule_payload(trainer_ref=draft_ref, location_id=location.id)],
    )

    finish_onboarding(club_id=club.id, draft_id=draft.id)

    created = Trainer.objects.for_club(club).get(first_name="Draft")
    schedule = Schedule.objects.for_club(club).get()
    assert schedule.trainer_id == created.id
    assert schedule.trainer_id != existing.id


@pytest.mark.parametrize(
    "reference_kind",
    ["unknown_draft", "deleted_existing", "inactive_existing", "cross_club"],
)
def test_finish_rejects_unresolvable_trainer_without_creating_any_rows(
    club,
    other_club,
    reference_kind,
):
    location = LocationFactory(club=club)
    draft = start_onboarding(club_id=club.id)

    if reference_kind == "unknown_draft":
        schedule = _schedule_payload(trainer_ref=str(uuid4()), location_id=location.id)
    elif reference_kind == "deleted_existing":
        deleted = TrainerFactory(club=club)
        deleted_id = deleted.id
        deleted.delete()
        schedule = _schedule_payload(trainer_id=deleted_id, location_id=location.id)
    elif reference_kind == "inactive_existing":
        inactive = TrainerFactory(club=club, is_active=False)
        schedule = _schedule_payload(trainer_id=inactive.id, location_id=location.id)
    else:
        schedule = _schedule_payload(
            trainer_id=TrainerFactory(club=other_club).id,
            location_id=location.id,
        )

    _save_schedule(draft=draft, schedules=[schedule])

    with pytest.raises(BusinessLogicError) as exc_info:
        finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert exc_info.value.code == "onboarding_schedule_trainer_unresolved"
    draft.refresh_from_db()
    assert draft.is_completed is False
    assert Schedule.objects.for_club(club).count() == 0


def test_finish_rejects_submitted_schedule_when_club_has_no_location(club):
    trainer = TrainerFactory(club=club)
    draft = start_onboarding(club_id=club.id)
    _save_schedule(draft=draft, schedules=[_schedule_payload(trainer_id=trainer.id)])

    with pytest.raises(BusinessLogicError) as exc_info:
        finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert exc_info.value.code == "onboarding_schedule_location_required"
    draft.refresh_from_db()
    assert draft.is_completed is False
    assert Schedule.objects.for_club(club).count() == 0


def test_finish_is_atomic_when_one_of_multiple_schedule_references_is_invalid(club):
    location = LocationFactory(club=club)
    valid = TrainerFactory(club=club)
    new_ref = str(uuid4())
    draft = start_onboarding(club_id=club.id)
    _save_trainers(
        draft=draft,
        trainers=[{"client_ref": new_ref, "first_name": "New", "last_name": "Coach", "phone": ""}],
    )
    _save_schedule(
        draft=draft,
        schedules=[
            _schedule_payload(trainer_id=valid.id, location_id=location.id),
            {
                **_schedule_payload(trainer_ref=str(uuid4()), location_id=location.id),
                "day": 2,
                "group": "Juniors",
            },
        ],
    )

    with pytest.raises(BusinessLogicError):
        finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert Trainer.objects.for_club(club).filter(first_name="New").count() == 0
    assert Schedule.objects.for_club(club).count() == 0


def test_successful_finish_retry_returns_same_completed_draft_without_duplicate_side_effects(
    club,
    django_capture_on_commit_callbacks,
    monkeypatch,
):
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    draft = start_onboarding(club_id=club.id)
    _save_schedule(
        draft=draft,
        schedules=[_schedule_payload(trainer_id=trainer.id, location_id=location.id)],
    )
    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "django_q.tasks.async_task",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )

    with django_capture_on_commit_callbacks(execute=True):
        first = finish_onboarding(club_id=club.id, draft_id=draft.id)
    with django_capture_on_commit_callbacks(execute=True):
        second = finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert first.id == second.id == draft.id
    assert first.is_completed is second.is_completed is True
    assert Schedule.objects.for_club(club).count() == 1
    assert len(queued) == 1


def test_legacy_unambiguous_name_is_upgraded_to_stable_existing_id(club):
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, first_name="Ivan", last_name="Petrov")
    legacy = OnboardingDraft.objects.create(
        club=club,
        current_step=2,
        data={
            "2": {
                "schedules": [
                    {
                        "day": 0,
                        "start": "10:00",
                        "end": "11:00",
                        "group": "Adults",
                        "trainer_name": "Ivan Petrov",
                    }
                ]
            }
        },
    )

    upgraded = start_onboarding(club_id=club.id)
    completed = finish_onboarding(club_id=club.id, draft_id=upgraded.id)

    assert upgraded.id == legacy.id
    assert upgraded.data["_schema_version"] == 2
    assert upgraded.data["3"]["schedules"][0]["trainer_id"] == trainer.id
    assert upgraded.data["3"]["schedules"][0]["location_id"] == location.id
    assert completed.is_completed is True
    assert Schedule.objects.for_club(club).get().trainer_id == trainer.id


def test_legacy_ambiguous_name_stays_recoverable_and_never_auto_assigns(club):
    LocationFactory(club=club)
    TrainerFactory(club=club, first_name="Ivan", last_name="One")
    TrainerFactory(club=club, first_name="Ivan", last_name="Two")
    legacy = OnboardingDraft.objects.create(
        club=club,
        current_step=5,
        data={
            "2": {
                "schedules": [
                    {
                        "day": 0,
                        "start": "10:00",
                        "end": "11:00",
                        "group": "Adults",
                        "trainer_name": "Ivan",
                    }
                ]
            }
        },
    )

    upgraded = start_onboarding(club_id=club.id)
    with pytest.raises(BusinessLogicError) as exc_info:
        finish_onboarding(club_id=club.id, draft_id=upgraded.id)

    assert upgraded.id == legacy.id
    assert upgraded.current_step == 3
    assert upgraded.data["3"]["schedules"][0]["legacy_trainer_name"] == "Ivan"
    assert exc_info.value.code == "onboarding_schedule_trainer_unresolved"
    assert Schedule.objects.for_club(club).count() == 0


def test_direct_finish_persists_recoverable_legacy_upgrade_before_returning_error(club):
    LocationFactory(club=club)
    TrainerFactory(club=club, first_name="Ivan", last_name="One")
    TrainerFactory(club=club, first_name="Ivan", last_name="Two")
    legacy = OnboardingDraft.objects.create(
        club=club,
        current_step=5,
        data={
            "2": {
                "schedules": [
                    {
                        "day": 0,
                        "start": "10:00",
                        "end": "11:00",
                        "group": "Adults",
                        "trainer_name": "Ivan",
                    }
                ]
            }
        },
    )

    with pytest.raises(BusinessLogicError):
        finish_onboarding(club_id=club.id, draft_id=legacy.id)

    legacy.refresh_from_db()
    assert legacy.data["_schema_version"] == 2
    assert legacy.current_step == 3
    assert legacy.data["3"]["schedules"][0]["legacy_trainer_name"] == "Ivan"


def test_explicitly_skipped_schedule_is_valid_and_distinct_from_unresolved_submission(club):
    from apps.onboarding.services import skip_step

    draft = start_onboarding(club_id=club.id)
    skipped = skip_step(club_id=club.id, draft_id=draft.id, step=3)

    completed = finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert 3 in skipped.data["_skipped_steps"]
    assert completed.is_completed is True
    assert Schedule.objects.for_club(club).count() == 0


def test_finish_uses_exact_location_when_club_has_multiple_locations(club):
    LocationFactory(club=club, name="First")
    selected_location = LocationFactory(club=club, name="Selected")
    trainer = TrainerFactory(club=club)
    draft = start_onboarding(club_id=club.id)
    _save_schedule(
        draft=draft,
        schedules=[
            _schedule_payload(
                trainer_id=trainer.id,
                location_id=selected_location.id,
            )
        ],
    )

    finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert Schedule.objects.for_club(club).get().location_id == selected_location.id


def test_finish_creates_every_submitted_schedule_once(club):
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    draft = start_onboarding(club_id=club.id)
    _save_schedule(
        draft=draft,
        schedules=[
            _schedule_payload(trainer_id=trainer.id, location_id=location.id),
            {
                **_schedule_payload(trainer_id=trainer.id, location_id=location.id),
                "day": 2,
                "start": "18:00",
                "end": "19:00",
                "group": "Juniors",
            },
        ],
    )

    finish_onboarding(club_id=club.id, draft_id=draft.id)
    finish_onboarding(club_id=club.id, draft_id=draft.id)

    assert list(
        Schedule.objects.for_club(club).order_by("day_of_week").values_list("group_name", flat=True)
    ) == ["Adults", "Juniors"]


@pytest.mark.django_db(transaction=True)
def test_concurrent_finish_creates_one_result_set_and_one_seed(club, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("row-lock concurrency contract requires PostgreSQL")

    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    draft = start_onboarding(club_id=club.id)
    _save_schedule(
        draft=draft,
        schedules=[_schedule_payload(trainer_id=trainer.id, location_id=location.id)],
    )
    queued: list[int] = []
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: queued.append(kwargs["club_id"]))
    barrier = Barrier(2)

    def finish_from_separate_connection() -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return finish_onboarding(club_id=club.id, draft_id=draft.id).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finish_from_separate_connection(), range(2)))

    assert results == [draft.id, draft.id]
    assert Schedule.objects.for_club(club).count() == 1
    assert queued == [club.id]
