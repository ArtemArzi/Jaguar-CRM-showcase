from uuid import UUID, uuid4

from ninja import Schema
from pydantic import BaseModel, Field


class GradesStepIn(BaseModel):
    disciplines: list[str] = Field(..., min_length=1)
    use_templates: bool = True


class ScheduleItem(BaseModel):
    day: int = Field(..., ge=0, le=6)
    start: str  # "HH:MM"
    end: str  # "HH:MM"
    group: str
    trainer_id: int | None = Field(default=None, gt=0)
    trainer_ref: UUID | None = None
    location_id: int | None = Field(default=None, gt=0)
    # Kept only while an active v1 draft is waiting for an explicit reselection.
    legacy_trainer_name: str = ""


class ScheduleStepIn(BaseModel):
    schedules: list[ScheduleItem] = []


class StudentItem(BaseModel):
    first_name: str
    last_name: str = ""
    phone: str


class StudentsStepIn(BaseModel):
    students: list[StudentItem] = []


class TariffItem(BaseModel):
    name: str
    price: int = Field(..., gt=0)
    training_limit: int | None = None
    duration_days: int = Field(..., gt=0)


class TariffsStepIn(BaseModel):
    tariffs: list[TariffItem] = []


class TrainerItem(BaseModel):
    client_ref: UUID = Field(default_factory=uuid4)
    first_name: str
    last_name: str = ""
    phone: str = ""


class TrainersStepIn(BaseModel):
    trainers: list[TrainerItem] = []


class SaveStepIn(Schema):
    draft_id: int = Field(..., gt=0)
    step: int = Field(..., ge=1, le=5)
    data: dict  # validated per step in service


class FinishOnboardingIn(Schema):
    draft_id: int = Field(..., gt=0)


class OnboardingDraftOut(Schema):
    id: int
    current_step: int
    data: dict
    is_completed: bool


class FinishOnboardingOut(Schema):
    status: str
    draft: OnboardingDraftOut
