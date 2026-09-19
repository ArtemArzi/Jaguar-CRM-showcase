from unittest.mock import patch

import pytest

from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.notifications.tests.factories import NotificationTemplateFactory, PushSubscriptionFactory
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestSendStudentNotificationUserLookup:
    @pytest.mark.parametrize(
        ("notification_type", "expected_url"),
        [
            ("sub_expiry_7d", "/student"),
            ("trainings_left_2", "/student"),
            ("training_reminder", "/student/schedule"),
            ("training_reminder_24h", "/student/schedule"),
            ("missed_training", "/student/schedule"),
        ],
    )
    @patch("apps.notifications.services.async_task")
    def test_send_student_notification_uses_real_pwa_deep_links(
        self,
        mock_async,
        notification_type,
        expected_url,
    ):
        from apps.notifications.services import send_student_notification

        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, user=user)
        PushSubscriptionFactory(user=user)
        NotificationTemplateFactory(
            club=club,
            trigger_type=notification_type,
        )

        result = send_student_notification(
            club=club,
            student=student,
            notification_type=notification_type,
            context={"name": "Test", "days": "7", "trainings_left": "5"},
        )

        assert result is True
        assert mock_async.call_args.args[4] == expected_url

    @patch("apps.notifications.services.async_task")
    def test_send_student_notification_uses_student_user(self, mock_async):
        """Push sent to student.user_id directly (not ClubMembership fallback)."""
        from apps.notifications.services import send_student_notification

        club = ClubFactory()
        user = UserFactory()
        student = StudentFactory(club=club, user=user)
        PushSubscriptionFactory(user=user)
        NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
        )

        result = send_student_notification(
            club=club,
            student=student,
            notification_type="sub_expiry_7d",
            context={"name": "Test", "days": "7", "trainings_left": "5"},
        )
        assert result is True
        assert mock_async.called

    @patch("apps.notifications.services.async_task")
    def test_send_student_notification_skips_no_user(self, mock_async):
        """Returns False when student.user is None (no fallback to membership)."""
        from apps.notifications.services import send_student_notification

        club = ClubFactory()
        student = StudentFactory(club=club, user=None)
        NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
        )

        result = send_student_notification(
            club=club,
            student=student,
            notification_type="sub_expiry_7d",
            context={"name": "Test", "days": "7", "trainings_left": "5"},
        )
        assert result is False
        assert not mock_async.called
