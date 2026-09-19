from django.conf import settings
from django.db import models

from apps.common.models import BaseModel, TenantMixin


class PushSubscription(BaseModel):
    """Browser push subscription. Not tenant-scoped -- user may be in multiple clubs."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500, unique=True)
    key_p256dh = models.CharField(max_length=200)
    key_auth = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["user"])]

    def __str__(self):
        return f"PushSub(id={self.id}, user_id={self.user_id}, active={self.is_active})"


class MassNotification(TenantMixin):
    text = models.TextField()
    segment_type = models.CharField(max_length=50)
    segment_filter = models.JSONField(default=dict)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sent_notifications",
    )

    def __str__(self):
        return f"MassNotif({self.id}): {self.segment_type} ({self.recipient_count})"


class NotificationTemplate(TenantMixin):
    """Per-club notification templates. Uses TenantMixin for .for_club() scoping."""

    class TriggerType(models.TextChoices):
        SUB_EXPIRY_7D = "sub_expiry_7d", "Абонемент истекает через 7 дней"
        SUB_EXPIRY_3D = "sub_expiry_3d", "Абонемент истекает через 3 дня"
        SUB_EXPIRY_1D = "sub_expiry_1d", "Абонемент истекает завтра"
        TRAININGS_LEFT_2 = "trainings_left_2", "Осталось 2 занятия"
        TRAININGS_LAST = "trainings_last", "Последнее занятие"
        # Родительские уведомления
        PARENT_CHECKIN = "parent_checkin", "Родитель: ребёнок на тренировке"
        PARENT_SUB_EXPIRY = "parent_sub_expiry", "Родитель: абонемент ребёнка истекает"
        PARENT_GRADE_UP = "parent_grade_up", "Родитель: повышение грейда"
        TRIAL_FEEDBACK = "trial_feedback", "Отзыв после пробной тренировки"
        # Вовлечение
        TRAINING_REMINDER = "training_reminder", "Напоминание о тренировке (за 1 час)"
        TRAINING_REMINDER_24H = "training_reminder_24h", "Напоминание о тренировке (за 24 часа)"
        MISSED_TRAINING = "missed_training", "Пропуск привычной тренировки"
        FOLLOW_UP = "follow_up", "Напоминание по задаче"
        CHURNED_SURVEY = "churned_survey", "Опрос ушедшего ученика"

    trigger_type = models.CharField(max_length=30, choices=TriggerType.choices)
    title_template = models.CharField(max_length=200, default="{name}, напоминание")
    body_template = models.CharField(max_length=500)
    is_enabled = models.BooleanField(default=True)
    days_before = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "trigger_type"],
                name="unique_template_per_club_trigger",
            ),
        ]

    def __str__(self):
        return f"{self.club} - {self.trigger_type}"


class SentNotification(TenantMixin):
    class DeliveryStage(models.TextChoices):
        ONE_HOUR = "one_hour", "One hour"
        TWENTY_FOUR_HOUR = "twenty_four_hour", "Twenty-four hours"
        MISSED = "missed", "Missed training"

    class DeliveryState(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        FAILED = "failed", "Failed"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="sent_notifications",
    )
    notification_type = models.CharField(max_length=50)
    sent_date = models.DateField()
    occurrence_schedule = models.ForeignKey(
        "attendance.Schedule",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sent_occurrence_notifications",
    )
    occurrence_date = models.DateField(null=True, blank=True)
    delivery_stage = models.CharField(
        max_length=32,
        choices=DeliveryStage.choices,
        blank=True,
        default="",
    )
    delivery_state = models.CharField(
        max_length=16,
        choices=DeliveryState.choices,
        default=DeliveryState.QUEUED,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student", "notification_type", "sent_date"],
                condition=models.Q(occurrence_schedule__isnull=True),
                name="unique_notification_per_student_per_day",
            ),
            models.UniqueConstraint(
                fields=[
                    "club",
                    "student",
                    "notification_type",
                    "occurrence_schedule",
                    "occurrence_date",
                    "delivery_stage",
                ],
                condition=models.Q(occurrence_schedule__isnull=False),
                name="unique_occurrence_stage_notification",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        occurrence_schedule__isnull=True,
                        occurrence_date__isnull=True,
                        delivery_stage="",
                    )
                    | (
                        models.Q(
                            occurrence_schedule__isnull=False,
                            occurrence_date__isnull=False,
                        )
                        & ~models.Q(delivery_stage="")
                    )
                ),
                name="valid_notification_occurrence_identity",
            ),
        ]

    def __str__(self):
        return f"{self.student} - {self.notification_type} ({self.sent_date})"


class NotificationPreference(BaseModel):
    """Per-user notification category preferences. Not tenant-scoped."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    disabled_categories = models.JSONField(default=list)

    def __str__(self) -> str:
        return f"NotifPref({self.user_id}): {self.disabled_categories}"
