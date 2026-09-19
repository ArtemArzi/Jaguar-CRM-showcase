import io
import json
import os
import subprocess
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone
from django.utils.module_loading import import_string
from ninja.testing import TestClient

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    KioskDevice,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroupMembership,
    TrainingGroupRolloutState,
)
from apps.attendance.services import (
    activate_kiosk,
    cancel_checkin,
    cancel_schedule_enrollment,
    cancel_session,
    close_session_from_existing_checkins,
    create_checkin,
    create_schedule,
    delete_exception,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    reschedule_session,
    substitute_trainer,
    transfer_schedule_enrollment,
    unfreeze_schedule_enrollment,
    update_schedule,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    Discount,
    Expense,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionFreeze,
    Tariff,
    TrainingType,
)
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_zoneinfo
from apps.documents.models import DocumentType, StudentDocument
from apps.feedback.models import FeedbackForm
from apps.grades.models import Grade, GradeProgressEvent, GradeSystem, StudentGrade
from apps.leads.models import LeadIntakeEvent, LeadLifecycleEvent
from apps.notifications.models import (
    MassNotification,
    NotificationPreference,
    NotificationTemplate,
    PushSubscription,
    SentNotification,
)
from apps.onboarding.models import OnboardingDraft
from apps.pipelines.models import PipelineExecution
from apps.retention.models import RetentionTask, TaskComment
from apps.students.models import AccountAccess, ParentInvite, Student, StudentNote
from apps.trainers.models import (
    Trainer,
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerLocation,
    TrainerPackageAllocation,
    TrainerPayrollPeriodClose,
    TrainerRate,
)
from config.api import api


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_attendance_task_sync(task_path: str, *args, **kwargs):
    if not task_path.startswith("apps.attendance.tasks."):
        return None
    task = import_string(task_path)
    return task(*args, **kwargs)


def _run_lead_trial_async_task_sync(task_path: str, *args, **kwargs):
    if task_path != "apps.retention.tasks.create_post_trial_task":
        return _run_attendance_task_sync(task_path, *args, **kwargs)
    task = import_string(task_path)
    return task(*args, **kwargs)


def _run_checkin_cancel_async_task_sync(task_path: str, *args, **kwargs):
    if task_path == "apps.retention.tasks.reverse_auto_close_retention":
        task = import_string(task_path)
        return task(*args, **kwargs)
    return _run_attendance_task_sync(task_path, *args, **kwargs)


def _run_checkin_lifecycle_async_task_sync(task_path: str, *args, **kwargs):
    if task_path in {
        "apps.retention.tasks.auto_close_retention_on_checkin",
        "apps.retention.tasks.reverse_auto_close_retention",
    }:
        task = import_string(task_path)
        return task(*args, **kwargs)
    return _run_attendance_task_sync(task_path, *args, **kwargs)


@pytest.mark.django_db
def test_prepare_real_stack_e2e_creates_isolated_fixture_without_stdout_secrets(tmp_path):
    output = tmp_path / "fixture.json"
    stdout = io.StringIO()

    call_command("prepare_real_stack_e2e", output=str(output), stdout=stdout)

    fixture = _load_fixture(output)
    stdout_text = stdout.getvalue()

    assert fixture["fixture_id"].startswith("rs-e2e-")
    assert fixture["kiosk_pin"]
    assert fixture["kiosk_pin"] not in stdout_text
    assert "token" not in stdout_text.lower()
    assert fixture["expected"]["trainings_left_before"] == 5
    assert fixture["expected"]["trainings_left_after"] == 4

    club = Club.objects.get(id=fixture["club_id"])
    assert ClubSettings.objects.filter(club=club).exists()
    assert Location.objects.filter(club=club).exists()
    assert KioskDevice.objects.filter(club=club, pin_code=fixture["kiosk_pin"], is_active=True).exists()
    assert Trainer.objects.for_club(club).filter(user__club_memberships__role=ClubMembership.Role.TRAINER).exists()
    assert TrainerLocation.objects.for_club(club).count() == 1
    assert TrainerRate.objects.for_club(club).filter(training_type_id=fixture["training_type_id"]).exists()

    student = Student.objects.for_club(club).get(id=fixture["student_id"])
    assert student.status == Student.Status.ACTIVE
    assert student.is_child is True
    assert student.user_id is not None
    assert student.parent_user_id is not None
    assert student.phone.endswith(fixture["phone_suffix"])
    assert ClubMembership.objects.filter(user=student.user, club=club, role=ClubMembership.Role.STUDENT).exists()
    assert ClubMembership.objects.filter(
        user=student.parent_user,
        club=club,
        role=ClubMembership.Role.PARENT,
    ).exists()

    training_type = TrainingType.objects.for_club(club).get(id=fixture["training_type_id"])
    assert training_type.kind == TrainingType.Kind.PERSONAL
    assert training_type.grade_system_id is not None

    subscription = Subscription.objects.for_club(club).get(id=fixture["subscription_id"])
    assert subscription.student_id == student.id
    assert subscription.status == Subscription.Status.ACTIVE
    assert subscription.trainings_left == fixture["expected"]["trainings_left_before"]
    assert subscription.trainings_used == 0

    schedule = Schedule.objects.for_club(club).get(id=fixture["schedule_id"])
    assert schedule.start_time.hour == 0
    assert schedule.start_time.minute == 0
    assert schedule.end_time.hour == 23
    assert schedule.end_time.minute == 59
    assert ScheduleEnrollment.objects.for_club(club).filter(
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=timezone.localdate(),
        ends_on__isnull=True,
    ).exists()

    student_grade = StudentGrade.objects.for_club(club).get(student=student, grade_system=training_type.grade_system)
    assert student_grade.trainings_since_last_grade == 0


@pytest.mark.django_db
def test_assert_real_stack_e2e_polls_and_validates_checkin_cascade(tmp_path):
    output = tmp_path / "fixture.json"
    call_command("prepare_real_stack_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    with pytest.raises(CommandError, match="check-in"):
        call_command("assert_real_stack_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        result = create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["student_id"],
            schedule_id=fixture["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )

    assert result["created"] is True

    stdout = io.StringIO()
    call_command("assert_real_stack_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["checkin_id"] == result["checkin_id"]
    assert evidence["subscription"]["trainings_left"] == fixture["expected"]["trainings_left_after"]
    assert evidence["subscription"]["trainings_used"] == 1
    assert evidence["checkin"]["is_debt"] is False
    assert evidence["checkin"]["subscription_id"] == fixture["subscription_id"]
    assert evidence["cascade_events"] == {
        CheckinCascadeEvent.Effect.SALARY: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.SALARY,
            ).id,
            "expected": True,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.attendance.tasks.calculate_salary",
        },
        CheckinCascadeEvent.Effect.GRADE_PROGRESS: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.GRADE_PROGRESS,
            ).id,
            "expected": True,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.attendance.tasks.update_grade_progress",
        },
        CheckinCascadeEvent.Effect.GROUP_ANALYTICS: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.GROUP_ANALYTICS,
            ).id,
            "expected": True,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.attendance.tasks.update_group_analytics",
        },
        CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.PARENT_NOTIFICATION,
            ).id,
            "expected": True,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.attendance.tasks.log_parent_event",
        },
        CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE,
            ).id,
            "expected": False,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.retention.tasks.auto_close_retention_on_checkin",
        },
        CheckinCascadeEvent.Effect.POST_TRIAL_TASK: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
            ).id,
            "expected": False,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.retention.tasks.create_post_trial_task",
        },
        CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: {
            "id": CheckinCascadeEvent.objects.get(
                checkin_id=result["checkin_id"],
                effect=CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH,
            ).id,
            "expected": False,
            "status": CheckinCascadeEvent.Status.QUEUED,
            "task_name": "apps.notifications.tasks.check_trainings_left_push",
        },
    }
    assert evidence["trainer_earning_id"] == TrainerEarning.objects.get(checkin_id=result["checkin_id"]).id
    assert evidence["grade_progress_event_id"] == GradeProgressEvent.objects.get(checkin_id=result["checkin_id"]).id
    assert evidence["student_grade"]["trainings_since_last_grade"] == 1
    assert evidence["group_session"]["attendee_count"] >= 1
    assert evidence["parent_notification"]["status"] == "recorded"
    assert SentNotification.objects.filter(notification_type=f"parent_checkin:{result['checkin_id']}").exists()
    assert GroupSession.objects.filter(schedule_id=fixture["schedule_id"], date=date.today()).exists()
    assert "kiosk_pin" not in evidence


@pytest.mark.django_db
def test_checkin_lifecycle_e2e_prepare_and_assert_validates_same_checkin_forward_and_cancelled(
    tmp_path,
):
    output = tmp_path / "checkin-lifecycle-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_checkin_lifecycle_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("checkin-lifecycle-e2e-")
    assert fixture["kiosk_pin"]
    assert fixture["phone_suffix"]
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["kiosk_pin"] not in stdout.getvalue()
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["retention_task_id"]
    assert fixture["expected"]["trainings_left_after_checkin"] == 4
    assert fixture["expected"]["trainings_left_after_cancel"] == 5

    with pytest.raises(CommandError, match="check-in row not found"):
        call_command(
            "assert_checkin_lifecycle_e2e",
            fixture=str(output),
            stage="forward",
            checkin_id=0,
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    with patch("apps.attendance.services.async_task", side_effect=_run_checkin_lifecycle_async_task_sync):
        result = create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["student"]["student_id"],
            schedule_id=fixture["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.fromisoformat(fixture["checkin_date"]),
        )

    assert result["created"] is True
    checkin_id = result["checkin_id"]

    stdout = io.StringIO()
    call_command(
        "assert_checkin_lifecycle_e2e",
        fixture=str(output),
        stage="forward",
        checkin_id=checkin_id,
        timeout_seconds=0,
        stdout=stdout,
    )
    forward = json.loads(stdout.getvalue())
    assert forward["ok"] is True
    assert forward["stage"] == "forward"
    assert forward["checkin"]["id"] == checkin_id
    assert forward["checkin"]["source"] == Checkin.Source.KIOSK
    assert forward["subscription"]["trainings_left"] == fixture["expected"]["trainings_left_after_checkin"]
    assert forward["subscription"]["trainings_used"] == fixture["expected"]["trainings_used_after_checkin"]
    assert forward["cascade_events"][CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE]["expected"] is True
    assert forward["earning"]["cancelled"] is False
    assert forward["grade"]["trainings_since_last_grade"] == 1
    assert forward["group_session"]["attendee_count"] == 1
    assert forward["retention_task"]["status"] == RetentionTask.TaskStatus.CLOSED
    assert forward["retention_task"]["resolution"] == RetentionTask.Resolution.AUTO_CHECKIN
    assert forward["parent_notification"]["notification_type"] == f"parent_checkin:{checkin_id}"

    with patch("apps.attendance.services.async_task", side_effect=_run_checkin_lifecycle_async_task_sync):
        cancel_checkin(
            checkin_id=checkin_id,
            club_id=fixture["club_id"],
            cancelled_by_user_id=fixture["owner"]["user_id"],
            user_role=ClubMembership.Role.OWNER,
        )

    stdout = io.StringIO()
    call_command(
        "assert_checkin_lifecycle_e2e",
        fixture=str(output),
        stage="cancelled",
        checkin_id=checkin_id,
        timeout_seconds=0,
        stdout=stdout,
    )
    cancelled = json.loads(stdout.getvalue())
    assert cancelled["ok"] is True
    assert cancelled["stage"] == "cancelled"
    assert cancelled["checkin"]["id"] == checkin_id
    assert cancelled["checkin"]["cancelled"] is True
    assert cancelled["checkin"]["soft_deleted"] is True
    assert cancelled["checkin"]["live_count"] == 0
    assert cancelled["subscription"]["trainings_left"] == fixture["expected"]["trainings_left_after_cancel"]
    assert cancelled["subscription"]["trainings_used"] == fixture["expected"]["trainings_used_after_cancel"]
    assert cancelled["earning"]["cancelled"] is True
    assert cancelled["grade"]["trainings_since_last_grade"] == 0
    assert cancelled["grade"]["progress_event_removed"] is True
    assert (
        cancelled["group_session"]["attendee_count"]
        == fixture["expected"]["group_session_attendee_count_after_cancel"]
    )
    assert cancelled["retention_task"]["status"] == RetentionTask.TaskStatus.OPEN
    assert cancelled["retention_task"]["resolution"] == ""
    assert cancelled["student"]["last_visit_date"] is None
    assert cancelled["parent_notifications"]["checkin"]["notification_type"] == f"parent_checkin:{checkin_id}"
    assert (
        cancelled["parent_notifications"]["cancelled"]["notification_type"]
        == f"parent_checkin_cancelled:{checkin_id}"
    )


@pytest.mark.django_db(transaction=True)
def test_kiosk_guest_book_checkin_e2e_prepare_and_assert_validates_subscription_and_drop_in_debt(
    tmp_path,
):
    output = tmp_path / "kiosk-guest-book-checkin-fixture.json"
    call_command("prepare_kiosk_guest_book_checkin_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("kiosk-guest-book-checkin-e2e-")
    assert fixture["kiosk_pin"]
    assert fixture["kiosk_pin"].isdigit()
    assert set(fixture["scenarios"]) == {"subscription", "drop_in_debt", "trial_free"}
    assert fixture["scenarios"]["subscription"]["expected"]["is_debt"] is False
    assert fixture["scenarios"]["drop_in_debt"]["expected"]["is_debt"] is True
    assert fixture["scenarios"]["trial_free"]["expected"]["is_debt"] is False
    assert fixture["scenarios"]["trial_free"]["expected"]["enrollment_status"] == ScheduleEnrollment.Status.TRIAL

    with pytest.raises(CommandError, match="guest one-day enrollment"):
        call_command(
            "assert_kiosk_guest_book_checkin_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    kiosk_token = activate_kiosk(pin=fixture["kiosk_pin"])["token"]
    client = TestClient(api)
    auth = {"headers": {"X-Kiosk-Token": kiosk_token}}

    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        for scenario in fixture["scenarios"].values():
            response = client.post(
                "/checkins/kiosk/guest-book-and-checkin/",
                json={
                    "student_id": scenario["student_id"],
                    "schedule_id": scenario["schedule_id"],
                    "training_type_id": scenario["training_type_id"],
                    "checkin_date": scenario["checkin_date"],
                },
                **auth,
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["created"] is True
            assert payload["duplicate"] is False
            assert payload["is_debt"] is scenario["expected"]["is_debt"]
            assert payload["subscription_effect"] == scenario["expected"]["subscription_effect"]
            assert payload["debt_effect"] == scenario["expected"]["debt_effect"]

    stdout = io.StringIO()
    call_command(
        "assert_kiosk_guest_book_checkin_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    subscription = evidence["scenarios"]["subscription"]
    assert subscription["subscription"]["trainings_left"] == fixture["expected"]["trainings_left_after"]
    assert subscription["subscription"]["trainings_used"] == fixture["expected"]["trainings_used_after"]
    assert subscription["checkin"]["is_debt"] is False
    assert subscription["debts"]["count"] == 0

    drop_in_debt = evidence["scenarios"]["drop_in_debt"]
    assert drop_in_debt["subscription"] is None
    assert drop_in_debt["checkin"]["is_debt"] is True
    assert drop_in_debt["debts"]["count"] == 1
    assert drop_in_debt["debts"]["amount"] == fixture["scenarios"]["drop_in_debt"]["expected"]["debt_amount"]
    assert drop_in_debt["trainer_earning_count"] == 0

    trial_free = evidence["scenarios"]["trial_free"]
    assert trial_free["booking"]["enrollment_status"] == ScheduleEnrollment.Status.TRIAL
    assert trial_free["subscription"] is None
    assert trial_free["checkin"]["is_debt"] is False
    assert trial_free["checkin"]["subscription_id"] is None
    assert trial_free["debts"]["count"] == 0
    assert trial_free["cascade_events"][CheckinCascadeEvent.Effect.POST_TRIAL_TASK]["expected"] is True
    assert evidence["booking"] == subscription["booking"]
    assert "kiosk_pin" not in evidence


@pytest.mark.django_db
def test_public_lead_intake_e2e_prepare_and_assert_validates_public_contract(tmp_path):
    output = tmp_path / "public-lead-intake-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_public_lead_intake_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("public-lead-intake-e2e-")
    assert fixture["landing_default_club_id"] == fixture["club_id"]
    assert fixture["payloads"]["first"]["phone"] not in stdout.getvalue()
    assert fixture["payloads"]["repeat_same_phone"]["phone"] == fixture["payloads"]["first"]["phone"]
    assert fixture["payloads"]["existing_active"]["phone"] not in stdout.getvalue()
    assert fixture["payloads"]["existing_lost"]["phone"] not in stdout.getvalue()
    assert fixture["existing_students"]["active"]["status"] == Student.Status.ACTIVE
    assert fixture["existing_students"]["active"]["lead_status"] is None
    assert fixture["existing_students"]["lost"]["status"] == Student.Status.LEAD
    assert fixture["existing_students"]["lost"]["lead_status"] == Student.LeadStatus.NEW
    assert fixture["existing_students"]["lost"]["assigned_trainer_id"] is None
    assert fixture["existing_students"]["lost"]["loss_reason"] is None
    assert fixture["existing_students"]["lost"]["in_pool"] is True
    assert fixture["existing_students"]["lost"]["in_trainer_mine"] is False
    assert (
        fixture["payloads"]["repeat_same_phone"]["idempotency_key"]
        != fixture["payloads"]["first"]["idempotency_key"]
    )

    with pytest.raises(CommandError, match="first lead intake event not found"):
        call_command(
            "assert_public_lead_intake_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from uuid import UUID

    from apps.leads.services import create_landing_lead_intake

    def create_from_payload(label: str):
        payload = fixture["payloads"][label]
        return create_landing_lead_intake(
            club_id=fixture["club_id"],
            name=payload["name"],
            phone=payload["phone"],
            goal=payload["goal"],
            preferred_format=payload["preferred_format"],
            is_child=payload["is_child"],
            consent=payload["consent"],
            source=payload["source"],
            request_id=f"test-public-lead-intake-{label}",
            client_ip_hash=f"client-ip-hash-{label}",
            user_agent=f"public lead intake command test {label}",
            idempotency_key=UUID(payload["idempotency_key"]),
        )

    first_event = create_from_payload("first")
    idempotent_event = create_from_payload("first")
    repeat_event = create_from_payload("repeat_same_phone")
    active_existing_event = create_from_payload("existing_active")
    lost_existing_event = create_from_payload("existing_lost")

    assert idempotent_event.id == first_event.id
    assert repeat_event.id != first_event.id
    assert active_existing_event.student_id == fixture["existing_students"]["active"]["student_id"]
    assert lost_existing_event.student_id == fixture["existing_students"]["lost"]["student_id"]

    stdout = io.StringIO()
    call_command(
        "assert_public_lead_intake_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["student"]["status"] == Student.Status.LEAD
    assert evidence["student"]["lead_status"] == Student.LeadStatus.NEW
    assert evidence["student"]["source"] == Student.Source.WEBSITE
    assert evidence["events"]["first"]["id"] == first_event.id
    assert evidence["events"]["first"]["is_repeat_submission"] is False
    assert evidence["events"]["repeat_same_phone"]["id"] == repeat_event.id
    assert evidence["events"]["repeat_same_phone"]["is_repeat_submission"] is True
    assert evidence["events"]["first_idempotency_key_count"] == 1
    assert evidence["existing_re_submits"]["active"]["event"]["id"] == active_existing_event.id
    assert evidence["existing_re_submits"]["active"]["event"]["is_repeat_submission"] is True
    assert evidence["existing_re_submits"]["active"]["student"]["status"] == Student.Status.ACTIVE
    assert evidence["existing_re_submits"]["active"]["student"]["lead_status"] is None
    assert evidence["existing_re_submits"]["active"]["in_pool"] is False
    assert evidence["existing_re_submits"]["active"]["in_trainer_mine"] is False
    assert evidence["existing_re_submits"]["lost"]["event"]["id"] == lost_existing_event.id
    assert evidence["existing_re_submits"]["lost"]["event"]["is_repeat_submission"] is True
    assert evidence["existing_re_submits"]["lost"]["student"]["status"] == Student.Status.LEAD
    assert evidence["existing_re_submits"]["lost"]["student"]["lead_status"] == Student.LeadStatus.NEW
    assert evidence["existing_re_submits"]["lost"]["student"]["loss_reason"] is None
    assert evidence["existing_re_submits"]["lost"]["in_pool"] is True
    assert evidence["existing_re_submits"]["lost"]["in_trainer_mine"] is False
    assert evidence["invalid_consent"] == {
        "event_created": False,
        "student_created": False,
    }
    assert LeadIntakeEvent.objects.for_club(fixture["club_id"]).filter(student_id=first_event.student_id).count() == 2


@pytest.mark.django_db
def test_kiosk_negative_e2e_prepare_and_assert_validates_duplicate_matches_revoked_and_blocked_students(
    tmp_path,
):
    output = tmp_path / "kiosk-negative-fixture.json"
    with override_settings(TRAINING_GROUP_NEW_WRITES_ENABLED=True):
        call_command("prepare_kiosk_negative_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("kiosk-negative-e2e-")
    assert fixture["kiosk_pin"]
    assert fixture["kiosk_pin"].isdigit()
    assert fixture["shared_guardian"]["phone_suffix"]
    assert fixture["shared_guardian"]["child_a"]["student_id"]
    assert fixture["shared_guardian"]["child_b"]["student_id"]
    assert fixture["shared_guardian"]["child_a"]["training_group_id"]
    assert fixture["shared_guardian"]["child_b"]["training_group_id"]
    assert (
        fixture["shared_guardian"]["child_a"]["training_group_id"]
        != fixture["shared_guardian"]["child_b"]["training_group_id"]
    )
    assert fixture["frozen_student_id"]
    assert fixture["blocked_student_id"]
    assert (
        Student.objects.for_club(fixture["club_id"])
        .get(id=fixture["blocked_student_id"])
        .status
        == Student.Status.AT_RISK
    )
    assert fixture["expected"]["shared_child_trainings_left_before"] == 5
    assert fixture["expected"]["shared_child_trainings_left_after"] == 4

    from apps.attendance.selectors import get_kiosk_checkin_options

    blocked_options = get_kiosk_checkin_options(
        club=Club.objects.get(id=fixture["club_id"]),
        student_id=fixture["blocked_student_id"],
        target_date=date.today(),
    )["options"]
    assert blocked_options
    assert {
        (option["self_checkin_status"], option["reason_code"])
        for option in blocked_options
    } == {("blocked", "student_ineligible")}

    with pytest.raises(CommandError, match="shared guardian child check-in"):
        call_command("assert_kiosk_negative_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        first_result = create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["shared_guardian"]["child_b"]["student_id"],
            schedule_id=fixture["shared_guardian"]["child_b"]["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )
        duplicate_result = create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["shared_guardian"]["child_b"]["student_id"],
            schedule_id=fixture["shared_guardian"]["child_b"]["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )

    assert first_result["created"] is True
    assert duplicate_result["created"] is False
    assert duplicate_result["checkin_id"] == first_result["checkin_id"]

    from apps.common.exceptions import BusinessLogicError

    with pytest.raises(BusinessLogicError, match="заморожена"):
        create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["frozen_student_id"],
            schedule_id=fixture["shared_guardian"]["child_a"]["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )
    with pytest.raises(BusinessLogicError, match="не записан"):
        create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["blocked_student_id"],
            schedule_id=fixture["shared_guardian"]["child_b"]["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )

    stdout = io.StringIO()
    call_command("assert_kiosk_negative_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["duplicate"]["checkin_count"] == 1
    assert (
        evidence["duplicate"]["subscription"]["trainings_left"]
        == fixture["expected"]["shared_child_trainings_left_after"]
    )
    assert evidence["duplicate"]["cascade_event_count"] == 7
    assert evidence["duplicate"]["earning_count"] == 0
    assert evidence["multiple_matches"]["shared_suffix_count"] == 2
    assert evidence["shared_guardian"]["child_a"]["checkin_count"] == 0
    assert evidence["shared_guardian"]["child_a"]["subscription"]["trainings_left"] == 5
    assert evidence["blocked_students"]["frozen"]["checkin_count"] == 0
    assert evidence["blocked_students"]["blocked"]["checkin_count"] == 0

    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        child_a_result = create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["shared_guardian"]["child_a"]["student_id"],
            schedule_id=fixture["shared_guardian"]["child_a"]["schedule_id"],
            training_type_id=fixture["training_type_id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.today(),
        )
    assert child_a_result["created"] is True

    child_a_stdout = io.StringIO()
    call_command(
        "assert_kiosk_negative_e2e",
        fixture=str(output),
        timeout_seconds=0,
        expect_shared_child_a_checkin=True,
        stdout=child_a_stdout,
    )
    child_a_evidence = json.loads(child_a_stdout.getvalue())
    assert child_a_evidence["shared_guardian"]["child_a"]["checkin_count"] == 1
    assert (
        child_a_evidence["shared_guardian"]["child_a"]["schedule_id"]
        == fixture["shared_guardian"]["child_a"]["schedule_id"]
    )

    call_command("deactivate_kiosk_negative_e2e", fixture=str(output), stdout=io.StringIO())
    assert KioskDevice.objects.filter(club_id=fixture["club_id"], is_active=True).count() == 0


@pytest.mark.django_db
def test_trainer_batch_e2e_prepare_and_assert_validates_active_and_frozen_students(tmp_path):
    output = tmp_path / "trainer-batch-fixture.json"
    call_command("prepare_trainer_batch_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-batch-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert TrainingGroupRolloutState.objects.for_club(fixture["club_id"]).get().mode == "off"
    assert fixture["expected"]["active_student_count"] == 2
    assert fixture["expected"]["frozen_student_count"] == 1
    assert fixture["schedule_form"]["created_group_name"]
    assert fixture["schedule_form"]["edited_group_name"]
    assert fixture["unclosed"]["group_name"]
    assert fixture["unclosed"]["date"]
    assert fixture["unclosed"]["schedule_id"]
    assert fixture["unclosed"]["student_id"]

    stdout = io.StringIO()
    with pytest.raises(CommandError, match="group session"):
        call_command("assert_trainer_batch_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)

    close_session_from_existing_checkins(
        club_id=fixture["club_id"],
        schedule_id=fixture["schedule_id"],
        checkin_date=date.today(),
        actor_user_id=fixture["trainer_user_id"],
    )

    with pytest.raises(CommandError, match="schedule form"):
        call_command("assert_trainer_batch_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    from apps.attendance.services.schedule import create_schedule, update_schedule

    form = fixture["schedule_form"]
    form_date = date.fromisoformat(form["date"])
    created_schedule = create_schedule(
        club_id=fixture["club_id"],
        day_of_week=0,
        start_time=time.fromisoformat(form["created_start_time"]),
        end_time=time.fromisoformat(form["created_end_time"]),
        group_name=form["created_group_name"],
        trainer_id=fixture["trainer_id"],
        location_id=form["created_location_id"],
        training_type_id=form["created_training_type_id"],
        one_time_date=form_date,
    )
    update_schedule(
        club_id=fixture["club_id"],
        schedule_id=created_schedule.id,
        start_time=time.fromisoformat(form["edited_start_time"]),
        end_time=time.fromisoformat(form["edited_end_time"]),
        group_name=form["edited_group_name"],
        location_id=form["edited_location_id"],
        training_type_id=form["edited_training_type_id"],
    )

    unclosed = fixture["unclosed"]
    close_session_from_existing_checkins(
        club_id=fixture["club_id"],
        schedule_id=unclosed["schedule_id"],
        checkin_date=date.fromisoformat(unclosed["date"]),
        actor_user_id=fixture["trainer_user_id"],
    )

    stdout = io.StringIO()
    call_command("assert_trainer_batch_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["group_session"]["attendee_count"] == 2
    assert sorted(evidence["checkins"]["student_ids"]) == sorted(fixture["active_student_ids"])
    assert evidence["frozen_student"]["checkin_count"] == 0
    assert evidence["frozen_student"]["debt_count"] == 0
    assert evidence["schedule_form"]["group_name"] == form["edited_group_name"]
    assert evidence["schedule_form"]["one_time_date"] == form["date"]
    assert evidence["schedule_form"]["start_time"] == form["edited_start_time"]
    assert evidence["schedule_form"]["end_time"] == form["edited_end_time"]
    assert evidence["schedule_form"]["location_id"] == form["edited_location_id"]
    assert evidence["schedule_form"]["training_type_id"] == form["edited_training_type_id"]
    assert evidence["unclosed"]["schedule_id"] == unclosed["schedule_id"]
    assert evidence["unclosed"]["date"] == unclosed["date"]
    assert evidence["unclosed"]["student_id"] == unclosed["student_id"]
    assert unclosed["schedule_id"] not in evidence["unclosed"]["remaining_unclosed_ids"]
    assert not Debt.objects.filter(
        club_id=fixture["club_id"],
        student_id=fixture["frozen_student_id"],
    ).exists()


@pytest.mark.django_db
def test_payment_confirm_e2e_prepare_and_assert_validates_money_debt_and_sale_earning(tmp_path):
    output = tmp_path / "payment-confirm-fixture.json"
    with override_settings(
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        call_command("prepare_payment_confirm_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("payment-confirm-e2e-")
    assert fixture["expected"]["payment_original_amount"] == "4000.00"
    assert fixture["expected"]["commercial_journey_protocol_version"] == "v1"
    assert fixture["expected"]["payment_amount"] == "4000.00"
    assert fixture["expected"]["retained_payment_amount"] == "3600.00"
    assert fixture["expected"]["subscription_trainings_left_after_confirm"] == 7
    target_schedule = Schedule.objects.for_club(fixture["club_id"]).get(
        id=fixture["target_schedule_id"]
    )
    assert target_schedule.one_time_date is None
    assert target_schedule.group_name == fixture["target_group"]["name"]
    assert target_schedule.training_group_id == fixture["target_group"]["training_group_id"]
    assert fixture["target_group"]["rollout_mode"] == "active"
    assert fixture["target_group"]["new_writes_enabled"] is True
    assert fixture["target_group"]["manual_operational_admission_enabled"] is True
    assert Schedule.objects.for_club(fixture["club_id"]).filter(
        id=fixture["target_group"]["second_schedule_id"],
        training_group_id=fixture["target_group"]["training_group_id"],
    ).exists()
    assert target_schedule.day_of_week == date.fromisoformat(
        fixture["target_start_date"]
    ).weekday()
    assert fixture["expected"]["student_status_before_confirm"] == Student.Status.LEAD
    assert fixture["expected"]["lead_status_before_confirm"] == Student.LeadStatus.NEW
    assert fixture["expected"]["student_status_after_confirm"] == Student.Status.ACTIVE
    assert fixture["expected"]["lead_status_after_confirm"] is None
    student_ids = [fixture["student_id"], fixture["child"]["id"]]
    assert not Payment.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Subscription.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not ScheduleEnrollment.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Checkin.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Debt.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert Checkin.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["retained"]["id"],
        id=fixture["retained"]["debt_checkin_id"],
        is_debt=True,
    ).exists()
    assert Debt.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["retained"]["id"],
        id=fixture["retained"]["debt_id"],
        resolved_at__isnull=True,
    ).exists()

    from apps.billing.services import create_payment, verify_payment
    from apps.billing.tasks import create_sale_earning
    from apps.students.access_services import open_account_access_for_student

    with override_settings(
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    ):
        with patch("django_q.tasks.async_task"):
            retained_payment = create_payment(
                club_id=fixture["club_id"],
                student_id=fixture["retained"]["id"],
                tariff_id=fixture["tariff_id"],
                payment_method=fixture["expected"]["payment_method"],
                discount_ids=[fixture["discount_id"]],
                debt_ids=[fixture["retained"]["debt_id"]],
                recorded_by_id=fixture["trainer"]["user_id"],
                seller_trainer_id=fixture["trainer_id"],
                target_schedule_id=fixture["target_schedule_id"],
                target_training_group_id=fixture["target_group"]["training_group_id"],
                target_start_date=date.fromisoformat(fixture["target_start_date"]),
                enforce_trainer_group_contract=True,
                create_manual_operational_admission=True,
            )
        fixture["runtime"] = {
            "retained_payment_id": retained_payment.id,
        }
        output.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        admission_stdout = io.StringIO()
        call_command(
            "assert_payment_confirm_e2e",
            fixture=str(output),
            stage="admission",
            timeout_seconds=0,
            stdout=admission_stdout,
        )
        admission_evidence = json.loads(admission_stdout.getvalue())
        assert admission_evidence["ok"] is True
        assert admission_evidence["stage"] == "admission"
        assert admission_evidence["variants"] == []
        assert admission_evidence["retained"] == {
            "payment_id": retained_payment.id,
            "selected_debt_id": fixture["retained"]["debt_id"],
            "checkin_id": None,
        }
    with patch("django_q.tasks.async_task"):
        verify_payment(
            payment_id=retained_payment.id,
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )
    retained_access = open_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["retained"]["id"],
        issued_by_id=fixture["trainer"]["user_id"],
    )
    create_sale_earning(retained_payment.id, fixture["club_id"])

    stdout = io.StringIO()
    call_command("assert_payment_confirm_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["variants"] == []
    assert evidence["retained"]["payment"]["id"] == retained_payment.id
    assert evidence["retained"]["payment"]["amount"] == "3600.00"
    assert evidence["retained"]["subscription"]["trainings_left"] == 7
    assert evidence["retained"]["subscription"]["trainings_used"] == 1
    assert AccountAccess.objects.for_club(fixture["club_id"]).get(
        student_id=fixture["retained"]["id"], role=AccountAccess.Role.STUDENT
    ).id == retained_access.access.id


@pytest.mark.django_db
def test_real_stack_rollout_state_normalizer_creates_only_missing_off_state(tmp_path):
    club = Club.objects.create(
        name="Real stack rollout normalizer",
        city="E2E",
        disciplines=["muay_thai"],
        timezone="Europe/Moscow",
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({"club_id": club.id}), encoding="utf-8")

    stdout = io.StringIO()
    call_command("ensure_real_stack_rollout_state_e2e", fixture=str(fixture_path), stdout=stdout)
    created = json.loads(stdout.getvalue())
    state = TrainingGroupRolloutState.objects.for_club(club).get()

    assert created == {"club_id": club.id, "created": True, "mode": "off"}
    assert state.mode == TrainingGroupRolloutState.Mode.OFF

    update_training_group_rollout_state_for_test(

        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE
    )
    stdout = io.StringIO()
    call_command("ensure_real_stack_rollout_state_e2e", fixture=str(fixture_path), stdout=stdout)

    assert json.loads(stdout.getvalue()) == {"club_id": club.id, "created": False, "mode": "active"}
    assert TrainingGroupRolloutState.objects.for_club(club).get().mode == TrainingGroupRolloutState.Mode.ACTIVE

    update_training_group_rollout_state_for_test(

        TrainingGroupRolloutState.objects.for_club(club),
        mode=TrainingGroupRolloutState.Mode.CONTAINMENT
    )
    stdout = io.StringIO()
    call_command("ensure_real_stack_rollout_state_e2e", fixture=str(fixture_path), stdout=stdout)

    assert json.loads(stdout.getvalue()) == {"club_id": club.id, "created": False, "mode": "containment"}
    assert (
        TrainingGroupRolloutState.objects.for_club(club).get().mode
        == TrainingGroupRolloutState.Mode.CONTAINMENT
    )


@pytest.mark.django_db
def test_real_stack_rollout_state_normalizer_accepts_two_club_fixture(tmp_path):
    first_club = Club.objects.create(
        name="Real stack two-club normalizer A",
        city="E2E",
        disciplines=["muay_thai"],
        timezone="Europe/Moscow",
    )
    second_club = Club.objects.create(
        name="Real stack two-club normalizer B",
        city="E2E",
        disciplines=["muay_thai"],
        timezone="Europe/Moscow",
    )
    fixture_path = tmp_path / "two-club-fixture.json"
    fixture_path.write_text(
        json.dumps({"club_a": {"club_id": first_club.id}, "club_b": {"club_id": second_club.id}}),
        encoding="utf-8",
    )

    stdout = io.StringIO()
    call_command("ensure_real_stack_rollout_state_e2e", fixture=str(fixture_path), stdout=stdout)

    assert json.loads(stdout.getvalue()) == {
        "clubs": [
            {"club_id": first_club.id, "created": True, "mode": "off"},
            {"club_id": second_club.id, "created": True, "mode": "off"},
        ]
    }

@pytest.mark.django_db
def test_real_stack_rollout_state_normalizer_covers_unreferenced_isolated_database_clubs(
    tmp_path,
):
    fixture_club = Club.objects.create(
        name="Real stack referenced club",
        city="E2E",
        disciplines=["muay_thai"],
        timezone="Europe/Moscow",
    )
    control_club = Club.objects.create(
        name="Real stack unreferenced control",
        city="E2E",
        disciplines=["boxing"],
        timezone="Europe/Moscow",
    )
    fixture_path = tmp_path / "fixture-with-control.json"
    fixture_path.write_text(json.dumps({"club_id": fixture_club.id}), encoding="utf-8")

    stdout = io.StringIO()
    call_command(
        "ensure_real_stack_rollout_state_e2e",
        fixture=str(fixture_path),
        all_clubs=True,
        stdout=stdout,
    )

    assert json.loads(stdout.getvalue()) == {
        "clubs": [
            {"club_id": fixture_club.id, "created": True, "mode": "off"},
            {"club_id": control_club.id, "created": True, "mode": "off"},
        ]
    }
    assert TrainingGroupRolloutState.objects.for_club(fixture_club).get().mode == "off"
    assert TrainingGroupRolloutState.objects.for_club(control_club).get().mode == "off"


@pytest.mark.django_db
@pytest.mark.parametrize("clock_reversed", [False, True])
def test_bank_payment_link_e2e_prepare_and_assert_validates_creator_and_debt_contract(
    tmp_path, settings, clock_reversed,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    output = tmp_path / "bank-payment-link-fixture.json"
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        call_command("prepare_bank_payment_link_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("bank-payment-link-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer_student"]["debt_id"]
    assert fixture["target_group"]["schedule_id"]
    assert fixture["target_group"]["start_date"]
    assert fixture["target_group"]["rollout_mode"] == "active"
    assert fixture["target_group"]["new_writes_enabled"] is True
    assert fixture["target_group"]["manual_operational_admission_enabled"] is True
    assert fixture["approval"]["order_id"]
    assert fixture["provider_failure"]["order_id"]
    assert fixture["expiry"]["order_id"]
    assert fixture["finance_workspace"]["manual_payment_id"]
    assert fixture["finance_workspace"]["confirmed_online_payment_id"]
    assert fixture["finance_workspace"]["browser_confirms_manual_payment"] is True
    review_order = BankPaymentOrder.objects.get(id=fixture["owner_manual_review"]["order_id"])
    review_event = BankPaymentProviderEvent.objects.get(order=review_order)
    assert review_order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert review_event.normalized_status_snapshot == "approved"
    assert review_event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
    confirmed_online_payment = Payment.objects.get(
        id=fixture["finance_workspace"]["confirmed_online_payment_id"]
    )
    confirmed_online_order = BankPaymentOrder.objects.get(payment=confirmed_online_payment)
    confirmed_online_event = BankPaymentProviderEvent.objects.get(order=confirmed_online_order)
    assert confirmed_online_payment.status == Payment.Status.CONFIRMED
    assert confirmed_online_order.status == BankPaymentOrder.Status.APPROVED
    assert confirmed_online_event.processing_status == BankPaymentProviderEvent.ProcessingStatus.PROCESSED
    assert "debt_id" not in fixture["student"]
    assert fixture["expected"]["staff_ttl_minutes"] == 10080
    assert fixture["expected"]["self_service_ttl_minutes"] == 4320

    from apps.billing.services import (
        cancel_bank_payment_order,
        create_bank_payment_order,
        process_bank_payment_webhook,
        verify_payment,
    )

    trainer_order = BankPaymentOrder.objects.get(id=fixture["trainer_student"]["order_id"])
    student_order = BankPaymentOrder.objects.get(id=fixture["student"]["order_id"])
    parent_order = BankPaymentOrder.objects.get(id=fixture["parent"]["order_id"])
    with patch("django_q.tasks.async_task"):
        owner_order = create_bank_payment_order(
            club_id=fixture["club_id"],
            student_id=fixture["owner_create_student"]["id"],
            tariff_id=fixture["tariff"]["id"],
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=fixture["owner"]["user_id"],
        )
        reserved_event = DebtSettlementEvent.objects.for_club(fixture["club_id"]).get(
            debt_id=fixture["trainer_student"]["debt_id"],
            payment_id=trainer_order.payment_id,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )
        cancellation_clock = (
            patch("django.utils.timezone.now", return_value=reserved_event.created_at - timedelta(seconds=1))
            if clock_reversed else nullcontext()
        )
        with cancellation_clock:
            cancel_bank_payment_order(
                club_id=fixture["club_id"],
                order_id=trainer_order.id,
                actor_user_id=fixture["trainer"]["user_id"],
                allowed_student_id=fixture["trainer_student"]["id"],
                allowed_sources={BankPaymentOrder.Source.TRAINER},
            )
        if clock_reversed:
            rejected_event = DebtSettlementEvent.objects.for_club(fixture["club_id"]).get(
                debt_id=reserved_event.debt_id,
                payment_id=trainer_order.payment_id,
                event_type=DebtSettlementEvent.EventType.REJECTED,
            )
            assert rejected_event.created_at < reserved_event.created_at
            assert rejected_event.id > reserved_event.id
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(
                {
                    "webhookType": "acquiringInternetPayment",
                    "event_id": "unit-browser-student-approved",
                    "status": "APPROVED",
                    "paymentLinkId": student_order.provider_payment_link_id,
                    "operationId": "unit-browser-student-operation",
                    "amount": str(student_order.amount_snapshot),
                    "paid_at": timezone.now().isoformat(),
                }
            ).encode(),
            headers={},
            request_id="unit-browser-student-approved",
        )
        cancel_bank_payment_order(
            club_id=fixture["club_id"],
            order_id=parent_order.id,
            actor_user_id=fixture["parent"]["user_id"],
            allowed_student_id=fixture["parent"]["child_id"],
            allowed_sources={BankPaymentOrder.Source.PARENT},
        )
        verify_payment(
            payment_id=fixture["finance_workspace"]["manual_payment_id"],
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )

    stdout = io.StringIO()
    call_command("assert_bank_payment_link_e2e", fixture=str(output), stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["orders"]["trainer"]["id"] == fixture["trainer_student"]["order_id"]
    assert evidence["orders"]["trainer"]["target_schedule_id"] == fixture["target_group"]["schedule_id"]
    assert evidence["orders"]["trainer"]["target_start_date"] == fixture["target_group"]["start_date"]
    assert evidence["orders"]["student"]["id"] == fixture["student"]["order_id"]
    assert evidence["orders"]["parent"]["id"] == fixture["parent"]["order_id"]
    assert evidence["orders"]["owner"]["id"] == owner_order.id
    assert evidence["trainer_debt"] == {
        "id": fixture["trainer_student"]["debt_id"],
        "settlement_payment_id": None,
        "settlement_event_types": [
            "reserved",
            "rejected",
        ],
    }
    assert evidence["pending_group_enrollment_count"] == 0
    assert evidence["pending_group_membership_count"] == 0
    assert evidence["group_lifecycle"]["approval"]["membership_count"] == 1
    assert evidence["group_lifecycle"]["approval"]["projection_schedule_ids"] == sorted(
        [
            fixture["target_group"]["schedule_id"],
            fixture["target_group"]["second_schedule_id"],
        ]
    )
    assert evidence["group_lifecycle"]["provider_failure"]["status"] == BankPaymentOrder.Status.FAILED
    assert evidence["group_lifecycle"]["provider_failure"]["membership_count"] == 0
    assert evidence["group_lifecycle"]["expiry"]["status"] == BankPaymentOrder.Status.EXPIRED
    assert evidence["group_lifecycle"]["expiry"]["membership_count"] == 0


@pytest.mark.django_db
def test_training_group_containment_e2e_fixture_preserves_existing_canonical_order_lifecycle(tmp_path, settings):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    output = tmp_path / "training-group-containment-fixture.json"
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        call_command("prepare_training_group_containment_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["target_group"]["rollout_mode"] == TrainingGroupRolloutState.Mode.CONTAINMENT
    assert fixture["target_group"]["new_writes_enabled"] is True
    assert fixture["target_group"]["manual_operational_admission_enabled"] is True
    assert fixture["finance_workspace"]["browser_confirms_manual_payment"] is False
    from apps.billing.services import cancel_bank_payment_order

    with patch("django_q.tasks.async_task"):
        cancel_bank_payment_order(
            club_id=fixture["club_id"],
            order_id=fixture["trainer_student"]["order_id"],
            actor_user_id=fixture["trainer"]["user_id"],
            allowed_student_id=fixture["trainer_student"]["id"],
            allowed_sources={BankPaymentOrder.Source.TRAINER},
        )
    assert fixture["containment"]["existing_order_id"] == fixture["trainer_student"]["order_id"]
    assert (
        fixture["containment"]["existing_order_training_group_id"]
        == fixture["target_group"]["training_group_id"]
    )
    assert fixture["containment"]["new_intent_selection_mode"] == "disabled"

    with patch("django_q.tasks.async_task"):
        stdout = io.StringIO()
        call_command("assert_training_group_containment_e2e", fixture=str(output), stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert "owner" not in evidence["orders"]
    assert evidence["containment"] == {
        "mode": TrainingGroupRolloutState.Mode.CONTAINMENT,
        "existing_order_id": fixture["trainer_student"]["order_id"],
        "existing_order_training_group_id": fixture["target_group"]["training_group_id"],
        "new_intent_selection_mode": "disabled",
    }
    assert evidence["group_lifecycle"]["approval"]["membership_count"] == 1


@pytest.mark.django_db
def test_subscription_payout_policy_e2e_prepare_and_assert_validates_component_payouts(tmp_path):
    output = tmp_path / "subscription-payout-policy-fixture.json"
    with patch("django_q.tasks.async_task"):
        call_command("prepare_subscription_payout_policy_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("subscription-payout-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["expected"]["personal_sale_amount"] == "2000.00"
    assert fixture["expected"]["personal_checkin_salary_amount"] == "400.00"
    assert fixture["expected"]["mini_salary_amount"] == "600.00"
    payment = Payment.objects.for_club(fixture["club_id"]).get(id=fixture["payment_id"])
    assert payment.status == Payment.Status.PENDING
    mini_component = SubscriptionComponent.objects.for_club(fixture["club_id"]).get(
        subscription_id=fixture["mini_subscription_id"],
        training_type_id=fixture["mini_training_type_id"],
    )
    assert mini_component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN

    with pytest.raises(CommandError, match="payment not confirmed"):
        call_command(
            "assert_subscription_payout_policy_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.billing.services import verify_payment
    from apps.billing.tasks import create_sale_earning

    with patch("django_q.tasks.async_task"):
        verify_payment(
            payment_id=fixture["payment_id"],
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )
    create_sale_earning(fixture["payment_id"], fixture["club_id"])

    stdout = io.StringIO()
    with patch("apps.attendance.services.async_task"):
        call_command(
            "assert_subscription_payout_policy_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=stdout,
        )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["payment"]["status"] == Payment.Status.CONFIRMED
    assert evidence["sale_earning"]["amount"] == "2000.00"
    assert evidence["sale_earning"]["basis"] == "10000.00"
    assert evidence["sale_earning"]["payout_policy"] == Tariff.PayoutPolicy.ON_PAYMENT
    assert evidence["personal_checkin_earning"]["amount"] == "400.00"
    assert evidence["personal_checkin_earning"]["basis"] == "2000.00"
    assert evidence["personal_checkin_earning"]["payout_policy"] == Tariff.PayoutPolicy.ON_CHECKIN
    assert evidence["mini_earning"]["amount"] == "600.00"
    assert evidence["mini_earning"]["basis"] == "2000.00"
    assert evidence["mini_earning"]["payout_policy"] == Tariff.PayoutPolicy.ON_CHECKIN
    assert evidence["mini_component"]["credits_left"] == 3
    assert evidence["mini_component"]["credits_used"] == 1

    payment.refresh_from_db()
    TrainerEarning.objects.create(
        club_id=fixture["club_id"],
        trainer_id=fixture["personal_owner_trainer_id"],
        payment=payment,
        earning_source=TrainerEarning.Source.SALE,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("20.00"),
        rate_percent=Decimal("20.00"),
        subscription_price=Decimal("100.00"),
        payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    with pytest.raises(CommandError, match="sale earning count mismatch"):
        with patch("apps.attendance.services.async_task"):
            call_command(
                "assert_subscription_payout_policy_e2e",
                fixture=str(output),
                timeout_seconds=0,
                stdout=io.StringIO(),
            )


@pytest.mark.django_db
def test_hybrid_package_entitlements_e2e_prepare_and_assert_validates_components(tmp_path):
    output = tmp_path / "hybrid-package-entitlements-fixture.json"
    with patch("django_q.tasks.async_task"):
        call_command("prepare_hybrid_package_entitlements_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("hybrid-entitlements-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["expected"]["sale_amount"] == "35.00"
    components = SubscriptionComponent.objects.for_club(fixture["club_id"]).filter(
        subscription_id=fixture["subscription_id"],
    )
    assert components.count() == 2
    assert {
        component.training_type_id: component.trainer_payout_policy_snapshot
        for component in components
    } == {
        fixture["group_training_type_id"]: Tariff.PayoutPolicy.ON_PAYMENT,
        fixture["personal_training_type_id"]: Tariff.PayoutPolicy.ON_CHECKIN,
    }
    payment = Payment.objects.for_club(fixture["club_id"]).get(id=fixture["payment_id"])
    assert payment.status == Payment.Status.PENDING

    with pytest.raises(CommandError, match="payment not confirmed"):
        call_command(
            "assert_hybrid_package_entitlements_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.billing.services import verify_payment
    from apps.billing.tasks import create_sale_earning

    with patch("django_q.tasks.async_task"):
        verify_payment(
            payment_id=fixture["payment_id"],
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )
    create_sale_earning(fixture["payment_id"], fixture["club_id"])

    stdout = io.StringIO()
    with patch("apps.attendance.services.async_task"):
        call_command(
            "assert_hybrid_package_entitlements_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=stdout,
        )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["payment"]["status"] == Payment.Status.CONFIRMED
    assert evidence["sale_earning"]["amount"] == "35.00"
    assert evidence["sale_earning"]["basis"] == "3500.00"
    assert evidence["debt"]["resolution_type"] == "payment"
    assert evidence["personal_component"]["credits_left"] == 2
    assert evidence["personal_component"]["credits_used"] == 1
    assert evidence["personal_earning"]["amount"] == "40.00"
    assert evidence["personal_earning"]["basis"] == "2000.00"
    assert evidence["group_component"]["credits_used"] == 2
    assert evidence["group_component"]["weekly_limit"] == 2
    assert len(evidence["group_checkin_ids"]) == 2

    payment.refresh_from_db()
    TrainerEarning.objects.create(
        club_id=fixture["club_id"],
        trainer_id=fixture["group_trainer_id"],
        payment=payment,
        earning_source=TrainerEarning.Source.SALE,
        earning_type=TrainingType.Kind.GROUP,
        amount=Decimal("1.00"),
        rate_percent=Decimal("1.00"),
        subscription_price=Decimal("100.00"),
        payout_policy_snapshot=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    with pytest.raises(CommandError, match="sale earning count mismatch"):
        with patch("apps.attendance.services.async_task"):
            call_command(
                "assert_hybrid_package_entitlements_e2e",
                fixture=str(output),
                timeout_seconds=0,
                stdout=io.StringIO(),
            )


@pytest.mark.django_db
def test_account_access_login_e2e_prepare_and_assert_validates_student_and_parent_phone_login(tmp_path):
    output = tmp_path / "account-access-login-fixture.json"
    prepare_stdout = io.StringIO()
    call_command("prepare_account_access_login_e2e", output=str(output), stdout=prepare_stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("account-access-login-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["student_id"] != fixture["child_student_id"]
    assert fixture["child"]["parent_phone_input"]
    assert fixture["child"]["parent_username"].startswith("+7")
    assert fixture["trainer"]["email"] not in prepare_stdout.getvalue()
    assert fixture["trainer"]["password"] not in prepare_stdout.getvalue()
    assert fixture["child"]["parent_phone_input"] not in prepare_stdout.getvalue()

    with pytest.raises(CommandError, match="student is not linked to a user"):
        call_command("assert_account_access_login_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    from apps.students.access_services import open_account_access_for_student, reset_account_access_for_student

    student_access = open_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["student_id"],
        issued_by_id=fixture["trainer"]["user_id"],
    )
    parent_access = open_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["child_student_id"],
        parent_phone=fixture["child"]["parent_phone_input"],
        issued_by_id=fixture["trainer"]["user_id"],
    )
    stdout = io.StringIO()
    call_command("assert_account_access_login_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["student"]["user_id"] == student_access.user.id
    assert evidence["account_access"]["role"] == AccountAccess.Role.STUDENT
    assert evidence["membership"]["role"] == ClubMembership.Role.STUDENT
    assert evidence["child"]["user_id"] is None
    assert evidence["child"]["parent_user_id"] == parent_access.user.id
    assert evidence["parent_account_access"]["role"] == AccountAccess.Role.PARENT
    assert evidence["parent_account_access"]["user_id"] == parent_access.user.id
    assert evidence["parent_membership"]["role"] == ClubMembership.Role.PARENT
    assert evidence["subscription"]["status"] == Subscription.Status.PENDING
    assert evidence["subscription"]["paid"] is False
    assert evidence["child_subscription"]["status"] == Subscription.Status.PENDING
    assert evidence["child_subscription"]["paid"] is False

    reset_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["student_id"],
        reset_by_id=fixture["trainer"]["user_id"],
    )
    reset_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["child_student_id"],
        reset_by_id=fixture["trainer"]["user_id"],
    )
    retry_stdout = io.StringIO()
    call_command(
        "assert_account_access_login_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=retry_stdout,
    )
    retry_evidence = json.loads(retry_stdout.getvalue())
    assert retry_evidence["account_access"]["status"] == AccountAccess.Status.RESET
    assert retry_evidence["parent_account_access"]["status"] == AccountAccess.Status.RESET


@pytest.mark.django_db
def test_trainer_payroll_close_correction_e2e_prepare_and_assert_validates_role_and_closed_mutation_guards(
    tmp_path,
):
    output = tmp_path / "trainer-payroll-close-correction-fixture.json"
    prepare_stdout = io.StringIO()
    call_command("prepare_trainer_payroll_close_correction_e2e", output=str(output), stdout=prepare_stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-payroll-close-correction-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["admin"]["email"]
    assert fixture["admin"]["password"]
    assert fixture["admin"]["role"] == ClubMembership.Role.ADMIN
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer"]["role"] == ClubMembership.Role.TRAINER
    assert fixture["salary_mutation"]["expected_error_code"] == "payroll_period_closed"
    assert fixture["owner"]["password"] not in prepare_stdout.getvalue()
    assert fixture["admin"]["password"] not in prepare_stdout.getvalue()
    assert fixture["trainer"]["password"] not in prepare_stdout.getvalue()

    club = Club.objects.get(id=fixture["club_id"])
    assert ClubMembership.objects.get(user_id=fixture["admin"]["user_id"], club=club).role == ClubMembership.Role.ADMIN
    assert ClubMembership.objects.get(
        user_id=fixture["trainer"]["user_id"],
        club=club,
    ).role == ClubMembership.Role.TRAINER

    with pytest.raises(CommandError, match="manual correction .* adjustment not found"):
        call_command(
            "assert_trainer_payroll_close_correction_e2e",
            fixture=str(output),
            stage="corrected",
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.trainers.services import close_trainer_payroll_period, correct_trainer_earning

    correct_trainer_earning(
        club_id=fixture["club_id"],
        earning_id=fixture["earnings"]["corrected_id"],
        target_trainer_id=fixture["target_trainer"]["trainer_id"],
        reason=fixture["expected"]["correction_reason"],
        actor_user_id=fixture["owner"]["user_id"],
        idempotency_key="command-test-payroll-correction",
    )

    corrected_stdout = io.StringIO()
    call_command(
        "assert_trainer_payroll_close_correction_e2e",
        fixture=str(output),
        stage="corrected",
        timeout_seconds=0,
        stdout=corrected_stdout,
    )
    corrected_evidence = json.loads(corrected_stdout.getvalue())
    assert corrected_evidence["ok"] is True
    assert corrected_evidence["stage"] == "corrected"

    close_trainer_payroll_period(
        club_id=fixture["club_id"],
        period_start=date.fromisoformat(fixture["period"]["date_from"]),
        period_end=date.fromisoformat(fixture["period"]["date_to"]),
        reason=fixture["period"]["reason"],
        actor_user_id=fixture["admin"]["user_id"],
    )

    closed_stdout = io.StringIO()
    call_command(
        "assert_trainer_payroll_close_correction_e2e",
        fixture=str(output),
        stage="closed",
        timeout_seconds=0,
        stdout=closed_stdout,
    )
    evidence = json.loads(closed_stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["stage"] == "closed"
    assert evidence["blocked_correction"]["code"] == "payroll_period_closed"
    assert evidence["blocked_salary_calculation"]["code"] == "payroll_period_closed"
    assert evidence["blocked_salary_calculation"]["earning_created"] is False
    assert TrainerPayrollPeriodClose.objects.for_club(fixture["club_id"]).count() == 1
    assert not TrainerEarning.objects.for_club(fixture["club_id"]).filter(
        checkin_id=evidence["blocked_salary_calculation"]["checkin_id"],
    ).exists()


@pytest.mark.django_db
def test_trainer_package_transfer_e2e_prepare_and_assert_validates_owner_transfer_and_salary(tmp_path):
    output = tmp_path / "trainer-package-transfer-fixture.json"
    prepare_stdout = io.StringIO()
    call_command("prepare_trainer_package_transfer_e2e", output=str(output), stdout=prepare_stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-package-transfer-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["actual_trainer"]["email"]
    assert fixture["actual_trainer"]["password"]
    assert fixture["owner"]["email"] not in prepare_stdout.getvalue()
    assert fixture["owner"]["password"] not in prepare_stdout.getvalue()
    assert fixture["actual_trainer"]["email"] not in prepare_stdout.getvalue()
    assert fixture["actual_trainer"]["password"] not in prepare_stdout.getvalue()
    assert fixture["checkin_date"]
    assert fixture["package_owner_trainer_id"] != fixture["actual_trainer_id"]
    assert fixture["expected"]["salary_amount"] == "500.00"

    with pytest.raises(CommandError, match="subscription not created"):
        call_command(
            "assert_trainer_package_transfer_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.billing.services import create_subscription

    subscription = create_subscription(
        club_id=fixture["club_id"],
        student_id=fixture["student_id"],
        tariff_id=fixture["tariff_id"],
        seller_trainer_id=fixture["package_owner_trainer_id"],
        package_owner_trainer_id=fixture["package_owner_trainer_id"],
        recorded_by_id=fixture["owner"]["user_id"],
        payment_method=Payment.Method.CASH,
    )

    stdout = io.StringIO()
    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        call_command("assert_trainer_package_transfer_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["subscription"]["id"] == subscription.id
    assert evidence["allocation"]["owner_trainer_id"] == fixture["package_owner_trainer_id"]
    assert evidence["checkin"]["date"] == fixture["checkin_date"]
    assert evidence["checkin"]["trainer_id"] == fixture["actual_trainer_id"]
    assert evidence["salary_cascade"]["task_name"] == "apps.attendance.tasks.calculate_salary"
    assert evidence["salary_cascade"]["expected"] is True
    assert evidence["salary_cascade"]["calculation_basis"] == "checkin_salary_snapshot"
    assert evidence["salary_cascade"]["snapshot_provenance"] == "checkin_queue"
    assert evidence["earning"]["trainer_id"] == fixture["actual_trainer_id"]
    assert evidence["earning"]["amount"] == "500.00"
    assert evidence["package_transfer"]["counterparty_trainer_id"] == fixture["package_owner_trainer_id"]
    assert evidence["package_transfer"]["payable_amount_delta"] == "0.00"
    assert evidence["salary"]["payable_total"] == "500.00"
    assert evidence["pnl"]["salary_expenses"] == "500.00"
    assert TrainerPackageAllocation.objects.for_club(fixture["club_id"]).filter(subscription=subscription).count() == 1
    assert TrainerEarningAdjustment.objects.for_club(fixture["club_id"]).filter(
        kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
    ).count() == 1

    retry_stdout = io.StringIO()
    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        call_command("assert_trainer_package_transfer_e2e", fixture=str(output), timeout_seconds=0, stdout=retry_stdout)
    retry_evidence = json.loads(retry_stdout.getvalue())

    assert retry_evidence["subscription"]["id"] == subscription.id
    assert retry_evidence["checkin"]["created"] is False
    assert TrainerPackageAllocation.objects.for_club(fixture["club_id"]).filter(subscription=subscription).count() == 1
    assert TrainerEarningAdjustment.objects.for_club(fixture["club_id"]).filter(
        kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
    ).count() == 1


@pytest.mark.django_db
def test_student_parent_self_booking_e2e_prepare_and_legacy_assertion_validate_slots(tmp_path):
    output = tmp_path / "student-parent-self-booking-fixture.json"
    prepare_stdout = io.StringIO()
    call_command("prepare_student_parent_self_booking_e2e", output=str(output), stdout=prepare_stdout)
    call_command(
        "ensure_real_stack_rollout_state_e2e",
        fixture=str(output),
        all_clubs=True,
        stdout=io.StringIO(),
    )
    fixture = _load_fixture(output)
    legacy = fixture["legacy"]

    assert fixture["fixture_id"].startswith("student-parent-self-booking-e2e-")
    assert legacy["student"]["email"]
    assert legacy["student"]["password"]
    assert legacy["parent"]["email"]
    assert legacy["parent"]["password"]
    assert legacy["student"]["password"] not in prepare_stdout.getvalue()
    assert legacy["parent"]["password"] not in prepare_stdout.getvalue()
    assert legacy["student_personal_slot_id"] != legacy["parent_personal_slot_id"]
    assert legacy["expected"]["student_personal_time_label"] == "10:00-11:00"
    assert legacy["expected"]["parent_personal_time_label"] == "14:00-15:00"

    with pytest.raises(CommandError, match="booking enrollment not found"):
        call_command(
            "assert_student_parent_self_booking_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import (
        book_guest_group_visit,
        book_personal_availability_slot,
        cancel_guest_booking,
        cancel_personal_booking,
    )

    club_id = legacy["club_id"]
    booking_date = date.fromisoformat(legacy["booking_date"])
    student_id = legacy["student"]["student_id"]
    student_user_id = legacy["student"]["user_id"]
    child_id = legacy["parent"]["child_id"]
    parent_user_id = legacy["parent"]["user_id"]

    student_group = book_guest_group_visit(
        club_id=club_id,
        schedule_id=legacy["group_schedule_id"],
        target_date=booking_date,
        student_id=student_id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        actor_user_id=student_user_id,
        idempotency_key=f"student-self-booking-{student_id}-{legacy['group_schedule_id']}-{booking_date.isoformat()}",
        created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        allow_lead_conversion=False,
        require_financial_eligibility=True,
    )
    parent_group = book_guest_group_visit(
        club_id=club_id,
        schedule_id=legacy["group_schedule_id"],
        target_date=booking_date,
        student_id=child_id,
        origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
        actor_user_id=parent_user_id,
        idempotency_key=f"parent-self-booking-{child_id}-{legacy['group_schedule_id']}-{booking_date.isoformat()}",
        created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        allow_lead_conversion=False,
        require_financial_eligibility=True,
    )

    student_slot = PersonalAvailabilitySlot.objects.for_club(club_id).get(
        id=legacy["student_personal_slot_id"]
    )
    parent_slot = PersonalAvailabilitySlot.objects.for_club(club_id).get(
        id=legacy["parent_personal_slot_id"]
    )
    student_subscription = Subscription.objects.for_club(club_id).get(
        student_id=student_id,
        tariff__training_type_id=student_slot.training_type_id,
        status=Subscription.Status.ACTIVE,
    )
    child_subscription = Subscription.objects.for_club(club_id).get(
        student_id=child_id,
        tariff__training_type_id=parent_slot.training_type_id,
        status=Subscription.Status.ACTIVE,
    )
    student_personal = book_personal_availability_slot(
        club_id=club_id,
        slot_id=student_slot.id,
        student_id=student_id,
        actor_user_id=student_user_id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        subscription_id=student_subscription.id,
        idempotency_key=f"student-personal-self-booking-{student_id}-{student_slot.id}",
    )
    parent_personal = book_personal_availability_slot(
        club_id=club_id,
        slot_id=parent_slot.id,
        student_id=child_id,
        actor_user_id=parent_user_id,
        origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
        subscription_id=child_subscription.id,
        idempotency_key=f"parent-personal-self-booking-{child_id}-{parent_slot.id}",
    )

    cancel_guest_booking(
        club_id=club_id,
        enrollment_id=student_group.enrollment.id,
        actor_user_id=student_user_id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
    )
    cancel_personal_booking(
        club_id=club_id,
        enrollment_id=student_personal.enrollment.id,
        actor_user_id=student_user_id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
    )
    cancel_guest_booking(
        club_id=club_id,
        enrollment_id=parent_group.enrollment.id,
        actor_user_id=parent_user_id,
        origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
    )
    cancel_personal_booking(
        club_id=club_id,
        enrollment_id=parent_personal.enrollment.id,
        actor_user_id=parent_user_id,
        origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
    )

    from apps.common.management.commands.assert_student_parent_self_booking_e2e import (
        Command as AssertStudentParentSelfBookingCommand,
    )

    evidence = AssertStudentParentSelfBookingCommand()._collect_legacy_evidence(legacy)

    assert evidence["student_group"]["origin"] == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    assert evidence["parent_group"]["origin"] == ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
    assert evidence["student_personal"]["slot_id"] == legacy["student_personal_slot_id"]
    assert evidence["student_personal"]["origin"] == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    assert evidence["parent_personal"]["slot_id"] == legacy["parent_personal_slot_id"]
    assert evidence["parent_personal"]["origin"] == ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
    assert evidence["reopened_personal_slot"]["slot_id"] == legacy["student_personal_slot_id"]
    assert evidence["reopened_personal_slot"]["booked_events"] == 1
    assert evidence["reopened_parent_personal_slot"]["slot_id"] == legacy["parent_personal_slot_id"]
    assert evidence["reopened_parent_personal_slot"]["booked_events"] == 1
    assert evidence["visibility"]["parent_child_ids"] == [child_id]


@pytest.mark.django_db
def test_trainer_guest_personal_booking_e2e_prepare_and_assert_validates_booking_layer(tmp_path):
    output = tmp_path / "trainer-guest-personal-booking-fixture.json"
    prepare_stdout = io.StringIO()
    call_command("prepare_trainer_guest_personal_booking_e2e", output=str(output), stdout=prepare_stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-guest-personal-booking-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer"]["email"] not in prepare_stdout.getvalue()
    assert fixture["trainer"]["password"] not in prepare_stdout.getvalue()
    assert fixture["group_schedule_id"]
    assert fixture["guest_student"]["student_id"] != fixture["personal_student"]["student_id"]
    assert fixture["expected"]["personal_trainings_left"] == 4
    assert fixture["expected"]["personal_idempotency_key_prefix"] == "trainer-personal-booking:"

    with pytest.raises(CommandError, match="guest enrollment not found"):
        call_command(
            "assert_trainer_guest_personal_booking_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import book_guest_group_visit, book_personal_session

    booking_date = date.fromisoformat(fixture["booking_date"])
    personal_date = date.fromisoformat(fixture["personal_booking"]["date"])
    starts_at = timezone.make_aware(
        timezone.datetime.combine(personal_date, time.fromisoformat(fixture["personal_booking"]["start_time"])),
        timezone=timezone.get_current_timezone(),
    )
    ends_at = timezone.make_aware(
        timezone.datetime.combine(personal_date, time.fromisoformat(fixture["personal_booking"]["end_time"])),
        timezone=timezone.get_current_timezone(),
    )

    guest_booking = book_guest_group_visit(
        club_id=fixture["club_id"],
        schedule_id=fixture["group_schedule_id"],
        target_date=booking_date,
        student_id=fixture["guest_student"]["student_id"],
        origin=ScheduleBookingEvent.Origin.WALK_IN_CHECKIN,
        actor_user_id=fixture["trainer"]["user_id"],
        idempotency_key=fixture["expected"]["guest_idempotency_key"],
    )
    personal_idempotency_key = f"{fixture['expected']['personal_idempotency_key_prefix']}command-test-stable-suffix"
    personal_booking = book_personal_session(
        club_id=fixture["club_id"],
        student_id=fixture["personal_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        subscription_id=fixture["personal_subscription_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        idempotency_key=personal_idempotency_key,
    )

    stdout = io.StringIO()
    call_command(
        "assert_trainer_guest_personal_booking_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["guest_booking"]["enrollment_id"] == guest_booking.enrollment.id
    assert evidence["guest_booking"]["origin"] == ScheduleBookingEvent.Origin.WALK_IN_CHECKIN
    assert evidence["guest_booking"]["created_from"] == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
    assert evidence["personal_booking"]["schedule_id"] == personal_booking.schedule.id
    assert evidence["personal_booking"]["enrollment_id"] == personal_booking.enrollment.id
    assert evidence["personal_booking"]["origin"] == ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION
    assert evidence["personal_booking"]["created_from"] == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
    assert evidence["personal_booking"]["subscription_id"] == fixture["personal_subscription_id"]
    personal_event = ScheduleBookingEvent.objects.for_club(fixture["club_id"]).get(
        id=evidence["personal_booking"]["event_id"]
    )
    assert personal_event.metadata["idempotency_key"] == personal_idempotency_key
    assert evidence["no_premature_side_effects"] == {
        "checkins": 0,
        "debts": 0,
        "trainer_earnings": 0,
    }
    assert evidence["subscription"]["trainings_left"] == 4
    assert evidence["subscription"]["trainings_used"] == 0


@pytest.mark.django_db
@override_settings(
    UNIFIED_CLIENT_JOURNEY_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
)
def test_payment_reject_e2e_prepare_and_assert_validates_released_debt_and_no_financial_side_effects(tmp_path):
    output = tmp_path / "payment-reject-fixture.json"
    with override_settings(
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    ):
        with patch("django_q.tasks.async_task"), patch("apps.attendance.services.async_task"):
            call_command("prepare_payment_reject_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("payment-reject-e2e-")
    assert fixture["expected"]["commercial_journey_protocol_version"] == "v2"
    assert fixture["expected"]["rejection_reason"] == "E2E receipt mismatch"
    assert fixture["schedule"]["rollout_mode"] == "active"
    assert fixture["schedule"]["new_writes_enabled"] is True
    assert fixture["schedule"]["manual_operational_admission_enabled"] is True
    assert Schedule.objects.for_club(fixture["club_id"]).filter(
        id=fixture["schedule"]["id"],
        training_group_id=fixture["schedule"]["training_group_id"],
    ).exists()
    student_ids = [fixture["child"]["id"], fixture["adult"]["id"]]
    assert not Payment.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Subscription.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not ScheduleEnrollment.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Checkin.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()
    assert not Debt.objects.for_club(fixture["club_id"]).filter(student_id__in=student_ids).exists()

    from apps.billing.services import create_payment, verify_payment
    from apps.students.access_services import open_account_access_for_student

    with override_settings(
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        child_payment = create_payment(
            club_id=fixture["club_id"],
            student_id=fixture["child"]["id"],
            tariff_id=fixture["tariff_id"],
            payment_method=Payment.Method.CASH,
            recorded_by_id=fixture["trainer"]["user_id"],
            seller_trainer_id=fixture["trainer_id"],
            target_schedule_id=fixture["schedule_id"],
            target_training_group_id=fixture["schedule"]["training_group_id"],
            target_start_date=date.fromisoformat(fixture["schedule"]["start_date"]),
            enforce_trainer_group_contract=True,
            create_manual_operational_admission=True,
        )
        adult_payment = create_payment(
            club_id=fixture["club_id"],
            student_id=fixture["adult"]["id"],
            tariff_id=fixture["tariff_id"],
            payment_method=Payment.Method.CASH,
            recorded_by_id=fixture["trainer"]["user_id"],
            seller_trainer_id=fixture["trainer_id"],
            target_schedule_id=fixture["schedule_id"],
            target_training_group_id=fixture["schedule"]["training_group_id"],
            target_start_date=date.fromisoformat(fixture["schedule"]["start_date"]),
            enforce_trainer_group_contract=True,
            create_manual_operational_admission=True,
        )
    parent_access = open_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["child"]["id"],
        parent_phone=fixture["child"]["parent_phone_input"],
        issued_by_id=fixture["trainer"]["user_id"],
    )
    adult_access = open_account_access_for_student(
        club_id=fixture["club_id"],
        student_id=fixture["adult"]["id"],
        issued_by_id=fixture["trainer"]["user_id"],
    )
    fixture["runtime"] = {
        "adult_payment_id": adult_payment.id,
        "child_payment_id": child_payment.id,
    }
    output.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    admission_stdout = io.StringIO()
    call_command(
        "assert_payment_reject_e2e",
        fixture=str(output),
        stage="admission",
        timeout_seconds=0,
        stdout=admission_stdout,
    )
    admission_evidence = json.loads(admission_stdout.getvalue())
    assert admission_evidence == {
        "ok": True,
        "stage": "admission",
        "variants": [
            {"label": "child", "payment_id": child_payment.id, "status": Payment.Status.PENDING},
            {"label": "adult", "payment_id": adult_payment.id, "status": Payment.Status.PENDING},
        ],
    }
    with patch("apps.attendance.services.async_task"):
        checkin_ids = {}
        for label, student_id in zip(("child", "adult"), student_ids, strict=True):
            checkin_ids[label] = create_checkin(
                club_id=fixture["club_id"],
                student_id=student_id,
                schedule_id=fixture["schedule_id"],
                training_type_id=fixture["training_type_id"],
                source=Checkin.Source.KIOSK,
                checkin_date=date.fromisoformat(fixture["schedule"]["start_date"]),
            )["checkin_id"]
    fixture["runtime"].update(
        child_checkin_id=checkin_ids["child"],
        adult_checkin_id=checkin_ids["adult"],
    )
    output.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    checked_in_stdout = io.StringIO()
    call_command(
        "assert_payment_reject_e2e",
        fixture=str(output),
        stage="checked_in",
        timeout_seconds=0,
        stdout=checked_in_stdout,
    )
    checked_in_evidence = json.loads(checked_in_stdout.getvalue())
    assert checked_in_evidence["ok"] is True
    assert checked_in_evidence["stage"] == "checked_in"

    with patch("django_q.tasks.async_task"):
        verify_payment(
            payment_id=child_payment.id,
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="reject",
            rejection_reason=fixture["expected"]["rejection_reason"],
        )
        verify_payment(
            payment_id=adult_payment.id,
            club_id=fixture["club_id"],
            verified_by_id=fixture["owner"]["user_id"],
            action="reject",
            rejection_reason=fixture["expected"]["rejection_reason"],
        )

    stdout = io.StringIO()
    call_command("assert_payment_reject_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["stage"] == "rejected"
    assert {variant["payment_id"] for variant in evidence["variants"]} == {
        child_payment.id,
        adult_payment.id,
    }
    assert {tuple(variant["projection_schedule_ids"]) for variant in evidence["variants"]} == {
        tuple(sorted([fixture["schedule"]["id"], fixture["schedule"]["second_schedule_id"]]))
    }
    assert {tuple(variant["projection_statuses"]) for variant in evidence["variants"]} == {
        (ScheduleEnrollment.Status.CANCELLED, ScheduleEnrollment.Status.CANCELLED)
    }
    assert {variant["second_slot_membership_eligible"] for variant in evidence["variants"]} == {False}
    assert AccountAccess.objects.for_club(fixture["club_id"]).get(
        student_id=fixture["child"]["id"], role=AccountAccess.Role.PARENT
    ).id == parent_access.access.id
    assert AccountAccess.objects.for_club(fixture["club_id"]).get(
        student_id=fixture["adult"]["id"], role=AccountAccess.Role.STUDENT
    ).id == adult_access.access.id


@pytest.mark.django_db
def test_debt_writeoff_e2e_prepare_and_assert_validates_audit_and_reserved_protection(tmp_path):
    output = tmp_path / "debt-writeoff-fixture.json"
    with patch("django_q.tasks.async_task"):
        call_command("prepare_debt_writeoff_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("debt-writeoff-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["expected"]["writeoff_reason"] == "E2E manual write-off"
    assert fixture["expected"]["reserved_error_text"] == "ожидающей оплате"
    assert fixture["expected"]["closed_error_text"] == "Период выплат за дату долга уже закрыт"
    assert fixture["closed_debt_id"]
    assert fixture["closed_checkin_id"]
    assert fixture["payroll_close_id"]
    assert TrainerPayrollPeriodClose.objects.for_club(fixture["club_id"]).filter(
        id=fixture["payroll_close_id"],
    ).exists()

    with pytest.raises(CommandError, match="target debt was not written off"):
        call_command("assert_debt_writeoff_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    from apps.billing.services import write_off_debt

    write_off_debt(
        debt_id=fixture["target_debt_id"],
        club_id=fixture["club_id"],
        written_off_by_id=fixture["owner"]["user_id"],
        reason=fixture["expected"]["writeoff_reason"],
    )

    stdout = io.StringIO()
    call_command("assert_debt_writeoff_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["target_debt"]["resolved"] is True
    assert evidence["target_debt"]["resolution_type"] == "writeoff"
    assert evidence["target_writeoff_event"]["reason"] == "E2E manual write-off"
    assert evidence["target_lifecycle_events"] == [
        DebtLifecycleEvent.EventType.WRITTEN_OFF,
    ]
    assert evidence["reserved_debt"]["resolved"] is False
    assert evidence["reserved_debt"]["settlement_payment_id"] == fixture["reserved_payment_id"]
    assert evidence["reserved_writeoff_event_count"] == 0
    assert evidence["reserved_lifecycle_events"] == [
        DebtLifecycleEvent.EventType.RESERVED,
    ]
    assert evidence["closed_period_debt"]["resolved"] is False
    assert evidence["closed_period_debt"]["resolution_type"] == ""
    assert evidence["closed_period_debt"]["writeoff_event_count"] == 0
    assert DebtLifecycleEvent.EventType.WRITTEN_OFF not in evidence["closed_period_debt"]["lifecycle_events"]
    assert evidence["closed_period_debt"]["payroll_close_id"] == fixture["payroll_close_id"]


@pytest.mark.django_db
def test_freeze_lifecycle_e2e_prepare_and_assert_validates_approve_reject_and_unfreeze(tmp_path):
    output = tmp_path / "freeze-lifecycle-fixture.json"
    call_command("prepare_freeze_lifecycle_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("freeze-lifecycle-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["trainer_user_id"]
    assert fixture["student_user_id"]
    assert fixture["parent_user_id"]
    assert fixture["expected"]["approve_days"] == 5
    assert fixture["expected"]["reject_decision_reason"] == "E2E needs document"
    assert fixture["expected"]["denial_days"] == 6
    assert fixture["expected"]["denial_unfreeze_days"] == 7

    with pytest.raises(CommandError, match="approve freeze not approved"):
        call_command("assert_freeze_lifecycle_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    from apps.billing.services import approve_freeze, reject_freeze, unfreeze_subscription

    approve_freeze(
        freeze_id=fixture["approve_freeze_id"],
        club_id=fixture["club_id"],
        approved_by_id=fixture["owner"]["user_id"],
    )
    reject_freeze(
        freeze_id=fixture["reject_freeze_id"],
        club_id=fixture["club_id"],
        rejected_by_id=fixture["owner"]["user_id"],
        decision_reason=fixture["expected"]["reject_decision_reason"],
    )
    unfreeze_subscription(
        freeze_id=fixture["unfreeze_freeze_id"],
        club_id=fixture["club_id"],
    )

    stdout = io.StringIO()
    call_command("assert_freeze_lifecycle_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["approve_freeze"]["status"] == SubscriptionFreeze.FreezeStatus.APPROVED
    assert evidence["approve_subscription"]["status"] == Subscription.Status.FROZEN
    assert evidence["reject_freeze"]["status"] == SubscriptionFreeze.FreezeStatus.REJECTED
    assert evidence["reject_freeze"]["decision_reason"] == "E2E needs document"
    assert evidence["reject_subscription"]["status"] == Subscription.Status.ACTIVE
    assert evidence["unfreeze_freeze"]["status"] == SubscriptionFreeze.FreezeStatus.APPROVED
    assert evidence["unfreeze_freeze"]["ended"] is True
    assert evidence["unfreeze_subscription"]["status"] == Subscription.Status.ACTIVE
    assert evidence["non_management_denials"]["denial_freeze"]["status"] == SubscriptionFreeze.FreezeStatus.PENDING
    assert evidence["non_management_denials"]["denial_subscription"]["status"] == Subscription.Status.ACTIVE
    assert (
        evidence["non_management_denials"]["denial_unfreeze_freeze"]["status"]
        == SubscriptionFreeze.FreezeStatus.APPROVED
    )
    assert evidence["non_management_denials"]["denial_unfreeze_freeze"]["ended"] is False
    assert evidence["non_management_denials"]["denial_unfreeze_subscription"]["status"] == Subscription.Status.FROZEN
    for role in ("trainer", "student", "parent"):
        assert evidence["non_management_denials"]["roles"][role] == {
            "approve_status": 403,
            "reject_status": 403,
            "unfreeze_status": 403,
        }


@pytest.mark.django_db
def test_trainer_lead_pool_lifecycle_e2e_prepare_and_assert_validates_pool_claim_release_and_loss(
    tmp_path,
):
    output = tmp_path / "trainer-lead-pool-lifecycle-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_trainer_lead_pool_lifecycle_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-lead-pool-lifecycle-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["other_trainer"]["email"]
    assert fixture["other_trainer"]["password"]
    assert fixture["trainer"]["password"] not in stdout.getvalue()
    assert fixture["other_trainer"]["password"] not in stdout.getvalue()
    assert fixture["pool_lead"]["intake_event_id"]
    assert fixture["conflict_lead"]["intake_event_id"]
    assert fixture["pool_lead"]["masked_phone"] != fixture["pool_lead"]["phone"]
    assert fixture["expected"]["profile_disabled_categories"] == ["trainer_tasks"]

    with pytest.raises(CommandError, match="released lead events mismatch"):
        call_command(
            "assert_trainer_lead_pool_lifecycle_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.leads.services import claim_lead, lose_lead, release_lead

    claim_lead(
        club_id=fixture["club_id"],
        student_id=fixture["pool_lead"]["id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        actor_user_id=fixture["trainer"]["user_id"],
    )
    release_lead(
        club_id=fixture["club_id"],
        student_id=fixture["pool_lead"]["id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        reason=fixture["expected"]["release_reason"],
    )
    claim_lead(
        club_id=fixture["club_id"],
        student_id=fixture["conflict_lead"]["id"],
        trainer_id=fixture["other_trainer"]["trainer_id"],
        actor_user_id=fixture["other_trainer"]["user_id"],
    )
    lose_lead(
        club_id=fixture["club_id"],
        student_id=fixture["loss_lead"]["id"],
        actor_user_id=fixture["trainer"]["user_id"],
        loss_reason=fixture["expected"]["loss_reason"],
    )

    with pytest.raises(CommandError, match="trainer notification preferences"):
        call_command(
            "assert_trainer_lead_pool_lifecycle_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    NotificationPreference.objects.create(
        user_id=fixture["trainer"]["user_id"],
        disabled_categories=fixture["expected"]["profile_disabled_categories"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_trainer_lead_pool_lifecycle_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["pool"]["released_lead_id"] == fixture["pool_lead"]["id"]
    assert fixture["pool_lead"]["id"] in evidence["pool"]["visible_pool_ids"]
    assert evidence["conflict"]["assigned_trainer_id"] == fixture["other_trainer"]["trainer_id"]
    assert evidence["lost"]["status"] == Student.Status.LOST
    assert evidence["lost"]["lead_status"] is None
    assert evidence["lost"]["loss_reason"] == fixture["expected"]["loss_reason"]
    assert evidence["lost"]["visible_in_trainer_mine"] is False
    assert evidence["lost"]["visible_in_pool"] is False
    assert evidence["lost"]["visible_in_all"] is False
    assert evidence["profile"]["disabled_categories"] == fixture["expected"]["profile_disabled_categories"]


@pytest.mark.django_db
def test_trainer_student_cockpit_scope_e2e_prepare_and_assert_validates_scope_boundaries(tmp_path):
    output = tmp_path / "trainer-student-cockpit-scope-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_trainer_student_cockpit_scope_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-student-cockpit-scope-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer"]["email"] not in stdout.getvalue()
    assert fixture["trainer"]["password"] not in stdout.getvalue()
    assert fixture["assigned_student"]["id"] != fixture["unassigned_student"]["id"]
    assert fixture["assigned_student"]["id"] != fixture["package_owned_student"]["id"]
    assert fixture["ids"]["unassigned_subscription_id"]
    assert fixture["ids"]["package_owned_subscription_id"]
    assert fixture["ids"]["package_owned_allocation_id"]
    assert fixture["ids"]["unassigned_student_grade_id"]
    assert fixture["ids"]["assigned_feedback_response_id"]
    assert fixture["expected"]["assigned_feedback_rating_text"] == "Оценка 4/5"
    assert fixture["expected"]["assigned_note_text"]
    assert fixture["expected"]["assigned_tariff_name"]
    assert fixture["expected"]["assigned_access_eligibility_text"] == "Можно открыть кабинет"
    assert fixture["expected"]["package_owned_tariff_name"]
    assert fixture["expected"]["package_owned_trainings_left_text"]

    stdout = io.StringIO()
    call_command(
        "assert_trainer_student_cockpit_scope_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["api"]["assigned_detail"]["status_code"] == 200
    assert evidence["api"]["assigned_detail_payload"]["status_code"] == 200
    assert evidence["api"]["assigned_detail_payload"]["id"] == fixture["assigned_student"]["id"]
    assert evidence["api"]["assigned_detail_payload"]["can_manage_account_access"] is True
    assert fixture["expected"]["assigned_note_text"] in evidence["api"]["assigned_detail_payload"]["note_texts"]
    assert evidence["api"]["assigned_subscriptions"]["status_code"] == 200
    assert any(
        item["tariff_name"] == fixture["expected"]["assigned_tariff_name"]
        and item["status"] == Subscription.Status.ACTIVE
        for item in evidence["api"]["assigned_subscriptions"]["items"]
    )
    assert evidence["api"]["assigned_feedback_responses"]["status_code"] == 200
    assert evidence["api"]["assigned_feedback_responses"]["count"] == 1
    assert evidence["api"]["assigned_feedback_responses"]["answers"] == [
        {
            "question_text": fixture["expected"]["assigned_feedback_question"],
            "rating_value": 4,
        }
    ]
    assert evidence["api"]["assigned_send_survey"]["status_code"] == 200
    assert fixture["package_owned_student"]["id"] in evidence["api"]["students_list"]["visible_ids"]
    assert evidence["api"]["package_owned_detail"]["status_code"] == 200
    assert evidence["api"]["package_owned_detail_payload"]["id"] == fixture["package_owned_student"]["id"]
    assert evidence["api"]["package_owned_detail_payload"]["can_manage_account_access"] is True
    assert evidence["api"]["package_owned_subscriptions"]["status_code"] == 200
    assert any(
        item["tariff_name"] == fixture["expected"]["package_owned_tariff_name"]
        and item["status"] == Subscription.Status.ACTIVE
        for item in evidence["api"]["package_owned_subscriptions"]["items"]
    )
    assert evidence["api"]["package_owned_send_survey"]["status_code"] == 200
    assert evidence["api"]["unassigned_detail"]["status_code"] == 403
    assert evidence["api"]["unassigned_account_access_open"]["status_code"] == 403
    assert evidence["api"]["unassigned_account_access_reset"]["status_code"] == 403
    assert evidence["api"]["unassigned_payment"]["status_code"] == 403
    assert fixture["assigned_student"]["id"] in evidence["api"]["ready_for_promotion"]["student_ids"]
    assert fixture["unassigned_student"]["id"] not in evidence["api"]["ready_for_promotion"]["student_ids"]


@pytest.mark.django_db
@override_settings(UNIFIED_CLIENT_JOURNEY_ENABLED=True)
def test_trainer_student_create_edit_e2e_prepare_and_assert_validates_create_edit_and_duplicate(tmp_path):
    output = tmp_path / "trainer-student-create-edit-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_trainer_student_create_edit_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-student-create-edit-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer"]["email"] not in stdout.getvalue()
    assert fixture["trainer"]["password"] not in stdout.getvalue()
    assert fixture["assigned_student"]["id"] != fixture["conflict_student"]["id"]
    assert fixture["new_student"]["phone"] != fixture["conflict_student"]["phone"]
    assert fixture["duplicate_attempt"]["phone"] == fixture["conflict_student"]["phone"]

    with pytest.raises(CommandError, match="created student not found"):
        call_command(
            "assert_trainer_student_create_edit_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.students.intake_services import submit_student_intake
    from apps.students.services import update_student

    common_intake = {
        "club_id": fixture["club_id"],
        "actor_user_id": fixture["trainer"]["user_id"],
        "actor_role": "trainer",
        "actor_trainer_id": fixture["trainer"]["trainer_id"],
        "guardian_phone": "",
        "date_of_birth": None,
        "is_child": False,
        "source": "other",
        "assigned_trainer_id": None,
        "confirm_distinct_child": False,
    }
    created_result = submit_student_intake(
        **common_intake,
        idempotency_key=uuid4(),
        intake_kind="new_contact",
        first_name=fixture["new_student"]["first_name"],
        last_name=fixture["new_student"]["last_name"],
        phone=fixture["new_student"]["phone"],
    )
    existing_result = submit_student_intake(
        **common_intake,
        idempotency_key=uuid4(),
        intake_kind="existing_student",
        first_name=fixture["existing_student_intake"]["first_name"],
        last_name=fixture["existing_student_intake"]["last_name"],
        phone=fixture["existing_student_intake"]["phone"],
    )
    update_student(
        club_id=fixture["club_id"],
        student_id=fixture["assigned_student"]["id"],
        first_name=fixture["edit"]["first_name"],
        last_name=fixture["edit"]["last_name"],
        phone=fixture["edit"]["phone"],
        contraindications=fixture["edit"]["contraindications"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_trainer_student_create_edit_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["created"]["id"] == created_result.student_id
    assert evidence["created"]["assigned_trainer_id"] == fixture["trainer"]["trainer_id"]
    assert evidence["created"]["status"] == Student.Status.LEAD
    assert evidence["existing_student"]["id"] == existing_result.student_id
    assert evidence["existing_student"]["status"] == Student.Status.ACTIVE
    assert evidence["existing_student"]["crm_entry_kind"] == Student.CrmEntryKind.EXISTING_STUDENT
    assert all(count == 0 for count in evidence["existing_student"]["artifact_counts"].values())
    assert evidence["edited"]["id"] == fixture["assigned_student"]["id"]
    assert evidence["edited"]["first_name"] == fixture["edit"]["first_name"]
    assert evidence["created"]["id"] in evidence["scope"]["lead_visible_ids"]
    assert evidence["created"]["id"] not in evidence["scope"]["student_visible_ids"]
    assert fixture["assigned_student"]["id"] in evidence["scope"]["student_visible_ids"]
    assert evidence["existing_student"]["id"] in evidence["scope"]["student_visible_ids"]
    assert evidence["scope"]["created_detail_status"] == 200
    assert evidence["scope"]["edited_detail_status"] == 200
    assert evidence["scope"]["existing_detail_status"] == 200
    assert evidence["scope"]["unassigned_update_status"] == 403
    assert evidence["duplicate"]["conflict_phone_count"] == 1


@pytest.mark.django_db
def test_trainer_lead_trial_e2e_prepare_and_assert_validates_booking_checkin_and_side_effects(tmp_path):
    output = tmp_path / "trainer-lead-trial-fixture.json"
    call_command("prepare_trainer_lead_trial_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-lead-trial-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["new_lead"]["first_name"] == "LeadTrial"
    assert fixture["new_lead"]["phone"].startswith("+7900")

    with pytest.raises(CommandError, match="target lead not found"):
        call_command("assert_trainer_lead_trial_e2e", fixture=str(output), timeout_seconds=0, stdout=io.StringIO())

    from apps.attendance.services import batch_checkin
    from apps.leads.services import book_trial, create_lead, update_lead_status
    from apps.retention.tasks import create_post_trial_task

    lead = create_lead(
        club_id=fixture["club_id"],
        first_name=fixture["new_lead"]["first_name"],
        last_name=fixture["new_lead"]["last_name"],
        phone=fixture["new_lead"]["phone"],
        is_child=fixture["new_lead"]["is_child"],
        source=fixture["new_lead"]["source"],
        assigned_trainer_id=fixture["trainer"]["trainer_id"],
    )
    update_lead_status(
        club_id=fixture["club_id"],
        student_id=lead.id,
        new_status=Student.LeadStatus.CONTACTED,
    )
    book_trial(
        club_id=fixture["club_id"],
        student_id=lead.id,
        trial_date=timezone.datetime.fromisoformat(fixture["trial"]["trial_date"]),
        schedule_id=fixture["schedule_id"],
        occurrence_date=date.fromisoformat(fixture["trial"]["checkin_date"]),
    )

    with (
        patch("apps.attendance.services.async_task", side_effect=_run_lead_trial_async_task_sync),
        patch("apps.attendance.selectors.timezone.now", return_value=timezone.now() + timedelta(days=2)),
        patch("django_q.tasks.schedule"),
    ):
        batch_checkin(
            club_id=fixture["club_id"],
            schedule_id=fixture["schedule_id"],
            checkin_date=date.fromisoformat(fixture["trial"]["checkin_date"]),
            present_student_ids=[lead.id],
            training_type_id=fixture["training_type_id"],
            actor_user_id=fixture["trainer"]["user_id"],
        )
    create_post_trial_task(
        lead.id,
        fixture["club_id"],
        fixture["trainer"]["trainer_id"],
    )

    stdout = io.StringIO()
    call_command("assert_trainer_lead_trial_e2e", fixture=str(output), timeout_seconds=0, stdout=stdout)
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["lead"]["status"] == Student.Status.TRIAL
    assert evidence["lead"]["lead_status"] == Student.LeadStatus.TRIAL_DONE
    assert evidence["lead"]["assigned_trainer_id"] == fixture["trainer"]["trainer_id"]
    assert evidence["scope"]["trainer_visible_lead_ids"] == [evidence["lead"]["id"]]
    assert evidence["enrollment"]["status"] == ScheduleEnrollment.Status.TRIAL
    assert evidence["enrollment"]["created_from"] == ScheduleEnrollment.CreatedFrom.LEAD_BOOKING
    assert evidence["enrollment"]["starts_on"] == fixture["trial"]["checkin_date"]
    assert evidence["enrollment"]["ends_on"] == fixture["trial"]["checkin_date"]
    assert evidence["checkin"]["is_debt"] is False
    assert evidence["checkin"]["post_trial_task_queued"] is True
    assert evidence["feedback"]["active_form_id"] == FeedbackForm.objects.get(
        club_id=fixture["club_id"],
        trigger_type="trial",
        is_active=True,
    ).id
    assert evidence["pipeline"]["execution_count"] == 1
    assert PipelineExecution.objects.for_club(fixture["club_id"]).filter(
        student_id=evidence["lead"]["id"],
    ).exists()
    assert evidence["retention_task"]["task_type"] == RetentionTask.TaskType.POST_TRIAL


@pytest.mark.django_db
def test_trainer_personal_drop_in_e2e_prepare_and_assert_validates_exact_lifecycle(tmp_path, monkeypatch):
    output = tmp_path / "trainer-personal-drop-in-fixture.json"
    call_command("prepare_trainer_personal_drop_in_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("trainer-personal-drop-in-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["kiosk_pin"].isdigit()
    assert len(fixture["kiosk_pin"]) == 6
    assert fixture["assigned_lead"]["student_id"]
    assert fixture["unrelated_lead"]["student_id"]
    assert fixture["personal_tariff"]["price"] == "2500.00"
    assert fixture["availability_slot"]["date"]
    assert fixture["availability_slot"]["student_id"]
    assert fixture["past_booking"]["booking_id"]
    assert fixture["grandfathered_personal_trial"]["enrollment_id"]

    with pytest.raises(CommandError, match="primary drop-in booking expected exactly one row"):
        call_command(
            "assert_trainer_personal_drop_in_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import (
        book_personal_drop_in,
        cancel_personal_drop_in_booking,
        create_personal_drop_in_payment,
        mark_personal_drop_in_no_show,
    )
    from apps.attendance.tasks import calculate_salary
    from apps.billing.services import verify_payment

    club = Club.objects.get(id=fixture["club_id"])
    target_date = date.fromisoformat(fixture["booking"]["date"])
    starts_at = timezone.make_aware(
        datetime.combine(target_date, time.fromisoformat(fixture["booking"]["start_time"])),
        club_zoneinfo(club),
    )
    ends_at = timezone.make_aware(
        datetime.combine(target_date, time.fromisoformat(fixture["booking"]["end_time"])),
        club_zoneinfo(club),
    )
    primary_result = book_personal_drop_in(
        club_id=club.id,
        student_id=fixture["assigned_lead"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        tariff_id=fixture["personal_tariff"]["id"],
        actor_user_id=fixture["trainer"]["user_id"],
        idempotency_key="trainer-personal-drop-in-e2e-primary",
    )
    assert primary_result.created is True
    assert (
        book_personal_drop_in(
            club_id=club.id,
            student_id=fixture["assigned_lead"]["student_id"],
            trainer_id=fixture["trainer"]["trainer_id"],
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=fixture["location"]["id"],
            training_type_id=fixture["personal_training_type"]["id"],
            tariff_id=fixture["personal_tariff"]["id"],
            actor_user_id=fixture["trainer"]["user_id"],
            idempotency_key="trainer-personal-drop-in-e2e-primary",
        ).created
        is False
    )

    with patch("apps.attendance.services.checkin.async_task"):
        checkin_result = create_checkin(
            club_id=club.id,
            student_id=fixture["assigned_lead"]["student_id"],
            schedule_id=primary_result.schedule.id,
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
        assert checkin_result["is_debt"] is True
        primary_checkin_replay = create_checkin(
            club_id=club.id,
            student_id=fixture["assigned_lead"]["student_id"],
            schedule_id=primary_result.schedule.id,
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
        assert primary_checkin_replay["created"] is False
        assert primary_checkin_replay["checkin_id"] == checkin_result["checkin_id"]

        grandfathered_trial = fixture["grandfathered_personal_trial"]
        grandfathered_checkin = create_checkin(
            club_id=club.id,
            student_id=grandfathered_trial["student_id"],
            schedule_id=grandfathered_trial["schedule_id"],
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.fromisoformat(grandfathered_trial["checkin_date"]),
        )
        assert grandfathered_checkin["created"] is True
        assert grandfathered_checkin["is_debt"] is False
        assert create_checkin(
            club_id=club.id,
            student_id=grandfathered_trial["student_id"],
            schedule_id=grandfathered_trial["schedule_id"],
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=date.fromisoformat(grandfathered_trial["checkin_date"]),
        )["created"] is False

    with patch("django_q.tasks.async_task"):
        payment_link = create_personal_drop_in_payment(
            club_id=club.id,
            booking_id=primary_result.booking.id,
            payment_method=Payment.Method.CASH,
            created_by_id=fixture["trainer"]["user_id"],
            idempotency_key="trainer-personal-drop-in-e2e-payment",
        )
        verified_payment = verify_payment(
            payment_id=payment_link.payment_id,
            club_id=club.id,
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )
        replayed_payment = verify_payment(
            payment_id=payment_link.payment_id,
            club_id=club.id,
            verified_by_id=fixture["owner"]["user_id"],
            action="confirm",
        )
        assert replayed_payment.id == verified_payment.id
        assert replayed_payment.status == Payment.Status.CONFIRMED
    calculate_salary(checkin_result["checkin_id"], club_id=club.id)
    calculate_salary(checkin_result["checkin_id"], club_id=club.id)

    slot = PersonalAvailabilitySlot.objects.for_club(club.id).get(id=fixture["availability_slot"]["id"])
    assert fixture["availability_slot"]["date"] == slot.starts_at.astimezone(club_zoneinfo(club)).date().isoformat()
    slot_result = book_personal_drop_in(
        club_id=club.id,
        student_id=fixture["availability_slot"]["student_id"],
        trainer_id=slot.trainer_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        location_id=slot.location_id,
        training_type_id=slot.training_type_id,
        tariff_id=fixture["personal_tariff"]["id"],
        actor_user_id=fixture["trainer"]["user_id"],
        availability_slot_id=slot.id,
        idempotency_key="trainer-personal-drop-in-e2e-slot",
    )
    cancel_personal_drop_in_booking(
        club_id=club.id,
        booking_id=slot_result.booking.id,
        actor_user_id=fixture["trainer"]["user_id"],
        reason="fixture cancellation",
    )
    mark_personal_drop_in_no_show(
        club_id=club.id,
        booking_id=fixture["past_booking"]["booking_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        reason="fixture no-show",
    )

    stdout = io.StringIO()
    call_command(
        "assert_trainer_personal_drop_in_e2e",
        fixture=str(output),
        require_grandfathered_completion=True,
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["grandfathered_personal_trial_preserved"] is True
    assert evidence["grandfathered_personal_trial_completed"] is True
    assert evidence["grandfathered_personal_trial"]["checkin_count"] == 1
    assert evidence["grandfathered_personal_trial"]["completion_event_count"] == 1
    assert evidence["grandfathered_personal_trial"]["post_trial_cascade_count"] == 1
    assert Checkin.objects.for_club(club.id).filter(id=grandfathered_checkin["checkin_id"]).count() == 1
    assert LeadLifecycleEvent.objects.for_club(club.id).filter(
        student_id=fixture["grandfathered_personal_trial"]["student_id"],
        event_type=LeadLifecycleEvent.EventType.TRIAL_DONE,
    ).count() == 1

    monkeypatch.setenv("REAL_STACK_E2E_REQUIRE_GRANDFATHERED_COMPLETION", "1")
    override_stdout = io.StringIO()
    call_command(
        "assert_trainer_personal_drop_in_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=override_stdout,
    )
    assert json.loads(override_stdout.getvalue())["grandfathered_personal_trial_completed"] is True
    assert evidence["primary"]["cardinalities"] == {
        "booking": 1,
        "checkin": 1,
        "debt": 1,
        "debt_settlement": 1,
        "payment": 1,
        "payment_link": 1,
        "subscription": 1,
        "subscription_component": 1,
        "salary_effect": 1,
    }
    assert evidence["slot_booking"]["cancel_event_count"] == 1
    assert evidence["past_booking"]["no_show_event_count"] == 1


@pytest.mark.django_db
def test_student_parent_reflection_e2e_prepare_and_assert_validates_safe_surfaces(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    output = tmp_path / "student-parent-reflection-fixture.json"
    call_command("prepare_student_parent_reflection_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("student-parent-reflection-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["student"]["email"]
    assert fixture["student"]["password"]
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["attention_child"]["student_id"]
    assert fixture["upcoming_schedule_id"]
    assert fixture["debt_id"]
    assert fixture["debt_checkin_id"]
    assert fixture["expected"]["trainings_left_after"] == 4
    assert fixture["expected"]["attendance_count_after"] == 2
    assert fixture["expected"]["open_debt_count"] == 1
    assert fixture["expected"]["grade_trainings_since_last_after"] == 1
    assert fixture["expected"]["grade_trainings_to_next_after"] == 9
    assert fixture["expected"]["attention_trainings_left"] == 1

    with pytest.raises(CommandError, match="reflection check-in"):
        call_command(
            "assert_student_parent_reflection_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import batch_checkin

    with (
        patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync),
        patch("apps.attendance.selectors.timezone.now", return_value=timezone.now() + timedelta(days=2)),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            batch_checkin(
                club_id=fixture["club_id"],
                schedule_id=fixture["schedule_id"],
                checkin_date=date.fromisoformat(fixture["checkin_date"]),
                present_student_ids=[fixture["student"]["student_id"]],
                training_type_id=fixture["training_type_id"],
                actor_user_id=fixture["trainer"]["user_id"],
            )

    stdout = io.StringIO()
    call_command(
        "assert_student_parent_reflection_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["student_api"]["id"] == fixture["student"]["student_id"]
    assert evidence["student_api"]["subscription"]["trainings_left"] == 4
    assert evidence["student_api"]["subscription"]["trainings_used"] == 1
    assert evidence["student_api"]["subscription"]["trainings_total"] == 5
    assert evidence["student_api"]["attendance_count"] == 2
    assert evidence["student_api"]["schedule_count"] == 2
    assert {item["id"] for item in evidence["student_api"]["schedule"]} == {
        fixture["schedule_id"],
        fixture["upcoming_schedule_id"],
    }
    assert evidence["student_api"]["open_debt_count"] == 1
    assert evidence["student_api"]["open_debt"]["id"] == fixture["debt_id"]
    assert evidence["student_api"]["open_debt"]["checkin_id"] == fixture["debt_checkin_id"]
    assert evidence["student_api"]["open_debt"]["reason"] == "no_subscription"
    assert evidence["student_api"]["open_debt"]["tariff_price"] == fixture["expected"]["debt_amount"]
    assert evidence["student_api"]["grade"]["trainings_since_last_grade"] == 1
    assert evidence["student_api"]["grade"]["trainings_to_next"] == 9
    assert evidence["student_api"]["upcoming"]["schedule_id"] == fixture["upcoming_schedule_id"]
    assert evidence["student_api"]["upcoming"]["group_name"] == fixture["expected"]["upcoming_group_name"]
    assert set(evidence["parent_api"]["child_ids"]) == {
        fixture["student"]["student_id"],
        fixture["attention_child"]["student_id"],
    }
    assert evidence["parent_api"]["profile"]["attendance_count"] == 2
    assert evidence["parent_api"]["profile"]["subscription"]["trainings_left"] == 4
    assert evidence["parent_api"]["profile"]["open_debt_count"] == 1
    assert evidence["parent_api"]["profile"]["open_debts"][0]["id"] == fixture["debt_id"]
    assert (
        evidence["parent_api"]["primary_child"]["next_training_group_name"]
        == fixture["expected"]["upcoming_group_name"]
    )
    assert evidence["parent_api"]["attention_child"]["subscription_remaining"] == 1
    assert evidence["parent_api"]["attention_child"]["grade_name"] == fixture["expected"]["attention_grade_name"]
    assert evidence["parent_api"]["attendance_count"] == 2
    assert evidence["privacy"]["staff_note_visible"] is False
    assert evidence["privacy"]["foreign_child_visible"] is False
    assert GradeProgressEvent.objects.for_club(fixture["club_id"]).filter(
        student_grade_id=fixture["student_grade_id"],
    ).exists()
    assert StudentNote.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["student"]["student_id"],
        text=fixture["expected"]["staff_only_note"],
    ).exists()


@pytest.mark.django_db
def test_tenant_negative_matrix_e2e_prepare_and_assert_validates_cross_tenant_denials(tmp_path):
    output = tmp_path / "tenant-negative-matrix-fixture.json"
    call_command("prepare_tenant_negative_matrix_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("tenant-negative-matrix-e2e-")
    assert fixture["club_a"]["owner"]["email"]
    assert fixture["club_a"]["owner"]["password"]
    assert fixture["club_a"]["parent"]["email"]
    assert fixture["club_a"]["parent"]["password"]
    assert fixture["club_b"]["markers"]["student_name"].startswith("TenantB")
    assert fixture["club_b"]["markers"]["group_name"].startswith("Tenant B")

    stdout = io.StringIO()
    call_command(
        "assert_tenant_negative_matrix_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["api"]["owner_list_students"]["foreign_marker_visible"] is False
    assert evidence["api"]["owner_get_foreign_student"]["status_code"] == 404
    assert evidence["api"]["owner_get_foreign_schedule"]["status_code"] == 404
    assert evidence["api"]["owner_batch_foreign_student"]["status_code"] in {400, 404}
    assert evidence["api"]["owner_get_foreign_payment"]["status_code"] == 404
    assert evidence["api"]["owner_get_foreign_debtors"]["foreign_marker_visible"] is False
    assert evidence["api"]["owner_get_foreign_document_checklist"]["status_code"] == 404
    assert evidence["api"]["owner_get_foreign_lead"]["status_code"] == 404
    assert evidence["api"]["owner_get_foreign_retention_task"]["status_code"] == 404
    assert evidence["api"]["parent_children"]["child_ids"] == [fixture["club_a"]["child_id"]]
    assert evidence["api"]["parent_get_foreign_child"]["status_code"] == 404
    assert evidence["api"]["kiosk_lookup_foreign_phone"]["match_count"] == 0
    assert evidence["api"]["kiosk_foreign_schedule_today"]["foreign_marker_visible"] is False
    assert evidence["database"]["club_a_foreign_row_counts"] == {
        "checkins": 0,
        "debts": 0,
        "documents": 0,
        "payments": 0,
        "students": 0,
    }


@pytest.mark.django_db
def test_offline_kiosk_sync_e2e_prepare_and_assert_validates_idempotent_queue_replay(tmp_path):
    output = tmp_path / "offline-kiosk-sync-fixture.json"
    call_command("prepare_offline_kiosk_sync_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("offline-kiosk-sync-e2e-")
    kiosk_token = activate_kiosk(pin=fixture["kiosk_pin"])["token"]
    enrollment = ScheduleEnrollment.objects.for_club(fixture["club_id"]).get(id=fixture["enrollment_id"])
    terminal = fixture["terminal"]
    terminal_enrollment = ScheduleEnrollment.objects.for_club(fixture["club_id"]).get(
        id=terminal["enrollment_id"]
    )
    assert enrollment.status == ScheduleEnrollment.Status.ACTIVE
    assert terminal_enrollment.status == ScheduleEnrollment.Status.ACTIVE
    assert terminal["student_id"] != fixture["student_id"]
    assert terminal["schedule_id"] == fixture["schedule_id"]
    assert fixture["expected"]["trainings_left_before"] == 5
    assert fixture["expected"]["trainings_left_after"] == 4

    with pytest.raises(CommandError, match="offline sync check-in"):
        call_command(
            "assert_offline_kiosk_sync_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    client = TestClient(api)
    item = {
        "student_id": fixture["student_id"],
        "schedule_id": fixture["schedule_id"],
        "training_type_id": fixture["training_type_id"],
        "checkin_date": fixture["checkin_date"],
        "client_id": "offline-kiosk-sync-replay-1",
        "idempotency_key": "offline-kiosk-sync-replay-1",
    }
    auth = {"headers": {"X-Kiosk-Token": kiosk_token}}

    stdout = io.StringIO()
    call_command(
        "set_offline_kiosk_sync_enrollment_status_e2e",
        fixture=str(output),
        status=ScheduleEnrollment.Status.FROZEN,
        target="terminal",
        stdout=stdout,
    )
    status_evidence = json.loads(stdout.getvalue())
    assert status_evidence["ok"] is True
    assert status_evidence["target"] == "terminal"
    assert status_evidence["enrollment"]["previous_status"] == ScheduleEnrollment.Status.ACTIVE
    assert status_evidence["enrollment"]["status"] == ScheduleEnrollment.Status.FROZEN

    terminal_item = {
        "student_id": terminal["student_id"],
        "schedule_id": terminal["schedule_id"],
        "training_type_id": fixture["training_type_id"],
        "checkin_date": terminal["checkin_date"],
        "client_id": "offline-kiosk-sync-frozen-1",
        "idempotency_key": "offline-kiosk-sync-frozen-1",
    }
    terminal_response = client.post("/checkins/sync/", json={"checkins": [terminal_item]}, **auth)
    assert terminal_response.status_code == 200
    terminal_payload = terminal_response.json()
    assert terminal_payload["synced"] == 0
    assert terminal_payload["failed"] == 1
    assert terminal_payload["results"][0]["error"] == "enrollment_frozen"
    assert terminal_payload["results"][0]["retryable"] is False
    assert terminal_payload["results"][0]["idempotency_key"] == "offline-kiosk-sync-frozen-1"

    stdout = io.StringIO()
    call_command(
        "assert_offline_kiosk_terminal_failure_e2e",
        fixture=str(output),
        target="terminal",
        stdout=stdout,
    )
    terminal_evidence = json.loads(stdout.getvalue())
    assert terminal_evidence["ok"] is True
    assert terminal_evidence["target"] == "terminal"
    assert terminal_evidence["terminal_failure"]["checkin_count"] == 0
    assert terminal_evidence["subscription"]["trainings_left"] == 5
    assert terminal_evidence["subscription"]["trainings_used"] == 0

    call_command(
        "set_offline_kiosk_sync_enrollment_status_e2e",
        fixture=str(output),
        status=ScheduleEnrollment.Status.ACTIVE,
        target="terminal",
        stdout=io.StringIO(),
    )

    stdout = io.StringIO()
    call_command(
        "set_offline_kiosk_sync_rollout_e2e",
        fixture=str(output),
        mode=TrainingGroupRolloutState.Mode.RECONCILING,
        stdout=stdout,
    )
    reconciling_state = json.loads(stdout.getvalue())
    assert reconciling_state["ok"] is True
    assert reconciling_state["mode"] == TrainingGroupRolloutState.Mode.RECONCILING
    assert reconciling_state["event_previous_mode"] == TrainingGroupRolloutState.Mode.OFF
    assert reconciling_state["event_new_mode"] == TrainingGroupRolloutState.Mode.RECONCILING

    reconciling_response = client.post("/checkins/sync/", json={"checkins": [item]}, **auth)
    assert reconciling_response.status_code == 200
    reconciling_payload = reconciling_response.json()
    assert reconciling_payload["synced"] == 0
    assert reconciling_payload["failed"] == 1
    assert reconciling_payload["results"][0]["error"] == "training_group_reconciling"
    assert reconciling_payload["results"][0]["retryable"] is True
    assert Checkin.objects.for_club(fixture["club_id"]).count() == 0

    stdout = io.StringIO()
    call_command(
        "set_offline_kiosk_sync_rollout_e2e",
        fixture=str(output),
        mode=TrainingGroupRolloutState.Mode.SHADOW,
        stdout=stdout,
    )
    shadow_state = json.loads(stdout.getvalue())
    assert shadow_state["mode"] == TrainingGroupRolloutState.Mode.SHADOW
    assert shadow_state["event_previous_mode"] == TrainingGroupRolloutState.Mode.RECONCILING
    assert shadow_state["event_new_mode"] == TrainingGroupRolloutState.Mode.SHADOW
    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        shadow_response = client.post("/checkins/sync/", json={"checkins": [item]}, **auth)
        shadow_retry_response = client.post("/checkins/sync/", json={"checkins": [item]}, **auth)

    assert shadow_response.status_code == 200
    shadow_payload = shadow_response.json()
    assert shadow_payload["synced"] == 1
    assert shadow_payload["failed"] == 0
    assert shadow_payload["results"][0]["success"] is True
    assert shadow_payload["results"][0]["duplicate"] is False
    assert shadow_retry_response.status_code == 200
    assert shadow_retry_response.json()["results"][0]["duplicate"] is True

    with patch("apps.attendance.services.async_task", side_effect=_run_attendance_task_sync):
        duplicate_response = client.post("/checkins/sync/", json={"checkins": [item]}, **auth)

    assert duplicate_response.status_code == 200
    duplicate_payload = duplicate_response.json()
    assert duplicate_payload["synced"] == 1
    assert duplicate_payload["failed"] == 0
    assert duplicate_payload["results"][0]["success"] is True
    assert duplicate_payload["results"][0]["duplicate"] is True
    assert duplicate_payload["results"][0]["retryable"] is False
    assert duplicate_payload["results"][0]["checkin_id"] == shadow_payload["results"][0]["checkin_id"]

    stdout = io.StringIO()
    call_command(
        "assert_offline_kiosk_sync_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["checkins"]["count"] == 1
    assert evidence["idempotency"]["duplicate_replay_safe"] is True
    assert evidence["subscription"]["trainings_left"] == 4
    assert evidence["subscription"]["trainings_used"] == 1
    assert evidence["debts"]["count"] == 0


@pytest.mark.django_db
def test_offline_rollout_fixture_command_uses_owner_audits_without_a_forward_audit_bypass(tmp_path, monkeypatch):
    output = tmp_path / "offline-kiosk-sync-rollout-fixture.json"
    call_command("prepare_offline_kiosk_sync_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    call_command(
        "set_offline_kiosk_sync_rollout_e2e",
        fixture=str(output),
        mode=TrainingGroupRolloutState.Mode.RECONCILING,
        stdout=io.StringIO(),
    )
    monkeypatch.setattr(
        "apps.attendance.services.training_group_reconciliation.audit_training_groups",
        lambda *, club: {"valid": False},
    )

    with pytest.raises(CommandError, match="training_group_rollout_prerequisite_missing"):
        call_command(
            "set_offline_kiosk_sync_rollout_e2e",
            fixture=str(output),
            mode=TrainingGroupRolloutState.Mode.SHADOW,
            stdout=io.StringIO(),
        )

    state = TrainingGroupRolloutState.objects.for_club(fixture["club_id"]).get()
    assert state.mode == TrainingGroupRolloutState.Mode.RECONCILING
    source = Path("apps/common/management/commands/set_offline_kiosk_sync_rollout_e2e.py").read_text(
        encoding="utf-8"
    )
    assert "transition_training_group_rollout_for_owner" in source
    assert "approved_reconciliation_rollout_gate_digest" in source
    assert "forward_audit_passed" not in source


@pytest.mark.django_db
@override_settings(
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
)
def test_schedule_exception_visibility_e2e_prepare_and_assert_validates_role_surfaces(tmp_path):
    output = tmp_path / "schedule-exception-visibility-fixture.json"
    call_command("prepare_schedule_exception_visibility_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("schedule-exception-visibility-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["substitute_trainer"]["email"]
    assert fixture["substitute_trainer"]["password"]
    assert fixture["student"]["email"]
    assert fixture["student"]["password"]
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["training_group_substitute"]["new_writes_enabled"] is True
    assert fixture["training_group_substitute"]["manual_operational_admission_enabled"] is True

    with pytest.raises(CommandError, match="schedule exceptions"):
        call_command(
            "assert_schedule_exception_visibility_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import cancel_session, reschedule_session

    cancel_session(
        club_id=fixture["club_id"],
        schedule_id=fixture["cancel_schedule_id"],
        date=date.fromisoformat(fixture["cancel_date"]),
        reason=fixture["expected"]["cancel_reason"],
    )
    reschedule_session(
        club_id=fixture["club_id"],
        schedule_id=fixture["reschedule_schedule_id"],
        date=date.fromisoformat(fixture["reschedule_old_date"]),
        new_date=date.fromisoformat(fixture["reschedule_new_date"]),
        new_start_time=time.fromisoformat(fixture["expected"]["reschedule_new_start_time"]),
        new_end_time=time.fromisoformat(fixture["expected"]["reschedule_new_end_time"]),
        reason=fixture["expected"]["reschedule_reason"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_schedule_exception_visibility_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["exceptions"]["types"] == ["cancelled", "rescheduled", "substitute"]
    assert evidence["training_group_substitute"]["new_writes_enabled"] is True
    assert evidence["training_group_substitute"]["manual_operational_admission_enabled"] is True
    assert (
        evidence["training_group_substitute"]["responsible_trainer_id"]
        == fixture["substitute_trainer"]["trainer_id"]
    )
    assert (
        evidence["training_group_substitute"]["membership_id"]
        == fixture["training_group_substitute"]["membership_id"]
    )
    assert evidence["training_group_substitute"]["membership_authority"] == "payment_owned"
    assert evidence["training_group_substitute"]["roster_authority"] == "training_group_membership"
    assert evidence["training_group_substitute"]["seller_trainer_id"] == fixture["trainer"]["trainer_id"]
    assert evidence["training_group_substitute"]["sale_trainer_id_snapshot"] == fixture["trainer"]["trainer_id"]
    assert evidence["training_group_substitute"]["sale_attribution_source"] == "training_group_responsible_trainer"
    assert evidence["trainer"]["cancelled_hidden"] is True
    assert evidence["trainer"]["rescheduled_visible"] is True
    assert evidence["trainer"]["substitute_visible_to_substitute"] is True
    assert evidence["student"]["cancelled_hidden"] is True
    assert evidence["student"]["rescheduled_visible"] is True
    assert evidence["student"]["substitute_visible"] is True
    assert evidence["student"]["payload_safety"]["staff_only_fields_absent"] is True
    assert evidence["student"]["payload_safety"]["reason_values_hidden"] is True
    assert "reason" not in evidence["student"]["payload_safety"]["field_names"]
    assert "exception_type" not in evidence["student"]["payload_safety"]["field_names"]
    assert "substitute_trainer_id" not in evidence["student"]["payload_safety"]["field_names"]
    assert evidence["parent"]["exceptions_visible"] == {
        "cancelled": True,
        "rescheduled": True,
        "substitute": True,
    }


@pytest.mark.django_db
def test_student_feedback_e2e_prepare_assert_and_deactivate_validates_submit_duplicate_and_no_active_state(
    tmp_path,
):
    output = tmp_path / "student-feedback-fixture.json"
    call_command("prepare_student_feedback_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("student-feedback-e2e-")
    assert fixture["student"]["email"]
    assert fixture["student"]["password"]
    assert fixture["form_id"]
    assert fixture["questions"]["rating_id"]
    assert fixture["questions"]["yes_no_id"]
    assert fixture["questions"]["text_id"]

    with pytest.raises(CommandError, match="feedback response"):
        call_command(
            "assert_student_feedback_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.feedback.services import submit_feedback_response

    answers = [
        {
            "question_id": fixture["questions"]["rating_id"],
            "rating_value": fixture["expected"]["rating_value"],
        },
        {
            "question_id": fixture["questions"]["yes_no_id"],
            "bool_value": fixture["expected"]["bool_value"],
        },
        {
            "question_id": fixture["questions"]["text_id"],
            "text_value": fixture["expected"]["text_value"],
        },
    ]
    first_response = submit_feedback_response(
        club_id=fixture["club_id"],
        form_id=fixture["form_id"],
        student_id=fixture["student"]["student_id"],
        answers=answers,
    )
    duplicate_response = submit_feedback_response(
        club_id=fixture["club_id"],
        form_id=fixture["form_id"],
        student_id=fixture["student"]["student_id"],
        answers=[
            {
                "question_id": fixture["questions"]["rating_id"],
                "rating_value": 1,
            },
            {
                "question_id": fixture["questions"]["yes_no_id"],
                "bool_value": False,
            },
        ],
    )

    assert first_response.id == duplicate_response.id

    stdout = io.StringIO()
    call_command(
        "assert_student_feedback_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["response"]["count"] == 1
    assert evidence["response"]["duplicate_safe"] is True
    assert evidence["answers"]["rating_value"] == fixture["expected"]["rating_value"]
    assert evidence["answers"]["bool_value"] == fixture["expected"]["bool_value"]
    assert evidence["answers"]["text_value"] == fixture["expected"]["text_value"]
    assert evidence["active_form"]["is_active"] is True

    stdout = io.StringIO()
    call_command("deactivate_student_feedback_form_e2e", fixture=str(output), stdout=stdout)
    deactivate_evidence = json.loads(stdout.getvalue())

    assert deactivate_evidence["ok"] is True
    assert deactivate_evidence["active_form"]["id"] is None

    stdout = io.StringIO()
    call_command(
        "assert_student_feedback_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["response"]["count"] == 1
    assert evidence["active_form"]["is_active"] is False


@pytest.mark.django_db
def test_parent_invite_feedback_e2e_prepare_and_assert_validates_invite_accept_and_child_feedback(
    tmp_path,
):
    output = tmp_path / "parent-invite-feedback-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_parent_invite_feedback_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("parent-invite-feedback-e2e-")
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["child"]["student_id"]
    assert fixture["invite"]["token"]
    assert fixture["questions"]["yes_no_id"]
    assert fixture["questions"]["text_id"]
    assert fixture["invite"]["token"] not in stdout.getvalue()

    with pytest.raises(CommandError, match="parent invite accepted"):
        call_command(
            "assert_parent_invite_feedback_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.students.parent_services import accept_parent_invite

    accepted_child = accept_parent_invite(
        token=fixture["invite"]["token"],
        user_id=fixture["parent"]["user_id"],
    )

    assert accepted_child.id == fixture["child"]["student_id"]

    with pytest.raises(CommandError, match="feedback response"):
        call_command(
            "assert_parent_invite_feedback_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.feedback.services import submit_feedback_response

    response = submit_feedback_response(
        club_id=fixture["club_id"],
        form_id=fixture["form_id"],
        student_id=fixture["child"]["student_id"],
        answers=[
            {
                "question_id": fixture["questions"]["yes_no_id"],
                "bool_value": fixture["expected"]["bool_value"],
            },
            {
                "question_id": fixture["questions"]["text_id"],
                "text_value": fixture["expected"]["text_value"],
            },
        ],
    )

    assert getattr(response, "_created", True) is True
    duplicate_response = submit_feedback_response(
        club_id=fixture["club_id"],
        form_id=fixture["form_id"],
        student_id=fixture["child"]["student_id"],
        answers=[
            {
                "question_id": fixture["questions"]["yes_no_id"],
                "bool_value": False,
            },
            {
                "question_id": fixture["questions"]["text_id"],
                "text_value": "duplicate value should not overwrite",
            },
        ],
    )
    assert getattr(duplicate_response, "_created", True) is False
    assert duplicate_response.id == response.id

    stdout = io.StringIO()
    call_command(
        "assert_parent_invite_feedback_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["invite"]["accepted"] is True
    assert evidence["parent"]["membership_role"] == ClubMembership.Role.PARENT
    assert evidence["child"]["parent_user_id"] == fixture["parent"]["user_id"]
    assert evidence["response"]["count"] == 1
    assert evidence["response"]["duplicate_safe"] is True
    assert evidence["active_form"]["is_active"] is True
    assert evidence["answers"]["bool_value"] == fixture["expected"]["bool_value"]
    assert evidence["answers"]["text_value"] == fixture["expected"]["text_value"]

    stdout = io.StringIO()
    call_command("deactivate_student_feedback_form_e2e", fixture=str(output), stdout=stdout)
    deactivate_evidence = json.loads(stdout.getvalue())

    assert deactivate_evidence["ok"] is True
    assert deactivate_evidence["active_form"]["id"] is None

    stdout = io.StringIO()
    call_command(
        "assert_parent_invite_feedback_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["response"]["count"] == 1
    assert evidence["response"]["duplicate_safe"] is True
    assert evidence["active_form"]["is_active"] is False


@pytest.mark.django_db
def test_document_checklist_upload_e2e_prepare_and_assert_validates_dashboard_upload_and_safe_access(
    tmp_path,
):
    output = tmp_path / "document-checklist-upload-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_document_checklist_upload_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("document-checklist-upload-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["admin"]["email"]
    assert fixture["admin"]["password"]
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["student_user"]["email"]
    assert fixture["student_user"]["password"]
    assert fixture["student"]["student_id"]
    assert fixture["parent"]["user_id"]
    assert fixture["document_type_id"]
    assert fixture["student_upload_document_type_id"]
    assert fixture["parent"]["password"] not in stdout.getvalue()
    assert fixture["student_user"]["password"] not in stdout.getvalue()
    assert fixture["expected"]["document_name"] not in stdout.getvalue()
    assert fixture["expected"]["student_upload_document_name"] not in stdout.getvalue()

    with pytest.raises(CommandError, match="student document"):
        call_command(
            "assert_document_checklist_upload_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.documents.services import mark_document_provided, upload_student_document

    mark_document_provided(
        club_id=fixture["club_id"],
        student_id=fixture["student"]["student_id"],
        document_type_id=fixture["document_type_id"],
        is_provided=True,
        notes=fixture["expected"]["staff_note"],
    )
    upload = SimpleUploadedFile(
        fixture["expected"]["upload_filename"],
        fixture["expected"]["upload_content"].encode(),
        content_type="application/pdf",
    )
    upload_student_document(
        club_id=fixture["club_id"],
        student_id=fixture["student"]["student_id"],
        document_type_id=fixture["document_type_id"],
        file=upload,
    )
    student_upload = SimpleUploadedFile(
        fixture["expected"]["student_upload_filename"],
        fixture["expected"]["student_upload_content"].encode(),
        content_type="application/pdf",
    )
    upload_student_document(
        club_id=fixture["club_id"],
        student_id=fixture["student"]["student_id"],
        document_type_id=fixture["student_upload_document_type_id"],
        file=student_upload,
    )

    stdout = io.StringIO()
    call_command(
        "assert_document_checklist_upload_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["document"]["count"] == 1
    assert evidence["document"]["is_provided"] is True
    assert evidence["document"]["has_file"] is True
    assert evidence["document"]["safe_stored_name"] is True
    assert evidence["student_upload_document"]["count"] == 1
    assert evidence["student_upload_document"]["is_provided"] is True
    assert evidence["student_upload_document"]["has_file"] is True
    assert evidence["student_upload_document"]["safe_stored_name"] is True
    assert evidence["checklist"]["missing_count"] == 0
    assert evidence["checklist"]["student_upload_is_provided"] is True
    assert evidence["checklist"]["student_upload_has_file"] is True
    assert evidence["api"]["parent_checklist"]["has_file"] is True
    assert evidence["api"]["parent_checklist"]["student_upload_has_file"] is True
    assert evidence["api"]["parent_checklist"]["staff_note_visible"] is False
    assert evidence["api"]["student_checklist"]["has_file"] is True
    assert evidence["api"]["student_checklist"]["student_upload_has_file"] is True
    assert evidence["api"]["student_checklist"]["staff_note_visible"] is False
    assert evidence["api"]["parent_upload_safe_notes"] == ""
    assert evidence["api"]["foreign_parent_upload_status"] == 404
    assert evidence["api"]["parent_foreign_checklist_status"] == 404
    assert evidence["api"]["student_foreign_checklist_status"] == 404
    assert StudentDocument.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["student"]["student_id"],
        document_type_id=fixture["document_type_id"],
    ).count() == 1
    assert StudentDocument.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["student"]["student_id"],
        document_type_id=fixture["student_upload_document_type_id"],
    ).count() == 1


@pytest.mark.django_db
def test_push_notification_preferences_e2e_prepare_and_assert_validates_subscription_and_category_optout(
    tmp_path,
):
    output = tmp_path / "push-notification-preferences-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_push_notification_preferences_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("push-notification-preferences-e2e-")
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["student"]["email"]
    assert fixture["student"]["password"]
    assert fixture["child"]["student_id"]
    assert fixture["expected"]["parent"]["endpoint"] not in stdout.getvalue()
    assert fixture["expected"]["parent"]["key_auth"] not in stdout.getvalue()
    assert fixture["expected"]["student"]["endpoint"] not in stdout.getvalue()
    assert fixture["expected"]["student"]["key_auth"] not in stdout.getvalue()
    assert fixture["expected"]["parent"]["disabled_categories"] == ["child_checkin"]
    assert fixture["expected"]["student"]["disabled_categories"] == ["training_reminders"]

    with pytest.raises(CommandError, match="push subscription"):
        call_command(
            "assert_push_notification_preferences_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    PushSubscription.objects.create(
        user_id=fixture["parent"]["user_id"],
        endpoint=fixture["expected"]["parent"]["endpoint"],
        key_p256dh=fixture["expected"]["parent"]["key_p256dh"],
        key_auth=fixture["expected"]["parent"]["key_auth"],
    )
    PushSubscription.objects.create(
        user_id=fixture["student"]["user_id"],
        endpoint=fixture["expected"]["student"]["endpoint"],
        key_p256dh=fixture["expected"]["student"]["key_p256dh"],
        key_auth=fixture["expected"]["student"]["key_auth"],
    )
    NotificationPreference.objects.create(
        user_id=fixture["parent"]["user_id"],
        disabled_categories=fixture["expected"]["parent"]["disabled_categories"],
    )
    NotificationPreference.objects.create(
        user_id=fixture["student"]["user_id"],
        disabled_categories=fixture["expected"]["student"]["disabled_categories"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_push_notification_preferences_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["subscription"]["active_count"] == 1
    assert evidence["subscription"]["endpoint_matches"] is True
    assert evidence["preferences"]["disabled_categories"] == fixture["expected"]["parent"]["disabled_categories"]
    assert (
        evidence["parent_push"]["preferences"]["disabled_categories"]
        == fixture["expected"]["parent"]["disabled_categories"]
    )
    assert evidence["student_push"]["subscription"]["active_count"] == 1
    assert evidence["student_push"]["subscription"]["endpoint_matches"] is True
    assert (
        evidence["student_push"]["preferences"]["disabled_categories"]
        == fixture["expected"]["student"]["disabled_categories"]
    )


@pytest.mark.django_db
def test_mass_notifications_e2e_prepare_and_assert_validates_group_scoping_and_trainer_rules(
    tmp_path,
):
    output = tmp_path / "mass-notifications-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_mass_notifications_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("mass-notifications-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["owned_schedule_id"] != fixture["foreign_schedule_id"]
    assert fixture["target_student"]["user_id"] != fixture["non_target_student"]["user_id"]
    assert fixture["opted_out_student"]["user_id"] != fixture["target_student"]["user_id"]
    assert fixture["expected"]["raw_recipient_count"] == 2
    assert fixture["expected"]["recipient_count"] == 2
    assert fixture["push_subscriptions"]["target_second_active_id"] != fixture["push_subscriptions"][
        "target_active_id"
    ]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["trainer"]["password"] not in stdout.getvalue()

    with pytest.raises(CommandError, match="mass notification"):
        call_command(
            "assert_mass_notifications_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.notifications.selectors import get_recipients_for_segment
    from apps.notifications.services import get_mass_notification_recipient_ids, send_mass_notification

    raw_recipients = list(
        get_recipients_for_segment(
            club_id=fixture["club_id"],
            segment_type="group",
            segment_filter={"schedule_id": fixture["owned_schedule_id"]},
        )
    )
    assert set(raw_recipients) == {
        fixture["target_student"]["user_id"],
        fixture["opted_out_student"]["user_id"],
    }
    recipients = list(
        get_mass_notification_recipient_ids(
            club_id=fixture["club_id"],
            segment_type="group",
            segment_filter={"schedule_id": fixture["owned_schedule_id"]},
        )
    )
    assert recipients == [
        fixture["target_student"]["user_id"],
        fixture["opted_out_student"]["user_id"],
    ]

    foreign_recipients = list(
        get_recipients_for_segment(
            club_id=fixture["club_id"],
            segment_type="group",
            segment_filter={"schedule_id": fixture["foreign_schedule_id"]},
        )
    )
    assert foreign_recipients == [fixture["non_target_student"]["user_id"]]
    assert fixture["target_student"]["user_id"] not in foreign_recipients

    with patch("apps.notifications.services.async_task") as mock_async:
        owner_notification = send_mass_notification(
            club_id=fixture["club_id"],
            text=fixture["expected"]["owner_message"],
            segment_type="group",
            segment_filter={"schedule_id": fixture["owned_schedule_id"]},
            sent_by_id=fixture["owner"]["user_id"],
        )
        trainer_notification = send_mass_notification(
            club_id=fixture["club_id"],
            text=fixture["expected"]["trainer_message"],
            segment_type="group",
            segment_filter={"schedule_id": fixture["owned_schedule_id"]},
            sent_by_id=fixture["trainer"]["user_id"],
        )

    assert owner_notification.recipient_count == fixture["expected"]["recipient_count"]
    assert trainer_notification.recipient_count == fixture["expected"]["recipient_count"]
    assert mock_async.call_count == 6
    queued_subscription_ids = [call.args[1] for call in mock_async.call_args_list]
    assert queued_subscription_ids == [
        fixture["push_subscriptions"]["target_active_id"],
        fixture["push_subscriptions"]["target_second_active_id"],
        fixture["push_subscriptions"]["opted_out_active_id"],
        fixture["push_subscriptions"]["target_active_id"],
        fixture["push_subscriptions"]["target_second_active_id"],
        fixture["push_subscriptions"]["opted_out_active_id"],
    ]
    assert MassNotification.objects.for_club(fixture["club_id"]).count() == 2

    stdout = io.StringIO()
    call_command(
        "assert_mass_notifications_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert set(evidence["raw_owned_group_recipients"]) == {
        fixture["target_student"]["user_id"],
        fixture["opted_out_student"]["user_id"],
    }
    assert evidence["owned_group_recipients"] == [
        fixture["target_student"]["user_id"],
        fixture["opted_out_student"]["user_id"],
    ]
    assert fixture["non_target_student"]["user_id"] not in evidence["owned_group_recipients"]
    assert fixture["opted_out_student"]["user_id"] in evidence["owned_group_recipients"]
    assert evidence["foreign_group_recipients"] == [fixture["non_target_student"]["user_id"]]
    assert evidence["opted_out"] == {
        "user_id": fixture["opted_out_student"]["user_id"],
        "disabled_categories": fixture["expected"]["disabled_categories"],
        "included_in_mass_recipients": True,
    }
    assert evidence["owner_notification"]["recipient_count"] == fixture["expected"]["recipient_count"]
    assert evidence["trainer_notification"]["recipient_count"] == fixture["expected"]["recipient_count"]
    assert evidence["subscriptions"]["target_active"] == 2
    assert evidence["subscriptions"]["target_inactive"] == 1
    assert evidence["subscriptions"]["non_target_active"] == 1
    assert evidence["subscriptions"]["opted_out_active"] == 1
    assert evidence["denied_notifications_created"] is False


@pytest.mark.django_db
def test_checkin_cancel_e2e_prepare_and_assert_validates_reverse_side_effects(tmp_path):
    output = tmp_path / "checkin-cancel-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_checkin_cancel_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("checkin-cancel-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["checkin_id"]
    assert fixture["subscription_id"]
    assert fixture["earning_id"]
    assert fixture["retention_task_id"]

    with pytest.raises(CommandError, match="check-in is not cancelled"):
        call_command(
            "assert_checkin_cancel_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    with patch("apps.attendance.services.async_task", side_effect=_run_checkin_cancel_async_task_sync):
        cancel_checkin(
            checkin_id=fixture["checkin_id"],
            club_id=fixture["club_id"],
            cancelled_by_user_id=fixture["owner"]["user_id"],
            user_role=ClubMembership.Role.OWNER,
        )

    stdout = io.StringIO()
    call_command(
        "assert_checkin_cancel_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["checkin"]["id"] == fixture["checkin_id"]
    assert evidence["checkin"]["cancelled"] is True
    assert evidence["checkin"]["soft_deleted"] is True
    assert evidence["subscription"]["trainings_left"] == fixture["expected"]["trainings_left_after_cancel"]
    assert evidence["subscription"]["trainings_used"] == fixture["expected"]["trainings_used_after_cancel"]
    assert evidence["earning"]["cancelled"] is True
    assert evidence["grade"]["trainings_since_last_grade"] == 0
    assert (
        evidence["group_session"]["attendee_count"]
        == fixture["expected"]["group_session_attendee_count_after_cancel"]
    )
    assert evidence["retention_task"]["status"] == RetentionTask.TaskStatus.OPEN


@pytest.mark.django_db
def test_retention_task_lifecycle_e2e_prepare_trigger_and_assert_validates_trainer_lifecycle(
    tmp_path,
):
    output = tmp_path / "retention-task-lifecycle-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_retention_task_lifecycle_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("retention-task-lifecycle-e2e-")
    assert fixture["trainer"]["email"]
    assert fixture["trainer"]["password"]
    assert fixture["trainer"]["password"] not in stdout.getvalue()
    assert fixture["target_student"]["student_id"]
    assert fixture["other_task_id"]
    assert fixture["push_subscription_ids"]["trainer"]
    assert not RetentionTask.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["target_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        task_type=RetentionTask.TaskType.RETENTION,
        resolved_at__isnull=True,
    ).exists()

    trigger_stdout = io.StringIO()
    call_command("trigger_retention_task_lifecycle_e2e", fixture=str(output), stdout=trigger_stdout)
    trigger = json.loads(trigger_stdout.getvalue())

    assert trigger["ok"] is True
    assert trigger["trainer_queued_push_count"] >= 1
    task = RetentionTask.objects.for_club(fixture["club_id"]).get(id=trigger["task_id"])
    assert task.student_id == fixture["target_student"]["student_id"]
    assert task.trainer_id == fixture["trainer"]["trainer_id"]
    assert task.level == fixture["expected"]["level"]
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert SentNotification.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["target_student"]["student_id"],
        notification_type=fixture["expected"]["trainer_notification_type"],
        sent_date=timezone.now().date(),
    ).exists()

    with pytest.raises(CommandError, match="was not closed"):
        call_command(
            "assert_retention_task_lifecycle_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.retention.services import add_task_comment, close_retention_task, snooze_task, update_task_status

    update_task_status(
        task_id=task.id,
        club_id=fixture["club_id"],
        status=RetentionTask.TaskStatus.IN_PROGRESS,
    )
    task.refresh_from_db()
    assert task.status == fixture["expected"]["status_after_in_progress"]

    add_task_comment(
        task_id=task.id,
        club_id=fixture["club_id"],
        author_id=fixture["trainer"]["user_id"],
        text=fixture["expected"]["comment_text"],
    )
    assert TaskComment.objects.for_club(fixture["club_id"]).filter(
        task=task,
        author_id=fixture["trainer"]["user_id"],
        text=fixture["expected"]["comment_text"],
    ).exists()

    snooze_task(
        task_id=task.id,
        club_id=fixture["club_id"],
        new_due_date=timezone.localdate() + timedelta(days=3),
    )
    task.refresh_from_db()
    assert task.status == fixture["expected"]["status_after_snooze"]

    close_retention_task(
        task_id=task.id,
        club_id=fixture["club_id"],
        resolution=fixture["expected"]["resolution_after_close"],
        notes=fixture["expected"]["close_notes"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_retention_task_lifecycle_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["task"]["id"] == task.id
    assert evidence["task"]["status"] == fixture["expected"]["status_after_close"]
    assert evidence["task"]["resolution"] == fixture["expected"]["resolution_after_close"]
    assert evidence["comment"]["author_id"] == fixture["trainer"]["user_id"]
    assert (
        evidence["trainer_notification"]["notification_type"]
        == fixture["expected"]["trainer_notification_type"]
    )
    assert (
        evidence["trainer_notification"]["push_subscription_id"]
        == fixture["push_subscription_ids"]["trainer"]
    )
    assert evidence["student"]["status"] == Student.Status.AT_RISK
    assert evidence["privacy"]["foreign_task_id"] == fixture["other_task_id"]
    assert evidence["privacy"]["foreign_task_status"] == RetentionTask.TaskStatus.OPEN


@pytest.mark.django_db
def test_automatic_notification_lifecycle_e2e_prepare_trigger_and_assert_validates_records(
    tmp_path,
):
    output = tmp_path / "automatic-notification-lifecycle-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_automatic_notification_lifecycle_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("automatic-notification-lifecycle-e2e-")
    assert fixture["student"]["email"]
    assert fixture["student"]["password"]
    assert fixture["parent"]["email"]
    assert fixture["parent"]["password"]
    assert fixture["student"]["password"] not in stdout.getvalue()
    assert fixture["parent"]["password"] not in stdout.getvalue()
    assert fixture["trainings_left_checkin_id"]
    assert fixture["last_training_checkin_id"]
    assert fixture["feedback_form_ids"]["trial"]
    assert fixture["feedback_form_ids"]["churned"]
    assert fixture["push_subscription_ids"]["student"]
    assert fixture["push_subscription_ids"]["parent"]
    assert fixture["reminder_schedule_id"]
    assert fixture["reminder_occurrence_date"]
    assert fixture["reminder_occurrence_at"]
    assert fixture["expected"]["feedback_disabled_categories"] == ["feedback_surveys"]
    assert fixture["expected"]["parent_disabled_categories"] == ["feedback_surveys", "child_checkin"]
    assert fixture["expected"]["child_checkin_expected_queued_pushes"] == 0
    assert fixture["expected"]["child_checkin_suppressed_notification_types"] == [
        f"parent_checkin:{fixture['trainings_left_checkin_id']}",
        f"parent_checkin_cancelled:{fixture['last_training_checkin_id']}",
    ]
    assert fixture["expected"]["feedback_expected_queued_pushes"] == 0

    with pytest.raises(CommandError, match="expected notification types missing"):
        call_command(
            "assert_automatic_notification_lifecycle_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    trigger_stdout = io.StringIO()
    call_command(
        "trigger_automatic_notification_lifecycle_e2e",
        fixture=str(output),
        stdout=trigger_stdout,
    )
    trigger = json.loads(trigger_stdout.getvalue())

    assert trigger["ok"] is True
    assert trigger["queued_push_count"] >= 8
    assert trigger["subscription_expiry"]["notifications_sent"] >= 3
    assert trigger["training_reminder_24h"]["reminders_sent"] >= 1
    assert trigger["training_reminder_1h"]["reminders_sent"] >= 1
    assert trigger["missed_training"]["pushes_sent"] >= 1
    assert (
        trigger["child_checkin"]["queued_push_count"]
        == fixture["expected"]["child_checkin_expected_queued_pushes"]
    )
    assert (
        trigger["feedback_surveys"]["queued_push_count"]
        == fixture["expected"]["feedback_expected_queued_pushes"]
    )

    stdout = io.StringIO()
    call_command(
        "assert_automatic_notification_lifecycle_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())
    assert evidence["reminder_stage_state"] == {
        "one_hour": "queued",
        "twenty_four_hour": "queued",
    }

    expected_types = sorted(
        fixture["expected"]["student_notification_types"]
        + fixture["expected"]["parent_notification_types"]
    )
    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["sent_notification_types"] == expected_types
    assert evidence["push_subscriptions"]["student_active"] == fixture["push_subscription_ids"]["student"]
    assert evidence["push_subscriptions"]["parent_active"] == fixture["push_subscription_ids"]["parent"]
    assert evidence["feedback_surveys"]["parent_disabled_categories"] == ["feedback_surveys", "child_checkin"]
    assert evidence["feedback_surveys"]["forms"] == {"churned": True, "trial": True}
    assert evidence["feedback_surveys"]["trial_feedback_push_expected"] is False
    assert evidence["feedback_surveys"]["churned_survey_push_expected"] is False
    assert evidence["child_checkin"]["recorded_suppressed_count"] == 0
    assert evidence["child_checkin"]["queued_push_expected"] == 0
    assert evidence["child_checkin"]["suppressed_notification_types"] == fixture["expected"][
        "child_checkin_suppressed_notification_types"
    ]
    assert SentNotification.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["student"]["student_id"],
    ).count() == len(expected_types)
    assert not SentNotification.objects.for_club(fixture["club_id"]).filter(
        student_id=fixture["student"]["student_id"],
        notification_type__in=fixture["expected"]["child_checkin_suppressed_notification_types"],
    ).exists()


@pytest.mark.django_db
def test_owner_notification_template_settings_e2e_prepare_and_assert_validates_timing_templates_and_tenant_scope(
    tmp_path,
):
    output = tmp_path / "owner-notification-template-settings-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_owner_notification_template_settings_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)
    expected = fixture["expected"]

    assert fixture["fixture_id"].startswith("owner-notification-template-settings-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["admin_push"]["subscription_id"]
    assert fixture["admin_push"]["endpoint"] not in stdout.getvalue()
    assert fixture["club_id"] != fixture["control_club_id"]
    assert fixture["template"]["trigger_type"] == NotificationTemplate.TriggerType.TRAINING_REMINDER

    with pytest.raises(CommandError, match="max push per week mismatch"):
        call_command(
            "assert_owner_notification_template_settings_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.billing.services import update_club_settings
    from apps.htmx_admin.views.settings.notifications import _seed_default_templates
    from apps.notifications.services import update_notification_template

    club = Club.objects.get(id=fixture["club_id"])
    _seed_default_templates(club)
    update_club_settings(
        club_id=club.id,
        max_push_per_week=expected["max_push_per_week"],
        feedback_delay_hours=expected["feedback_delay_hours"],
        quiet_hours_start=time.fromisoformat(expected["quiet_hours_start"]),
        quiet_hours_end=time.fromisoformat(expected["quiet_hours_end"]),
    )
    update_notification_template(
        template_id=fixture["template"]["template_id"],
        club_id=club.id,
        title_template=f"  {expected['updated_title']}  ",
        body_template=f"  {expected['updated_body']}  ",
        is_enabled=expected["template_is_enabled"],
    )
    expiry_template = NotificationTemplate.objects.for_club(club).get(
        trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
    )
    update_notification_template(
        template_id=expiry_template.id,
        club_id=club.id,
        days_before=expected["expiry_days_before"],
    )

    with pytest.raises(CommandError, match="admin push subscription was not unsubscribed"):
        call_command(
            "assert_owner_notification_template_settings_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    PushSubscription.objects.filter(
        id=fixture["admin_push"]["subscription_id"],
        user_id=fixture["owner"]["user_id"],
    ).delete()

    stdout = io.StringIO()
    call_command(
        "assert_owner_notification_template_settings_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["settings"]["max_push_per_week"] == expected["max_push_per_week"]
    assert evidence["settings"]["feedback_delay_hours"] == expected["feedback_delay_hours"]
    assert evidence["templates"]["count"] == expected["default_template_count"]
    assert evidence["templates"]["is_enabled"] == expected["template_is_enabled"]
    assert evidence["templates"]["expiry_days_before"] == expected["expiry_days_before"]
    assert evidence["admin_push"]["server_subscription_removed"] is True
    assert evidence["tenant_control"]["unchanged"] is True
    assert not NotificationTemplate.objects.for_club(fixture["control_club_id"]).filter(
        title_template=expected["updated_title"],
    ).exists()


@pytest.mark.django_db
def test_club_settings_business_config_e2e_prepare_and_assert_validates_settings_catalog_billing(
    tmp_path,
):
    output = tmp_path / "club-settings-business-config-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_club_settings_business_config_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)
    expected = fixture["expected"]

    assert fixture["fixture_id"].startswith("club-settings-business-config-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["club_id"] != fixture["control_club_id"]
    assert expected["document_type_is_active"] is False
    assert expected["discount_is_active"] is False
    assert expected["discounted_tariff_amount"] == "2125.00"
    assert expected["drop_in_debt_amount"] == expected["drop_in_price"]
    assert fixture["consumption"]["sale_student_id"]
    assert fixture["consumption"]["drop_in_student_id"]
    assert fixture["consumption"]["trainer_id"]

    with pytest.raises(CommandError, match="club name display mismatch"):
        call_command(
            "assert_club_settings_business_config_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.billing.services import (
        create_discount,
        create_tariff,
        create_training_type,
        update_club_settings,
        update_discount,
        update_tariff,
        update_training_type,
    )
    from apps.clubs.services import create_location, delete_location, update_location
    from apps.documents.services import create_document_type, update_document_type
    from apps.grades.services import add_grade, create_grade_system, delete_grade, delete_grade_system, update_grade

    club = Club.objects.get(id=fixture["club_id"])
    club.timezone = expected["timezone"]
    club.save(update_fields=["timezone", "updated_at"])
    update_club_settings(
        club_id=club.id,
        club_name_display=expected["club_name_display"],
        primary_color=expected["primary_color"],
        accent_color=expected["primary_color"],
        freeze_enabled=expected["freeze_enabled"],
        freeze_max_days=expected["freeze_max_days"],
        freeze_max_count=expected["freeze_max_count"],
        min_trainings_to_freeze=expected["min_trainings_to_freeze"],
    )
    location = create_location(
        club_id=club.id,
        name=expected["location_initial_name"],
        address=expected["location_initial_address"],
    )
    location = update_location(
        location_id=location.id,
        club_id=club.id,
        name=expected["location_name"],
        address=expected["location_address"],
    )
    temporary_location = create_location(
        club_id=club.id,
        name=expected["location_delete_name"],
        address=expected["location_delete_address"],
    )
    delete_location(location_id=temporary_location.id, club_id=club.id)
    grade_system = create_grade_system(club_id=club.id, discipline=expected["grade_system_name"])
    grade = add_grade(
        club_id=club.id,
        grade_system_id=grade_system.id,
        name=expected["grade_initial_name"],
        order=expected["grade_final_order"],
    )
    grade = update_grade(
        grade_id=grade.id,
        club_id=club.id,
        name=expected["grade_final_name"],
        order=expected["grade_final_order"],
        min_trainings=expected["grade_final_min_trainings"],
    )
    temporary_grade = add_grade(
        club_id=club.id,
        grade_system_id=grade_system.id,
        name=expected["grade_delete_name"],
        order=expected["grade_delete_order"],
    )
    delete_grade(grade_id=temporary_grade.id, club_id=club.id)
    temporary_grade_system = create_grade_system(
        club_id=club.id,
        discipline=expected["grade_system_delete_name"],
    )
    delete_grade_system(grade_system_id=temporary_grade_system.id, club_id=club.id)
    document_type = create_document_type(
        club_id=club.id,
        name=expected["document_type_initial_name"],
        description="Initial club settings E2E document",
        is_required=True,
        scope="all",
    )
    document_type = update_document_type(
        club_id=club.id,
        document_type_id=document_type.id,
        name=expected["document_type_name"],
        description=expected["document_type_description"],
        is_required=expected["document_type_is_required"],
        scope=expected["document_type_scope"],
        is_active=expected["document_type_is_active"],
    )
    training_type = create_training_type(
        club_id=club.id,
        name=expected["training_type_initial_name"],
        slug=f"settings-group-{fixture['fixture_id'][-8:]}",
    )
    training_type = update_training_type(
        training_type_id=training_type.id,
        club_id=club.id,
        name=expected["training_type_name"],
    )
    training_type.drop_in_price = Decimal(expected["drop_in_price"])
    training_type.trial_free = expected["trial_free"]
    training_type.save(update_fields=["drop_in_price", "trial_free", "updated_at"])
    tariff = create_tariff(
        club_id=club.id,
        name=expected["tariff_initial_name"],
        training_type_id=training_type.id,
        price=Decimal(expected["tariff_initial_price"]),
        trainings_limit=expected["tariff_initial_trainings_limit"],
        duration_days=expected["tariff_initial_duration_days"],
        scope=expected["tariff_scope"],
        location_id=location.id,
        description=expected["tariff_initial_description"],
    )
    tariff = update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        name=expected["tariff_name"],
        price=Decimal(expected["tariff_price"]),
        trainings_limit=expected["tariff_trainings_limit"],
        duration_days=expected["tariff_duration_days"],
        description=expected["tariff_description"],
    )
    discount = create_discount(
        club_id=club.id,
        name=expected["discount_initial_name"],
        discount_type=expected["discount_type"],
        value=Decimal(expected["discount_initial_value"]),
    )
    discount = update_discount(
        discount_id=discount.id,
        club_id=club.id,
        name=expected["discount_name"],
        discount_type=expected["discount_type"],
        value=Decimal(expected["discount_value"]),
    )

    stdout = io.StringIO()
    call_command(
        "assert_club_settings_business_config_e2e",
        fixture=str(output),
        timeout_seconds=0,
        mode="active-consumption",
        stdout=stdout,
    )
    active_evidence = json.loads(stdout.getvalue())

    assert active_evidence["ok"] is True
    assert active_evidence["mode"] == "active-consumption"
    assert active_evidence["documents"]["document_type_active"] is False
    assert active_evidence["billing"]["training_type_active"] is True
    assert active_evidence["billing"]["tariff_active"] is True
    assert active_evidence["billing"]["discount_active"] is True
    assert active_evidence["active_consumption"]["payment_amount"] == expected["discounted_tariff_amount"]
    assert active_evidence["active_consumption"]["payment_original_amount"] == expected["tariff_price"]
    assert active_evidence["active_consumption"]["payment_discount_ids"] == [discount.id]
    assert active_evidence["active_consumption"]["subscription_status"] == Subscription.Status.PENDING
    assert active_evidence["active_consumption"]["subscription_trainings_left"] == expected["tariff_trainings_limit"]
    assert active_evidence["active_consumption"]["checkin_is_debt"] is True
    assert active_evidence["active_consumption"]["checkin_subscription_id"] is None
    assert active_evidence["active_consumption"]["debt_amount"] == expected["drop_in_debt_amount"]

    update_tariff(
        tariff_id=tariff.id,
        club_id=club.id,
        is_active=expected["tariff_is_active"],
    )
    update_discount(
        discount_id=discount.id,
        club_id=club.id,
        is_active=expected["discount_is_active"],
    )
    update_training_type(
        training_type_id=training_type.id,
        club_id=club.id,
        is_active=expected["training_type_is_active"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_club_settings_business_config_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["mode"] == "final"
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["settings"]["club_name_display"] == expected["club_name_display"]
    assert evidence["settings"]["primary_color"] == expected["primary_color"]
    assert evidence["settings"]["timezone"] == expected["timezone"]
    assert evidence["settings"]["logo_file"] is False
    assert evidence["settings"]["logo_url"] == ""
    assert evidence["catalog"]["location_id"] == location.id
    assert evidence["catalog"]["location_name"] == expected["location_name"]
    assert evidence["catalog"]["location_address"] == expected["location_address"]
    assert evidence["catalog"]["temporary_location_deleted"] is True
    assert evidence["catalog"]["grade_system_id"] == grade_system.id
    assert evidence["catalog"]["grade_system_name"] == expected["grade_system_name"]
    assert evidence["catalog"]["grade_id"] == grade.id
    assert evidence["catalog"]["grade_name"] == expected["grade_final_name"]
    assert evidence["catalog"]["grade_min_trainings"] == expected["grade_final_min_trainings"]
    assert evidence["catalog"]["temporary_grade_deleted"] is True
    assert evidence["catalog"]["temporary_grade_system_deleted"] is True
    assert evidence["documents"]["document_type_id"] == document_type.id
    assert evidence["documents"]["document_type_name"] == expected["document_type_name"]
    assert evidence["documents"]["document_type_scope"] == expected["document_type_scope"]
    assert evidence["documents"]["document_type_required"] == expected["document_type_is_required"]
    assert evidence["documents"]["document_type_active"] == expected["document_type_is_active"]
    assert evidence["billing"]["training_type_id"] == training_type.id
    assert evidence["billing"]["training_type_active"] == expected["training_type_is_active"]
    assert evidence["billing"]["drop_in_price"] == expected["drop_in_price"]
    assert evidence["billing"]["tariff_id"] == tariff.id
    assert evidence["billing"]["tariff_active"] == expected["tariff_is_active"]
    assert evidence["billing"]["tariff_location_id"] == location.id
    assert evidence["billing"]["discount_id"] == discount.id
    assert evidence["billing"]["discount_active"] == expected["discount_is_active"]
    assert evidence["active_consumption"] is None
    assert evidence["tenant_control"]["unchanged"] is True
    assert not Location.objects.filter(
        club_id=fixture["control_club_id"],
        name__in=[
            expected["location_initial_name"],
            expected["location_name"],
            expected["location_delete_name"],
        ],
    ).exists()
    assert not Location.objects.filter(club=club, name=expected["location_initial_name"]).exists()
    assert not Location.objects.filter(club=club, name=expected["location_delete_name"]).exists()
    assert not GradeSystem.objects.for_club(fixture["control_club_id"]).filter(
        discipline=expected["grade_system_name"],
    ).exists()
    assert not Grade.objects.for_club(club).filter(name=expected["grade_initial_name"]).exists()
    assert not Grade.objects.for_club(club).filter(name=expected["grade_delete_name"]).exists()
    assert not GradeSystem.objects.for_club(club).filter(
        discipline=expected["grade_system_delete_name"],
    ).exists()
    assert not DocumentType.objects.for_club(club).filter(
        name=expected["document_type_initial_name"],
    ).exists()
    assert not DocumentType.objects.for_club(fixture["control_club_id"]).filter(
        name__in=[expected["document_type_initial_name"], expected["document_type_name"]],
    ).exists()
    assert not TrainingType.objects.for_club(fixture["control_club_id"]).filter(
        name__in=[expected["training_type_initial_name"], expected["training_type_name"]],
    ).exists()
    assert not TrainingType.objects.for_club(club).filter(name=expected["training_type_initial_name"]).exists()
    assert not Tariff.objects.for_club(club).filter(name=expected["tariff_initial_name"]).exists()
    assert not Tariff.objects.for_club(fixture["control_club_id"]).filter(
        name__in=[expected["tariff_initial_name"], expected["tariff_name"]],
    ).exists()
    assert not Discount.objects.for_club(club).filter(name=expected["discount_initial_name"]).exists()
    assert not Discount.objects.for_club(fixture["control_club_id"]).filter(
        name__in=[expected["discount_initial_name"], expected["discount_name"]],
    ).exists()


@pytest.mark.django_db
def test_owner_dashboard_business_e2e_prepare_and_assert_validates_metrics_and_kiosk_controls(
    tmp_path,
):
    output = tmp_path / "owner-dashboard-business-fixture.json"
    stdout = io.StringIO()
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ):
        call_command("prepare_owner_dashboard_business_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("owner-dashboard-business-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["student"]["student_id"]
    assert fixture["payment_ids"]["confirmed"]
    assert fixture["payment_ids"]["pending"]
    assert fixture["debt_id"]
    assert fixture["expense_id"]
    assert fixture["retention_admin"]["task_id"]
    assert fixture["retention_admin"]["comment_id"]
    assert fixture["retention_admin"]["comment_text"]
    assert fixture["report_range"]["date_from"]
    assert fixture["report_range"]["date_to"]
    assert fixture["browser_expense"]["name"]
    assert fixture["browser_expense"]["amount"]
    assert fixture["enrollment_admin"]["student_id"]
    assert fixture["enrollment_admin"]["source_schedule_id"] != fixture["enrollment_admin"]["target_schedule_id"]
    assert fixture["enrollment_consistency"]["student_id"] == fixture["enrollment_admin"]["student_id"]
    assert fixture["enrollment_consistency"]["active_enrollment_id"]
    assert fixture["enrollment_consistency"]["transferred_enrollment_id"]
    assert fixture["schedule_admin"]["create"]["created_group_name"]
    assert fixture["schedule_admin"]["create"]["edited_group_name"]
    assert fixture["schedule_admin"]["cancel"]["schedule_id"]
    assert fixture["schedule_admin"]["reschedule"]["schedule_id"]
    assert fixture["schedule_admin"]["substitute"]["schedule_id"]
    assert fixture["schedule_admin"]["revert"]["schedule_id"]
    assert fixture["training_group_reconciliation"]["initial_rollout_mode"] == "off"
    assert fixture["training_group_reconciliation"]["new_writes_enabled"] is True
    assert fixture["training_group_reconciliation"]["manual_operational_admission_enabled"] is True
    assert TrainingGroupRolloutState.objects.for_club(fixture["club_id"]).get().mode == "off"
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["admin"]["password"] not in stdout.getvalue()

    with pytest.raises(CommandError, match="kiosk device"):
        call_command(
            "assert_owner_dashboard_business_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.attendance.services import deactivate_kiosk, generate_kiosk_pin

    generated_pin = generate_kiosk_pin(club_id=fixture["club_id"])
    assert generated_pin
    browser_expense = fixture["browser_expense"]
    deleted_expense = Expense.objects.create(
        club_id=fixture["club_id"],
        name=browser_expense["name"],
        amount=Decimal(browser_expense["amount"]),
        date=date.fromisoformat(browser_expense["date"]),
        category=browser_expense["category"],
        is_recurring=False,
    )
    deleted_expense.soft_delete()

    with pytest.raises(CommandError, match="kiosk active count"):
        call_command(
            "assert_owner_dashboard_business_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    deactivate_kiosk(club_id=fixture["club_id"])
    with pytest.raises(CommandError, match="enrollment admin source transfer"):
        call_command(
            "assert_owner_dashboard_business_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    enrollment_admin = fixture["enrollment_admin"]
    session_date = date.fromisoformat(enrollment_admin["session_date"])
    transfer_date = date.fromisoformat(enrollment_admin["transfer_date"])
    cancel_date = date.fromisoformat(enrollment_admin["cancel_date"])
    enrollment = enroll_student_in_schedule(
        club_id=fixture["club_id"],
        student_id=enrollment_admin["student_id"],
        schedule_id=enrollment_admin["source_schedule_id"],
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=session_date,
        created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
    )
    frozen = freeze_schedule_enrollment(
        club_id=fixture["club_id"],
        enrollment_id=enrollment.id,
    )
    assert frozen.status == ScheduleEnrollment.Status.FROZEN
    active = unfreeze_schedule_enrollment(
        club_id=fixture["club_id"],
        enrollment_id=enrollment.id,
    )
    assert active.status == ScheduleEnrollment.Status.ACTIVE
    closed, opened = transfer_schedule_enrollment(
        club_id=fixture["club_id"],
        enrollment_id=enrollment.id,
        target_schedule_id=enrollment_admin["target_schedule_id"],
        ends_on=transfer_date - timedelta(days=1),
    )
    assert closed.status == ScheduleEnrollment.Status.TRANSFERRED
    assert opened.starts_on == transfer_date
    cancelled = cancel_schedule_enrollment(
        club_id=fixture["club_id"],
        enrollment_id=opened.id,
        ends_on=cancel_date - timedelta(days=1),
    )
    assert cancelled.status == ScheduleEnrollment.Status.CANCELLED

    with pytest.raises(CommandError, match="schedule admin created schedule"):
        call_command(
            "assert_owner_dashboard_business_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    schedule_admin = fixture["schedule_admin"]
    create_form = schedule_admin["create"]
    create_date = date.fromisoformat(create_form["date"])
    created_schedule = create_schedule(
        club_id=fixture["club_id"],
        day_of_week=create_date.weekday(),
        start_time=time.fromisoformat(create_form["created_start_time"]),
        end_time=time.fromisoformat(create_form["created_end_time"]),
        group_name=create_form["created_group_name"],
        trainer_id=create_form["trainer_id"],
        location_id=create_form["location_id"],
        training_type_id=create_form["created_training_type_id"],
        one_time_date=create_date,
    )
    updated_schedule = update_schedule(
        club_id=fixture["club_id"],
        schedule_id=created_schedule.id,
        start_time=time.fromisoformat(create_form["edited_start_time"]),
        end_time=time.fromisoformat(create_form["edited_end_time"]),
        group_name=create_form["edited_group_name"],
        training_type_id=create_form["edited_training_type_id"],
    )
    assert updated_schedule.group_name == create_form["edited_group_name"]

    cancel_form = schedule_admin["cancel"]
    cancel_session(
        club_id=fixture["club_id"],
        schedule_id=cancel_form["schedule_id"],
        date=date.fromisoformat(cancel_form["date"]),
        reason=cancel_form["reason"],
    )
    reschedule_form = schedule_admin["reschedule"]
    reschedule_session(
        club_id=fixture["club_id"],
        schedule_id=reschedule_form["schedule_id"],
        date=date.fromisoformat(reschedule_form["old_date"]),
        new_date=date.fromisoformat(reschedule_form["new_date"]),
        new_start_time=time.fromisoformat(reschedule_form["new_start_time"]),
        new_end_time=time.fromisoformat(reschedule_form["new_end_time"]),
    )
    substitute_form = schedule_admin["substitute"]
    substitute_trainer(
        club_id=fixture["club_id"],
        schedule_id=substitute_form["schedule_id"],
        date=date.fromisoformat(substitute_form["date"]),
        substitute_trainer_id=substitute_form["substitute_trainer_id"],
    )
    revert_form = schedule_admin["revert"]
    delete_exception(
        club_id=fixture["club_id"],
        schedule_id=revert_form["schedule_id"],
        exception_date=date.fromisoformat(revert_form["date"]),
    )

    from apps.attendance.services.training_group_reconciliation import (
        apply_training_group_reconciliation,
        build_training_group_reconciliation_preview,
    )
    from apps.attendance.services.training_groups import transition_training_group_rollout_for_owner

    reconciliation = fixture["training_group_reconciliation"]
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        transition_training_group_rollout_for_owner(
            club_id=fixture["club_id"],
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=fixture["owner"]["user_id"],
            rationale="Focused owner reconciliation transition.",
            idempotency_key=f"{reconciliation['idempotency_key']}-reconciling",
            rollout_gate_digest="",
        )
        preview = build_training_group_reconciliation_preview(
            club=Club.objects.get(id=fixture["club_id"]),
            schedule_ids=reconciliation["schedule_ids"],
            canonical_name=reconciliation["canonical_name"],
            responsible_trainer_id=reconciliation["responsible_trainer_id"],
            start_dates=[],
        )
        applied = apply_training_group_reconciliation(
            club=Club.objects.get(id=fixture["club_id"]),
            schedule_ids=reconciliation["schedule_ids"],
            canonical_name=reconciliation["canonical_name"],
            responsible_trainer_id=reconciliation["responsible_trainer_id"],
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=fixture["owner"]["user_id"],
            rationale=reconciliation["rationale"],
            idempotency_key=reconciliation["idempotency_key"],
        )
        retry = apply_training_group_reconciliation(
            club=Club.objects.get(id=fixture["club_id"]),
            schedule_ids=reconciliation["schedule_ids"],
            canonical_name=reconciliation["canonical_name"],
            responsible_trainer_id=reconciliation["responsible_trainer_id"],
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=fixture["owner"]["user_id"],
            rationale=reconciliation["rationale"],
            idempotency_key=reconciliation["idempotency_key"],
        )
        assert retry == applied
        for target_mode in (TrainingGroupRolloutState.Mode.SHADOW, TrainingGroupRolloutState.Mode.ACTIVE):
            transition_training_group_rollout_for_owner(
                club_id=fixture["club_id"],
                target_mode=target_mode,
                actor_id=fixture["owner"]["user_id"],
                rationale=f"Focused owner reconciliation {target_mode} transition.",
                idempotency_key=f"{reconciliation['idempotency_key']}-{target_mode}",
                rollout_gate_digest=applied["rollout_gate_digest"],
            )
    fixture["runtime"] = {
        "training_group_reconciliation": {
            "ui_preview_digest": preview["digest"],
            "apply": applied,
        }
    }
    output.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    stdout = io.StringIO()
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ):
        call_command(
            "assert_owner_dashboard_business_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=stdout,
        )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["dashboard"]["revenue"] == fixture["expected"]["dashboard_revenue"]
    assert evidence["dashboard"]["active_subscriptions"] == 1
    assert evidence["dashboard"]["debtors"] == 1
    assert evidence["payments"]["pending_count"] == 1
    assert evidence["payments"]["confirmed_count"] == 1
    assert evidence["debtors"]["open_count"] == 1
    assert evidence["pnl"]["income"] == fixture["expected"]["pnl_income"]
    assert evidence["pnl"]["manual_expenses"] == fixture["expected"]["manual_expense"]
    assert evidence["pnl"]["report_from"] == fixture["report_range"]["date_from"]
    assert evidence["pnl"]["report_to"] == fixture["report_range"]["date_to"]
    assert evidence["pnl"]["active_expense_count"] == 1
    assert evidence["pnl"]["browser_expense_deleted_count"] == 1
    assert evidence["kiosk"]["active_count"] == 0
    assert evidence["kiosk"]["device_count"] == 1
    assert evidence["kiosk"]["stored_pin_count"] == 0
    assert evidence["retention_admin"]["task_id"] == fixture["retention_admin"]["task_id"]
    assert evidence["retention_admin"]["comment_id"] == fixture["retention_admin"]["comment_id"]
    assert evidence["retention_admin"]["comment_count"] == 1
    assert evidence["retention_admin"]["status"] == RetentionTask.TaskStatus.OPEN
    assert evidence["retention_admin"]["level"] == RetentionTask.Level.RED
    assert evidence["enrollment_admin"]["source_status"] == ScheduleEnrollment.Status.TRANSFERRED
    assert evidence["enrollment_admin"]["target_status"] == ScheduleEnrollment.Status.CANCELLED
    assert evidence["enrollment_admin"]["open_count"] == 0
    assert evidence["enrollment_consistency"]["roster_enrollment_id"] == (
        fixture["enrollment_consistency"]["active_enrollment_id"]
    )
    assert evidence["enrollment_consistency"]["roster_status"] == ScheduleEnrollment.Status.ACTIVE
    assert evidence["schedule_admin"]["edited_group_name"] == create_form["edited_group_name"]
    assert evidence["schedule_admin"]["edited_training_type_id"] == create_form["edited_training_type_id"]
    assert evidence["schedule_admin"]["reschedule_new_visible"] is True
    assert evidence["schedule_admin"]["substitute_visible"] is True
    assert evidence["schedule_admin"]["revert_exception_exists"] is False
    assert evidence["schedule_admin"]["reverted_visible"] is True
    assert evidence["training_group_reconciliation"]["rollout_mode"] == "active"
    assert evidence["training_group_reconciliation"]["new_writes_enabled"] is True
    assert evidence["training_group_reconciliation"]["manual_operational_admission_enabled"] is True
    assert evidence["training_group_reconciliation"]["selected_schedule_ids"] == sorted(
        fixture["training_group_reconciliation"]["schedule_ids"]
    )
    assert evidence["training_group_reconciliation"]["membership_authority"] == "independent"
    assert evidence["training_group_reconciliation"]["source_enrollment_id"] == (
        fixture["training_group_reconciliation"]["source_enrollment_id"]
    )
    assert evidence["training_group_reconciliation"]["projection_schedule_ids"] == [
        fixture["training_group_reconciliation"]["projection_schedule_id"]
    ]
    assert evidence["training_group_reconciliation"]["roster_membership_schedule_ids"] == sorted(
        fixture["training_group_reconciliation"]["schedule_ids"]
    )
    assert evidence["training_group_reconciliation"]["apply_event_count"] == 1
    assert evidence["training_group_reconciliation"]["audit_valid"] is True
    assert "has_active_pin" not in evidence["kiosk"]
    assert KioskDevice.objects.filter(club_id=fixture["club_id"], is_active=True).count() == 0
    assert TaskComment.objects.for_club(fixture["club_id"]).filter(
        id=fixture["retention_admin"]["comment_id"],
        task_id=fixture["retention_admin"]["task_id"],
    ).exists()
    assert Expense.objects.for_club(fixture["club_id"]).filter(id=fixture["expense_id"]).exists()
    assert ScheduleException.objects.for_club(fixture["club_id"]).filter(
        schedule_id=cancel_form["schedule_id"],
        exception_type=ScheduleException.ExceptionType.CANCELLED,
    ).exists()
    assert not ScheduleException.objects.for_club(fixture["club_id"]).filter(
        schedule_id=revert_form["schedule_id"],
        date=date.fromisoformat(revert_form["date"]),
    ).exists()


@pytest.mark.django_db
def test_owner_trainer_rate_grid_e2e_prepare_and_assert_validates_rate_grid_and_salary(
    tmp_path,
):
    output = tmp_path / "owner-trainer-rate-grid-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_owner_trainer_rate_grid_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)
    expected = fixture["expected"]

    assert fixture["fixture_id"].startswith("owner-trainer-rate-grid-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["control_club_id"] != fixture["club_id"]
    assert fixture["seeded_trainer"]["trainer_id"]
    assert fixture["checkin_id"]

    with pytest.raises(CommandError, match="UI-created trainer not found"):
        call_command(
            "assert_owner_trainer_rate_grid_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )

    from apps.trainers.services import create_trainer, update_trainer_rates

    create_trainer(
        club_id=fixture["club_id"],
        first_name=expected["new_trainer_first_name"],
        last_name=expected["new_trainer_last_name"],
        locations=[
            {
                "location_id": fixture["location"]["location_id"],
                "rates": [
                    {
                        "training_type_id": fixture["training_types"]["group"]["training_type_id"],
                        "percent": Decimal(expected["new_trainer_group_rate"]),
                    },
                    {
                        "training_type_id": fixture["training_types"]["personal"]["training_type_id"],
                        "percent": Decimal(expected["new_trainer_personal_rate"]),
                    },
                ],
            },
        ],
    )
    update_trainer_rates(
        club_id=fixture["club_id"],
        trainer_id=fixture["seeded_trainer"]["trainer_id"],
        rates=[
            {
                "location_id": fixture["location"]["location_id"],
                "training_type_id": fixture["training_types"]["personal"]["training_type_id"],
                "percent": Decimal(expected["seeded_personal_rate"]),
            },
        ],
    )

    stdout = io.StringIO()
    call_command(
        "assert_owner_trainer_rate_grid_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["owner"]["membership_role"] == ClubMembership.Role.OWNER
    assert evidence["new_trainer"]["location_count"] == 1
    assert evidence["new_trainer"]["rate_count"] == 2
    assert evidence["seeded_trainer"]["missing_rate_count"] == 0
    assert evidence["salary"]["rate_percent"] == expected["seeded_personal_rate"]
    assert evidence["salary"]["amount"] == expected["salary_amount"]
    assert not Trainer.objects.for_club(fixture["control_club_id"]).filter(
        first_name=expected["new_trainer_first_name"],
        last_name=expected["new_trainer_last_name"],
    ).exists()


@pytest.mark.django_db
def test_pwa_ux_ledger_smoke_e2e_prepare_and_assert_are_scoped_and_redacted(tmp_path):
    output = tmp_path / "pwa-ux-ledger-fixture.json"
    stdout = io.StringIO()

    call_command("prepare_pwa_ux_ledger_smoke_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)
    prepare_stdout = stdout.getvalue()

    assert fixture["fixture_id"].startswith("pwa-ux-ledger-e2e-")
    assert fixture["app_time_zone"] == "Asia/Yekaterinburg"
    club = Club.objects.get(id=fixture["club_id"])
    trainer_session = Schedule.objects.for_club(club).get(id=fixture["trainer_session"]["schedule_id"])
    assert club.timezone == "Europe/Moscow"
    assert trainer_session.one_time_date.isoformat() == fixture["trainer_session"]["date"]
    assert trainer_session.start_time == time(0, 0)
    assert trainer_session.end_time == time(0, 45)
    assert fixture["location"]["id"]
    assert fixture["location"]["name"]
    assert fixture["tariffs"]["personal_id"]
    assert fixture["tariffs"]["personal_training_type_id"]
    assert fixture["slots"]["trainer_payment"]["id"]
    assert fixture["slots"]["trainer_payment"]["time_label"]
    assert fixture["trainer_recovery"]["order_id"]
    assert fixture["trainer_recovery"]["subscription_id"]
    assert fixture["control"]["order_status"] == BankPaymentOrder.Status.PENDING
    assert fixture["control"]["reservation_status"] == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
    assert fixture["control"]["debt_reason"] == "control_decoy"
    assert fixture["control"]["debt_tariff_price"]
    assert fixture["control"]["task_status"] == RetentionTask.TaskStatus.OPEN
    assert "pay.example" not in prepare_stdout
    for account in ("trainer", "student", "package_student", "parent"):
        assert fixture[account]["password"] not in prepare_stdout
        assert fixture[account]["email"] not in prepare_stdout

    renewal_stdout = io.StringIO()
    call_command(
        "assert_pwa_ux_ledger_smoke_e2e",
        fixture=str(output),
        stage="renewal_reused",
        stdout=renewal_stdout,
    )
    renewal_evidence = json.loads(renewal_stdout.getvalue())
    assert renewal_evidence["ok"] is True
    assert renewal_evidence["order_id"] == fixture["renewal"]["order_id"]

    trainer_recovery_stdout = io.StringIO()
    call_command(
        "assert_pwa_ux_ledger_smoke_e2e",
        fixture=str(output),
        stage="trainer_direct_order_pending",
        stdout=trainer_recovery_stdout,
    )
    trainer_recovery_evidence = json.loads(trainer_recovery_stdout.getvalue())
    assert trainer_recovery_evidence["ok"] is True
    assert trainer_recovery_evidence["order_id"] == fixture["trainer_recovery"]["order_id"]

    from apps.billing.services import cancel_bank_payment_order

    cancelled = cancel_bank_payment_order(
        club_id=fixture["club_id"],
        order_id=fixture["trainer_recovery"]["order_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        allowed_sources={BankPaymentOrder.Source.TRAINER},
    )
    retried = cancel_bank_payment_order(
        club_id=fixture["club_id"],
        order_id=fixture["trainer_recovery"]["order_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        allowed_sources={BankPaymentOrder.Source.TRAINER},
    )
    assert cancelled.id == retried.id == fixture["trainer_recovery"]["order_id"]
    cancelled_stdout = io.StringIO()
    call_command(
        "assert_pwa_ux_ledger_smoke_e2e",
        fixture=str(output),
        stage="trainer_direct_order_cancelled",
        stdout=cancelled_stdout,
    )
    assert json.loads(cancelled_stdout.getvalue())["ok"] is True

    for stage in ("empty_states", "trainer_roster_readonly"):
        stdout = io.StringIO()
        call_command(
            "assert_pwa_ux_ledger_smoke_e2e",
            fixture=str(output),
            stage=stage,
            stdout=stdout,
        )
        evidence = json.loads(stdout.getvalue())
        assert evidence["ok"] is True
        assert evidence["stage"] == stage
        assert evidence["control"]["order_status"] == fixture["control"]["order_status"]
        assert evidence["control"]["reservation_status"] == fixture["control"]["reservation_status"]
        assert "pay.example" not in stdout.getvalue()
        for account in ("trainer", "student", "package_student", "parent"):
            assert fixture[account]["password"] not in stdout.getvalue()
            assert fixture[account]["email"] not in stdout.getvalue()


@pytest.mark.django_db
def test_pwa_ux_ledger_smoke_e2e_assert_rejects_control_club_mutation(tmp_path):
    output = tmp_path / "pwa-ux-ledger-fixture.json"
    call_command("prepare_pwa_ux_ledger_smoke_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    order = BankPaymentOrder.objects.for_club(int(fixture["control_club_id"])).get(
        id=int(fixture["control"]["order_id"])
    )
    order.status = BankPaymentOrder.Status.CANCELLED
    order.save(update_fields=["status"])

    with pytest.raises(CommandError, match="control bank payment order changed"):
        call_command(
            "assert_pwa_ux_ledger_smoke_e2e",
            fixture=str(output),
            stage="empty_states",
            stdout=io.StringIO(),
        )

    order.status = fixture["control"]["order_status"]
    order.save(update_fields=["status"])
    debt = Debt.objects.for_club(int(fixture["control_club_id"])).get(
        id=int(fixture["control"]["debt_id"])
    )
    debt.reason = "mutated_control_decoy"
    debt.save(update_fields=["reason"])

    with pytest.raises(CommandError, match="control debt reason changed"):
        call_command(
            "assert_pwa_ux_ledger_smoke_e2e",
            fixture=str(output),
            stage="empty_states",
            stdout=io.StringIO(),
        )


@pytest.mark.django_db
def test_dashboard_access_subscriptions_e2e_prepare_and_assert_validates_freeze_inbox_and_role_denials(
    tmp_path,
):
    output = tmp_path / "dashboard-access-subscriptions-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_dashboard_access_subscriptions_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("dashboard-access-subscriptions-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["admin"]["email"]
    assert fixture["admin"]["password"]
    assert fixture["admin"]["role"] == ClubMembership.Role.ADMIN
    assert fixture["subscription_id"]
    assert fixture["sale_student"]["student_id"]
    assert fixture["pending_freeze_id"]
    assert fixture["expected"]["pending_freeze_count"] == 1
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["admin"]["password"] not in stdout.getvalue()

    club = Club.objects.get(id=fixture["club_id"])
    admin_membership = ClubMembership.objects.get(user_id=fixture["admin"]["user_id"], club=club)
    assert admin_membership.role == ClubMembership.Role.ADMIN

    subscription = Subscription.objects.for_club(club).get(id=fixture["subscription_id"])
    pending_freeze = SubscriptionFreeze.objects.for_club(club).get(id=fixture["pending_freeze_id"])

    assert subscription.status == Subscription.Status.ACTIVE
    assert pending_freeze.status == SubscriptionFreeze.FreezeStatus.PENDING
    assert pending_freeze.subscription_id == subscription.id

    for role_name in ("trainer", "student", "parent"):
        role_fixture = fixture["denied_users"][role_name]
        membership = ClubMembership.objects.get(user_id=role_fixture["user_id"], club=club)
        assert membership.role == role_fixture["role"]
        assert membership.role not in {
            ClubMembership.Role.OWNER,
            ClubMembership.Role.ADMIN,
        }
        assert role_fixture["password"] not in stdout.getvalue()

    from apps.billing.services import create_subscription

    direct_subscription = create_subscription(
        club_id=club.id,
        student_id=fixture["sale_student"]["student_id"],
        tariff_id=fixture["tariff_id"],
        recorded_by_id=fixture["owner"]["user_id"],
        payment_method=Payment.Method.TRANSFER,
    )
    direct_payment = Payment.objects.for_club(club).get(subscription=direct_subscription)
    assert direct_payment.payment_method == Payment.Method.TRANSFER

    stdout = io.StringIO()
    call_command(
        "assert_dashboard_access_subscriptions_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["admin"]["membership_role"] == ClubMembership.Role.ADMIN
    assert evidence["subscriptions"]["active_count"] == 2
    assert evidence["direct_sale"] == {
        "subscription_id": direct_subscription.id,
        "payment_id": direct_payment.id,
        "payment_method": Payment.Method.TRANSFER,
        "payment_status": Payment.Status.CONFIRMED,
        "recorded_by_id": fixture["owner"]["user_id"],
        "verified_by_id": fixture["owner"]["user_id"],
    }
    assert evidence["freeze_inbox"]["pending_count"] == 1
    assert evidence["denied_roles"] == {
        "trainer": "trainer",
        "student": "student",
        "parent": "parent",
    }


@pytest.mark.django_db
def test_dashboard_onboarding_e2e_prepare_and_assert_validates_completed_draft_and_tenant_boundary(
    tmp_path,
):
    output = tmp_path / "dashboard-onboarding-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_dashboard_onboarding_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("dashboard-onboarding-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()

    club = Club.objects.get(id=fixture["club_id"])
    control_club = Club.objects.get(id=fixture["control_club_id"])
    membership = ClubMembership.objects.get(user_id=fixture["owner"]["user_id"], club=club)
    control_draft = OnboardingDraft.objects.for_club(control_club).get(id=fixture["control_draft_id"])

    assert membership.role == ClubMembership.Role.OWNER
    assert not OnboardingDraft.objects.for_club(club).exists()
    assert not control_draft.is_completed
    assert control_draft.current_step == fixture["expected"]["control_current_step"]

    from apps.onboarding.services import finish_onboarding, save_step, start_onboarding

    draft = start_onboarding(club_id=club.id)
    trainer_ref = "00000000-0000-0000-0000-000000000010"
    save_step(
        club_id=club.id,
        draft_id=draft.id,
        step=2,
        data={
            "trainers": [
                {
                    "client_ref": trainer_ref,
                    "first_name": "Draft",
                    "last_name": "Coach",
                    "phone": "+79000000002",
                }
            ]
        },
    )
    save_step(
        club_id=club.id,
        draft_id=draft.id,
        step=3,
        data={
            "schedules": [
                {
                    "day": 0,
                    "start": "10:00",
                    "end": "11:00",
                    "group": fixture["expected"]["schedule_group"],
                    "trainer_ref": trainer_ref,
                    "location_id": fixture["location_id"],
                }
            ]
        },
    )
    finish_onboarding(club_id=club.id, draft_id=draft.id)
    finish_onboarding(club_id=club.id, draft_id=draft.id)

    stdout = io.StringIO()
    call_command(
        "assert_dashboard_onboarding_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["onboarding"]["completed_draft_count"] == 1
    assert evidence["onboarding"]["active_draft_count"] == 0
    assert evidence["onboarding"]["completed_current_step"] == 5
    assert evidence["control"]["is_completed"] is False
    assert evidence["control"]["current_step"] == fixture["expected"]["control_current_step"]
    assert evidence["created_objects"] == {
        "students": 0,
        "trainers": 1,
        "tariffs": 0,
        "schedules": 1,
    }
    assert evidence["assignment"]["trainer_id"] == evidence["assignment"]["schedule_trainer_id"]
    assert evidence["assignment"]["schedule_location_id"] == fixture["location_id"]


@pytest.mark.django_db
def test_dashboard_student_management_e2e_prepare_and_assert_validates_student_workflow_and_tenant_boundary(
    tmp_path,
):
    output = tmp_path / "dashboard-student-management-fixture.json"
    stdout = io.StringIO()
    call_command("prepare_dashboard_student_management_e2e", output=str(output), stdout=stdout)
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("dashboard-student-management-e2e-")
    assert fixture["owner"]["email"]
    assert fixture["owner"]["password"]
    assert fixture["owner"]["password"] not in stdout.getvalue()
    assert fixture["import_file"]["filename"].endswith(".xlsx")
    assert fixture["import_file"]["base64"]

    club = Club.objects.get(id=fixture["club_id"])
    control_club = Club.objects.get(id=fixture["control_club_id"])
    membership = ClubMembership.objects.get(user_id=fixture["owner"]["user_id"], club=club)
    control_student = Student.objects.for_club(control_club).get(id=fixture["control_student_id"])

    assert membership.role == ClubMembership.Role.OWNER
    assert not Student.objects.for_club(club).exists()
    assert control_student.phone == fixture["control_student"]["phone"]

    manual = fixture["manual_student"]
    manual_student = Student.objects.create(
        club=club,
        first_name=manual["edited_first_name"],
        last_name=manual["edited_last_name"],
        phone=manual["phone"],
        email=manual["edited_email"],
        is_child=manual["is_child"],
        source=manual["source"],
        status=manual["final_status"],
        contraindications=manual["contraindications"],
    )
    StudentNote.objects.create(
        club=club,
        student=manual_student,
        author_id=fixture["owner"]["user_id"],
        text=manual["initial_note"],
    )
    StudentNote.objects.create(
        club=club,
        student=manual_student,
        author_id=fixture["owner"]["user_id"],
        text=manual["follow_up_note"],
    )
    ParentInvite.objects.create(
        club=club,
        student=manual_student,
        expires_at=timezone.now() + timedelta(days=7),
    )

    imported = fixture["imported_student"]
    Student.objects.create(
        club=club,
        first_name=imported["first_name"],
        last_name=imported["last_name"],
        phone=imported["phone"],
        status=imported["status"],
        lead_status=imported["lead_status"],
    )

    stdout = io.StringIO()
    call_command(
        "assert_dashboard_student_management_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["fixture_id"] == fixture["fixture_id"]
    assert evidence["students"]["club_count"] == 2
    assert evidence["students"]["manual"]["phone"] == manual["phone"]
    assert evidence["students"]["manual"]["status"] == manual["final_status"]
    assert evidence["students"]["manual"]["is_child"] is True
    assert evidence["students"]["manual"]["note_count"] == 2
    assert evidence["students"]["manual"]["parent_invite_count"] == 1
    assert evidence["students"]["imported"]["phone"] == imported["phone"]
    assert evidence["students"]["imported"]["status"] == imported["status"]
    assert evidence["students"]["imported"]["lead_status"] == imported["lead_status"]
    assert evidence["control"]["student_id"] == fixture["control_student_id"]
    assert evidence["control"]["status"] == fixture["control_student"]["status"]


def test_real_stack_runner_points_frontend_to_own_backend():
    script = Path("scripts/run-real-stack-e2e.sh").read_text(encoding="utf-8")
    config = Path("frontend/playwright.real-stack.config.ts").read_text(encoding="utf-8")
    spec = Path("frontend/e2e/real-stack-kiosk.spec.ts").read_text(encoding="utf-8")

    assert 'VITE_API_URL="$BACKEND_URL" npm --prefix "$FRONTEND_DIR" run build' in script
    assert 'env VITE_API_URL="$BACKEND_URL" npm --prefix "$FRONTEND_DIR" run preview' in script
    assert 'VITE_API_URL="$BACKEND_URL" \\' in script
    assert 'PYTHON_BIN="$PYTHON_BIN" \\' in script
    assert 'REAL_STACK_E2E_TEST_MATCH="${REAL_STACK_E2E_TEST_MATCH:-}" \\' in script
    assert 'REAL_STACK_E2E_BACKEND_URL="$BACKEND_URL" \\' in script
    assert 'export DEBUG=true' in script
    assert 'export PAYMENT_PROVIDER=mock' in script
    assert 'export TOCHKA_PAYMENT_RECONCILIATION_ENABLED=false' in script
    assert 'export MOCK_PAYMENT_ORDER_CREATION_ENABLED="${MOCK_PAYMENT_ORDER_CREATION_ENABLED:-false}"' in script
    assert 'export MOCK_PAYMENT_WEBHOOKS_ENABLED="${MOCK_PAYMENT_WEBHOOKS_ENABLED:-false}"' in script
    assert 'export MOCK_PAYMENT_BASE_URL="$BACKEND_URL"' in script
    assert 'export JAGUAR_PAYMENT_RETURN_ORIGIN="$FRONTEND_URL"' in script
    assert "require_command setsid" in script
    assert 'setsid "$@" >"$logfile" 2>&1 &' in script
    assert 'kill -- "-$pid" >/dev/null 2>&1 || kill "$pid"' in script
    assert 'wait_for_port_release "Django" "$BACKEND_HOST" "$BACKEND_PORT"' in script
    assert 'wait_for_port_release "Vite preview" "$FRONTEND_HOST" "$FRONTEND_PORT"' in script
    assert 'trace: "off"' in script
    assert 'trace: "off"' in config
    assert "REAL_STACK_E2E_TEST_MATCH" in config
    assert 'FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jaguar-real-stack-e2e-fixture.XXXXXX")"' in script
    assert 'FIXTURE_FILE="$FIXTURE_DIR/fixture.json"' in script
    assert 'rm -f "$FIXTURE_FILE"' in script
    assert 'ensure_real_stack_rollout_state_e2e --fixture "$FIXTURE_FILE" --all-clubs' in script
    assert "ensure_jwt_private_key" in script
    assert 'if [[ -n "${JWT_PRIVATE_KEY:-}" ]]; then' in script
    assert 'if [[ -f "$PROJECT_DIR/jwt-key.pem" ]]; then' in script
    assert "rsa.generate_private_key(public_exponent=65537, key_size=2048)" in script
    assert 'export JWT_PRIVATE_KEY="$generated_key"' in script
    assert 'getByText("Выберите тренировку")' in spec
    assert 'getByRole("button", { name: /00:00-23:59/ })' in spec


@pytest.mark.django_db
def test_owner_batch_checkin_e2e_prepare_and_assert_proves_guard_and_first_close_metadata(tmp_path):
    output = tmp_path / "owner-batch-checkin-fixture.json"
    call_command("prepare_owner_batch_checkin_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    assert fixture["fixture_id"].startswith("owner-batch-checkin-e2e-")
    assert fixture["owner"]["user_id"]
    assert fixture["admin"]["user_id"]
    assert fixture["trainer"]["user_id"]
    assert fixture["early"]["schedule_id"]
    assert fixture["finished"]["schedule_id"]

    with pytest.raises(CommandError, match="finished batch session not found"):
        call_command(
            "assert_owner_batch_checkin_e2e",
            fixture=str(output),
            stdout=io.StringIO(),
        )

    from apps.attendance.services import batch_checkin
    from apps.common.exceptions import BusinessLogicError

    with pytest.raises(BusinessLogicError) as exc_info:
        batch_checkin(
            club_id=fixture["club_id"],
            schedule_id=fixture["early"]["schedule_id"],
            checkin_date=date.fromisoformat(fixture["early"]["date"]),
            present_student_ids=[fixture["early"]["student_id"]],
            training_type_id=fixture["training_type_id"],
            actor_user_id=fixture["owner"]["user_id"],
        )
    assert exc_info.value.code == "session_close_not_allowed_yet"

    with patch("apps.attendance.services.async_task"):
        first = batch_checkin(
            club_id=fixture["club_id"],
            schedule_id=fixture["finished"]["schedule_id"],
            checkin_date=date.fromisoformat(fixture["finished"]["date"]),
            present_student_ids=[fixture["finished"]["student_id"]],
            training_type_id=fixture["training_type_id"],
            actor_user_id=fixture["owner"]["user_id"],
            topic_tags=fixture["expected"]["owner_topic_tags"],
            notes=fixture["expected"]["owner_notes"],
        )
        session = GroupSession.objects.get(id=first["group_session_id"])
        first_closed_at = session.closed_at
        second = batch_checkin(
            club_id=fixture["club_id"],
            schedule_id=fixture["finished"]["schedule_id"],
            checkin_date=date.fromisoformat(fixture["finished"]["date"]),
            present_student_ids=[fixture["finished"]["student_id"]],
            training_type_id=fixture["training_type_id"],
            actor_user_id=fixture["admin"]["user_id"],
            topic_tags=fixture["expected"]["admin_topic_tags"],
            notes=fixture["expected"]["admin_notes"],
        )

    session.refresh_from_db()
    assert second["group_session_id"] == first["group_session_id"]
    assert second["checkins"][0]["created"] is False
    assert session.closed_at == first_closed_at
    assert session.closed_by_id == fixture["owner"]["user_id"]
    assert session.close_source == GroupSession.CloseSource.BATCH

    stdout = io.StringIO()
    call_command(
        "assert_owner_batch_checkin_e2e",
        fixture=str(output),
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["early"]["checkin_count"] == 0
    assert evidence["early"]["group_session_count"] == 0
    assert evidence["finished"]["closed_by_id"] == fixture["owner"]["user_id"]
    assert evidence["finished"]["close_source"] == GroupSession.CloseSource.BATCH
    assert evidence["finished"]["checkin_count"] == 1


@pytest.mark.django_db
def test_refund_resolution_e2e_prepare_and_assert_proves_accounting_entitlement_and_payroll(
    tmp_path,
):
    output = tmp_path / "refund-resolution-fixture.json"
    with override_settings(
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    ):
        call_command("prepare_refund_resolution_e2e", output=str(output), stdout=io.StringIO())
    fixture = _load_fixture(output)

    from apps.billing.models import PaymentRefund, PaymentRefundCase
    from apps.billing.refund_services import (
        approve_payment_refund_case,
        complete_payment_refund_payroll,
    )

    assert fixture["fixture_id"].startswith("refund-resolution-e2e-")
    assert TrainingGroupRolloutState.objects.for_club(fixture["club_id"]).get().mode == "active"
    assert fixture["target_group"]["new_writes_enabled"] is True
    assert fixture["target_group"]["manual_operational_admission_enabled"] is True
    assert fixture["full"]["refund_case_id"]
    assert fixture["partial"]["refund_case_id"]
    assert fixture["mixed"]["refund_case_id"]
    assert PaymentRefundCase.objects.for_club(fixture["club_id"]).count() == 3

    with pytest.raises(CommandError, match="full refund not posted"):
        call_command(
            "assert_refund_resolution_e2e",
            fixture=str(output),
            stdout=io.StringIO(),
        )

    full_refund = approve_payment_refund_case(
        club_id=fixture["club_id"],
        case_id=fixture["full"]["refund_case_id"],
        actor_user_id=fixture["owner"]["user_id"],
        idempotency_key=f"payment-refund-case-{fixture['full']['refund_case_id']}",
        amount=Decimal(fixture["full"]["amount"]),
        refund_kind=PaymentRefund.Kind.FULL,
        reason=fixture["expected"]["full_reason"],
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )
    partial_refund = approve_payment_refund_case(
        club_id=fixture["club_id"],
        case_id=fixture["partial"]["refund_case_id"],
        actor_user_id=fixture["owner"]["user_id"],
        idempotency_key=f"payment-refund-case-{fixture['partial']['refund_case_id']}",
        amount=Decimal(fixture["partial"]["refund_amount"]),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason=fixture["expected"]["partial_reason"],
    )
    mixed_refund = approve_payment_refund_case(
        club_id=fixture["club_id"],
        case_id=fixture["mixed"]["refund_case_id"],
        actor_user_id=fixture["owner"]["user_id"],
        idempotency_key=f"payment-refund-case-{fixture['mixed']['refund_case_id']}",
        amount=Decimal(fixture["mixed"]["amount"]),
        refund_kind=PaymentRefund.Kind.FULL,
        reason=fixture["expected"]["full_reason"],
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    assert full_refund.status == PaymentRefund.Status.COMPLETED
    assert mixed_refund.status == PaymentRefund.Status.COMPLETED
    assert partial_refund.status == PaymentRefund.Status.PAYROLL_ACTION_REQUIRED
    complete_payment_refund_payroll(
        club_id=fixture["club_id"],
        refund_id=partial_refund.id,
        actor_user_id=fixture["owner"]["user_id"],
        effective_date=date.fromisoformat(fixture["payroll"]["open_date"]),
    )

    stdout = io.StringIO()
    call_command(
        "assert_refund_resolution_e2e",
        fixture=str(output),
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["full"]["subscription_status"] == Subscription.Status.CANCELLED
    assert evidence["full"]["membership_status"] == TrainingGroupMembership.Status.CANCELLED
    assert evidence["full"]["projection_count"] == 2
    assert evidence["partial"]["subscription_status"] == Subscription.Status.ACTIVE
    assert evidence["partial"]["membership_status"] == TrainingGroupMembership.Status.ACTIVE
    assert evidence["partial"]["projection_count"] == 2
    assert evidence["partial"]["debt_remains_settled"] is True
    assert evidence["mixed"]["authority"] == TrainingGroupMembership.Authority.INDEPENDENT
    assert evidence["mixed"]["manual_enrollment_id"] == fixture["mixed_legacy"]["manual_enrollment_id"]
    assert evidence["mixed"]["manual_schedule_id"] == fixture["mixed_legacy"]["manual_schedule_id"]
    assert evidence["mixed"]["paid_enrollment_id"] == fixture["mixed"]["conversion_enrollment_id"]
    assert evidence["mixed"]["group_projection_schedule_ids"] == []
    assert evidence["mixed"]["covered_schedule_ids"] == sorted(fixture["target_group"]["schedule_ids"])
    assert evidence["mixed"]["roster_membership_schedule_ids"] == sorted(fixture["target_group"]["schedule_ids"])
    assert evidence["mixed"]["conversion_group_membership_id"] is None
    assert evidence["payroll"]["refund_adjustment_count"] == 1
    assert evidence["pnl"] == {
        "gross": "15000.00",
        "refunded": "11000.00",
        "net": "4000.00",
    }


def test_real_stack_pack_runner_lists_and_dry_runs_pack_matrix():
    script = Path("scripts/run-real-stack-e2e-pack.sh")

    list_result = subprocess.run(
        ["bash", str(script), "--list"],
        check=True,
        capture_output=True,
        text=True,
    )

    packs = list_result.stdout.strip().splitlines()
    assert packs[0] == "all"
    assert "base-kiosk" in packs
    assert "kiosk-negative" in packs
    assert "kiosk-guest-book-checkin" in packs
    assert "owner-dashboard-business" in packs
    assert "owner-batch-checkin" in packs
    assert "refund-resolution" in packs
    assert "parent-invite-feedback" in packs
    assert "push-notification-preferences" in packs
    assert "owner-notification-template-settings" in packs

    single_result = subprocess.run(
        ["bash", str(script), "kiosk-negative", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "real-stack pack: kiosk-negative" in single_result.stdout
    assert "Dry run passed" in single_result.stdout

    all_result = subprocess.run(
        ["bash", str(script), "all", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    for pack in packs[1:]:
        assert f"real-stack pack: {pack}" in all_result.stdout

    pack_script = script.read_text(encoding="utf-8")
    assert 'local mock_online_payment_enabled="false"' in pack_script
    assert '"bank-payment-link" || "$pack" == "training-group-containment"' in pack_script
    assert '"refund-resolution" || "$pack" == "pwa-ux-ledger-smoke"' in pack_script
    assert '"kiosk-negative" || "$pack" == "owner-dashboard-business"' in pack_script
    assert 'MOCK_PAYMENT_ORDER_CREATION_ENABLED="$mock_online_payment_enabled"' in pack_script
    assert 'MOCK_PAYMENT_WEBHOOKS_ENABLED="$mock_online_payment_enabled"' in pack_script


def test_real_stack_pack_runner_refuses_real_run_without_isolated_database_url():
    result = subprocess.run(
        ["bash", "scripts/run-real-stack-e2e-pack.sh", "payment-confirm"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "REAL_STACK_E2E_DATABASE_URL": ""},
    )

    assert result.returncode != 0
    assert "REAL_STACK_E2E_DATABASE_URL is required" in result.stdout


@pytest.mark.parametrize(
    ("real_stack_database_url", "ordinary_database_url", "expected_error"),
    [
        (
            "postgresql://runner:runner-secret@example.invalid:5432/real_stack_e2e",
            "",
            "must use a local PostgreSQL host",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/crm",
            "",
            "database name must identify an isolated test or e2e database",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e",
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e",
            "must differ from the ordinary DATABASE_URL",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e?dbname=crm",
            "",
            "must not contain query parameters",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e",
            "postgres://other-user:other-secret@localhost:5432/real_stack_e2e",
            "must differ from the ordinary DATABASE_URL",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e",
            "postgresql://other-user:other-secret@127.0.0.1:5432/ordinary?dbname=real_stack_e2e",
            "must differ from the ordinary DATABASE_URL",
        ),
        (
            "postgresql://runner:runner-secret@127.0.0.1:5432/real_stack_e2e",
            (
                "postgres://other-user:other-secret@localhost:5432/ordinary"
                "?dbname=real_stack_e2e&host=remote.invalid&port=6543"
            ),
            "must differ from the ordinary DATABASE_URL",
        ),
    ],
)
def test_real_stack_runner_rejects_nonisolated_database_urls_before_migrations(
    real_stack_database_url,
    ordinary_database_url,
    expected_error,
):
    result = subprocess.run(
        ["bash", "scripts/run-real-stack-e2e-pack.sh", "payment-confirm"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REAL_STACK_E2E_DATABASE_URL": real_stack_database_url,
            "DATABASE_URL": ordinary_database_url,
        },
    )

    assert result.returncode != 0
    assert expected_error in result.stdout
    assert "runner-secret" not in result.stdout
    assert "runner-secret" not in result.stderr
    assert "other-secret" not in result.stdout
    assert "other-secret" not in result.stderr
    assert "Running migrations" not in result.stdout


def test_root_package_exposes_real_stack_pack_runner():
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["e2e:real-stack:pack"] == "bash scripts/run-real-stack-e2e-pack.sh"


def test_frontend_playwright_scripts_use_writable_tmpdir():
    package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["e2e"].startswith("env TMPDIR=/tmp ")
    assert package["scripts"]["e2e:devices"].startswith("env TMPDIR=/tmp ")
    assert package["scripts"]["e2e:real-stack"].startswith("env TMPDIR=/tmp ")


def test_real_stack_pack_runner_matrix_matches_local_release_and_specs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path.cwd() / "scripts"))
    from release_checks import commands
    from release_contract import STEP_NAMES, list_real_stack_packs

    script = Path("scripts/run-real-stack-e2e-pack.sh")
    packs = ["all", *list_real_stack_packs(Path.cwd())]
    assert "e2e-real-stack" in STEP_NAMES
    assert commands()["e2e-real-stack"] == ["npm", "run", "e2e:real-stack:pack", "--", "all"]

    for pack in packs:
        if pack == "all":
            continue
        dry_run = subprocess.run(
            ["bash", str(script), pack, "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert f"real-stack pack: {pack}" in dry_run.stdout
        assert "Dry run passed" in dry_run.stdout

    describe_result = subprocess.run(
        ["bash", str(script), "--describe"],
        check=True,
        capture_output=True,
        text=True,
    )
    specs = {path.name for path in Path("frontend/e2e").glob("real-stack-*.spec.ts")}
    command_dir = Path("apps/common/management/commands")
    prepare_commands = {path.stem for path in command_dir.glob("prepare*_e2e.py")}
    assert_commands = {path.stem for path in command_dir.glob("assert*_e2e.py")}
    described_packs = []
    for line in describe_result.stdout.strip().splitlines():
        pack, fixture_command, spec = line.split("\t")
        described_packs.append(pack)
        assert pack in packs
        assert fixture_command in prepare_commands
        assert spec in specs
        if fixture_command == "prepare_real_stack_e2e":
            expected_assert_command = "assert_real_stack_e2e"
        else:
            expected_assert_command = fixture_command.replace("prepare_", "assert_", 1)
        assert expected_assert_command in assert_commands
        spec_text = Path("frontend/e2e", spec).read_text(encoding="utf-8")
        assert expected_assert_command in spec_text
        assert "REAL_STACK_E2E_ASSERT_COMMAND" in spec_text
    assert described_packs == packs[1:]
