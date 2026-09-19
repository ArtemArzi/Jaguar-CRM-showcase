from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.common.models import TenantMixin


class LeadIntakeEvent(TenantMixin):
    class PreferredFormat(models.TextChoices):
        GROUP = "group", "Группа"
        HYBRID = "hybrid", "Группа + персонально"
        PERSONAL = "personal", "Персонально"
        UNSURE = "unsure", "Нужна помощь"

    class TelegramStatus(models.TextChoices):
        PENDING = "pending", "Ожидает"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка"
        SKIPPED = "skipped", "Пропущено"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="lead_intake_events",
    )
    goal = models.CharField(max_length=500)
    preferred_format = models.CharField(max_length=20, choices=PreferredFormat.choices)
    source_page = models.CharField(max_length=200, blank=True, default="")
    utm_source = models.CharField(max_length=120, blank=True, default="")
    utm_medium = models.CharField(max_length=120, blank=True, default="")
    utm_campaign = models.CharField(max_length=120, blank=True, default="")
    utm_content = models.CharField(max_length=120, blank=True, default="")
    utm_term = models.CharField(max_length=120, blank=True, default="")
    privacy_policy_version = models.CharField(max_length=50)
    consent_text_hash = models.CharField(max_length=128)
    consent_accepted_at = models.DateTimeField(default=timezone.now)
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    client_ip_hash = models.CharField(max_length=64, blank=True, default="")
    user_agent_hash = models.CharField(max_length=64, blank=True, default="")
    idempotency_key = models.UUIDField(null=True, blank=True)
    is_repeat_submission = models.BooleanField(default=False)
    requires_owner_review = models.BooleanField(default=False, db_index=True)
    telegram_status = models.CharField(
        max_length=20,
        choices=TelegramStatus.choices,
        default=TelegramStatus.PENDING,
        db_index=True,
    )
    telegram_attempt_count = models.PositiveIntegerField(default=0)
    telegram_sent_at = models.DateTimeField(null=True, blank=True)
    telegram_error_code = models.CharField(max_length=80, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["club", "created_at"]),
            models.Index(fields=["club", "student", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_lead_intake_idempotency_key_per_club",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LeadIntakeEvent(id={self.id}, student_id={self.student_id})"


class LeadLifecycleEvent(TenantMixin):
    class EventType(models.TextChoices):
        LEAD_CLAIMED = "lead_claimed", "Lead claimed"
        LEAD_RELEASED = "lead_released", "Lead released"
        LEAD_ASSIGNED = "lead_assigned", "Lead assigned"
        LEAD_REASSIGNED = "lead_reassigned", "Lead reassigned"
        STATUS_CHANGED = "status_changed", "Status changed"
        TRIAL_BOOKED = "trial_booked", "Trial booked"
        TRIAL_DONE = "trial_done", "Trial done"
        CONTACT_OUTCOME_RECORDED = "contact_outcome_recorded", "Contact outcome recorded"
        LEAD_LOST = "lead_lost", "Lead lost"
        LEAD_CONVERTED = "lead_converted", "Lead converted"
        LEAD_REOPENED = "lead_reopened", "Lead reopened"
        LEAD_RESTORED = "lead_restored", "Lead restored"
        PERSONAL_ADMISSION_PENDING = "personal_admission_pending", "Personal admission pending"
        PERSONAL_ADMISSION_RESTORED = "personal_admission_restored", "Personal admission restored"
        GROUP_ADMISSION_PENDING = "group_admission_pending", "Group admission pending"
        GROUP_ADMISSION_RESTORED = "group_admission_restored", "Group admission restored"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="lead_lifecycle_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lead_lifecycle_events",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    old_lead_status = models.CharField(max_length=20, blank=True, default="")
    new_lead_status = models.CharField(max_length=20, blank=True, default="")
    old_trainer = models.ForeignKey(
        "trainers.Trainer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="old_lead_lifecycle_events",
    )
    new_trainer = models.ForeignKey(
        "trainers.Trainer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="new_lead_lifecycle_events",
    )
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["club", "student", "created_at"],
                name="leadlife_club_student_created",
            ),
            models.Index(
                fields=["club", "event_type", "created_at"],
                name="leadlife_club_type_created",
            ),
        ]
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"LeadLifecycleEvent(id={self.id}, type={self.event_type}, student_id={self.student_id})"
