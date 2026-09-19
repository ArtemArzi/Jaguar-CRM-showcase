from __future__ import annotations

import logging
import re
from datetime import date as date_type
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from django_q.tasks import async_task

from apps.common.exceptions import BusinessLogicError
from apps.notifications.models import MassNotification, PushSubscription
from apps.notifications.routes import STUDENT_HOME_URL, STUDENT_SCHEDULE_URL, trainer_task_url

logger = logging.getLogger(__name__)

PRIORITY_CRITICAL = "critical"  # debt, expiry -- always send
PRIORITY_MEDIUM = "medium"      # training reminder, follow-up
PRIORITY_LOW = "low"            # win-back, "we miss you"
TRAINER_RETENTION_TASK = "trainer_retention_task"


def can_send_push(
    *,
    student_id: int,
    club_id: int,
    priority: str,
    now: datetime | None = None,
) -> bool:
    """Check anti-spam budget per D-14. Critical always passes."""
    if priority == PRIORITY_CRITICAL:
        return True

    from apps.clubs.models import ClubSettings
    from apps.notifications.models import SentNotification

    try:
        settings = ClubSettings.objects.select_related("club").get(club_id=club_id)
    except ClubSettings.DoesNotExist:
        return True  # no settings = no limit

    from apps.clubs.timezones import club_localdate

    week_ago = club_localdate(settings.club, now) - timedelta(days=7)
    sent_count = SentNotification.objects.filter(
        club_id=club_id,
        student_id=student_id,
        sent_date__gte=week_ago,
        delivery_state=SentNotification.DeliveryState.QUEUED,
    ).count()
    return sent_count < settings.max_push_per_week


def is_quiet_hours(*, club_id: int, now: datetime | None = None) -> bool:
    """Check if current time is within quiet hours per D-15. Default 21:00-09:00."""
    from apps.clubs.models import ClubSettings

    try:
        settings = ClubSettings.objects.select_related("club").get(club_id=club_id)
    except ClubSettings.DoesNotExist:
        return False

    from apps.clubs.timezones import club_localtime

    now_time = club_localtime(settings.club, now).time()
    start = settings.quiet_hours_start  # e.g. 21:00
    end = settings.quiet_hours_end      # e.g. 09:00

    if start > end:
        # Overnight range: 21:00 - 09:00
        return now_time >= start or now_time < end
    else:
        return start <= now_time < end


def get_habitual_schedules(*, student_id: int, club_id: int, weeks: int = 4) -> list[int]:
    """Return schedule IDs student attended >= 50% of last N weeks.
    Empty list for new students (< 2 checkins for any schedule).
    """
    from apps.attendance.models import Checkin

    cutoff = timezone.now().date() - timedelta(weeks=weeks)
    checkins = (
        Checkin.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            date__gte=cutoff,
            deleted_at__isnull=True,
        )
        .values("schedule_id")
        .annotate(count=Count("id"))
    )
    threshold = weeks * 0.5  # at least 2 out of 4 weeks
    return [c["schedule_id"] for c in checkins if c["count"] >= threshold]


def subscribe_device(*, user_id: int, endpoint: str, key_p256dh: str, key_auth: str) -> PushSubscription:
    sub = PushSubscription.objects.filter(endpoint=endpoint).first()
    if sub is not None:
        if sub.user_id != user_id:
            raise BusinessLogicError(
                "Push subscription endpoint belongs to another user",
                code="push_subscription_not_owned",
            )
        sub.key_p256dh = key_p256dh
        sub.key_auth = key_auth
        sub.save(update_fields=["key_p256dh", "key_auth", "updated_at"])
    else:
        try:
            with transaction.atomic():
                sub = PushSubscription.objects.create(
                    user_id=user_id,
                    endpoint=endpoint,
                    key_p256dh=key_p256dh,
                    key_auth=key_auth,
                )
        except IntegrityError:
            sub = PushSubscription.objects.get(endpoint=endpoint)
            if sub.user_id != user_id:
                raise BusinessLogicError(
                    "Push subscription endpoint belongs to another user",
                    code="push_subscription_not_owned",
                )
            sub.key_p256dh = key_p256dh
            sub.key_auth = key_auth
            sub.save(update_fields=["key_p256dh", "key_auth", "updated_at"])
    logger.info("device_subscribed", extra={"user_id": user_id, "sub_id": sub.id})
    return sub


def unsubscribe_device(*, user_id: int, endpoint: str) -> None:
    deleted, _ = PushSubscription.objects.filter(user_id=user_id, endpoint=endpoint).delete()
    logger.info("device_unsubscribed", extra={"user_id": user_id, "deleted": deleted})


def send_push_to_user(
    *,
    user_id: int,
    title: str,
    body: str,
    url: str | None = None,
    actions: list[dict] | None = None,
    data: dict | None = None,
) -> int:
    subs = list(PushSubscription.objects.filter(user_id=user_id, is_active=True))
    for sub in subs:
        async_task(
            "apps.notifications.tasks.send_push_task",
            sub.id,
            title,
            body,
            url,
            actions,
            data,
        )
    logger.info(
        "push_queued",
        extra={"user_id": user_id, "sub_count": len(subs)},
    )
    return len(subs)


# Deep link URLs per notification type
DEEP_LINK_MAP: dict[str, str] = {
    "sub_expiry_7d": STUDENT_HOME_URL,
    "sub_expiry_3d": STUDENT_HOME_URL,
    "sub_expiry_1d": STUDENT_HOME_URL,
    "trainings_left_2": STUDENT_HOME_URL,
    "trainings_last": STUDENT_HOME_URL,
    "training_reminder": STUDENT_SCHEDULE_URL,
    "training_reminder_24h": STUDENT_SCHEDULE_URL,
    "missed_training": STUDENT_SCHEDULE_URL,
}

# Maps frontend UI category keys to backend trigger types they cover
CATEGORY_MAP: dict[str, list[str]] = {
    "training_reminders": [
        "training_reminder",
        "training_reminder_24h",
        "missed_training",
    ],
    "subscription_alerts": [
        "sub_expiry_7d", "sub_expiry_3d", "sub_expiry_1d",
        "trainings_left_2", "trainings_last", "parent_sub_expiry",
    ],
    "child_checkin": ["parent_checkin", "parent_checkin_cancelled", "parent_grade_up"],
    "feedback_surveys": ["trial_feedback", "churned_survey"],
    "trainer_tasks": [TRAINER_RETENTION_TASK],
    "schedule_updates": [],  # No backend triggers yet
}

# Reverse map: trigger_type -> UI category key
TRIGGER_TO_CATEGORY: dict[str, str] = {
    trigger: cat for cat, triggers in CATEGORY_MAP.items() for trigger in triggers
}

def user_disabled_notification(*, user_id: int, notification_type: str) -> bool:
    from apps.notifications.models import NotificationPreference

    pref = NotificationPreference.objects.filter(user_id=user_id).first()
    if not pref:
        return False

    ui_category = TRIGGER_TO_CATEGORY.get(notification_type)
    return bool(ui_category and ui_category in pref.disabled_categories)


def _user_disabled_notification(*, user_id: int, notification_type: str) -> bool:
    return user_disabled_notification(user_id=user_id, notification_type=notification_type)


def get_mass_notification_recipient_ids(
    *,
    club_id: int,
    segment_type: str,
    segment_filter: dict,
) -> list[int]:
    from apps.notifications.selectors import get_recipients_for_segment

    user_ids = get_recipients_for_segment(
        club_id=club_id,
        segment_type=segment_type,
        segment_filter=segment_filter,
    )
    active_user_ids = set(
        PushSubscription.objects.filter(user_id__in=user_ids, is_active=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    return [
        user_id
        for user_id in user_ids
        if user_id in active_user_ids
    ]


def _claim_occurrence_notification(
    *,
    club,
    student,
    notification_type: str,
    sent_date: date_type,
    occurrence_schedule_id: int,
    occurrence_date: date_type,
    delivery_stage: str,
):
    from apps.notifications.models import SentNotification

    identity = {
        "club": club,
        "student": student,
        "notification_type": notification_type,
        "occurrence_schedule_id": occurrence_schedule_id,
        "occurrence_date": occurrence_date,
        "delivery_stage": delivery_stage,
    }
    try:
        with transaction.atomic():
            return SentNotification.objects.create(
                **identity,
                sent_date=sent_date,
                delivery_state=SentNotification.DeliveryState.PENDING,
            )
    except IntegrityError:
        claim = SentNotification.objects.for_club(club).filter(**identity).first()
        if claim is None:
            logger.warning(
                "occurrence_notification_claim_race_unresolved",
                extra={
                    "club_id": club.id,
                    "student_id": student.id,
                    "schedule_id": occurrence_schedule_id,
                    "delivery_stage": delivery_stage,
                },
            )
            return None
        reclaimed = SentNotification.objects.for_club(club).filter(
            id=claim.id,
            delivery_state=SentNotification.DeliveryState.FAILED,
        ).update(
            delivery_state=SentNotification.DeliveryState.PENDING,
            sent_date=sent_date,
        )
        if reclaimed != 1:
            return None
        claim.delivery_state = SentNotification.DeliveryState.PENDING
        claim.sent_date = sent_date
        return claim


def send_occurrence_student_notification(
    *,
    club,
    student,
    notification_type: str,
    context: dict,
    template,
    occurrence_schedule_id: int,
    occurrence_date: date_type,
    delivery_stage: str,
    sent_date: date_type,
) -> bool:
    """Claim and enqueue one student notification for one occurrence stage."""
    from apps.notifications.models import SentNotification

    if not template or not template.is_enabled or not student.user_id:
        return False
    if _user_disabled_notification(
        user_id=student.user_id,
        notification_type=notification_type,
    ):
        return False

    claim = _claim_occurrence_notification(
        club=club,
        student=student,
        notification_type=notification_type,
        sent_date=sent_date,
        occurrence_schedule_id=occurrence_schedule_id,
        occurrence_date=occurrence_date,
        delivery_stage=delivery_stage,
    )
    if claim is None:
        return False

    title = render_template(template_str=template.title_template, context=context)
    body = render_template(template_str=template.body_template, context=context)
    if not PushSubscription.objects.filter(
        user_id=student.user_id,
        is_active=True,
    ).exists():
        claim.delivery_state = SentNotification.DeliveryState.FAILED
        claim.save(update_fields=["delivery_state", "updated_at"])
        return False
    try:
        async_task(
            "apps.notifications.tasks.send_push_to_user_task",
            student.user_id,
            title,
            body,
            DEEP_LINK_MAP.get(notification_type),
        )
    except Exception:
        claim.delivery_state = SentNotification.DeliveryState.FAILED
        claim.save(update_fields=["delivery_state", "updated_at"])
        logger.exception(
            "occurrence_notification_enqueue_failed",
            extra={
                "club_id": club.id,
                "student_id": student.id,
                "schedule_id": occurrence_schedule_id,
                "delivery_stage": delivery_stage,
            },
        )
        return False

    claim.delivery_state = SentNotification.DeliveryState.QUEUED
    claim.save(update_fields=["delivery_state", "updated_at"])
    logger.info(
        "occurrence_notification_queued",
        extra={
            "club_id": club.id,
            "student_id": student.id,
            "schedule_id": occurrence_schedule_id,
            "occurrence_date": occurrence_date.isoformat(),
            "delivery_stage": delivery_stage,
        },
    )
    return True


def send_student_notification(
    *,
    club,
    student,
    notification_type: str,
    context: dict,
    template=None,
    sent_today: set | None = None,
) -> bool:
    """Send push notification to a student with dedup and template rendering.
    Returns True if sent, False if skipped (dedup, disabled, no user, opted out).

    Optional params for batch optimization:
    - template: pre-loaded NotificationTemplate (avoids per-call DB lookup)
    - sent_today: set of (student_id, notification_type) already sent today (in-memory dedup)
    """
    from apps.notifications.models import NotificationTemplate, SentNotification

    today = timezone.now().date()

    # 1. Dedup check (in-memory if provided, else DB)
    dedup_key = (student.id, notification_type)
    if sent_today is not None:
        if dedup_key in sent_today:
            return False
    else:
        if SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=notification_type,
            sent_date=today,
            occurrence_schedule__isnull=True,
        ).exists():
            return False

    # 2. Template lookup (use pre-loaded if provided)
    if template is None:
        template = (
            NotificationTemplate.objects.for_club(club).filter(trigger_type=notification_type, is_enabled=True).first()
        )
    if not template:
        return False

    # 3. Render template
    title = render_template(template_str=template.title_template, context=context)
    body = render_template(template_str=template.body_template, context=context)

    # 4. Find student's user directly via Student.user FK
    if not student.user_id:
        logger.info("student_notification_skipped_no_user", extra={"student_id": student.id, "club_id": club.id})
        return False

    target_user_id = student.user_id

    # 4b. Category opt-out check (D-05) -- translate trigger_type to UI category
    if _user_disabled_notification(user_id=target_user_id, notification_type=notification_type):
        return False

    # 5. Send push first — record dedup only on success
    deep_link = DEEP_LINK_MAP.get(notification_type)
    try:
        send_push_to_user(user_id=target_user_id, title=title, body=body, url=deep_link)
    except Exception:
        logger.exception(
            "student_notification_push_failed",
            extra={"student_id": student.id, "club_id": club.id, "notification_type": notification_type},
        )
        return False

    # 6. Record sent notification (dedup for future calls) — only after successful push
    try:
        SentNotification.objects.create(
            club=club,
            student=student,
            notification_type=notification_type,
            sent_date=today,
        )
    except IntegrityError:
        logger.info(
            "student_notification_race_skipped",
            extra={"student_id": student.id, "club_id": club.id, "notification_type": notification_type},
        )
        return False
    if sent_today is not None:
        sent_today.add(dedup_key)

    logger.info(
        "student_notification_sent",
        extra={
            "student_id": student.id,
            "club_id": club.id,
            "notification_type": notification_type,
        },
    )
    return True


def send_parent_notification(
    *,
    club,
    student,
    notification_type: str,
    context: dict,
    fallback_title: str,
    fallback_body: str,
    url: str | None = None,
    template=None,
    sent_today: set | None = None,
    record_type: str | None = None,
    use_fallback_content: bool = False,
) -> bool:
    """Send parent-scoped child notification with opt-out and per-day dedup."""
    from apps.notifications.models import NotificationTemplate, SentNotification

    if not student.is_child or not student.parent_user_id:
        return False

    today = timezone.now().date()
    sent_notification_type = record_type or notification_type
    dedup_key = (student.id, sent_notification_type)
    if sent_today is not None:
        if dedup_key in sent_today:
            return False
    else:
        if SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type=sent_notification_type,
            sent_date=today,
            occurrence_schedule__isnull=True,
        ).exists():
            return False

    if _user_disabled_notification(user_id=student.parent_user_id, notification_type=notification_type):
        return False

    if template is None:
        template = (
            NotificationTemplate.objects.for_club(club)
            .filter(trigger_type=notification_type)
            .first()
        )

    if template:
        if not template.is_enabled:
            return False
        title = fallback_title if use_fallback_content else render_template(
            template_str=template.title_template, context=context,
        )
        body = fallback_body if use_fallback_content else render_template(
            template_str=template.body_template, context=context,
        )
    else:
        title = fallback_title
        body = fallback_body

    try:
        send_push_to_user(user_id=student.parent_user_id, title=title, body=body, url=url)
    except Exception:
        logger.exception(
            "parent_notification_push_failed",
            extra={
                "student_id": student.id,
                "club_id": club.id,
                "notification_type": notification_type,
            },
        )
        return False

    try:
        SentNotification.objects.create(
            club=club,
            student=student,
            notification_type=sent_notification_type,
            sent_date=today,
        )
    except IntegrityError:
        logger.info(
            "parent_notification_race_skipped",
            extra={
                "student_id": student.id,
                "club_id": club.id,
                "notification_type": notification_type,
                "record_type": sent_notification_type,
            },
        )
        return False

    if sent_today is not None:
        sent_today.add(dedup_key)

    logger.info(
        "parent_notification_sent",
        extra={
            "student_id": student.id,
            "parent_user_id": student.parent_user_id,
            "club_id": club.id,
            "notification_type": notification_type,
            "record_type": sent_notification_type,
        },
    )
    return True


def send_trainer_retention_task_notification(*, club, task) -> bool:
    """Notify the assigned trainer about a newly created retention task.

    The push payload intentionally avoids raw phone/email or full student details;
    the trainer opens the task deep link to see authorized data in the PWA.
    """
    from apps.notifications.models import NotificationTemplate, SentNotification

    trainer_user_id = task.trainer.user_id if task.trainer_id else None
    if not trainer_user_id:
        logger.info(
            "trainer_retention_notification_skipped_no_user",
            extra={"task_id": task.id, "club_id": club.id, "trainer_id": task.trainer_id},
        )
        return False

    today = timezone.now().date()
    if SentNotification.objects.filter(
        club=club,
        student_id=task.student_id,
        notification_type=TRAINER_RETENTION_TASK,
        sent_date=today,
        occurrence_schedule__isnull=True,
    ).exists():
        return False

    if _user_disabled_notification(
        user_id=trainer_user_id,
        notification_type=TRAINER_RETENTION_TASK,
    ):
        return False

    template = (
        NotificationTemplate.objects.for_club(club)
        .filter(trigger_type=NotificationTemplate.TriggerType.FOLLOW_UP)
        .first()
    )
    context = {"name": str(task.student)}
    if template:
        if not template.is_enabled:
            return False
        title = render_template(template_str=template.title_template, context=context)
        body = render_template(template_str=template.body_template, context=context)
    else:
        title = "Новая задача по удержанию"
        body = "Ученик давно не был на занятии. Откройте задачу в приложении тренера."

    try:
        send_push_to_user(
            user_id=trainer_user_id,
            title=title,
            body=body,
            url=trainer_task_url(task.id),
            data={
                "type": TRAINER_RETENTION_TASK,
                "task_id": task.id,
                "student_id": task.student_id,
            },
        )
    except Exception:
        logger.exception(
            "trainer_retention_notification_push_failed",
            extra={"task_id": task.id, "club_id": club.id, "trainer_id": task.trainer_id},
        )
        return False

    try:
        SentNotification.objects.create(
            club=club,
            student_id=task.student_id,
            notification_type=TRAINER_RETENTION_TASK,
            sent_date=today,
        )
    except IntegrityError:
        logger.info(
            "trainer_retention_notification_race_skipped",
            extra={"task_id": task.id, "club_id": club.id, "trainer_id": task.trainer_id},
        )
        return False

    logger.info(
        "trainer_retention_notification_sent",
        extra={
            "task_id": task.id,
            "club_id": club.id,
            "trainer_id": task.trainer_id,
            "student_id": task.student_id,
        },
    )
    return True


def render_template(*, template_str: str, context: dict) -> str:
    """Safe template rendering — only allows simple {key} placeholders, no attribute/index access."""
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return str(context.get(key, match.group(0)))
    try:
        return re.sub(r"\{(\w+)\}", _replace, template_str)
    except Exception:
        return template_str


_UPDATE_NOTIFICATION_TEMPLATE_FIELDS = frozenset({"title_template", "body_template", "is_enabled", "days_before"})


def update_notification_template(*, template_id: int, club_id: int, **fields):
    bad = set(fields) - _UPDATE_NOTIFICATION_TEMPLATE_FIELDS
    if bad:
        raise BusinessLogicError(f"Fields not allowed: {bad}", code="invalid_fields")
    for field in ("title_template", "body_template"):
        if field not in fields:
            continue
        value = fields[field]
        if not isinstance(value, str) or not value.strip():
            raise BusinessLogicError("Заголовок и текст обязательны", code="invalid_template")
        fields[field] = value.strip()
    if "days_before" in fields:
        value = fields["days_before"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 365
        ):
            raise BusinessLogicError("Дней до окончания должно быть от 1 до 365", code="invalid_template")
    from apps.notifications.models import NotificationTemplate

    template = NotificationTemplate.objects.for_club(club_id).get(id=template_id)
    for attr, value in fields.items():
        setattr(template, attr, value)
    template.save(update_fields=[*fields.keys(), "updated_at"])
    logger.info("notification_template_updated", extra={"template_id": template_id, "club_id": club_id})
    return template


def send_mass_notification(
    *,
    club_id: int,
    text: str,
    segment_type: str,
    segment_filter: dict,
    sent_by_id: int,
) -> MassNotification:
    user_ids = get_mass_notification_recipient_ids(
        club_id=club_id,
        segment_type=segment_type,
        segment_filter=segment_filter,
    )

    notif = MassNotification.objects.create(
        club_id=club_id,
        text=text,
        segment_type=segment_type,
        segment_filter=segment_filter,
        recipient_count=len(user_ids),
        sent_by_id=sent_by_id,
    )

    # Fan out push to each recipient's subscriptions
    subs = list(
        PushSubscription.objects.filter(user_id__in=user_ids, is_active=True).order_by("id")
    )
    for sub in subs:
        async_task(
            "apps.notifications.tasks.send_push_task",
            sub.id,
            "Уведомление",  # title
            text,            # body
        )

    logger.info(
        "mass_notification_sent",
        extra={
            "notif_id": notif.id,
            "club_id": club_id,
            "recipient_count": len(user_ids),
            "sub_count": len(subs),
        },
    )
    return notif
