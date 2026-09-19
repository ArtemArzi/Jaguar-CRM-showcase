from ninja import Router

from apps.common.permissions import role_required
from apps.feedback.schemas import (
    AverageRatingOut,
    FormIn,
    FormOut,
    ResponseOut,
    SubmitResponseIn,
)
from apps.feedback.selectors import (
    get_active_form,
    get_form_average_rating,
    get_form_response_count,
    get_student_feedback_responses,
)
from apps.feedback.services import create_feedback_form, schedule_trial_feedback, submit_feedback_response
from apps.students.scopes import actor_can_manage_student_sensitive_actions, assert_actor_is_scoped_to_student

router = Router(tags=["feedback"])


def _assert_trainer_feedback_read_student_scope(request, *, student_id: int) -> None:
    assert_actor_is_scoped_to_student(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _assert_trainer_feedback_management_student_scope(request, *, student_id: int) -> None:
    if not actor_can_manage_student_sensitive_actions(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    ):
        from ninja.errors import HttpError

        raise HttpError(403, "Access denied: not your student")


@router.post("/forms/", response={201: FormOut})
@role_required("owner", "admin")
def create_form(request, payload: FormIn):
    form = create_feedback_form(
        club_id=request.club.id,
        name=payload.name,
        questions=[
            {
                "question_type": question.question_type,
                "text": question.text,
                "is_required": question.is_required,
            }
            for question in payload.questions
        ],
    )
    return 201, form


@router.get("/forms/active/", response={200: FormOut | None})
@role_required("owner", "admin", "trainer")
def active_form(request):
    form = get_active_form(club=request.club)
    return 200, form


@router.post("/submit/", response={200: ResponseOut, 201: ResponseOut})
@role_required("owner", "admin", "trainer")
def submit_response(request, payload: SubmitResponseIn):
    _assert_trainer_feedback_management_student_scope(request, student_id=payload.student_id)
    resp = submit_feedback_response(
        club_id=request.club.id,
        form_id=payload.form_id,
        student_id=payload.student_id,
        answers=[
            {
                "question_id": answer.question_id,
                "rating_value": answer.rating_value,
                "bool_value": answer.bool_value,
                "text_value": answer.text_value,
            }
            for answer in payload.answers
        ],
    )
    return 201 if getattr(resp, "_created", True) else 200, resp


@router.post("/send-survey/{student_id}/", response={200: dict})
@role_required("owner", "admin", "trainer")
def send_survey_manually(request, student_id: int):
    _assert_trainer_feedback_management_student_scope(request, student_id=student_id)
    schedule_trial_feedback(club_id=request.club.id, student_id=student_id, checkin_id=0)
    return 200, {"status": "scheduled"}


@router.get("/students/{student_id}/responses/", response=list[ResponseOut])
@role_required("owner", "admin", "trainer")
def student_responses(request, student_id: int):
    _assert_trainer_feedback_read_student_scope(request, student_id=student_id)
    return get_student_feedback_responses(club=request.club, student_id=student_id)


@router.get("/forms/{form_id}/stats/", response=AverageRatingOut)
@role_required("owner", "admin")
def form_stats(request, form_id: int):
    avg = get_form_average_rating(club=request.club, form_id=form_id)
    count = get_form_response_count(club=request.club, form_id=form_id)
    return {"form_id": form_id, "average_rating": avg, "response_count": count}
