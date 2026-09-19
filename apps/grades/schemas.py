from __future__ import annotations

from ninja import Schema


class GradeSystemIn(Schema):
    discipline: str


class GradeSystemOut(Schema):
    id: int
    discipline: str
    is_active: bool


class GradeIn(Schema):
    name: str
    order: int
    min_trainings: int = 0


class GradeOut(Schema):
    id: int
    name: str
    order: int
    min_trainings: int


class StudentGradeOut(Schema):
    id: int
    student_id: int
    grade_system: GradeSystemOut
    current_grade: GradeOut | None
    trainings_since_last_grade: int


class GradeProgressOut(Schema):
    student_grade_id: int
    grade_system_id: int
    grade_system_name: str | None = None
    current_grade: GradeOut | None
    trainings_since_last_grade: int
    next_grade: GradeOut | None
    trainings_to_next: int | None


class PromoteIn(Schema):
    new_grade_id: int


class AssignGradeIn(Schema):
    student_id: int
    grade_system_id: int
    initial_grade_id: int | None = None


class SeedGradeTemplatesIn(Schema):
    disciplines: list[str]
