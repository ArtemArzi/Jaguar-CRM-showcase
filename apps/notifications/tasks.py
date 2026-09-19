from __future__ import annotations

import json
import logging
from datetime import datetime as datetime_type
from datetime import timedelta

from django.conf import settings as django_settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

from apps.notifications.routes import parent_child_url
from apps.notifications.services import (
    PRIORITY_MEDIUM,
    can_send_push,
    is_quiet_hours,
    send_parent_notification,
    send_student_notification,
)

logger = logging.getLogger(__name__)


def send_push_task(
    subscription_id: int,
    title: str,
    body: str,
    url: str | None = None,
    actions: list[dict] | None = None,
    data: dict | None = None,
) -> None:
    from apps.notifications.models import PushSubscription

    try:
        sub = PushSubscription.objects.get(id=subscription_id)
    except PushSubscription.DoesNotExist:
        return

    payload_dict: dict = {"title": title, "body": body, "url": url}
    if data:
        payload_dict["tag"] = data.get("tag")
        payload_dict["icon"] = data.get("icon")
        payload_dict["data"] = data
    if actions:
        payload_dict["actions"] = actions
    payload = json.dumps(payload_dict)
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.key_p256dh, "auth": sub.key_auth},
            },
            data=payload,
            vapid_private_key=django_settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{django_settings.VAPID_ADMIN_EMAIL}"},
        )
    except WebPushException as e:
        status_code = getattr(e.response, "status_code", None)
        if status_code in (404, 410):
            sub.delete()
            logger.info("push_subscription_expired", extra={"sub_id": subscription_id})
        else:
            logger.error(
                "push_send_failed",
                extra={
                    "sub_id": subscription_id,
                    "status_code": status_code,
                    "exception_class": e.__class__.__name__,
                },
            )


def send_push_to_user_task(
    user_id: int,
    title: str,
    body: str,
    url: str | None = None,
    actions: list[dict] | None = None,
    data: dict | None = None,
) -> int:
    """Deliver one queued user-level push to every currently active device."""
    from apps.notifications.models import PushSubscription

    subscription_ids = list(
        PushSubscription.objects.filter(user_id=user_id, is_active=True)
        .order_by("id")
        .values_list("id", flat=True)
    )
    for subscription_id in subscription_ids:
        send_push_task(
            subscription_id,
            title,
            body,
            url,
            actions,
            data,
        )
    logger.info(
        "user_push_dispatched",
        extra={"user_id": user_id, "sub_count": len(subscription_ids)},
    )
    return len(subscription_ids)


def check_subscription_expiry() -> dict:
    """Daily task: check all clubs for subscriptions nearing expiry.
    Sends push for 7d, 3d, 1d thresholds (or custom per-club thresholds).
    """
    from apps.billing.models import Subscription
    from apps.clubs.models import Club
    from apps.notifications.models import NotificationTemplate

    results = {"clubs_checked": 0, "notifications_sent": 0}

    default_thresholds = {
        NotificationTemplate.TriggerType.SUB_EXPIRY_7D: 7,
        NotificationTemplate.TriggerType.SUB_EXPIRY_3D: 3,
        NotificationTemplate.TriggerType.SUB_EXPIRY_1D: 1,
    }

    from apps.notifications.models import SentNotification

    for club in Club.objects.filter(is_active=True).only("id", "timezone"):
        results["clubs_checked"] += 1

        templates = {
            t.trigger_type: t
            for t in NotificationTemplate.objects.for_club(club).filter(
                is_enabled=True,
                trigger_type__in=default_thresholds.keys(),
            )
        }

        # Pre-load dedup set for this club (membership stays per-student — see KNOWN LIMITATION)
        today = timezone.now().date()
        sent_today = set(
            SentNotification.objects.filter(club=club, sent_date=today).values_list("student_id", "notification_type")
        )

        for trigger_type, default_days in default_thresholds.items():
            template = templates.get(trigger_type)
            if not template:
                continue

            days = template.days_before if template.days_before is not None else default_days
            target_date = timezone.now() + timedelta(days=days)
            target_date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            target_date_end = target_date.replace(hour=23, minute=59, second=59)

            expiring_subs = (
                Subscription.objects.for_club(club)
                .filter(
                    status=Subscription.Status.ACTIVE,
                    expires_at__gte=target_date_start,
                    expires_at__lte=target_date_end,
                    deleted_at__isnull=True,
                )
                .select_related("student")
            )

            for sub in expiring_subs:
                student = sub.student
                context = {
                    "name": str(student),
                    "days": str(days),
                    "trainings_left": str(sub.trainings_left or 0),
                }
                sent = send_student_notification(
                    club=club,
                    student=student,
                    notification_type=trigger_type,
                    context=context,
                    template=template,
                    sent_today=sent_today,
                )
                if sent:
                    results["notifications_sent"] += 1

                # Parent push for child students
                if student.is_child and student.parent_user_id:
                    body = (
                        f"Абонемент {student.first_name} истекает через {days} дн. "
                        f"Осталось {context['trainings_left']} тренировок."
                    )
                    send_parent_notification(
                        club=club,
                        student=student,
                        notification_type=NotificationTemplate.TriggerType.PARENT_SUB_EXPIRY,
                        context=context,
                        fallback_title="Абонемент ребёнка",
                        fallback_body=body,
                        url=parent_child_url(student.id),
                    )

    logger.info("check_subscription_expiry_complete", extra=results)
    return results


def check_trainings_left_push(checkin_id: int, club_id: int) -> None:
    """Called from checkin cascade. Check if trainings_left is 2 or 0 and send push."""
    from apps.attendance.models import Checkin
    from apps.notifications.models import NotificationTemplate

    checkin = (
        Checkin.objects.select_related("student", "club", "subscription__tariff")
        .filter(
            id=checkin_id,
            club_id=club_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
    )
    if checkin is None:
        return

    club = checkin.club

    sub = checkin.subscription
    if not sub or sub.trainings_left is None:
        return

    context = {
        "name": str(checkin.student),
        "days": str(max(0, (sub.expires_at.date() - timezone.now().date()).days)) if sub.expires_at else "0",
        "trainings_left": str(sub.trainings_left),
    }

    if sub.trainings_left == 2:
        send_student_notification(
            club=club,
            student=checkin.student,
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
            context=context,
        )
    elif sub.trainings_left <= 0:
        send_student_notification(
            club=club,
            student=checkin.student,
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LAST,
            context=context,
        )


def _occurrences_in_offset_window(*, club, now, lead_time: timedelta):
    from apps.attendance.selectors import get_schedule_occurrences_for_range
    from apps.clubs.timezones import club_localtime, club_zoneinfo

    local_now = club_localtime(club, now)
    window_start = local_now + lead_time - timedelta(minutes=30)
    window_end = local_now + lead_time + timedelta(minutes=30)
    occurrences_by_date = get_schedule_occurrences_for_range(
        club=club,
        date_from=window_start.date(),
        date_to=window_end.date(),
    )
    club_timezone = club_zoneinfo(club)
    return [
        occurrence
        for occurrences in occurrences_by_date.values()
        for occurrence in occurrences
        if window_start
        <= timezone.make_aware(
            datetime_type.combine(
                occurrence.effective_date,
                occurrence.effective_start_time,
            ),
            club_timezone,
        )
        <= window_end
    ]


def _send_training_reminder_stage(
    *,
    now,
    lead_time: timedelta,
    notification_type: str,
    delivery_stage: str,
) -> dict:
    from apps.clubs.models import Club
    from apps.clubs.timezones import club_localdate
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.selectors import get_students_for_training_reminder
    from apps.notifications.services import send_occurrence_student_notification

    results = {"clubs_checked": 0, "reminders_sent": 0}
    for club in Club.objects.filter(is_active=True).only("id", "timezone"):
        results["clubs_checked"] += 1
        if is_quiet_hours(club_id=club.id, now=now):
            continue
        template = (
            NotificationTemplate.objects.for_club(club)
            .filter(trigger_type=notification_type, is_enabled=True)
            .first()
        )
        if template is None:
            continue

        sent_date = club_localdate(club, now)
        for occurrence in _occurrences_in_offset_window(
            club=club,
            now=now,
            lead_time=lead_time,
        ):
            students = get_students_for_training_reminder(
                club=club,
                schedule_id=occurrence.schedule_id,
                target_date=occurrence.effective_date,
            )
            for student in students:
                if not can_send_push(
                    student_id=student.id,
                    club_id=club.id,
                    priority=PRIORITY_MEDIUM,
                    now=now,
                ):
                    continue
                sent = send_occurrence_student_notification(
                    club=club,
                    student=student,
                    notification_type=notification_type,
                    context={
                        "name": str(student),
                        "group": occurrence.group_name,
                        "time": occurrence.effective_start_time.strftime("%H:%M"),
                    },
                    template=template,
                    occurrence_schedule_id=occurrence.schedule_id,
                    occurrence_date=occurrence.occurrence_date,
                    delivery_stage=delivery_stage,
                    sent_date=sent_date,
                )
                if sent:
                    results["reminders_sent"] += 1
    return results


def send_training_reminders() -> dict:
    """Hourly task: queue the one-hour stage for canonical club-local occurrences."""
    from apps.notifications.models import NotificationTemplate, SentNotification

    results = _send_training_reminder_stage(
        now=timezone.now(),
        lead_time=timedelta(hours=1),
        notification_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
        delivery_stage=SentNotification.DeliveryStage.ONE_HOUR,
    )
    logger.info("send_training_reminders_complete", extra=results)
    return results


def send_training_reminders_24h() -> dict:
    """Hourly task: queue the 24-hour stage for canonical club-local occurrences."""
    from apps.notifications.models import NotificationTemplate, SentNotification

    results = _send_training_reminder_stage(
        now=timezone.now(),
        lead_time=timedelta(hours=24),
        notification_type=NotificationTemplate.TriggerType.TRAINING_REMINDER_24H,
        delivery_stage=SentNotification.DeliveryStage.TWENTY_FOUR_HOUR,
    )
    logger.info("send_training_reminders_24h_complete", extra=results)
    return results


def check_missed_trainings() -> dict:
    """Hourly task: queue yesterday's missed canonical occurrences after quiet hours."""
    from apps.attendance.models import Checkin
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.clubs.models import Club
    from apps.clubs.timezones import club_localdate
    from apps.notifications.models import NotificationTemplate, SentNotification
    from apps.notifications.selectors import get_students_for_training_reminder
    from apps.notifications.services import send_occurrence_student_notification

    results = {"clubs_checked": 0, "pushes_sent": 0}
    now = timezone.now()
    for club in Club.objects.filter(is_active=True).only("id", "timezone"):
        results["clubs_checked"] += 1
        if is_quiet_hours(club_id=club.id, now=now):
            continue
        template = (
            NotificationTemplate.objects.for_club(club)
            .filter(
                trigger_type=NotificationTemplate.TriggerType.MISSED_TRAINING,
                is_enabled=True,
            )
            .first()
        )
        if template is None:
            continue

        sent_date = club_localdate(club, now)
        missed_date = sent_date - timedelta(days=1)
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=missed_date,
        ):
            checked_in_ids = set(
                Checkin.objects.for_club(club)
                .filter(
                    schedule_id=occurrence.schedule_id,
                    date=occurrence.effective_date,
                    deleted_at__isnull=True,
                    cancelled_at__isnull=True,
                )
                .values_list("student_id", flat=True)
            )
            expected_students = get_students_for_training_reminder(
                club=club,
                schedule_id=occurrence.schedule_id,
                target_date=occurrence.effective_date,
            )
            for student in expected_students:
                if student.id in checked_in_ids:
                    continue
                if not can_send_push(
                    student_id=student.id,
                    club_id=club.id,
                    priority=PRIORITY_MEDIUM,
                    now=now,
                ):
                    continue
                sent = send_occurrence_student_notification(
                    club=club,
                    student=student,
                    notification_type=NotificationTemplate.TriggerType.MISSED_TRAINING,
                    context={
                        "name": student.first_name,
                        "missed_day": occurrence.effective_date.strftime("%A"),
                        "group": occurrence.group_name,
                    },
                    template=template,
                    occurrence_schedule_id=occurrence.schedule_id,
                    occurrence_date=occurrence.occurrence_date,
                    delivery_stage=SentNotification.DeliveryStage.MISSED,
                    sent_date=sent_date,
                )
                if sent:
                    results["pushes_sent"] += 1

    logger.info("check_missed_trainings_complete", extra=results)
    return results
