from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Subscription, TrainingType
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.feedback.models import FeedbackResponse
from apps.feedback.tests.factories import (
    FeedbackAnswerFactory,
    FeedbackFormFactory,
    FeedbackQuestionFactory,
    FeedbackResponseFactory,
)
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


NON_CURRENT_PACKAGE_CASES = [
    ("expired", Subscription.Status.EXPIRED, 30, 8, False),
    ("pending", Subscription.Status.PENDING, 30, 8, False),
    ("frozen", Subscription.Status.FROZEN, 30, 8, False),
    ("past_expiry", Subscription.Status.ACTIVE, -1, 8, False),
    ("depleted", Subscription.Status.ACTIVE, 30, 0, False),
    ("soft_deleted", Subscription.Status.ACTIVE, 30, 8, True),
]


def _create_active_package_allocation(
    *,
    club,
    student,
    owner_trainer,
    status=Subscription.Status.ACTIVE,
    expires_at=None,
    trainings_left=None,
    deleted=False,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    subscription = SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=status,
        expires_at=expires_at,
        trainings_left=tariff.trainings_limit if trainings_left is None else trainings_left,
        paid_amount=Decimal("5000"),
    )
    if deleted:
        subscription.soft_delete()
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
        is_active=True,
    )
    return subscription


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestCreateForm:
    def test_owner_creates_form(self, club, owner_user):
        response = client.post(
            "/feedback/forms/",
            json={
                "name": "Post-trial survey",
                "questions": [
                    {"question_type": "rating", "text": "Rate training", "is_required": True},
                    {"question_type": "text", "text": "Comments", "is_required": False},
                ],
            },
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Post-trial survey"
        assert len(data["questions"]) == 2
        assert data["is_active"] is True

    def test_trainer_cannot_create_form(self, club, trainer_user):
        response = client.post(
            "/feedback/forms/",
            json={
                "name": "Form",
                "questions": [{"question_type": "rating", "text": "Rate", "is_required": True}],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestActiveForm:
    def test_get_active_form(self, club, owner_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        FeedbackQuestionFactory(club=club, form=form, order=1)

        response = client.get("/feedback/forms/active/", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == form.id

    def test_get_active_form_returns_trial_not_churned(self, club, owner_user):
        FeedbackFormFactory(club=club, name="Churned survey", trigger_type="churned", is_active=True)
        trial_form = FeedbackFormFactory(club=club, name="Trial survey", trigger_type="trial", is_active=True)
        FeedbackQuestionFactory(club=club, form=trial_form, order=1)

        response = client.get("/feedback/forms/active/", **_auth_params(owner_user, club, role="owner"))

        assert response.status_code == 200
        assert response.json()["id"] == trial_form.id

    def test_get_active_form_none(self, club, owner_user):
        response = client.get("/feedback/forms/active/", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 200
        assert response.json() is None


@pytest.mark.django_db
class TestSubmitResponse:
    def test_submit_feedback(self, club, owner_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        q1 = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1, is_required=True)
        q2 = FeedbackQuestionFactory(club=club, form=form, question_type="text", order=2, is_required=False)
        student = StudentFactory(club=club)

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": student.id,
                "answers": [
                    {"question_id": q1.id, "rating_value": 5},
                    {"question_id": q2.id, "text_value": "Loved it"},
                ],
            },
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert len(data["answers"]) == 2

    def test_student_role_cannot_use_legacy_student_id_submit(self, club, student_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club, user=student_user)
        other_student = StudentFactory(club=club)

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": other_student.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(student.user, club, role="student"),
        )

        assert response.status_code == 403

    def test_parent_role_cannot_use_legacy_student_id_submit(self, club, parent_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": child.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 403

    def test_trainer_cannot_submit_feedback_for_unscoped_student(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        unassigned = StudentFactory(club=club)

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": unassigned.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not FeedbackResponse.objects.for_club(club).filter(student=unassigned).exists()

    def test_trainer_package_owner_can_submit_feedback_for_student(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": student.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert FeedbackResponse.objects.for_club(club).filter(student=student, form=form).count() == 1

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_package_owner_without_current_subscription_cannot_submit_feedback_for_student(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )

        response = client.post(
            "/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": student.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not FeedbackResponse.objects.for_club(club).filter(student=student).exists()


@pytest.mark.django_db
class TestStudentSafeFeedback:
    def test_student_gets_active_feedback_form(self, club, student_user):
        FeedbackFormFactory(club=club, name="Churned survey", trigger_type="churned", is_active=True)
        form = FeedbackFormFactory(club=club, name="Trial survey", trigger_type="trial", is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        StudentFactory(club=club, user=student_user)

        response = client.get(
            "/students/me/feedback/form/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == form.id
        assert data["questions"][0]["id"] == question.id

    def test_student_submit_derives_student_from_authenticated_user(self, club, student_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1, is_required=True)
        student = StudentFactory(club=club, user=student_user)
        other_student = StudentFactory(club=club)

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": other_student.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert FeedbackResponse.objects.for_club(club).filter(student=student, form=form).count() == 1
        assert not FeedbackResponse.objects.for_club(club).filter(student=other_student, form=form).exists()

    def test_student_duplicate_submit_is_idempotent(self, club, student_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club, user=student_user)

        payload = {
            "form_id": form.id,
            "answers": [{"question_id": question.id, "rating_value": 5}],
        }
        first = client.post(
            "/students/me/feedback/submit/",
            json=payload,
            **_auth_params(student_user, club, role="student"),
        )
        second = client.post(
            "/students/me/feedback/submit/",
            json=payload,
            **_auth_params(student_user, club, role="student"),
        )

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert FeedbackResponse.objects.for_club(club).filter(student=student, form=form).count() == 1

    def test_student_submit_rejects_question_from_another_form(self, club, student_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, question_type="rating", order=1)
        StudentFactory(club=club, user=student_user)

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": foreign_question.id, "rating_value": 5}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_question"


@pytest.mark.django_db
class TestStaffStudentFeedbackResponses:
    def test_trainer_cannot_read_unscoped_student_feedback_responses(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        response = FeedbackResponseFactory(club=club, student=unassigned)
        FeedbackAnswerFactory(club=club, response=response)

        api_response = client.get(
            f"/feedback/students/{unassigned.id}/responses/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert api_response.status_code == 403

    def test_trainer_with_actual_checkin_can_read_student_feedback_responses(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=schedule.training_type,
        )
        response = FeedbackResponseFactory(club=club, student=student)
        FeedbackAnswerFactory(club=club, response=response)

        api_response = client.get(
            f"/feedback/students/{student.id}/responses/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert api_response.status_code == 200
        assert len(api_response.json()) == 1

    @patch("django_q.tasks.schedule")
    def test_trainer_cannot_send_survey_to_unscoped_student(self, mock_schedule, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        FeedbackFormFactory(club=club, is_active=True, trigger_type="trial")
        unassigned = StudentFactory(club=club)

        response = client.post(
            f"/feedback/send-survey/{unassigned.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    def test_trainer_cannot_send_survey_to_checkin_only_student(self, mock_schedule, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        FeedbackFormFactory(club=club, is_active=True, trigger_type="trial")
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=schedule.training_type,
        )

        response = client.post(
            f"/feedback/send-survey/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        mock_schedule.assert_not_called()


@pytest.mark.django_db
class TestParentSafeFeedback:
    def test_parent_gets_own_child_feedback_form(self, club, parent_user):
        FeedbackFormFactory(club=club, name="Churned survey", trigger_type="churned", is_active=True)
        form = FeedbackFormFactory(club=club, name="Trial survey", trigger_type="trial", is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="yes_no", order=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{child.id}/feedback/form/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == form.id
        assert data["questions"][0]["id"] == question.id

    def test_parent_submit_is_scoped_to_owned_child(self, club, parent_user):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="text", order=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_child = StudentFactory(club=club, is_child=True, parent_user=UserFactory())

        response = client.post(
            f"/parents/children/{child.id}/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": other_child.id,
                "answers": [{"question_id": question.id, "text_value": "Спасибо"}],
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == child.id
        assert FeedbackResponse.objects.for_club(club).filter(student=child, form=form).count() == 1
        assert not FeedbackResponse.objects.for_club(club).filter(student=other_child, form=form).exists()

    @pytest.mark.parametrize("case", ["other_parent", "non_child", "other_club", "deleted"])
    def test_parent_child_feedback_form_rejects_unowned_or_invalid_child(
        self,
        case,
        club,
        other_club,
        parent_user,
    ):
        FeedbackFormFactory(club=club, is_active=True)
        if case == "other_parent":
            child = StudentFactory(club=club, is_child=True, parent_user=UserFactory())
        elif case == "non_child":
            child = StudentFactory(club=club, is_child=False, parent_user=parent_user)
        elif case == "other_club":
            child = StudentFactory(club=other_club, is_child=True, parent_user=parent_user)
        else:
            child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
            child.soft_delete()

        response = client.get(
            f"/parents/children/{child.id}/feedback/form/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestSendSurveyManually:
    @patch("django_q.tasks.schedule")
    def test_trainer_sends_survey(self, mock_schedule, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club, assigned_trainer=trainer)

        response = client.post(
            f"/feedback/send-survey/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"

    @patch("django_q.tasks.schedule")
    def test_trainer_package_owner_sends_survey(self, mock_schedule, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)

        response = client.post(
            f"/feedback/send-survey/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    @patch("django_q.tasks.schedule")
    def test_trainer_package_owner_without_current_subscription_cannot_send_survey(
        self,
        mock_schedule,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )

        response = client.post(
            f"/feedback/send-survey/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        mock_schedule.assert_not_called()


@pytest.mark.django_db
class TestStudentResponses:
    def test_get_student_responses(self, club, owner_user):
        form = FeedbackFormFactory(club=club)
        student = StudentFactory(club=club)
        resp = FeedbackResponseFactory(club=club, form=form, student=student)

        response = client.get(
            f"/feedback/students/{student.id}/responses/",
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == resp.id


@pytest.mark.django_db
class TestFormStats:
    def test_form_stats(self, club, owner_user):
        form = FeedbackFormFactory(club=club)
        q = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club)
        r = FeedbackResponseFactory(club=club, form=form, student=student)
        FeedbackAnswerFactory(club=club, response=r, question=q, rating_value=4)

        response = client.get(
            f"/feedback/forms/{form.id}/stats/",
            **_auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["average_rating"] == 4.0
        assert data["response_count"] == 1


@pytest.mark.django_db
class TestTenantIsolation:
    def test_form_isolated_across_clubs(self, club, owner_user):
        other_club = ClubFactory()
        my_form = FeedbackFormFactory(club=club, is_active=True)
        FeedbackFormFactory(club=other_club, is_active=True)

        response = client.get("/feedback/forms/active/", **_auth_params(owner_user, club, role="owner"))
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == my_form.id
