from django.db import models

from apps.common.models import TenantMixin


class FeedbackForm(TenantMixin):
    class TriggerType(models.TextChoices):
        TRIAL = "trial", "After trial"
        CHURNED = "churned", "Churned student"

    name = models.CharField(max_length=200, default="Опрос после пробной")
    trigger_type = models.CharField(
        max_length=20, choices=TriggerType.choices, default="trial"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "trigger_type"],
                condition=models.Q(is_active=True),
                name="unique_active_form_per_club_trigger",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.club})"


class FeedbackQuestion(TenantMixin):
    class QuestionType(models.TextChoices):
        RATING = "rating", "Rating 1-5"
        YES_NO = "yes_no", "Yes/No"
        TEXT = "text", "Text comment"

    form = models.ForeignKey(FeedbackForm, on_delete=models.PROTECT, related_name="questions")
    question_type = models.CharField(max_length=20, choices=QuestionType.choices)
    text = models.CharField(max_length=500)
    order = models.PositiveIntegerField()
    is_required = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["form", "order"],
                name="unique_question_order_per_form",
            ),
        ]

    def __str__(self):
        return f"Q{self.order}: {self.text[:50]}"


class FeedbackResponse(TenantMixin):
    form = models.ForeignKey(FeedbackForm, on_delete=models.PROTECT, related_name="responses")
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="feedback_responses"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "form", "student"],
                name="unique_response_per_student_per_form",
            ),
        ]

    def __str__(self):
        return f"Response by {self.student} to {self.form.name}"


class FeedbackAnswer(TenantMixin):
    response = models.ForeignKey(FeedbackResponse, on_delete=models.PROTECT, related_name="answers")
    question = models.ForeignKey(FeedbackQuestion, on_delete=models.PROTECT, related_name="answers")
    rating_value = models.PositiveSmallIntegerField(null=True, blank=True)  # 1-5
    bool_value = models.BooleanField(null=True, blank=True)
    text_value = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["response", "question"],
                name="unique_answer_per_question",
            ),
        ]

    def __str__(self):
        return f"Answer to Q{self.question.order}"
