from unittest.mock import patch

import pytest

from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    ClubSettingsFactory,
    UserFactory,
)
from apps.feedback.tasks import send_churned_survey_push
from apps.feedback.tests.factories import FeedbackFormFactory, FeedbackResponseFactory
from apps.notifications.models import NotificationPreference, NotificationTemplate
from apps.notifications.tests.factories import NotificationTemplateFactory, PushSubscriptionFactory
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestChurnedSurveyPush:
    @patch("apps.notifications.services.async_task")
    def test_churned_survey_push_sent(self, mock_async):
        """Push sent when student becomes churned with active churned form."""
        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, status="churned", user=user)
        PushSubscriptionFactory(user=user)

        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        assert mock_async.called

    @patch("apps.notifications.services.send_push_to_user")
    def test_churned_survey_uses_enabled_notification_template(self, mock_push):
        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, status="churned", user=user, first_name="Dima")
        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.CHURNED_SURVEY,
            title_template="Custom churned title for {name}",
            body_template="Custom churned body for {name}",
            is_enabled=True,
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["title"].startswith("Custom churned title")
        assert call_kwargs["body"].startswith("Custom churned body")
        assert "Dima" in call_kwargs["body"]

    @patch("apps.notifications.services.send_push_to_user")
    def test_churned_survey_skips_disabled_notification_template(self, mock_push):
        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, status="churned", user=user)
        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.CHURNED_SURVEY,
            is_enabled=False,
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.send_push_to_user")
    def test_churned_survey_respects_feedback_survey_opt_out(self, mock_push):
        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, status="churned", user=user)
        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )
        NotificationPreference.objects.create(
            user=user,
            disabled_categories=["feedback_surveys"],
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        mock_push.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_churned_survey_no_form(self, mock_async):
        """No push when no active churned form exists."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="churned")
        user = UserFactory()
        ClubMembershipFactory(user=user, club=club, role="student")

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        assert not mock_async.called

    @patch("apps.notifications.services.async_task")
    def test_churned_survey_already_responded(self, mock_async):
        """No push when student already responded to churned form."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="churned")
        user = UserFactory()
        ClubMembershipFactory(user=user, club=club, role="student")

        form = FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )
        FeedbackResponseFactory(club=club, form=form, student=student)

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        assert not mock_async.called

    @patch("apps.notifications.services.async_task")
    def test_churned_survey_skips_no_user(self, mock_async):
        """No push when student.user is None and not is_child."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="churned", user=None)

        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        assert not mock_async.called

    @patch("apps.notifications.services.async_task")
    def test_churned_survey_respects_budget(self, mock_async):
        """Low priority respects anti-spam budget."""
        club = ClubFactory()
        ClubSettingsFactory(club=club, max_push_per_week=0)
        student = StudentFactory(club=club, status="churned")
        user = UserFactory()
        ClubMembershipFactory(user=user, club=club, role="student")

        FeedbackFormFactory(
            club=club, name="Churned survey", trigger_type="churned", is_active=True
        )

        send_churned_survey_push(student_id=student.id, club_id=club.id)

        assert not mock_async.called
