import pytest
from ninja.testing import TestClient

from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.onboarding.services import skip_step, start_onboarding
from apps.onboarding.tests.factories import OnboardingDraftFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.fixture(autouse=True)
def _disable_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


@pytest.mark.django_db
class TestStartEndpoint:
    def test_start_endpoint_returns_200(self, club, owner_user):
        response = client.post("/onboarding/start", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 200
        data = response.json()
        assert data["current_step"] == 1
        assert data["is_completed"] is False


@pytest.mark.django_db
class TestSaveStepEndpoint:
    def test_save_step_endpoint_returns_200(self, club, owner_user):
        draft = OnboardingDraftFactory(club=club)
        response = client.post(
            "/onboarding/step",
            json={
                "draft_id": draft.id,
                "step": 1,
                "data": {"disciplines": ["Boxing"], "use_templates": True},
            },
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 200
        data = response.json()
        assert "1" in data["data"]


@pytest.mark.django_db
class TestGetDraftEndpoint:
    def test_get_draft_endpoint(self, club, owner_user):
        OnboardingDraftFactory(club=club)
        response = client.get("/onboarding/draft", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 200

    def test_get_draft_404_when_none(self, club, owner_user):
        response = client.get("/onboarding/draft", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 404


@pytest.mark.django_db
class TestFinishEndpoint:
    def test_finish_endpoint(self, club, owner_user):
        draft = start_onboarding(club_id=club.id)
        skip_step(club_id=club.id, draft_id=draft.id, step=3)
        response = client.post(
            "/onboarding/finish",
            json={"draft_id": draft.id},
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["draft"]["id"] == draft.id
        assert response.json()["draft"]["is_completed"] is True

    def test_finish_returns_stable_business_error_for_unresolved_schedule(self, club, owner_user):
        draft = start_onboarding(club_id=club.id)
        draft.data["3"] = {
            "schedules": [
                {
                    "day": 0,
                    "start": "10:00",
                    "end": "11:00",
                    "group": "Adults",
                    "trainer_id": None,
                    "trainer_ref": None,
                    "location_id": None,
                    "legacy_trainer_name": "Ivan",
                }
            ]
        }
        draft.save(update_fields=["data"])

        response = client.post(
            "/onboarding/finish",
            json={"draft_id": draft.id},
            **_auth_params(owner_user, club, role="owner"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "onboarding_schedule_trainer_unresolved"

    def test_finish_retry_returns_same_completed_draft(self, club, owner_user):
        draft = start_onboarding(club_id=club.id)
        skip_step(club_id=club.id, draft_id=draft.id, step=3)
        auth = _auth_params(owner_user, club, role="owner")

        first = client.post("/onboarding/finish", json={"draft_id": draft.id}, **auth)
        second = client.post("/onboarding/finish", json={"draft_id": draft.id}, **auth)

        assert first.status_code == second.status_code == 200
        assert first.json()["draft"]["id"] == second.json()["draft"]["id"] == draft.id


@pytest.mark.django_db
class TestUnauthorizedRole:
    def test_unauthorized_role_rejected(self, club, trainer_user):
        response = client.post("/onboarding/start", **_auth_params(trainer_user, club, role="trainer"))
        assert response.status_code == 403

    def test_student_role_rejected(self, club, student_user):
        response = client.post("/onboarding/start", **_auth_params(student_user, club, role="student"))
        assert response.status_code == 403
