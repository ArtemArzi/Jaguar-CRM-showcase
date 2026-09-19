from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from ninja import Schema

from apps.documents.schemas import ChecklistItemOut
from apps.grades.schemas import GradeProgressOut
from apps.students.schemas import CabinetFinancialOut


class ParentScheduleOccurrenceOut(Schema):
    schedule_id: int
    effective_date: date
    effective_start_time: time
    effective_end_time: time
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    is_rescheduled: bool = False
    is_substitute: bool = False
    training_type_id: int | None = None
    training_type_name: str = ""
    training_type_kind: str = ""
    one_time_date: date | None = None
    enrollment_id: int | None = None
    created_from: str = ""
    can_cancel: bool = False


class ParentScheduleExceptionOut(Schema):
    id: int
    schedule_id: int
    date: date
    exception_type: str
    reason: str
    new_date: date | None = None
    new_start_time: time | None = None
    new_end_time: time | None = None
    substitute_trainer_id: int | None = None
    substitute_trainer_name: str = ""


class ScheduleItemOut(Schema):
    id: int
    day_of_week: int
    start_time: time
    end_time: time
    group_name: str
    training_type_id: int | None = None
    training_type_name: str = ""
    training_type_kind: str = ""
    one_time_date: date | None = None
    trainer_name: str
    location_name: str
    upcoming_occurrences: list[ParentScheduleOccurrenceOut] = []
    upcoming_exceptions: list[ParentScheduleExceptionOut] = []


class ChildSummaryOut(Schema):
    id: int
    first_name: str
    last_name: str
    status: str
    is_child: bool
    grade_name: str | None = None
    subscription_remaining: int | None = None
    subscription_total: int | None = None
    subscription_status: str | None = None
    subscription_freeze_status: str | None = None
    last_visit_date: str | None = None
    next_training_day_of_week: int | None = None
    next_training_start_time: str | None = None
    next_training_group_name: str | None = None
    next_training_trainer_name: str | None = None
    next_training_is_rescheduled: bool | None = None
    next_training_is_substitute: bool | None = None


class SubscriptionSummaryOut(Schema):
    id: int
    tariff_id: int
    tariff_name: str
    trainings_used: int
    trainings_total: int | None
    trainings_left: int | None
    expires_at: str | None
    status: str
    freeze_status: str | None = None
    renewal_target_tariff_id: int | None = None
    renewal_target_tariff_name: str = ""
    renewal_target_price: Decimal | None = None


class DebtSummaryOut(Schema):
    id: int
    checkin_id: int
    tariff_price: Decimal | None
    reason: str
    training_type_name: str
    checkin_date: date
    created_at: datetime


class ChildProfileOut(Schema):
    id: int
    first_name: str
    last_name: str
    status: str
    grade_progress: list[GradeProgressOut]
    attendance_count: int
    active_subscription: SubscriptionSummaryOut | None
    active_subscriptions: list[SubscriptionSummaryOut]
    open_debts: list[DebtSummaryOut]
    financial_state: CabinetFinancialOut
    document_checklist: list[ChecklistItemOut]
    schedule: list[ScheduleItemOut]


class ParentInviteOut(Schema):
    token: uuid.UUID
    student_id: int
    student_name: str
    expires_at: datetime


class CreateInviteIn(Schema):
    student_id: int


class AcceptInviteIn(Schema):
    token: uuid.UUID


class AcceptInviteOut(Schema):
    student_id: int
    student_name: str
    club_id: int
    club_name: str
    access_token: str | None = None


class ChildAttendanceOut(Schema):
    date: str
    group_name: str
    trainer_name: str
    training_type_name: str
    start_time: str
