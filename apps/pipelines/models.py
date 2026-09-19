from django.db import models

from apps.common.models import TenantMixin


class Pipeline(TenantMixin):
    class PipelineType(models.TextChoices):
        FOLLOW_UP = "follow_up", "Follow-up after trial"
        WIN_BACK = "win_back", "Win-back churned"

    name = models.CharField(max_length=100)
    pipeline_type = models.CharField(max_length=20, choices=PipelineType.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "pipeline_type"],
                condition=models.Q(is_active=True),
                name="unique_active_pipeline_per_club_type",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.club_id})"


class PipelineStep(TenantMixin):
    class ActionType(models.TextChoices):
        CREATE_TASK = "create_task", "Create trainer task"
        SEND_PUSH = "send_push", "Send push notification"
        CHANGE_STATUS = "change_status", "Change student status"

    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="steps")
    order = models.PositiveIntegerField()
    delay_hours = models.PositiveIntegerField()
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    action_config = models.JSONField(default=dict)
    is_terminal = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["pipeline", "order"], name="unique_step_order"),
        ]

    def __str__(self):
        return f"Step {self.order} of {self.pipeline.name}"


class PipelineExecution(TenantMixin):
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="executions")
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="pipeline_executions"
    )
    current_step = models.ForeignKey(
        PipelineStep, on_delete=models.PROTECT, null=True, blank=True
    )
    started_at = models.DateTimeField(auto_now_add=True)
    next_step_at = models.DateTimeField(null=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "next_step_at"]),
            models.Index(fields=["club", "student", "completed_at"]),
        ]

    def __str__(self):
        return f"Exec({self.pipeline.name}, {self.student_id})"
