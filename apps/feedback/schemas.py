from __future__ import annotations

from datetime import datetime
from typing import Literal

from ninja import Schema


class QuestionIn(Schema):
    question_type: Literal["rating", "yes_no", "text"]
    text: str
    is_required: bool = False


class FormIn(Schema):
    name: str
    questions: list[QuestionIn]


class QuestionOut(Schema):
    id: int
    question_type: str
    text: str
    order: int
    is_required: bool


class FormOut(Schema):
    id: int
    name: str
    is_active: bool
    questions: list[QuestionOut]
    created_at: datetime


class AnswerIn(Schema):
    question_id: int
    rating_value: int | None = None
    bool_value: bool | None = None
    text_value: str = ""


class SubmitResponseIn(Schema):
    form_id: int
    student_id: int
    answers: list[AnswerIn]


class SubmitSelfResponseIn(Schema):
    form_id: int
    answers: list[AnswerIn]


class AnswerOut(Schema):
    question_id: int
    question_type: str
    question_text: str

    rating_value: int | None = None
    bool_value: bool | None = None
    text_value: str = ""

    @staticmethod
    def resolve_question_type(obj):
        return obj.question.question_type

    @staticmethod
    def resolve_question_text(obj):
        return obj.question.text


class ResponseOut(Schema):
    id: int
    student_id: int
    form_id: int
    submitted_at: datetime
    answers: list[AnswerOut]


class SelfServiceResponseOut(ResponseOut):
    already_submitted: bool = False


class AverageRatingOut(Schema):
    form_id: int
    average_rating: float | None
    response_count: int
