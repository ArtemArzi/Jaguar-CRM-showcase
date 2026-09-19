import json
from unittest.mock import patch

import pytest
from django.test import Client as DjangoClient
from ninja.testing import TestClient

from apps.attendance.models import Schedule
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    LocationFactory,
    UserFactory,
)
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.notifications.models import MassNotification, PushSubscription
from apps.notifications.tests.factories import (
    MassNotificationFactory,
    NotificationTemplateFactory,
    PushSubscriptionFactory,
)
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestSubscribeAPI:
    def test_subscribe_endpoint(self, club, owner_user):
        response = client.post(
            "/notifications/subscribe/",
            json={
                "endpoint": "https://push.example.com/sub/1",
                "key_p256dh": "test_key",
                "key_auth": "test_auth",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["endpoint"] == "https://push.example.com/sub/1"
        assert "id" in data

    def test_unsubscribe_endpoint(self, club, owner_user):
        sub = PushSubscriptionFactory(user=owner_user)
        response = client.post(
            "/notifications/unsubscribe/",
            json={"endpoint": sub.endpoint},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 204
        assert not PushSubscription.objects.filter(id=sub.id).exists()

    def test_subscribe_endpoint_accepts_dashboard_session(self, club, owner_user):
        django_client = DjangoClient()
        django_client.force_login(owner_user)

        response = django_client.post(
            "/api/notifications/subscribe/",
            data=json.dumps(
                {
                    "endpoint": "https://push.example.com/dashboard-session",
                    "key_p256dh": "dashboard_key",
                    "key_auth": "dashboard_auth",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 201
        assert PushSubscription.objects.filter(
            user=owner_user,
            endpoint="https://push.example.com/dashboard-session",
        ).exists()

    def test_unsubscribe_endpoint_accepts_dashboard_session(self, club, owner_user):
        sub = PushSubscriptionFactory(user=owner_user)
        django_client = DjangoClient()
        django_client.force_login(owner_user)

        response = django_client.post(
            "/api/notifications/unsubscribe/",
            data=json.dumps({"endpoint": sub.endpoint}),
            content_type="application/json",
        )

        assert response.status_code == 204
        assert not PushSubscription.objects.filter(id=sub.id).exists()

    def test_unsubscribe_endpoint_does_not_delete_another_users_endpoint(self, club, owner_user):
        other_user = UserFactory()
        sub = PushSubscriptionFactory(user=other_user)

        response = client.post(
            "/notifications/unsubscribe/",
            json={"endpoint": sub.endpoint},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 204
        assert PushSubscription.objects.filter(id=sub.id, user=other_user).exists()


@pytest.mark.django_db
class TestMassNotificationAPI:
    @patch("apps.notifications.services.async_task")
    def test_mass_notification_endpoint(self, mock_async, club, owner_user):
        student_user = UserFactory()
        ClubMembershipFactory(
            user=student_user,
            club=club,
            role="student",
        )
        StudentFactory(
            club=club,
            user=student_user,
            status=Student.Status.ACTIVE,
        )
        PushSubscriptionFactory(user=student_user)
        PushSubscriptionFactory(user=owner_user)
        response = client.post(
            "/notifications/mass/",
            json={"text": "Hello club!", "segment_type": "club", "segment_filter": {}},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["text"] == "Hello club!"
        assert data["segment_type"] == "club"
        assert data["recipient_count"] == 1
        mock_async.assert_called_once()

    @patch("apps.notifications.services.async_task")
    def test_mass_notification_preview(self, mock_async, club, owner_user):
        student_user = UserFactory()
        ClubMembershipFactory(
            user=student_user,
            club=club,
            role="student",
        )
        StudentFactory(
            club=club,
            user=student_user,
            status=Student.Status.ACTIVE,
        )
        PushSubscriptionFactory(user=student_user)
        PushSubscriptionFactory(user=owner_user)
        response = client.post(
            "/notifications/mass/preview/",
            json={"text": "Preview", "segment_type": "club", "segment_filter": {}},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert "recipient_count" in data
        assert data["recipient_count"] == 1

    @pytest.mark.parametrize(
        ("segment_type", "segment_filter"),
        [
            ("unknown", {}),
            ("status", {}),
            ("status", {"status": "not-a-status"}),
            ("group", {"schedule_id": "not-an-id"}),
            ("location", {}),
        ],
    )
    @patch("apps.notifications.services.async_task")
    def test_invalid_mass_segment_returns_400_without_side_effects(
        self,
        mock_async,
        segment_type,
        segment_filter,
        club,
        owner_user,
    ):
        response = client.post(
            "/notifications/mass/",
            json={
                "text": "Must not persist",
                "segment_type": segment_type,
                "segment_filter": segment_filter,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert "code" in response.json()
        assert not MassNotification.objects.for_club(club).exists()
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_foreign_mass_segment_returns_400_without_side_effects(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
    ):
        foreign_schedule = Schedule.objects.create(
            club=other_club,
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Foreign",
            trainer=TrainerFactory(club=other_club),
            location=LocationFactory(club=other_club),
        )

        response = client.post(
            "/notifications/mass/",
            json={
                "text": "Must not persist",
                "segment_type": "group",
                "segment_filter": {"schedule_id": foreign_schedule.id},
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert not MassNotification.objects.for_club(club).exists()
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_trainer_can_only_send_to_own_group(self, mock_async, club, trainer_user):
        response = client.post(
            "/notifications/mass/",
            json={"text": "All club", "segment_type": "club", "segment_filter": {}},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    @patch("apps.notifications.services.async_task")
    def test_trainer_sends_to_own_group(self, mock_async, club, trainer_user):
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club, user=trainer_user)
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Trainer Group",
            trainer=trainer,
            location=location,
        )
        response = client.post(
            "/notifications/mass/",
            json={
                "text": "Group msg",
                "segment_type": "group",
                "segment_filter": {"schedule_id": schedule.id},
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201

    @patch("apps.notifications.services.async_task")
    def test_list_mass_notifications(self, mock_async, club, owner_user):
        MassNotificationFactory(club=club, sent_by=owner_user)
        MassNotificationFactory(club=club, sent_by=owner_user)
        response = client.get(
            "/notifications/mass/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_vapid_key_public(self):
        response = client.get("/notifications/vapid-key/")
        assert response.status_code == 200
        data = response.json()
        assert "public_key" in data

    @patch("apps.notifications.services.async_task")
    def test_mass_notification_tenant_isolation(self, mock_async, club, owner_user):
        other_club = ClubFactory()
        other_user = UserFactory()
        ClubMembershipFactory(user=other_user, club=other_club, role="owner")

        MassNotificationFactory(club=club, sent_by=owner_user)
        MassNotificationFactory(club=other_club, sent_by=other_user)

        response = client.get(
            "/notifications/mass/",
            **_auth_params(owner_user, club),
        )
        data = response.json()
        assert data["count"] == 1


@pytest.mark.django_db
class TestNotificationTemplateAPI:
    def test_list_templates(self, club, owner_user):
        NotificationTemplateFactory(club=club, trigger_type="sub_expiry_7d")
        NotificationTemplateFactory(club=club, trigger_type="sub_expiry_3d")
        response = client.get(
            "/notifications/templates/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_update_template(self, club, owner_user):
        template = NotificationTemplateFactory(club=club, trigger_type="sub_expiry_7d", is_enabled=True)
        response = client.patch(
            f"/notifications/templates/{template.id}/",
            json={"is_enabled": False, "days_before": 10},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_enabled"] is False
        assert data["days_before"] == 10

    def test_update_template_rejects_invalid_days_before_without_mutation(self, club, owner_user):
        template = NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            days_before=7,
        )

        response = client.patch(
            f"/notifications/templates/{template.id}/",
            json={"days_before": 0},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_template"
        template.refresh_from_db()
        assert template.days_before == 7

    @pytest.mark.parametrize("field", ["title_template", "body_template"])
    def test_update_template_rejects_blank_text_without_mutation(self, field, club, owner_user):
        template = NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            title_template="Original title",
            body_template="Original body",
        )

        response = client.patch(
            f"/notifications/templates/{template.id}/",
            json={field: "   "},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_template"
        template.refresh_from_db()
        assert template.title_template == "Original title"
        assert template.body_template == "Original body"

    def test_templates_tenant_isolation(self, club, owner_user):
        other_club = ClubFactory()
        NotificationTemplateFactory(club=club, trigger_type="sub_expiry_7d")
        NotificationTemplateFactory(club=other_club, trigger_type="sub_expiry_7d")
        response = client.get(
            "/notifications/templates/",
            **_auth_params(owner_user, club),
        )
        data = response.json()
        assert len(data) == 1

    def test_templates_requires_owner(self, club, trainer_user):
        response = client.get(
            "/notifications/templates/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestOptOutAPI:
    def test_opt_out(self, club, owner_user):
        PushSubscriptionFactory(user=owner_user, is_active=True)
        response = client.post(
            "/notifications/opt-out/",
            json={"opt_out": True},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

    def test_opt_in(self, club, owner_user):
        PushSubscriptionFactory(user=owner_user, is_active=False)
        response = client.post(
            "/notifications/opt-out/",
            json={"opt_out": False},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True
