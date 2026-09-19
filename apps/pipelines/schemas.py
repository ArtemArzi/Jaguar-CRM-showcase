from datetime import datetime

from ninja import Schema


class PipelineStepOut(Schema):
    id: int
    order: int
    delay_hours: int
    action_type: str
    action_config: dict
    is_terminal: bool


class PipelineOut(Schema):
    id: int
    name: str
    pipeline_type: str
    is_active: bool
    steps: list[PipelineStepOut]


class PipelineExecutionOut(Schema):
    id: int
    pipeline_type: str
    student_id: int
    student_name: str
    current_step_order: int | None
    next_step_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None

    @staticmethod
    def resolve_pipeline_type(obj) -> str:
        return obj.pipeline.pipeline_type

    @staticmethod
    def resolve_student_name(obj) -> str:
        return f"{obj.student.first_name} {obj.student.last_name}"

    @staticmethod
    def resolve_current_step_order(obj) -> int | None:
        return obj.current_step.order if obj.current_step else None


class UpdatePipelineStepIn(Schema):
    delay_hours: int
