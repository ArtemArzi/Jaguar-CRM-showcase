from datetime import date
from unittest.mock import patch

import pytest

from apps.attendance.tests.factories import ScheduleFactory
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.notifications import services
from apps.notifications.models import NotificationPreference, PushSubscription, SentNotification
from apps.notifications.tests.factories import NotificationTemplateFactory, PushSubscriptionFactory
from apps.students.tests.factories import StudentFactory


def _delivery_context():
    club = ClubFactory()
    schedule = ScheduleFactory(club=club)
    user = UserFactory()
    student = StudentFactory(club=club, status="active", user=user)
    ClubMembershipFactory(user=user, club=club, role="student")
    PushSubscriptionFactory(user=user)
    template = NotificationTemplateFactory(
        club=club,
        trigger_type="training_reminder",
        title_template="Soon",
        body_template="At {time}",
    )
    return club, schedule, student, template


@pytest.mark.django_db
def test_existing_pending_occurrence_claim_prevents_second_worker_enqueue():
    club, schedule, student, template = _delivery_context()
    occurrence_date = date(2026, 7, 15)
    SentNotification.objects.create(
        club=club,
        student=student,
        notification_type="training_reminder",
        sent_date=occurrence_date,
        occurrence_schedule=schedule,
        occurrence_date=occurrence_date,
        delivery_stage="one_hour",
        delivery_state="pending",
    )

    with patch("apps.notifications.services.async_task") as enqueue_push:
        sent = services.send_occurrence_student_notification(
            club=club,
            student=student,
            notification_type="training_reminder",
            context={"time": "18:00"},
            template=template,
            occurrence_schedule_id=schedule.id,
            occurrence_date=occurrence_date,
            delivery_stage="one_hour",
            sent_date=occurrence_date,
        )

    assert sent is False
    enqueue_push.assert_not_called()
    assert SentNotification.objects.filter(student=student).count() == 1


@pytest.mark.django_db
def test_enqueue_failure_is_persisted_and_same_claim_can_retry():
    club, schedule, student, template = _delivery_context()
    occurrence_date = date(2026, 7, 15)

    with patch(
        "apps.notifications.services.async_task",
        side_effect=[RuntimeError("queue unavailable"), "task-id"],
    ) as enqueue_push:
        first = services.send_occurrence_student_notification(
            club=club,
            student=student,
            notification_type="training_reminder",
            context={"time": "18:00"},
            template=template,
            occurrence_schedule_id=schedule.id,
            occurrence_date=occurrence_date,
            delivery_stage="one_hour",
            sent_date=occurrence_date,
        )
        failed = SentNotification.objects.get(student=student)
        assert failed.delivery_state == "failed"

        second = services.send_occurrence_student_notification(
            club=club,
            student=student,
            notification_type="training_reminder",
            context={"time": "18:00"},
            template=template,
            occurrence_schedule_id=schedule.id,
            occurrence_date=occurrence_date,
            delivery_stage="one_hour",
            sent_date=occurrence_date,
        )

    assert first is False
    assert second is True
    assert enqueue_push.call_count == 2
    rows = SentNotification.objects.filter(student=student)
    assert rows.count() == 1
    assert rows.get().delivery_state == "queued"


@pytest.mark.django_db
def test_no_active_device_keeps_occurrence_claim_retryable():
    club, schedule, student, template = _delivery_context()
    PushSubscription.objects.filter(user=student.user).update(is_active=False)
    occurrence_date = date(2026, 7, 15)

    sent = services.send_occurrence_student_notification(
        club=club,
        student=student,
        notification_type="training_reminder",
        context={"time": "18:00"},
        template=template,
        occurrence_schedule_id=schedule.id,
        occurrence_date=occurrence_date,
        delivery_stage="one_hour",
        sent_date=occurrence_date,
    )

    assert sent is False
    assert SentNotification.objects.get(student=student).delivery_state == "failed"


@pytest.mark.django_db
def test_training_reminder_opt_out_skips_claim_and_enqueue_for_24h_stage():
    club, schedule, student, template = _delivery_context()
    template.trigger_type = "training_reminder_24h"
    template.save(update_fields=["trigger_type", "updated_at"])
    NotificationPreference.objects.create(
        user=student.user,
        disabled_categories=["training_reminders"],
    )
    occurrence_date = date(2026, 7, 15)

    with patch("apps.notifications.services.async_task") as enqueue_push:
        sent = services.send_occurrence_student_notification(
            club=club,
            student=student,
            notification_type="training_reminder_24h",
            context={"time": "18:00"},
            template=template,
            occurrence_schedule_id=schedule.id,
            occurrence_date=occurrence_date,
            delivery_stage="twenty_four_hour",
            sent_date=occurrence_date,
        )

    assert sent is False
    enqueue_push.assert_not_called()
    assert not SentNotification.objects.filter(student=student).exists()


@pytest.mark.django_db
def test_user_level_occurrence_task_fans_out_after_single_queue_claim():
    _club, _schedule, student, _template = _delivery_context()
    PushSubscriptionFactory(user=student.user)

    with patch("apps.notifications.tasks.send_push_task") as send_device_push:
        from apps.notifications.tasks import send_push_to_user_task

        delivered = send_push_to_user_task(
            student.user_id,
            "Title",
            "Body",
            "/student/schedule",
        )

    assert delivered == 2
    assert send_device_push.call_count == 2
    assert {
        call.args[0] for call in send_device_push.call_args_list
    } == set(
        PushSubscription.objects.filter(
            user=student.user,
            is_active=True,
        ).values_list("id", flat=True)
    )
