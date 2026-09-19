from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.exceptions import BusinessLogicError
from apps.feedback.models import FeedbackAnswer, FeedbackForm, FeedbackQuestion, FeedbackResponse
from apps.students.models import Student

logger = logging.getLogger(__name__)


def _mark_response_created(response: FeedbackResponse, *, created: bool) -> FeedbackResponse:
    response._created = created
    return response


def _answer_has_required_value(question: FeedbackQuestion, answer: dict | None) -> bool:
    if answer is None:
        return False
    if question.question_type == FeedbackQuestion.QuestionType.RATING:
        return answer.get("rating_value") is not None
    if question.question_type == FeedbackQuestion.QuestionType.YES_NO:
        return answer.get("bool_value") is not None
    if question.question_type == FeedbackQuestion.QuestionType.TEXT:
        return bool(str(answer.get("text_value") or "").strip())
    return False


def _validate_answer_payload(question: FeedbackQuestion, answer: dict) -> None:
    rating_value = answer.get("rating_value")
    bool_value = answer.get("bool_value")
    text_value = str(answer.get("text_value") or "")

    if question.question_type == FeedbackQuestion.QuestionType.RATING:
        if bool_value is not None or text_value.strip():
            raise BusinessLogicError("Answer type does not match question", code="invalid_answer_type")
        if rating_value is None:
            raise BusinessLogicError("Rating answer is required", code="invalid_answer_type")
        if not isinstance(rating_value, int) or not 1 <= rating_value <= 5:
            raise BusinessLogicError("Rating must be between 1 and 5", code="invalid_rating_value")
        return

    if question.question_type == FeedbackQuestion.QuestionType.YES_NO:
        if rating_value is not None or text_value.strip() or bool_value is None:
            raise BusinessLogicError("Answer type does not match question", code="invalid_answer_type")
        return

    if question.question_type == FeedbackQuestion.QuestionType.TEXT:
        if rating_value is not None or bool_value is not None:
            raise BusinessLogicError("Answer type does not match question", code="invalid_answer_type")


def create_feedback_form(
    *, club_id: int, name: str, questions: list[dict], trigger_type: str = "trial"
) -> FeedbackForm:
    with transaction.atomic():
        # Deactivate previous active form of same trigger_type
        FeedbackForm.objects.for_club(club_id).filter(
            trigger_type=trigger_type, is_active=True
        ).update(is_active=False)

        form = FeedbackForm.objects.create(
            club_id=club_id, name=name, trigger_type=trigger_type, is_active=True
        )

        for idx, q in enumerate(questions):
            FeedbackQuestion.objects.create(
                club_id=club_id,
                form=form,
                question_type=q["question_type"],
                text=q["text"],
                order=idx + 1,
                is_required=q.get("is_required", False),
            )

    logger.info("feedback_form_created", extra={"form_id": form.id, "club_id": club_id})
    return form


def submit_feedback_response(
    *, club_id: int, form_id: int, student_id: int, answers: list[dict]
) -> FeedbackResponse:
    form = FeedbackForm.objects.for_club(club_id).get(id=form_id)
    if not Student.objects.for_club(club_id).filter(
        id=student_id,
        deleted_at__isnull=True,
    ).exists():
        raise BusinessLogicError("Student not found", code="student_not_found")

    if not form.is_active:
        raise BusinessLogicError("Form is not active", code="form_inactive")

    questions = list(FeedbackQuestion.objects.for_club(club_id).filter(form=form))
    questions_by_id = {question.id: question for question in questions}
    form_question_ids = set(questions_by_id)
    answer_question_ids = [a["question_id"] for a in answers]
    answered_ids = set(answer_question_ids)
    if len(answer_question_ids) != len(answered_ids):
        raise BusinessLogicError("Question answered more than once", code="duplicate_question_answer")
    if not answered_ids.issubset(form_question_ids):
        raise BusinessLogicError("Question does not belong to this form", code="invalid_question")

    answers_by_id = {answer["question_id"]: answer for answer in answers}
    for answer in answers:
        _validate_answer_payload(questions_by_id[answer["question_id"]], answer)

    # Idempotency: one response per student per form
    existing = (
        FeedbackResponse.objects.for_club(club_id)
        .filter(form=form, student_id=student_id)
        .prefetch_related("answers__question")
        .first()
    )
    if existing:
        return _mark_response_created(existing, created=False)

    # Validate required questions
    required_ids = {
        question.id
        for question in questions
        if question.is_required
    }
    missing = {
        question_id
        for question_id in required_ids
        if not _answer_has_required_value(questions_by_id[question_id], answers_by_id.get(question_id))
    }
    if missing:
        raise BusinessLogicError("Missing required answers", code="missing_required")

    try:
        with transaction.atomic():
            response = FeedbackResponse.objects.create(
                club_id=club_id, form=form, student_id=student_id
            )
            for a in answers:
                FeedbackAnswer.objects.create(
                    club_id=club_id,
                    response=response,
                    question_id=a["question_id"],
                    rating_value=a.get("rating_value"),
                    bool_value=a.get("bool_value"),
                    text_value=a.get("text_value", ""),
                )
    except IntegrityError:
        existing = (
            FeedbackResponse.objects.for_club(club_id)
            .filter(form=form, student_id=student_id)
            .prefetch_related("answers__question")
            .first()
        )
        if existing:
            return _mark_response_created(existing, created=False)
        raise

    logger.info(
        "feedback_response_submitted",
        extra={"response_id": response.id, "student_id": student_id, "club_id": club_id},
    )
    return _mark_response_created(response, created=True)


def schedule_trial_feedback(*, club_id: int, student_id: int, checkin_id: int = 0) -> None:
    if not Student.objects.for_club(club_id).filter(
        id=student_id,
        deleted_at__isnull=True,
    ).exists():
        raise BusinessLogicError("Student not found", code="student_not_found")

    # Skip if already responded
    active_form = FeedbackForm.objects.for_club(club_id).filter(
        trigger_type="trial", is_active=True
    ).first()
    if not active_form:
        return

    if FeedbackResponse.objects.for_club(club_id).filter(
        form=active_form, student_id=student_id
    ).exists():
        return

    # Get delay from ClubSettings
    from apps.clubs.models import ClubSettings

    try:
        settings = ClubSettings.objects.get(club_id=club_id)
        delay_hours = settings.feedback_delay_hours
    except ClubSettings.DoesNotExist:
        delay_hours = 2

    from django_q.tasks import schedule

    schedule(
        "apps.feedback.tasks.send_trial_feedback_push",
        student_id,
        club_id,
        schedule_type="O",
        next_run=timezone.now() + timedelta(hours=delay_hours),
    )

    logger.info(
        "trial_feedback_scheduled",
        extra={
            "student_id": student_id,
            "club_id": club_id,
            "delay_hours": delay_hours,
        },
    )


def create_default_feedback_form(*, club_id: int) -> FeedbackForm:
    return create_feedback_form(
        club_id=club_id,
        name="Опрос после пробной",
        questions=[
            {"question_type": "rating", "text": "Оцените тренировку от 1 до 5", "is_required": True},
            {"question_type": "text", "text": "Комментарий (по желанию)", "is_required": False},
        ],
    )
