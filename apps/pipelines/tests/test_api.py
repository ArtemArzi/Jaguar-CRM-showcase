import pytest
from ninja.testing import TestClient

from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.pipelines.models import Pipeline
from apps.pipelines.services import seed_default_pipelines, trigger_pipeline
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestListPipelines:
    def test_list_pipelines_endpoint(self, club, owner_user):
        seed_default_pipelines(club_id=club.id)
        response = client.get("/pipelines/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        # Each pipeline has steps
        for pipeline in data:
            assert len(pipeline["steps"]) > 0

    def test_tenant_isolation(self, club, other_club, owner_user):
        seed_default_pipelines(club_id=club.id)
        seed_default_pipelines(club_id=other_club.id)
        response = client.get("/pipelines/", **_auth_params(owner_user, club))
        data = response.json()
        assert len(data) == 2  # only club's pipelines


@pytest.mark.django_db
class TestUpdateStep:
    def test_update_step_delay(self, club, owner_user):
        seed_default_pipelines(club_id=club.id)
        pipeline = Pipeline.objects.for_club(club).get(
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP
        )
        step = pipeline.steps.first()
        response = client.put(
            f"/pipelines/{pipeline.id}/steps/{step.id}",
            json={"delay_hours": 4},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["delay_hours"] == 4
        step.refresh_from_db()
        assert step.delay_hours == 4

    def test_trainer_cannot_update(self, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        pipeline = Pipeline.objects.for_club(club).first()
        step = pipeline.steps.first()
        response = client.put(
            f"/pipelines/{pipeline.id}/steps/{step.id}",
            json={"delay_hours": 10},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestListExecutions:
    def test_list_executions(self, club, owner_user):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        response = client.get("/pipelines/executions", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["student_id"] == student.id
        assert data["items"][0]["pipeline_type"] == "follow_up"
        assert data["items"][0]["student_name"] is not None

    def test_admin_list_executions_can_see_all_students(self, club, admin_user):
        seed_default_pipelines(club_id=club.id)
        student_a = StudentFactory(club=club)
        student_b = StudentFactory(club=club)
        trigger_pipeline(
            club_id=club.id, student_id=student_a.id, pipeline_type="follow_up"
        )
        trigger_pipeline(
            club_id=club.id, student_id=student_b.id, pipeline_type="follow_up"
        )

        response = client.get(
            "/pipelines/executions",
            **_auth_params(admin_user, club, role="admin"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        student_ids = {item["student_id"] for item in data["items"]}
        assert student_ids == {student_a.id, student_b.id}

    def test_trainer_list_executions_without_student_id_sees_only_assigned_students(
        self, club, trainer_user
    ):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        assigned_student = StudentFactory(club=club, assigned_trainer=trainer)
        other_student = StudentFactory(club=club, assigned_trainer=other_trainer)
        trigger_pipeline(
            club_id=club.id, student_id=assigned_student.id, pipeline_type="follow_up"
        )
        trigger_pipeline(
            club_id=club.id, student_id=other_student.id, pipeline_type="follow_up"
        )

        response = client.get(
            "/pipelines/executions",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert [item["student_id"] for item in data["items"]] == [assigned_student.id]

    def test_trainer_list_executions_with_unassigned_student_id_returns_empty(
        self, club, trainer_user
    ):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        assigned_student = StudentFactory(club=club, assigned_trainer=trainer)
        other_student = StudentFactory(club=club, assigned_trainer=other_trainer)
        trigger_pipeline(
            club_id=club.id, student_id=assigned_student.id, pipeline_type="follow_up"
        )
        trigger_pipeline(
            club_id=club.id, student_id=other_student.id, pipeline_type="follow_up"
        )

        response = client.get(
            f"/pipelines/executions?student_id={other_student.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["items"] == []

    def test_inactive_trainer_cannot_list_assigned_executions(self, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user, is_active=False)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )

        response = client.get(
            "/pipelines/executions",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestCancelExecution:
    def test_cancel_execution(self, club, owner_user):
        seed_default_pipelines(club_id=club.id)
        student = StudentFactory(club=club)
        execution = trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        response = client.post(
            f"/pipelines/executions/{execution.id}/cancel",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["cancelled"] == 1


@pytest.mark.django_db
class TestMyTasks:
    def test_trainer_my_tasks(self, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )
        response = client.get(
            "/pipelines/my-tasks",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["student_id"] == student.id

    def test_inactive_trainer_cannot_read_my_tasks(self, club, trainer_user):
        seed_default_pipelines(club_id=club.id)
        trainer = TrainerFactory(club=club, user=trainer_user, is_active=False)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        trigger_pipeline(
            club_id=club.id, student_id=student.id, pipeline_type="follow_up"
        )

        response = client.get(
            "/pipelines/my-tasks",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
