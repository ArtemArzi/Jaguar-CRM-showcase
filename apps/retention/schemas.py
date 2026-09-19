from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from ninja import Schema
from pydantic import Field

_PIPELINE_STEP_PREFIX = "Pipeline step:"
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")


def _pipeline_step_message(notes: str) -> str | None:
    notes = (notes or "").lstrip()
    if not notes.startswith(_PIPELINE_STEP_PREFIX):
        return None
    message = notes[len(_PIPELINE_STEP_PREFIX) :].strip()
    if not message:
        return None
    message = _EMAIL_RE.sub("[redacted-email]", message)
    message = _PHONE_RE.sub("[redacted-phone]", message)
    return message[:500]


class RetentionTaskOut(Schema):
    id: int
    student_id: int
    student_name: str
    student_phone: str
    last_visit_date: date | None
    days_missed: int
    trainer_id: int
    level: str
    status: str
    due_date: date
    resolved_at: datetime | None
    resolution: str
    notes: str
    created_at: datetime
    last_activity_date: datetime | None
    task_type: str = "retention"
    attempt_count: int = 0
    automation_source: str | None = None
    automation_step_message: str | None = None

    @staticmethod
    def resolve_automation_source(obj) -> str | None:
        if _pipeline_step_message(obj.notes):
            return "pipeline"
        return None

    @staticmethod
    def resolve_automation_step_message(obj) -> str | None:
        return _pipeline_step_message(obj.notes)

    @staticmethod
    def resolve_last_activity_date(obj) -> datetime | None:
        if hasattr(obj, "_last_activity_date"):
            return obj._last_activity_date
        return obj.updated_at

    @staticmethod
    def resolve_student_name(obj) -> str:
        return str(obj.student)

    @staticmethod
    def resolve_student_phone(obj) -> str:
        return obj.student.phone or ""

    @staticmethod
    def resolve_last_visit_date(obj) -> date | None:
        return obj.student.last_visit_date

    @staticmethod
    def resolve_days_missed(obj) -> int:
        if obj.student.last_visit_date:
            return (date.today() - obj.student.last_visit_date).days
        return 0


class CloseTaskIn(Schema):
    resolution: Literal[
        "manual_contacted",
        "manual_other",
        "called_will_come",
        "no_answer",
        "quit",
    ]
    notes: str = ""


class TaskCommentOut(Schema):
    id: int
    author_email: str
    text: str
    created_at: datetime

    @staticmethod
    def resolve_author_email(obj) -> str:
        return obj.author.email


class TaskCommentIn(Schema):
    text: str = Field(min_length=1, max_length=2000)


class SnoozeTaskIn(Schema):
    new_due_date: date
    increment_attempt: bool = False


class UpdateTaskStatusIn(Schema):
    status: Literal["in_progress", "snoozed", "closed"]
