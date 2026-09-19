from django.db import models

from apps.common.models import TenantMixin


class OnboardingDraft(TenantMixin):
    class Step(models.IntegerChoices):
        GRADES = 1, "Grades"
        TRAINERS = 2, "Trainers"
        SCHEDULE = 3, "Schedule"
        STUDENTS = 4, "Students"
        TARIFFS = 5, "Tariffs"

    current_step = models.IntegerField(default=1)
    data = models.JSONField(default=dict)
    is_completed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club"],
                condition=models.Q(is_completed=False),
                name="unique_active_draft_per_club",
            ),
        ]

    def __str__(self):
        return f"OnboardingDraft(club={self.club_id}, step={self.current_step})"
