from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.attendance.tests.factories import CheckinFactory
from apps.billing.models import Debt, Payment
from apps.billing.tests.factories import DebtFactory, PaymentFactory, TariffFactory, TrainingTypeFactory
from apps.leads.models import LeadLifecycleEvent
from apps.leads.services import (
    restore_lead_after_terminal_group_payment,
    snooze_lead_for_pending_group_payment,
)
from apps.retention.models import RetentionTask
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def _lead_payment(*, club, student):
    tariff = TariffFactory(
        club=club,
        training_type=TrainingTypeFactory(club=club),
    )
    return PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.REJECTED,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("lead_status", "task_type"),
    [
        (Student.LeadStatus.NEW, RetentionTask.TaskType.NEW_LEAD),
        (Student.LeadStatus.CONTACTED, RetentionTask.TaskType.NEW_LEAD),
        (Student.LeadStatus.THINKING, RetentionTask.TaskType.NEW_LEAD),
        (Student.LeadStatus.TRIAL_DONE, RetentionTask.TaskType.POST_TRIAL),
    ],
)
def test_group_pending_snooze_and_pre_attendance_restore_exact_task_matrix(
    club,
    lead_status,
    task_type,
):
    trainer = TrainerFactory(club=club)
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=lead_status,
        assigned_trainer=trainer,
    )
    due_date = timezone.localdate() + timedelta(days=3)
    task = RetentionTaskFactory(
        club=club,
        student=student,
        trainer=trainer,
        task_type=task_type,
        status=RetentionTask.TaskStatus.OPEN,
        due_date=due_date,
    )
    payment = _lead_payment(club=club, student=student)

    snooze_lead_for_pending_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment.id,
        actor_user_id=None,
    )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.SNOOZED
    pending = LeadLifecycleEvent.objects.for_club(club).get(
        student=student,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_PENDING,
    )
    assert pending.metadata["task"] == {
        "id": task.id,
        "task_type": task_type,
        "status": RetentionTask.TaskStatus.OPEN,
        "due_date": due_date.isoformat(),
    }

    restore_lead_after_terminal_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment.id,
        actor_user_id=None,
        outcome="rejected",
    )

    task.refresh_from_db()
    student.refresh_from_db()
    assert student.lead_status == lead_status
    assert task.task_type == task_type
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert task.due_date == due_date


@pytest.mark.django_db
def test_group_restore_ignores_historical_checkin_but_respects_exact_pending_debt(club):
    trainer = TrainerFactory(club=club)
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
        assigned_trainer=trainer,
    )
    task = RetentionTaskFactory(
        club=club,
        student=student,
        trainer=trainer,
        task_type=RetentionTask.TaskType.NEW_LEAD,
    )
    payment = _lead_payment(club=club, student=student)
    snooze_lead_for_pending_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment.id,
        actor_user_id=None,
    )
    # This visit predates and is unrelated to the payment-owned admission.
    CheckinFactory(club=club, student=student)
    restore_lead_after_terminal_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment.id,
        actor_user_id=None,
        outcome="rejected",
    )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.OPEN

    # A new captured attempt with exact debt evidence is the only attendance
    # fact that blocks restore.
    second_payment = _lead_payment(club=club, student=student)
    snooze_lead_for_pending_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=second_payment.id,
        actor_user_id=None,
    )
    exact_debt = DebtFactory(
        club=club,
        student=student,
        checkin=CheckinFactory(club=club, student=student),
        settlement_payment=second_payment,
        reason="pending_manual_admission",
    )
    assert exact_debt.checkin_id is not None
    assert Debt.objects.for_club(club).filter(
        student_id=student.id,
        settlement_payment_id=second_payment.id,
        reason="pending_manual_admission",
        checkin__isnull=False,
    ).exists()
    restore_lead_after_terminal_group_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=second_payment.id,
        actor_user_id=None,
        outcome="rejected",
    )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.SNOOZED
