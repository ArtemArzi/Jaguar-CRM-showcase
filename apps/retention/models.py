from django.conf import settings
from django.db import models

from apps.common.models import TenantMixin


class RetentionTask(TenantMixin):
    class Level(models.TextChoices):
        YELLOW = "yellow", "Yellow"
        RED = "red", "Red"
        CHURNED = "churned", "Churned"

    class Resolution(models.TextChoices):
        AUTO_CHECKIN = "auto_checkin", "Student returned"
        AUTO_SUBSCRIPTION = "auto_subscription", "Subscription created"
        MANUAL_CONTACTED = "manual_contacted", "Contacted, won't come"
        MANUAL_OTHER = "manual_other", "Other"
        CALLED_WILL_COME = "called_will_come", "Called, will come"
        NO_ANSWER = "no_answer", "No answer"
        QUIT = "quit", "Quit"
        LEAD_CLOSED = "lead_closed", "Lead closed"
        AUTO_ADMISSION = "auto_admission", "Personal admission"

    class TaskStatus(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        SNOOZED = "snoozed", "Snoozed"
        CLOSED = "closed", "Closed"

    class TaskType(models.TextChoices):
        RETENTION = "retention", "Retention"
        NEW_LEAD = "new_lead", "New Lead"
        POST_TRIAL = "post_trial", "Post Trial"
        RENEWAL = "renewal", "Renewal"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="retention_tasks",
    )
    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        related_name="retention_tasks",
    )
    level = models.CharField(max_length=20, choices=Level.choices, blank=True, default="")
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    due_date = models.DateField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=30, choices=Resolution.choices, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    task_type = models.CharField(max_length=20, choices=TaskType.choices, default=TaskType.RETENTION)
    attempt_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["club", "trainer", "resolved_at"]),
            models.Index(fields=["club", "student", "resolved_at"]),
            models.Index(fields=["club", "resolved_at", "due_date"]),
            models.Index(fields=["club", "student", "task_type", "resolved_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student", "task_type"],
                condition=models.Q(resolved_at__isnull=True),
                name="unique_open_task_per_type_per_student",
            ),
        ]

    def __str__(self):
        return f"RetentionTask({self.student}, {self.level})"


class TaskComment(TenantMixin):
    task = models.ForeignKey(RetentionTask, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"TaskComment(task={self.task_id}, author={self.author_id})"
