import logging
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    LocationFactory,
    UserFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.notifications.models import (
    MassNotification,
    NotificationPreference,
    NotificationTemplate,
    PushSubscription,
    SentNotification,
)
from apps.notifications.tests.factories import NotificationTemplateFactory, PushSubscriptionFactory
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def _student_portal_member(
    *,
    club,
    status: str = Student.Status.ACTIVE,
    membership_role: str = "student",
    membership_active: bool = True,
    deleted_at=None,
):
    user = UserFactory()
    student = StudentFactory(
        club=club,
        user=user,
        status=status,
        deleted_at=deleted_at,
    )
    ClubMembershipFactory(
        user=user,
        club=club,
        role=membership_role,
        is_active=membership_active,
    )
    return student, user


@pytest.mark.django_db
class TestSubscribeDevice:
    def test_subscribe_device(self):
        from apps.notifications.services import subscribe_device

        user = UserFactory()
        sub = subscribe_device(
            user_id=user.id,
            endpoint="https://push.example.com/sub/1",
            key_p256dh="p256dh_key_data",
            key_auth="auth_key_data",
        )
        assert sub.user_id == user.id
        assert sub.endpoint == "https://push.example.com/sub/1"
        assert sub.key_p256dh == "p256dh_key_data"
        assert sub.key_auth == "auth_key_data"
        assert PushSubscription.objects.count() == 1

    def test_subscribe_device_update_existing_for_same_user(self):
        from apps.notifications.services import subscribe_device

        user = UserFactory()
        subscribe_device(
            user_id=user.id,
            endpoint="https://push.example.com/sub/1",
            key_p256dh="old_key",
            key_auth="old_auth",
        )
        sub = subscribe_device(
            user_id=user.id,
            endpoint="https://push.example.com/sub/1",
            key_p256dh="new_key",
            key_auth="new_auth",
        )
        assert PushSubscription.objects.count() == 1
        assert sub.key_p256dh == "new_key"
        assert sub.key_auth == "new_auth"

    def test_subscribe_device_rejects_cross_user_endpoint_reassignment(self):
        from apps.notifications.services import subscribe_device

        owner = UserFactory()
        attacker = UserFactory()
        sub = PushSubscriptionFactory(
            user=owner,
            endpoint="https://push.example.com/sub/protected",
            key_p256dh="owner_key",
            key_auth="owner_auth",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            subscribe_device(
                user_id=attacker.id,
                endpoint=sub.endpoint,
                key_p256dh="attacker_key",
                key_auth="attacker_auth",
            )

        assert exc_info.value.code == "push_subscription_not_owned"
        sub.refresh_from_db()
        assert sub.user_id == owner.id
        assert sub.key_p256dh == "owner_key"
        assert sub.key_auth == "owner_auth"
        assert PushSubscription.objects.count() == 1


@pytest.mark.django_db
class TestUnsubscribeDevice:
    def test_unsubscribe_device(self):
        from apps.notifications.services import unsubscribe_device

        sub = PushSubscriptionFactory()
        unsubscribe_device(user_id=sub.user_id, endpoint=sub.endpoint)
        assert PushSubscription.objects.count() == 0

    def test_unsubscribe_device_does_not_delete_cross_user_endpoint(self):
        from apps.notifications.services import unsubscribe_device

        owner = UserFactory()
        attacker = UserFactory()
        sub = PushSubscriptionFactory(user=owner, endpoint="https://push.example.com/sub/protected")

        unsubscribe_device(user_id=attacker.id, endpoint=sub.endpoint)

        assert PushSubscription.objects.filter(id=sub.id, user=owner).exists()


@pytest.mark.django_db
class TestSendPushToUser:
    @patch("apps.notifications.services.async_task")
    def test_send_push_to_user(self, mock_async):
        from apps.notifications.services import send_push_to_user

        user = UserFactory()
        sub1 = PushSubscriptionFactory(user=user)
        sub2 = PushSubscriptionFactory(user=user)
        send_push_to_user(user_id=user.id, title="Test", body="Body")
        assert mock_async.call_count == 2
        call_args = [c[0] for c in mock_async.call_args_list]
        sub_ids = {args[1] for args in call_args}
        assert sub_ids == {sub1.id, sub2.id}


@pytest.mark.django_db
class TestSendPushTask:
    @patch("apps.notifications.tasks.webpush")
    def test_send_push_task(self, mock_webpush):
        from apps.notifications.tasks import send_push_task

        sub = PushSubscriptionFactory()
        send_push_task(sub.id, "Title", "Body", "/url/")
        mock_webpush.assert_called_once()
        call_kwargs = mock_webpush.call_args
        assert call_kwargs[1]["subscription_info"]["endpoint"] == sub.endpoint
        assert call_kwargs[1]["subscription_info"]["keys"]["p256dh"] == sub.key_p256dh

    @patch("apps.notifications.tasks.webpush")
    def test_send_push_task_cleans_expired(self, mock_webpush):
        from pywebpush import WebPushException

        from apps.notifications.tasks import send_push_task

        sub = PushSubscriptionFactory()
        response_mock = MagicMock()
        response_mock.status_code = 410
        mock_webpush.side_effect = WebPushException("Gone", response=response_mock)
        send_push_task(sub.id, "Title", "Body")
        assert PushSubscription.objects.filter(id=sub.id).count() == 0

    @patch("apps.notifications.tasks.webpush")
    def test_send_push_task_logs_provider_failure_without_secrets(self, mock_webpush):
        from pywebpush import WebPushException

        from apps.notifications.tasks import send_push_task

        sub = PushSubscriptionFactory(
            endpoint="https://push.example.com/sub/raw-endpoint-secret",
            key_p256dh="raw-p256dh-secret",
            key_auth="raw-auth-secret",
        )
        response_mock = MagicMock()
        response_mock.status_code = 500
        mock_webpush.side_effect = WebPushException(
            "provider echoed https://push.example.com/sub/raw-endpoint-secret "
            "raw-p256dh-secret raw-auth-secret",
            response=response_mock,
        )

        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("apps.notifications.tasks")
        handler = RecordHandler(level=logging.ERROR)
        logger.addHandler(handler)
        try:
            send_push_task(sub.id, "Title", "Body")
        finally:
            logger.removeHandler(handler)

        assert PushSubscription.objects.filter(id=sub.id).exists()
        record = next(record for record in records if record.getMessage() == "push_send_failed")
        assert record.sub_id == sub.id
        assert record.status_code == 500
        assert record.exception_class == "WebPushException"
        assert not hasattr(record, "error")
        assert record.exc_info is None

        record_values = " ".join(str(value) for value in record.__dict__.values())
        assert "provider echoed" not in record_values
        assert "https://push.example.com/sub/raw-endpoint-secret" not in record_values
        assert "raw-p256dh-secret" not in record_values
        assert "raw-auth-secret" not in record_values

    def test_push_subscription_string_does_not_expose_endpoint_or_keys(self):
        sub = PushSubscriptionFactory(
            endpoint="https://push.example.com/sub/raw-endpoint-secret",
            key_p256dh="raw-p256dh-secret",
            key_auth="raw-auth-secret",
        )

        rendered = str(sub)

        assert f"id={sub.id}" in rendered
        assert f"user_id={sub.user_id}" in rendered
        assert "https://push.example.com/sub/raw-endpoint-secret" not in rendered
        assert "raw-p256dh-secret" not in rendered
        assert "raw-auth-secret" not in rendered


@pytest.mark.django_db
class TestGetRecipientsForSegment:
    def test_club_segment_resolves_only_linked_active_student_members(self, club):
        from apps.notifications.selectors import get_recipients_for_segment

        _target_student, target_user = _student_portal_member(club=club)
        _student_portal_member(club=club, membership_active=False)
        _student_portal_member(club=club, membership_role="trainer")
        unlinked_user = UserFactory()
        ClubMembershipFactory(user=unlinked_user, club=club, role="student")
        deleted_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        _student_portal_member(club=club, deleted_at=deleted_at)

        result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="club",
            segment_filter={},
        )

        assert result == [target_user.id]

    def test_status_segment_filters_student_status_exactly(self, club, other_club):
        from apps.notifications.selectors import get_recipients_for_segment

        _active_student, active_user = _student_portal_member(
            club=club,
            status=Student.Status.ACTIVE,
        )
        _student_portal_member(club=club, status=Student.Status.AT_RISK)
        _student_portal_member(club=club, status=Student.Status.LEAD)
        _student_portal_member(club=club, status=Student.Status.CHURNED)
        _student_portal_member(club=other_club, status=Student.Status.ACTIVE)

        result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="status",
            segment_filter={"status": Student.Status.ACTIVE},
        )

        assert result == [active_user.id]

    def test_location_segment_uses_effective_enrollment_not_subscription_scope(self, club):
        from apps.notifications.selectors import get_recipients_for_segment

        target_location = LocationFactory(club=club)
        other_location = LocationFactory(club=club)
        target_schedule = ScheduleFactory(club=club, location=target_location)
        other_schedule = ScheduleFactory(club=club, location=other_location)
        target_student, target_user = _student_portal_member(club=club)
        other_student, _other_user = _student_portal_member(club=club)
        billing_only_student, _billing_only_user = _student_portal_member(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=target_student,
            schedule=target_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=other_student,
            schedule=other_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
        )
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(
            club=club,
            training_type=tt,
            scope="location",
            location=target_location,
        )
        SubscriptionFactory(
            club=club,
            student=billing_only_student,
            tariff=tariff,
            status="active",
            location=target_location,
        )

        result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="location",
            segment_filter={"location_id": target_location.id},
        )

        assert result == [target_user.id]

    def test_group_segment_uses_club_local_effective_enrollment_boundaries(self, club):
        from apps.notifications.selectors import get_recipients_for_segment

        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        schedule = ScheduleFactory(club=club)
        club_today = date(2026, 7, 16)
        expected_users = []
        cases = [
            (ScheduleEnrollment.Status.ACTIVE, None, None, True),
            (ScheduleEnrollment.Status.TRIAL, club_today, club_today, True),
            (ScheduleEnrollment.Status.FROZEN, None, None, True),
            (
                ScheduleEnrollment.Status.TRANSFERRED,
                club_today - timedelta(days=10),
                club_today,
                True,
            ),
            (
                ScheduleEnrollment.Status.CANCELLED,
                club_today - timedelta(days=10),
                club_today,
                True,
            ),
            (ScheduleEnrollment.Status.ACTIVE, club_today + timedelta(days=1), None, False),
            (ScheduleEnrollment.Status.ACTIVE, None, club_today - timedelta(days=1), False),
            (
                ScheduleEnrollment.Status.CANCELLED,
                club_today,
                club_today,
                False,
            ),
        ]
        for status, starts_on, ends_on, expected in cases:
            student, user = _student_portal_member(club=club)
            ScheduleEnrollment.objects.create(
                club_id=club.id,
                student=student,
                schedule=schedule,
                status=status,
                starts_on=starts_on,
                ends_on=ends_on,
                created_from=(
                    ScheduleEnrollment.CreatedFrom.LEAD_BOOKING
                    if status == ScheduleEnrollment.Status.CANCELLED
                    and starts_on == club_today
                    and ends_on == club_today
                    else ScheduleEnrollment.CreatedFrom.MANUAL
                ),
            )
            if expected:
                expected_users.append(user.id)

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 7, 15, 20, 30, tzinfo=UTC),
        ):
            result = get_recipients_for_segment(
                club_id=club.id,
                segment_type="group",
                segment_filter={"schedule_id": schedule.id},
            )

        assert result == expected_users

    def test_group_segment_includes_new_enrollment_and_excludes_checkin_only_student(self, club):
        from apps.notifications.selectors import get_recipients_for_segment

        schedule = ScheduleFactory(club=club)
        enrolled_student, enrolled_user = _student_portal_member(club=club)
        checkin_only_student, _checkin_only_user = _student_portal_member(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=enrolled_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
        )
        CheckinFactory(club=club, schedule=schedule, student=checkin_only_student)

        result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="group",
            segment_filter={"schedule_id": schedule.id},
        )

        assert result == [enrolled_user.id]


    def test_group_segment_expands_canonical_membership_across_slots(self, club):
        from apps.attendance.models import TrainingGroup, TrainingGroupMembership
        from apps.notifications.selectors import get_recipients_for_segment

        training_type = TrainingTypeFactory(club=club, kind="group")
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        group = TrainingGroup.objects.create(
            club=club,
            name="Canonical group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        first_slot = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            training_group=group,
        )
        second_slot = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            training_group=group,
            day_of_week=(first_slot.day_of_week + 1) % 7,
        )
        student, user = _student_portal_member(club=club)
        TrainingGroupMembership.objects.create(
            club=club,
            student=student,
            training_group=group,
            status=TrainingGroupMembership.Status.ACTIVE,
            starts_on=date(2020, 1, 1),
            source=TrainingGroupMembership.Source.MANUAL,
        )

        group_result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="group",
            segment_filter={"training_group_id": group.id},
        )
        compatibility_result = get_recipients_for_segment(
            club_id=club.id,
            segment_type="group",
            segment_filter={"schedule_id": second_slot.id},
        )

        assert group_result == [user.id]
        assert compatibility_result == [user.id]
        assert first_slot.training_group_id == group.id


@pytest.mark.django_db
class TestSendMassNotification:
    @patch("apps.notifications.services.async_task")
    def test_send_mass_notification(self, mock_async, club, owner_user):
        from apps.notifications.services import send_mass_notification

        _student, user1 = _student_portal_member(club=club)
        PushSubscriptionFactory(user=user1)
        PushSubscriptionFactory(user=owner_user)

        notif = send_mass_notification(
            club_id=club.id,
            text="Hello club!",
            segment_type="club",
            segment_filter={},
            sent_by_id=owner_user.id,
        )
        assert notif.text == "Hello club!"
        assert notif.club_id == club.id
        assert isinstance(notif, MassNotification)
        mock_async.assert_called_once()

    @patch("apps.notifications.services.async_task")
    def test_mass_notification_records_count(self, mock_async, club, owner_user):
        from apps.notifications.services import send_mass_notification

        _student1, user1 = _student_portal_member(club=club)
        _student2, user2 = _student_portal_member(club=club)
        _opted_out_student, opted_out_user = _student_portal_member(club=club)
        _inactive_student, inactive_only_user = _student_portal_member(club=club)
        owner_sub = PushSubscriptionFactory(user=owner_user)
        user1_sub1 = PushSubscriptionFactory(user=user1)
        user1_sub2 = PushSubscriptionFactory(user=user1)
        user2_sub = PushSubscriptionFactory(user=user2)
        opted_out_sub = PushSubscriptionFactory(user=opted_out_user)
        PushSubscriptionFactory(user=inactive_only_user, is_active=False)
        NotificationPreference.objects.create(
            user=opted_out_user,
            disabled_categories=["training_reminders"],
        )

        notif = send_mass_notification(
            club_id=club.id,
            text="Count test",
            segment_type="club",
            segment_filter={},
            sent_by_id=owner_user.id,
        )
        assert notif.recipient_count == 3
        assert mock_async.call_count == 4
        queued_subscription_ids = [call.args[1] for call in mock_async.call_args_list]
        assert queued_subscription_ids == [
            user1_sub1.id,
            user1_sub2.id,
            user2_sub.id,
            opted_out_sub.id,
        ]
        assert owner_sub.id not in queued_subscription_ids

    @pytest.mark.parametrize(
        ("segment_type", "segment_filter"),
        [
            ("unknown", {}),
            ("status", {}),
            ("status", {"status": "not-a-status"}),
            ("group", {}),
            ("group", {"schedule_id": "not-an-id"}),
            ("location", {"location_id": True}),
            ("club", {"status": Student.Status.ACTIVE}),
        ],
    )
    @patch("apps.notifications.services.async_task")
    def test_invalid_segment_fails_before_row_or_task(
        self,
        mock_async,
        segment_type,
        segment_filter,
        club,
        owner_user,
    ):
        from apps.notifications.services import send_mass_notification

        with pytest.raises(BusinessLogicError):
            send_mass_notification(
                club_id=club.id,
                text="Must not persist",
                segment_type=segment_type,
                segment_filter=segment_filter,
                sent_by_id=owner_user.id,
            )

        assert not MassNotification.objects.for_club(club).exists()
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_foreign_segment_ids_fail_before_row_or_task(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
    ):
        from apps.notifications.services import send_mass_notification

        foreign_schedule = ScheduleFactory(club=other_club)
        foreign_location = LocationFactory(club=other_club)
        for segment_type, segment_filter in (
            ("group", {"schedule_id": foreign_schedule.id}),
            ("location", {"location_id": foreign_location.id}),
        ):
            with pytest.raises(BusinessLogicError):
                send_mass_notification(
                    club_id=club.id,
                    text="Must not persist",
                    segment_type=segment_type,
                    segment_filter=segment_filter,
                    sent_by_id=owner_user.id,
                )

        assert not MassNotification.objects.for_club(club).exists()
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_send_recalculates_audience_after_preview(self, mock_async, club, owner_user):
        from apps.notifications.services import (
            get_mass_notification_recipient_ids,
            send_mass_notification,
        )

        student, user = _student_portal_member(club=club, status=Student.Status.ACTIVE)
        PushSubscriptionFactory(user=user)
        preview_ids = get_mass_notification_recipient_ids(
            club_id=club.id,
            segment_type="status",
            segment_filter={"status": Student.Status.ACTIVE},
        )
        assert preview_ids == [user.id]
        student.status = Student.Status.AT_RISK
        student.save(update_fields=["status", "updated_at"])

        notification = send_mass_notification(
            club_id=club.id,
            text="Recalculated",
            segment_type="status",
            segment_filter={"status": Student.Status.ACTIVE},
            sent_by_id=owner_user.id,
        )

        assert notification.recipient_count == 0
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_mass_notification_tenant_isolation(self, mock_async, club, owner_user):
        from apps.notifications.services import send_mass_notification

        other_club = ClubFactory()
        other_user = UserFactory()
        ClubMembershipFactory(user=other_user, club=other_club, role="owner")

        send_mass_notification(
            club_id=club.id,
            text="Club A msg",
            segment_type="club",
            segment_filter={},
            sent_by_id=owner_user.id,
        )
        send_mass_notification(
            club_id=other_club.id,
            text="Club B msg",
            segment_type="club",
            segment_filter={},
            sent_by_id=other_user.id,
        )

        club_a_notifs = MassNotification.objects.for_club(club.id)
        club_b_notifs = MassNotification.objects.for_club(other_club.id)
        assert club_a_notifs.count() == 1
        assert club_b_notifs.count() == 1
        assert club_a_notifs.first().text == "Club A msg"
        assert club_b_notifs.first().text == "Club B msg"


@pytest.mark.django_db
class TestSendStudentNotification:
    @patch("apps.notifications.services.async_task")
    def test_send_student_notification_dedup(self, mock_async, club):
        """Second call for same student+type+day returns False."""
        from apps.notifications.services import send_student_notification

        user = UserFactory()
        student = StudentFactory(club=club, user=user)
        PushSubscriptionFactory(user=user)
        NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            body_template="{name}, напоминание",
        )

        context = {"name": "Test", "days": "7", "trainings_left": "5"}
        result1 = send_student_notification(
            club=club,
            student=student,
            notification_type="sub_expiry_7d",
            context=context,
        )
        result2 = send_student_notification(
            club=club,
            student=student,
            notification_type="sub_expiry_7d",
            context=context,
        )
        assert result1 is True
        assert result2 is False

    @patch("apps.notifications.models.SentNotification.objects.create")
    @patch("apps.notifications.services.async_task")
    def test_send_student_notification_handles_dedup_race(self, mock_async, mock_create, club):
        from apps.notifications.services import send_student_notification

        user = UserFactory()
        student = StudentFactory(club=club, user=user)
        PushSubscriptionFactory(user=user)
        NotificationTemplateFactory(
            club=club,
            trigger_type="sub_expiry_7d",
            body_template="{name}, напоминание",
        )
        mock_create.side_effect = IntegrityError("duplicate")

        result = send_student_notification(
            club=club,
            student=student,
            notification_type="sub_expiry_7d",
            context={"name": "Test", "days": "7", "trainings_left": "5"},
        )

        assert result is False

    @patch("apps.notifications.services.async_task")
    def test_send_push_to_user_filters_inactive(self, mock_async):
        """send_push_to_user only sends to is_active=True subscriptions."""
        from apps.notifications.services import send_push_to_user

        user = UserFactory()
        PushSubscriptionFactory(user=user, is_active=True)
        PushSubscriptionFactory(user=user, is_active=False)

        send_push_to_user(user_id=user.id, title="Test", body="Body")
        # Only 1 active subscription should be sent to
        assert mock_async.call_count == 1

    def test_render_template_handles_missing_keys(self):
        """render_template returns original on missing keys."""
        from apps.notifications.services import render_template

        result = render_template(
            template_str="{name}, осталось {missing_key} дней",
            context={"name": "Иван"},
        )
        assert result == "Иван, осталось {missing_key} дней"


@pytest.mark.django_db
class TestSendParentNotification:
    @patch("apps.notifications.services.async_task")
    def test_disabled_parent_template_skips_fallback_send(self, mock_async, club):
        from apps.notifications.services import send_parent_notification

        parent_user = UserFactory()
        student = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
        )
        PushSubscriptionFactory(user=parent_user)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.PARENT_CHECKIN,
            is_enabled=False,
        )

        sent = send_parent_notification(
            club=club,
            student=student,
            notification_type=NotificationTemplate.TriggerType.PARENT_CHECKIN,
            context={"name": student.first_name},
            fallback_title="Чек-ин",
            fallback_body="Fallback body",
        )

        assert sent is False
        assert not mock_async.called

    @patch("apps.notifications.services.async_task")
    def test_unconfigured_parent_notification_can_use_fallback(self, mock_async, club):
        from apps.notifications.services import send_parent_notification

        parent_user = UserFactory()
        student = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
        )
        PushSubscriptionFactory(user=parent_user)

        sent = send_parent_notification(
            club=club,
            student=student,
            notification_type="parent_checkin_cancelled",
            context={"name": student.first_name},
            fallback_title="Чек-ин отменён",
            fallback_body="Fallback body",
        )

        assert sent is True
        assert mock_async.called

    def test_render_template_success(self):
        """render_template replaces placeholders correctly."""
        from apps.notifications.services import render_template

        result = render_template(
            template_str="{name}, ваш абонемент истекает через {days} дней",
            context={"name": "Иван", "days": "7"},
        )
        assert result == "Иван, ваш абонемент истекает через 7 дней"


@pytest.mark.django_db
class TestSendTrainerRetentionTaskNotification:
    @patch("apps.notifications.services.send_push_to_user")
    def test_uses_enabled_follow_up_template(self, mock_push, club):
        from apps.notifications.services import TRAINER_RETENTION_TASK, send_trainer_retention_task_notification

        trainer_user = UserFactory()
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, first_name="Dima", last_name="Lead")
        task = RetentionTaskFactory(club=club, student=student, trainer=trainer)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.FOLLOW_UP,
            title_template="Custom task for {name}",
            body_template="Custom body for {name}",
            is_enabled=True,
        )

        sent = send_trainer_retention_task_notification(club=club, task=task)

        assert sent is True
        mock_push.assert_called_once()
        call_kwargs = mock_push.call_args.kwargs
        assert call_kwargs["user_id"] == trainer_user.id
        assert call_kwargs["title"].startswith("Custom task")
        assert call_kwargs["body"].startswith("Custom body")
        assert "Dima" in call_kwargs["body"]
        assert call_kwargs["data"]["type"] == TRAINER_RETENTION_TASK
        assert SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=TRAINER_RETENTION_TASK,
        ).exists()

    @patch("apps.notifications.services.send_push_to_user")
    def test_disabled_follow_up_template_skips_send(self, mock_push, club):
        from apps.notifications.services import send_trainer_retention_task_notification

        trainer_user = UserFactory()
        trainer = TrainerFactory(club=club, user=trainer_user)
        task = RetentionTaskFactory(club=club, trainer=trainer)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.FOLLOW_UP,
            is_enabled=False,
        )

        sent = send_trainer_retention_task_notification(club=club, task=task)

        assert sent is False
        mock_push.assert_not_called()
