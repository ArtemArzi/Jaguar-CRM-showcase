from unittest.mock import patch

import pytest
from ninja.testing import TestClient

from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.notifications.models import NotificationPreference
from apps.notifications.tests.factories import (
    NotificationTemplateFactory,
    PushSubscriptionFactory,
)
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestNotificationPreferenceModel:
    def test_create_with_disabled_categories(self, owner_user):
        pref = NotificationPreference.objects.create(
            user=owner_user,
            disabled_categories=["subscription_alerts", "training_reminders"],
        )
        assert pref.disabled_categories == ["subscription_alerts", "training_reminders"]

    def test_default_empty_list(self, owner_user):
        pref = NotificationPreference.objects.create(user=owner_user)
        assert pref.disabled_categories == []


@pytest.mark.django_db
class TestCategoryOptOutInService:
    def test_send_student_notification_skips_when_category_disabled(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)

        # Create template
        template = NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            body_template="{name}, ваш абонемент истекает",
        )

        # Create push subscription for the student user
        PushSubscriptionFactory(user=student_user)

        # Disable the UI category that covers sub_expiry_7d
        NotificationPreference.objects.create(
            user=student_user,
            disabled_categories=["subscription_alerts"],
        )

        from apps.notifications.services import send_student_notification

        with patch("apps.notifications.services.send_push_to_user") as mock_push:
            result = send_student_notification(
                club=club,
                student=student,
                notification_type="sub_expiry_7d",
                context={"name": "Test", "days": "7", "trainings_left": "5"},
                template=template,
            )
            assert result is False
            mock_push.assert_not_called()

    def test_send_student_notification_sends_when_category_not_disabled(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        template = NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            body_template="{name}, ваш абонемент истекает",
        )
        PushSubscriptionFactory(user=student_user)

        # Disable a different UI category (training_reminders, not subscription_alerts)
        NotificationPreference.objects.create(
            user=student_user,
            disabled_categories=["training_reminders"],
        )

        from apps.notifications.services import send_student_notification

        with patch("apps.notifications.services.send_push_to_user") as mock_push:
            result = send_student_notification(
                club=club,
                student=student,
                notification_type="sub_expiry_7d",
                context={"name": "Test", "days": "7", "trainings_left": "5"},
                template=template,
            )
            assert result is True
            mock_push.assert_called_once()


@pytest.mark.django_db
class TestPreferencesAPI:
    def test_get_preferences_returns_empty_default(self, club, owner_user):
        response = client.get(
            "/notifications/preferences/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["disabled_categories"] == []

    def test_put_preferences_saves_categories(self, club, owner_user):
        response = client.put(
            "/notifications/preferences/",
            json={"disabled_categories": ["subscription_alerts", "training_reminders"]},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert set(data["disabled_categories"]) == {"subscription_alerts", "training_reminders"}

        # Verify persisted
        pref = NotificationPreference.objects.get(user=owner_user)
        assert "subscription_alerts" in pref.disabled_categories

    def test_put_preferences_updates_existing(self, club, owner_user):
        NotificationPreference.objects.create(
            user=owner_user,
            disabled_categories=["subscription_alerts"],
        )
        response = client.put(
            "/notifications/preferences/",
            json={"disabled_categories": ["training_reminders"]},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["disabled_categories"] == ["training_reminders"]


@pytest.mark.django_db
class TestOptOutBackwardCompat:
    def test_old_opt_out_endpoint_still_works(self, club, owner_user):
        PushSubscriptionFactory(user=owner_user)

        response = client.post(
            "/notifications/opt-out/",
            json={"opt_out": True},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        # Re-enable
        response = client.post(
            "/notifications/opt-out/",
            json={"opt_out": False},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True
