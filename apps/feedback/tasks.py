from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _render_feedback_push_copy(
    *,
    club_id: int,
    trigger_type: str,
    context: dict,
    fallback_title: str,
    fallback_body: str,
) -> tuple[str, str] | None:
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.services import render_template

    template = (
        NotificationTemplate.objects.for_club(club_id)
        .filter(trigger_type=trigger_type)
        .first()
    )
    if template is None:
        return fallback_title, fallback_body
    if not template.is_enabled:
        return None
    return (
        render_template(template_str=template.title_template, context=context),
        render_template(template_str=template.body_template, context=context),
    )


def send_trial_feedback_push(student_id: int, club_id: int) -> None:
    from apps.feedback.models import FeedbackForm, FeedbackResponse
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.services import send_push_to_user, user_disabled_notification
    from apps.students.models import Student

    student = Student.objects.for_club(club_id).get(id=student_id)

    # Determine push target: parent for children, student's own user for adults
    user_id = None
    if student.is_child and student.parent_user_id:
        user_id = student.parent_user_id
    elif student.user_id:
        user_id = student.user_id

    if not user_id:
        logger.info("trial_feedback_push_skipped_no_user", extra={"student_id": student_id})
        return
    if user_disabled_notification(
        user_id=user_id,
        notification_type=NotificationTemplate.TriggerType.TRIAL_FEEDBACK,
    ):
        logger.info(
            "trial_feedback_push_skipped_opt_out",
            extra={"student_id": student_id, "club_id": club_id, "user_id": user_id},
        )
        return

    # Check if already submitted
    active_form = FeedbackForm.objects.for_club(club_id).filter(
        trigger_type=FeedbackForm.TriggerType.TRIAL, is_active=True
    ).first()
    if not active_form:
        return
    if FeedbackResponse.objects.for_club(club_id).filter(
        form=active_form, student_id=student_id
    ).exists():
        return

    copy = _render_feedback_push_copy(
        club_id=club_id,
        trigger_type=NotificationTemplate.TriggerType.TRIAL_FEEDBACK,
        context={"name": str(student)},
        fallback_title="Как прошла тренировка?",
        fallback_body=f"Расскажите о первой тренировке {student.first_name}",
    )
    if copy is None:
        return
    title, body = copy

    send_push_to_user(
        user_id=user_id,
        title=title,
        body=body,
    )

    logger.info(
        "trial_feedback_push_sent",
        extra={"student_id": student_id, "club_id": club_id, "user_id": user_id},
    )


def send_churned_survey_push(student_id: int, club_id: int) -> None:
    """Send churned survey push to student. Called when status -> churned."""
    from apps.feedback.models import FeedbackForm, FeedbackResponse
    from apps.notifications.models import NotificationTemplate
    from apps.notifications.services import (
        PRIORITY_LOW,
        can_send_push,
        is_quiet_hours,
        send_push_to_user,
        user_disabled_notification,
    )
    from apps.students.models import Student

    if is_quiet_hours(club_id=club_id):
        return

    student = Student.objects.for_club(club_id).get(id=student_id)
    if not can_send_push(student_id=student_id, club_id=club_id, priority=PRIORITY_LOW):
        return

    active_form = FeedbackForm.objects.for_club(club_id).filter(
        trigger_type=FeedbackForm.TriggerType.CHURNED, is_active=True
    ).first()
    if not active_form:
        return
    if FeedbackResponse.objects.for_club(club_id).filter(
        form=active_form, student_id=student_id
    ).exists():
        return

    # Find user: try parent for child students, else direct student.user
    user_id = None
    if student.is_child and student.parent_user_id:
        user_id = student.parent_user_id
    elif student.user_id:
        user_id = student.user_id

    if not user_id:
        return
    if user_disabled_notification(
        user_id=user_id,
        notification_type=NotificationTemplate.TriggerType.CHURNED_SURVEY,
    ):
        logger.info(
            "churned_survey_push_skipped_opt_out",
            extra={"student_id": student_id, "club_id": club_id, "user_id": user_id},
        )
        return

    copy = _render_feedback_push_copy(
        club_id=club_id,
        trigger_type=NotificationTemplate.TriggerType.CHURNED_SURVEY,
        context={"name": str(student)},
        fallback_title="Нам важно ваше мнение",
        fallback_body=f"{student.first_name}, расскажите, почему вы перестали заниматься?",
    )
    if copy is None:
        return
    title, body = copy

    send_push_to_user(
        user_id=user_id,
        title=title,
        body=body,
    )

    logger.info(
        "churned_survey_push_sent",
        extra={"student_id": student_id, "club_id": club_id},
    )
