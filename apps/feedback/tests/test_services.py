from unittest.mock import patch

import pytest

from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.feedback.selectors import get_active_form, get_form_average_rating, get_student_feedback_responses
from apps.feedback.services import (
    create_feedback_form,
    schedule_trial_feedback,
    submit_feedback_response,
)
from apps.feedback.tasks import send_trial_feedback_push
from apps.feedback.tests.factories import FeedbackFormFactory, FeedbackQuestionFactory, FeedbackResponseFactory
from apps.notifications.models import NotificationPreference, NotificationTemplate
from apps.notifications.tests.factories import NotificationTemplateFactory
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestCreateFeedbackForm:
    def test_create_form_with_questions(self, club):
        form = create_feedback_form(
            club_id=club.id,
            name="Test Form",
            questions=[
                {"question_type": "rating", "text": "Rate us", "is_required": True},
                {"question_type": "yes_no", "text": "Would you come again?", "is_required": False},
                {"question_type": "text", "text": "Comments", "is_required": False},
            ],
        )

        assert form.name == "Test Form"
        assert form.is_active is True
        questions = list(form.questions.all())
        assert len(questions) == 3
        assert questions[0].question_type == "rating"
        assert questions[0].order == 1
        assert questions[0].is_required is True
        assert questions[1].question_type == "yes_no"
        assert questions[1].order == 2
        assert questions[2].question_type == "text"
        assert questions[2].order == 3

    def test_create_form_deactivates_previous(self, club):
        old_form = FeedbackFormFactory(club=club, is_active=True)
        new_form = create_feedback_form(
            club_id=club.id,
            name="New Form",
            questions=[{"question_type": "rating", "text": "Rate", "is_required": True}],
        )

        old_form.refresh_from_db()
        assert old_form.is_active is False
        assert new_form.is_active is True


@pytest.mark.django_db
class TestGetActiveForm:
    def test_get_active_form_returns_active(self, club):
        FeedbackFormFactory(club=club, is_active=False)
        active = FeedbackFormFactory(club=club, is_active=True)

        result = get_active_form(club=club)
        assert result is not None
        assert result.id == active.id

    def test_get_active_form_defaults_to_trial_form(self, club):
        FeedbackFormFactory(club=club, trigger_type="churned", is_active=True)
        trial = FeedbackFormFactory(club=club, trigger_type="trial", is_active=True)

        result = get_active_form(club=club)

        assert result is not None
        assert result.id == trial.id

    def test_get_active_form_returns_none_when_no_active(self, club):
        FeedbackFormFactory(club=club, is_active=False)
        assert get_active_form(club=club) is None


@pytest.mark.django_db
class TestSubmitFeedbackResponse:
    def test_submit_response_with_answers(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        q1 = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1, is_required=True)
        q2 = FeedbackQuestionFactory(club=club, form=form, question_type="text", order=2, is_required=False)
        student = StudentFactory(club=club)

        response = submit_feedback_response(
            club_id=club.id,
            form_id=form.id,
            student_id=student.id,
            answers=[
                {"question_id": q1.id, "rating_value": 5},
                {"question_id": q2.id, "text_value": "Great training!"},
            ],
        )

        assert response.form_id == form.id
        assert response.student_id == student.id
        answers = list(response.answers.all())
        assert len(answers) == 2

    def test_submit_response_already_submitted_returns_existing_response(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        existing = FeedbackResponseFactory(club=club, form=form, student=student)

        response = submit_feedback_response(
            club_id=club.id, form_id=form.id, student_id=student.id, answers=[]
        )

        assert response.id == existing.id

    def test_submit_response_missing_required_raises(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1, is_required=True)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id, form_id=form.id, student_id=student.id, answers=[]
            )
        assert exc_info.value.code == "missing_required"

    def test_submit_response_rejects_student_from_other_club(self, club):
        other_club = ClubFactory()
        form = FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=other_club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[],
            )

        assert exc_info.value.code == "student_not_found"

    def test_submit_response_rejects_deleted_student(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        student.soft_delete()

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[],
            )

        assert exc_info.value.code == "student_not_found"

    def test_submit_response_rejects_question_from_another_form(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, order=1)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[{"question_id": foreign_question.id, "rating_value": 5}],
            )

        assert exc_info.value.code == "invalid_question"

    def test_submit_response_rejects_question_from_other_club(self, club):
        other_club = ClubFactory()
        form = FeedbackFormFactory(club=club, is_active=True)
        foreign_form = FeedbackFormFactory(club=other_club, is_active=True)
        foreign_question = FeedbackQuestionFactory(club=other_club, form=foreign_form, order=1)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[{"question_id": foreign_question.id, "rating_value": 5}],
            )

        assert exc_info.value.code == "invalid_question"

    def test_submit_response_rejects_duplicate_question_answers(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[
                    {"question_id": question.id, "rating_value": 5},
                    {"question_id": question.id, "rating_value": 4},
                ],
            )

        assert exc_info.value.code == "duplicate_question_answer"

    def test_submit_response_rejects_wrong_answer_type_for_required_question(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(
            club=club,
            form=form,
            question_type="rating",
            order=1,
            is_required=True,
        )
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[{"question_id": question.id, "text_value": "Five"}],
            )

        assert exc_info.value.code == "invalid_answer_type"

    def test_submit_response_rejects_out_of_range_rating(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[{"question_id": question.id, "rating_value": 6}],
            )

        assert exc_info.value.code == "invalid_rating_value"

    def test_submit_response_rejects_empty_required_text_answer(self, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(
            club=club,
            form=form,
            question_type="text",
            order=1,
            is_required=True,
        )
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            submit_feedback_response(
                club_id=club.id,
                form_id=form.id,
                student_id=student.id,
                answers=[{"question_id": question.id, "text_value": "   "}],
            )

        assert exc_info.value.code == "missing_required"


@pytest.mark.django_db
class TestScheduleTrialFeedback:
    @patch("django_q.tasks.schedule")
    def test_schedule_with_default_delay(self, mock_schedule, club):
        FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)

        schedule_trial_feedback(club_id=club.id, student_id=student.id, checkin_id=1)

        mock_schedule.assert_called_once()
        call_kwargs = mock_schedule.call_args.kwargs
        assert call_kwargs["schedule_type"] == "O"

    @patch("django_q.tasks.schedule")
    def test_schedule_with_custom_delay(self, mock_schedule, club):
        ClubSettingsFactory(club=club, feedback_delay_hours=4)
        FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)

        schedule_trial_feedback(club_id=club.id, student_id=student.id, checkin_id=1)

        mock_schedule.assert_called_once()

    @patch("django_q.tasks.schedule")
    def test_schedule_skips_if_already_responded(self, mock_schedule, club):
        form = FeedbackFormFactory(club=club, is_active=True)
        student = StudentFactory(club=club)
        FeedbackResponseFactory(club=club, form=form, student=student)

        schedule_trial_feedback(club_id=club.id, student_id=student.id, checkin_id=1)

        mock_schedule.assert_not_called()

    @patch("django_q.tasks.schedule")
    def test_schedule_skips_if_no_active_form(self, mock_schedule, club):
        student = StudentFactory(club=club)

        schedule_trial_feedback(club_id=club.id, student_id=student.id, checkin_id=1)

        mock_schedule.assert_not_called()


@pytest.mark.django_db
class TestSendTrialFeedbackPush:
    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_to_parent(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Dima")
        FeedbackFormFactory(club=club, is_active=True)

        send_trial_feedback_push(student.id, club.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["user_id"] == parent.id
        assert "Dima" in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_uses_enabled_trial_feedback_template(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent, first_name="Dima")
        FeedbackFormFactory(club=club, is_active=True)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRIAL_FEEDBACK,
            title_template="Custom trial title for {name}",
            body_template="Custom trial body for {name}",
            is_enabled=True,
        )

        send_trial_feedback_push(student.id, club.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["title"].startswith("Custom trial title")
        assert call_kwargs["body"].startswith("Custom trial body")
        assert "Dima" in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_skips_disabled_trial_feedback_template(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        FeedbackFormFactory(club=club, is_active=True)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRIAL_FEEDBACK,
            is_enabled=False,
        )

        send_trial_feedback_push(student.id, club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_respects_feedback_survey_opt_out(self, mock_push, club):
        parent = UserFactory()
        student = StudentFactory(club=club, is_child=True, parent_user=parent)
        FeedbackFormFactory(club=club, is_active=True)
        NotificationPreference.objects.create(
            user=parent,
            disabled_categories=["feedback_surveys"],
        )

        send_trial_feedback_push(student.id, club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_send_push_skips_no_user(self, mock_push, club):
        student = StudentFactory(club=club, is_child=False)
        FeedbackFormFactory(club=club, is_active=True)

        send_trial_feedback_push(student.id, club.id)

        mock_push.assert_not_called()


@pytest.mark.django_db
class TestGetStudentFeedbackResponses:
    def test_returns_student_responses(self, club):
        form = FeedbackFormFactory(club=club)
        student = StudentFactory(club=club)
        r1 = FeedbackResponseFactory(club=club, form=form, student=student)
        # Other student's response
        FeedbackResponseFactory(club=club, form=form, student=StudentFactory(club=club))

        responses = get_student_feedback_responses(club=club, student_id=student.id)
        assert responses.count() == 1
        assert responses.first().id == r1.id


@pytest.mark.django_db
class TestGetFormAverageRating:
    def test_average_rating(self, club):
        form = FeedbackFormFactory(club=club)
        q = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        s1 = StudentFactory(club=club)
        s2 = StudentFactory(club=club)
        r1 = FeedbackResponseFactory(club=club, form=form, student=s1)
        r2 = FeedbackResponseFactory(club=club, form=form, student=s2)
        from apps.feedback.tests.factories import FeedbackAnswerFactory

        FeedbackAnswerFactory(club=club, response=r1, question=q, rating_value=4)
        FeedbackAnswerFactory(club=club, response=r2, question=q, rating_value=2)

        avg = get_form_average_rating(club=club, form_id=form.id)
        assert avg == 3.0

    def test_average_rating_none_when_no_responses(self, club):
        form = FeedbackFormFactory(club=club)
        avg = get_form_average_rating(club=club, form_id=form.id)
        assert avg is None


@pytest.mark.django_db
class TestTenantIsolation:
    def test_feedback_form_isolated(self, club):
        other_club = ClubFactory()
        FeedbackFormFactory(club=club, is_active=True)
        FeedbackFormFactory(club=other_club, is_active=True)

        result = get_active_form(club=club)
        assert result is not None
        assert result.club_id == club.id

    def test_feedback_response_isolated(self, club):
        other_club = ClubFactory()
        form = FeedbackFormFactory(club=club)
        student = StudentFactory(club=club)
        FeedbackResponseFactory(club=club, form=form, student=student)

        other_form = FeedbackFormFactory(club=other_club)
        other_student = StudentFactory(club=other_club)
        FeedbackResponseFactory(club=other_club, form=other_form, student=other_student)

        responses = get_student_feedback_responses(club=club, student_id=student.id)
        assert responses.count() == 1
