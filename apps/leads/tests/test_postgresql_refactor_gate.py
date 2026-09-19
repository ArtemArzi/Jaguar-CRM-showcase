from __future__ import annotations

import os
from datetime import timedelta
from threading import Event, Thread
from time import monotonic

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.billing.models import Payment
from apps.billing.tests.factories import PaymentFactory, TariffFactory, TrainingTypeFactory
from apps.leads.models import LeadLifecycleEvent
from apps.leads.services import (
    restore_lead_after_terminal_group_payment,
    restore_lead_after_terminal_personal_payment,
    snooze_lead_for_pending_group_payment,
    snooze_lead_for_pending_personal_payment,
)
from apps.leads.tests.postgresql_refactor_guard import (
    UnsafeLeadsRefactorDatabaseError,
    derived_test_database_name,
    validate_leads_refactor_database_urls,
)
from apps.retention.models import RetentionTask
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def test_refactor_database_url_validation_rejects_mismatched_targets():
    with pytest.raises(UnsafeLeadsRefactorDatabaseError, match="must identify the same target"):
        validate_leads_refactor_database_urls(
            database_url="postgresql://localhost:5432/leads_refactor",
            refactor_database_url="postgresql://localhost:5432/other_refactor",
        )


def test_refactor_database_url_validation_rejects_remote_host():
    with pytest.raises(UnsafeLeadsRefactorDatabaseError, match="loopback host"):
        validate_leads_refactor_database_urls(
            database_url="postgresql://db.example.invalid:5432/leads_refactor",
            refactor_database_url="postgresql://db.example.invalid:5432/leads_refactor",
        )


def _assert_isolated_postgresql_gate() -> None:
    refactor_database_url = os.environ.get("LEADS_REFACTOR_POSTGRES_URL", "")
    target = validate_leads_refactor_database_urls(
        database_url=os.environ.get("DATABASE_URL", ""),
        refactor_database_url=refactor_database_url,
    )
    expected_test_database_name = derived_test_database_name(target)

    assert connection.vendor == "postgresql"
    assert str(connection.settings_dict["NAME"]) == expected_test_database_name
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone() == (expected_test_database_name,)

    active_target = connection.connection.info
    assert active_target.host == target.host
    assert active_target.port == (target.port or 5432)
    assert active_target.user == target.username
    assert active_target.dbname == expected_test_database_name


@pytest.mark.django_db
def test_leads_refactor_postgresql_gate_uses_isolated_postgresql():
    refactor_database_url = os.environ.get("LEADS_REFACTOR_POSTGRES_URL", "")
    gate_required = os.environ.get("LEADS_REFACTOR_POSTGRES_GATE_REQUIRED") == "1"
    if not refactor_database_url and not gate_required:
        pytest.skip("leads refactor PostgreSQL gate is opt-in")

    _assert_isolated_postgresql_gate()


def _lead_with_open_task(*, club):
    trainer = TrainerFactory(club=club)
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
        assigned_trainer=trainer,
    )
    due_date = timezone.localdate() + timedelta(days=3)
    task = RetentionTaskFactory(
        club=club,
        student=student,
        trainer=trainer,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        status=RetentionTask.TaskStatus.OPEN,
        due_date=due_date,
    )
    return student, task, due_date


def _lead_group_payment(*, club, student):
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


def _run_restore_once_race(
    *,
    club_id: int,
    student_id: int,
    payment_id: int,
    restore,
    restored_event_type: str,
) -> None:
    first_row_locked = Event()
    first_restore_recorded = Event()
    release_first_restore = Event()
    first_finished = Event()
    second_started = Event()
    second_finished = Event()
    backend_pids: dict[str, int] = {}
    results: dict[str, str] = {}
    errors: dict[str, BaseException] = {}

    def current_backend_pid() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def second_backend_waits_on_first_transaction() -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks AS waiter
                    JOIN pg_locks AS holder
                        ON holder.locktype = 'transactionid'
                        AND holder.transactionid = waiter.transactionid
                        AND holder.granted
                    WHERE waiter.pid = %s
                        AND holder.pid = %s
                        AND waiter.locktype = 'transactionid'
                        AND NOT waiter.granted
                )
                """,
                [backend_pids["second"], backend_pids["first"]],
            )
            return cursor.fetchone()[0]

    def wait_for_second_backend_to_block_on_first_transaction() -> bool:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if second_backend_waits_on_first_transaction():
                return True
        return False

    def restore_first() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                locked_student = (
                    Student.objects.for_club(club_id)
                    .select_for_update()
                    .get(id=student_id, deleted_at__isnull=True)
                )
                assert locked_student.id == student_id
                backend_pids["first"] = current_backend_pid()
                first_row_locked.set()
                restore(
                    club_id=club_id,
                    student_id=student_id,
                    payment_id=payment_id,
                    actor_user_id=None,
                    outcome="rejected",
                )
                first_restore_recorded.set()
                if not release_first_restore.wait(timeout=5):
                    raise TimeoutError("test did not release first restore")
                results["first"] = "restored"
        except BaseException as exc:
            errors["first"] = exc
        finally:
            first_finished.set()
            close_old_connections()

    def restore_second() -> None:
        close_old_connections()
        try:
            backend_pids["second"] = current_backend_pid()
            second_started.set()
            with transaction.atomic():
                restore(
                    club_id=club_id,
                    student_id=student_id,
                    payment_id=payment_id,
                    actor_user_id=None,
                    outcome="rejected",
                )
            results["second"] = "restored"
        except BaseException as exc:
            errors["second"] = exc
        finally:
            second_finished.set()
            close_old_connections()

    first_thread = Thread(target=restore_first)
    first_thread.start()
    assert first_row_locked.wait(timeout=5)
    assert first_restore_recorded.wait(timeout=5)

    second_thread = Thread(target=restore_second)
    second_thread.start()
    assert second_started.wait(timeout=5)
    try:
        assert wait_for_second_backend_to_block_on_first_transaction()
    finally:
        release_first_restore.set()
    assert first_finished.wait(timeout=5)
    assert second_finished.wait(timeout=5)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == {}
    assert results == {"first": "restored", "second": "restored"}
    assert LeadLifecycleEvent.objects.for_club(club_id).filter(
        student_id=student_id,
        event_type=restored_event_type,
        metadata__payment_id=payment_id,
    ).count() == 1


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires isolated PostgreSQL two-connection row-lock semantics",
)
@pytest.mark.django_db(transaction=True)
def test_postgresql_personal_payment_restore_is_once_under_race(club):
    _assert_isolated_postgresql_gate()
    student, task, due_date = _lead_with_open_task(club=club)
    payment_id = 880001

    with transaction.atomic():
        snooze_lead_for_pending_personal_payment(
            club_id=club.id,
            student_id=student.id,
            payment_id=payment_id,
            actor_user_id=None,
        )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.SNOOZED

    _run_restore_once_race(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment_id,
        restore=restore_lead_after_terminal_personal_payment,
        restored_event_type=LeadLifecycleEvent.EventType.PERSONAL_ADMISSION_RESTORED,
    )

    student.refresh_from_db()
    task.refresh_from_db()
    assert student.lead_status == Student.LeadStatus.NEW
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert task.due_date == due_date


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires isolated PostgreSQL two-connection row-lock semantics",
)
@pytest.mark.django_db(transaction=True)
def test_postgresql_group_payment_restore_is_once_under_race(club):
    _assert_isolated_postgresql_gate()
    student, task, due_date = _lead_with_open_task(club=club)
    payment = _lead_group_payment(club=club, student=student)

    with transaction.atomic():
        snooze_lead_for_pending_group_payment(
            club_id=club.id,
            student_id=student.id,
            payment_id=payment.id,
            actor_user_id=None,
        )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.SNOOZED

    _run_restore_once_race(
        club_id=club.id,
        student_id=student.id,
        payment_id=payment.id,
        restore=restore_lead_after_terminal_group_payment,
        restored_event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
    )

    student.refresh_from_db()
    task.refresh_from_db()
    assert student.lead_status == Student.LeadStatus.NEW
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert task.due_date == due_date
