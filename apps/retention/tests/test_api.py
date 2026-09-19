from __future__ import annotations

import pytest
from ninja.testing import TestClient

from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestListRetentionTasks:
    def test_list_tasks_for_trainer(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club)
        RetentionTaskFactory(club=club, student=student, trainer=trainer)

        response = client.get(
            f"/retention/tasks/?trainer_id={trainer.id}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["student_id"] == student.id
        assert data["items"][0]["level"] == "yellow"

    def test_tasks_tenant_isolation(self, club, other_club, owner_user):
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=other_club)
        RetentionTaskFactory(club=club, student=StudentFactory(club=club), trainer=trainer)
        RetentionTaskFactory(club=other_club, student=StudentFactory(club=other_club), trainer=other_trainer)

        response = client.get(
            f"/retention/tasks/?trainer_id={trainer.id}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_tasks_requires_auth(self):
        response = client.get("/retention/tasks/")
        assert response.status_code in (401, 403)

    def test_pipeline_task_provenance_is_derived_from_notes(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        pipeline_task = RetentionTaskFactory(
            club=club,
            student=StudentFactory(club=club),
            trainer=trainer,
            notes="Pipeline step: Позвонить, спросить впечатления",
        )
        normal_task = RetentionTaskFactory(
            club=club,
            student=StudentFactory(club=club),
            trainer=trainer,
            notes="Manual retention note",
        )

        response = client.get(
            f"/retention/tasks/?trainer_id={trainer.id}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        by_id = {item["id"]: item for item in response.json()["items"]}
        assert by_id[pipeline_task.id]["automation_source"] == "pipeline"
        assert (
            by_id[pipeline_task.id]["automation_step_message"]
            == "Позвонить, спросить впечатления"
        )
        assert by_id[pipeline_task.id]["notes"] == "Pipeline step: Позвонить, спросить впечатления"
        assert by_id[normal_task.id]["automation_source"] is None
        assert by_id[normal_task.id]["automation_step_message"] is None


@pytest.mark.django_db
class TestCloseRetentionTask:
    def test_close_task(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club)
        task = RetentionTaskFactory(club=club, student=student, trainer=trainer)

        response = client.post(
            f"/retention/tasks/{task.id}/close/",
            json={"resolution": "manual_contacted", "notes": "Called student"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["resolution"] == "manual_contacted"
        assert data["notes"] == "Called student"
        assert data["resolved_at"] is not None

        task.refresh_from_db()
        assert task.resolved_at is not None


@pytest.mark.django_db
class TestRetentionTaskTrainerScope:
    """Review concern #1: verify retention tasks are tenant-isolated even with trainer_id param."""

    def test_retention_tasks_tenant_isolated(self, club, other_club, trainer_user):
        """Trainer in club A cannot see tasks from club B even by passing trainer_id."""
        TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=other_club)
        RetentionTaskFactory(club=other_club, trainer=other_trainer)
        response = client.get(
            f"/retention/tasks/?trainer_id={other_trainer.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 200
        data = response.json()
        items = data if isinstance(data, list) else data.get("items", [])
        assert len(items) == 0
