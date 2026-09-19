from __future__ import annotations

from django.db.models import Avg, QuerySet

from apps.feedback.models import FeedbackAnswer, FeedbackForm, FeedbackResponse


def get_active_form(*, club, trigger_type: str = FeedbackForm.TriggerType.TRIAL) -> FeedbackForm | None:
    return (
        FeedbackForm.objects.for_club(club)
        .filter(trigger_type=trigger_type, is_active=True)
        .prefetch_related("questions")
        .order_by("-id")
        .first()
    )


def get_student_feedback_responses(*, club, student_id: int) -> QuerySet[FeedbackResponse]:
    return (
        FeedbackResponse.objects.for_club(club)
        .filter(student_id=student_id)
        .prefetch_related("answers", "answers__question")
        .select_related("form")
    )


def get_form_average_rating(*, club, form_id: int) -> float | None:
    result = (
        FeedbackAnswer.objects.for_club(club)
        .filter(
            response__form_id=form_id,
            question__question_type="rating",
            rating_value__isnull=False,
        )
        .aggregate(avg=Avg("rating_value"))
    )
    return result["avg"]


def get_form_response_count(*, club, form_id: int) -> int:
    return FeedbackResponse.objects.for_club(club).filter(form_id=form_id).count()
