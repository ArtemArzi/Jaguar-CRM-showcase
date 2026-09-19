from __future__ import annotations

from datetime import date

from apps.billing.models import Tariff, TrainingType
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student


def validate_trainer_group_payment_contract(
    *,
    club_id: int,
    student: Student,
    tariff: Tariff,
    target_schedule_id: int | None,
    target_start_date: date | None,
    lock_enrollments: bool = False,
) -> None:
    if tariff.training_type.kind != TrainingType.Kind.GROUP:
        return
    if target_schedule_id is None or target_start_date is None:
        raise BusinessLogicError(
            "Для групповой оплаты укажите группу и дату старта",
            code="target_schedule_required",
        )
    if student.status in {Student.Status.LOST, Student.Status.CHURNED} or (
        student.status == Student.Status.LEAD
        and student.lead_status not in Student.LeadStatus.values
    ):
        raise BusinessLogicError(
            "Для потерянного или ушедшего ученика решение принимает владелец или администратор",
            code="trainer_group_student_not_eligible",
        )

    from apps.attendance.models import ScheduleEnrollment

    permanent_enrollments = ScheduleEnrollment.objects.for_club(club_id).filter(
        student_id=student.id,
        schedule__training_type_id=tariff.training_type_id,
        schedule__one_time_date__isnull=True,
        status__in=[
            ScheduleEnrollment.Status.ACTIVE,
            ScheduleEnrollment.Status.FROZEN,
        ],
        created_from__in=[
            ScheduleEnrollment.CreatedFrom.MANUAL,
            ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            ScheduleEnrollment.CreatedFrom.IMPORT,
        ],
        ends_on__isnull=True,
    )
    if lock_enrollments:
        permanent_enrollments = permanent_enrollments.select_for_update(of=("self",))

    permanent_schedule_ids = set(
        permanent_enrollments.values_list("schedule_id", flat=True)
    )
    if permanent_schedule_ids and target_schedule_id not in permanent_schedule_ids:
        raise BusinessLogicError(
            "Смену постоянной группы проводит владелец или администратор",
            code="trainer_group_transfer_requires_management",
        )
