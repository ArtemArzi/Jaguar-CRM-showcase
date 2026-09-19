from unittest.mock import patch

import pytest

from apps.attendance.models import Schedule, TrainingGroup, TrainingGroupRolloutState
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import TrainingType
from apps.clubs.tests.factories import LocationFactory
from apps.grades.models import Grade, GradeSystem
from apps.onboarding.models import OnboardingDraft
from apps.onboarding.services import finish_onboarding, save_step, skip_step, start_onboarding
from apps.onboarding.tests.factories import OnboardingDraftFactory
from apps.students.models import Student
from apps.trainers.tests.factories import TrainerFactory


@pytest.fixture(autouse=True)
def _disable_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


@pytest.mark.django_db
class TestStartOnboarding:
    def test_start_onboarding_creates_draft(self, club):
        draft = start_onboarding(club_id=club.id)
        assert draft.current_step == 1
        assert draft.data == {"_schema_version": 2, "_skipped_steps": []}
        assert draft.is_completed is False
        assert draft.club_id == club.id

    def test_start_onboarding_returns_existing(self, club):
        first = start_onboarding(club_id=club.id)
        second = start_onboarding(club_id=club.id)
        assert first.id == second.id


@pytest.mark.django_db
class TestSaveStep:
    def test_save_step_grades(self, club):
        started = start_onboarding(club_id=club.id)
        draft = save_step(
            club_id=club.id,
            draft_id=started.id,
            step=1,
            data={"disciplines": ["BJJ"], "use_templates": True},
        )
        assert "1" in draft.data
        assert draft.data["1"]["disciplines"] == ["BJJ"]

    def test_save_step_skip(self, club):
        """Steps are skippable -- save step 3 without saving step 2."""
        started = start_onboarding(club_id=club.id)
        draft = save_step(
            club_id=club.id,
            draft_id=started.id,
            step=4,
            data={"students": [{"first_name": "Ivan", "phone": "+79001112233"}]},
        )
        assert "4" in draft.data
        assert draft.current_step == 5


@pytest.mark.django_db
class TestFinishOnboarding:
    def test_finish_onboarding_creates_grades(self, club):
        draft = start_onboarding(club_id=club.id)
        save_step(
            club_id=club.id,
            draft_id=draft.id,
            step=1,
            data={"disciplines": ["BJJ"], "use_templates": True},
        )
        skip_step(club_id=club.id, draft_id=draft.id, step=3)
        finish_onboarding(club_id=club.id, draft_id=draft.id)

        assert GradeSystem.objects.for_club(club.id).filter(discipline="BJJ").exists()
        assert Grade.objects.for_club(club.id).count() == 5  # BJJ has 5 grades

    def test_finish_onboarding_creates_students(self, club):
        draft = start_onboarding(club_id=club.id)
        save_step(
            club_id=club.id,
            draft_id=draft.id,
            step=4,
            data={"students": [
                {"first_name": "Ivan", "last_name": "Petrov", "phone": "+79001112233"},
                {"first_name": "Anna", "phone": "+79001112234"},
            ]},
        )
        skip_step(club_id=club.id, draft_id=draft.id, step=3)
        finish_onboarding(club_id=club.id, draft_id=draft.id)

        students = Student.objects.for_club(club.id)
        assert students.count() == 2
        assert students.filter(status="lead").count() == 2

    @patch("django_q.tasks.async_task")
    def test_finish_onboarding_assigns_canonical_training_group_to_schedules(self, mock_async, club, settings):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club, first_name="Ivan")
        rollout_state = TrainingGroupRolloutState.objects.for_club(club).get()
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.filter(id=rollout_state.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )
        draft = start_onboarding(club_id=club.id)
        save_step(
            club_id=club.id,
            draft_id=draft.id,
            step=3,
            data={
                "schedules": [
                    {
                        "day": 0,
                        "start": "10:00",
                        "end": "11:00",
                        "group": "Adults",
                        "trainer_id": trainer.id,
                        "location_id": location.id,
                    },
                ]
            },
        )

        finish_onboarding(club_id=club.id, draft_id=draft.id)

        schedule = Schedule.objects.for_club(club).get(group_name="Adults")
        assert schedule.training_type_id is not None
        assert TrainingType.objects.for_club(club).filter(id=schedule.training_type_id).exists()
        assert schedule.training_group_id is not None
        assert TrainingGroup.objects.for_club(club).get(id=schedule.training_group_id).name == "Adults"

    def test_finish_onboarding_atomic(self, club):
        """If a step fails, all objects are rolled back."""
        draft = start_onboarding(club_id=club.id)
        save_step(
            club_id=club.id,
            draft_id=draft.id,
            step=1,
            data={"disciplines": ["BJJ"], "use_templates": True},
        )
        # Directly inject invalid tariff data to bypass save_step validation
        draft.refresh_from_db()
        draft.data["5"] = {"tariffs": [{"name": "Bad", "price": -1, "duration_days": 30}]}
        draft.data["_skipped_steps"] = [3]
        draft.save(update_fields=["data"])

        with pytest.raises(Exception):
            finish_onboarding(club_id=club.id, draft_id=draft.id)

        # Grades should NOT have been created (atomic rollback)
        assert GradeSystem.objects.for_club(club.id).count() == 0

    def test_finish_marks_completed(self, club):
        draft = start_onboarding(club_id=club.id)
        skip_step(club_id=club.id, draft_id=draft.id, step=3)
        finish_onboarding(club_id=club.id, draft_id=draft.id)

        draft = OnboardingDraft.objects.for_club(club.id).get()
        assert draft.is_completed is True


@pytest.mark.django_db
class TestTenantIsolation:
    def test_tenant_isolation(self, club, other_club):
        OnboardingDraftFactory(club=club)
        OnboardingDraftFactory(club=other_club)

        drafts_a = OnboardingDraft.objects.for_club(club.id)
        drafts_b = OnboardingDraft.objects.for_club(other_club.id)

        assert drafts_a.count() == 1
        assert drafts_b.count() == 1
        assert drafts_a.first().club_id == club.id
        assert drafts_b.first().club_id == other_club.id
