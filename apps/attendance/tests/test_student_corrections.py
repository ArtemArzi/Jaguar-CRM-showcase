from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import cancel_checkin
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import SubscriptionRenewalEvent
from apps.billing.tests.factories import PaymentFactory, SubscriptionComponentFactory, SubscriptionFactory
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def recorded_visit(club):
    actor = UserFactory()
    student = StudentFactory(club=club)
    sub = SubscriptionFactory(
        club=club,
        student=student,
        tariff__club=club,
        tariff__training_type__club=club,
        trainings_left=7,
        trainings_used=1,
    )
    component = SubscriptionComponentFactory(
        club=club, subscription=sub, credits_total=8, credits_left=7, credits_used=1
    )
    day = timezone.now().date() - timedelta(days=2)
    schedule = ScheduleFactory(
        club=club, training_type=component.training_type, one_time_date=day, day_of_week=day.weekday()
    )
    checkin = CheckinFactory(
        club=club,
        student=student,
        schedule=schedule,
        training_type=component.training_type,
        location=schedule.location,
        trainer=schedule.trainer,
        date=day,
        subscription=sub,
        subscription_component=component,
    )
    PaymentFactory(
        club=club,
        tariff=sub.tariff,
        student=student,
        subscription=sub,
        status="confirmed",
        verified_at=timezone.now() - timedelta(days=7),
    )
    return actor, sub, component, checkin


def cancel(recorded_visit):
    actor, sub, _, checkin = recorded_visit
    with patch("apps.attendance.services.async_task"):
        cancel_checkin(club_id=sub.club_id, checkin_id=checkin.id, cancelled_by_user_id=actor.id, user_role="owner")


@pytest.mark.parametrize(
    "status,expired,expected",
    [
        ("expired", True, "expired"),
        ("expired", False, "active"),
        ("frozen", False, "frozen"),
    ],
)
def test_common_cancel_restores_leaf_counters_without_reviving_expiry_or_freeze(
    recorded_visit, status, expired, expected
):
    _, sub, component, checkin = recorded_visit
    sub.status = status
    if expired:
        sub.expires_at = timezone.now() - timedelta(days=1)
    sub.save()
    cancel(recorded_visit)
    sub.refresh_from_db()
    component.refresh_from_db()
    checkin.refresh_from_db()
    assert sub.status == expected
    assert (sub.trainings_left, sub.trainings_used) == (8, 0)
    assert (component.credits_total, component.credits_left, component.credits_used) == (8, 8, 0)
    assert checkin.cancelled_at is not None
    cancel(recorded_visit)
    component.refresh_from_db()
    assert component.credits_left == 8


@pytest.mark.parametrize("finalized", [False, True])
def test_common_cancel_does_not_return_already_carried_credit(recorded_visit, finalized):
    _, sub, component, checkin = recorded_visit
    child = SubscriptionFactory(
        club=sub.club,
        student=sub.student,
        tariff=sub.tariff,
        renewed_from=sub,
        status="active" if finalized else "pending",
    )
    if finalized:
        payment = PaymentFactory(
            club=sub.club, student=sub.student, tariff=sub.tariff, subscription=child, status="confirmed"
        )
        SubscriptionRenewalEvent.objects.create(
            club=sub.club,
            renewed_from=sub,
            renewed_to=child,
            payment=payment,
            finalized_at=timezone.now(),
            carry_snapshot={"legacy_finite_credits": 7},
        )
        sub.status = "expired"
        sub.save()
    with pytest.raises(BusinessLogicError) as error:
        cancel(recorded_visit)
    assert error.value.code == (
        "checkin_cancellation_non_leaf" if finalized else "checkin_cancellation_renewal_pending"
    )
    component.refresh_from_db()
    checkin.refresh_from_db()
    assert (component.credits_left, component.credits_used) == (7, 1)
    assert checkin.cancelled_at is None and Checkin.objects.filter(id=checkin.id, deleted_at__isnull=True).exists()


def test_common_cancel_blocks_revoked_source(recorded_visit):
    _, sub, component, checkin = recorded_visit
    sub.status = "cancelled"
    sub.save()
    with pytest.raises(BusinessLogicError) as error:
        cancel(recorded_visit)
    assert error.value.code == "checkin_cancellation_subscription_revoked"
    component.refresh_from_db()
    checkin.refresh_from_db()
    assert component.credits_left == 7 and checkin.cancelled_at is None


@pytest.mark.parametrize("policy", ["silent_correction", "dated_correction"])
def test_historical_parent_policy_and_sent_facts_survive_task_replay(recorded_visit, policy):
    from apps.attendance.tasks import log_parent_event, reverse_parent_checkin_push

    _, sub, _, checkin = recorded_visit
    student = sub.student
    student.is_child = True
    student.parent_user = UserFactory()
    student.save()
    checkin.notification_policy = policy
    checkin.save()
    with patch("apps.notifications.services.send_parent_notification", return_value=True) as send:
        log_parent_event(checkin.id, sub.club_id)
        log_parent_event(checkin.id, sub.club_id)
        if policy == "silent_correction":
            send.assert_not_called()
        else:
            assert send.call_count == 1
            assert checkin.date.strftime("%d.%m.%Y") in send.call_args.kwargs["fallback_body"]
            assert send.call_args.kwargs["use_fallback_content"] is True
        checkin.refresh_from_db()
        assert bool(checkin.parent_notified_at) == (policy == "dated_correction")
        send.reset_mock()
        reverse_parent_checkin_push(checkin.id, sub.club_id)
        reverse_parent_checkin_push(checkin.id, sub.club_id)
        if policy == "silent_correction":
            send.assert_not_called()
        else:
            assert send.call_count == 1
            assert checkin.date.strftime("%d.%m.%Y") in send.call_args.kwargs["fallback_body"]
        checkin.refresh_from_db()
        assert bool(checkin.parent_cancellation_notified_at) == (policy == "dated_correction")


def test_historical_factual_copy_honors_disabled_template(recorded_visit):
    from types import SimpleNamespace

    from apps.notifications.services import send_parent_notification

    _, sub, _, checkin = recorded_visit
    student = sub.student
    student.is_child = True
    student.parent_user = UserFactory()
    student.save()
    template = SimpleNamespace(is_enabled=False, title_template="Сейчас", body_template="Только что пришёл")
    with patch("apps.notifications.services.send_push_to_user") as push:
        sent = send_parent_notification(
            club=sub.club,
            student=student,
            notification_type="parent_checkin",
            context={},
            fallback_title="Уточнение",
            fallback_body="Посещение 01.09.2026",
            use_fallback_content=True,
            template=template,
        )
        assert not sent
        push.assert_not_called()
        template.is_enabled = True
        assert send_parent_notification(
            club=sub.club,
            student=student,
            notification_type="parent_checkin",
            context={},
            fallback_title="Уточнение",
            fallback_body="Посещение 01.09.2026",
            use_fallback_content=True,
            template=template,
        )
        assert push.call_args.kwargs["body"] == "Посещение 01.09.2026"


def test_historical_visit_neither_closes_nor_reopens_current_retention_work(recorded_visit):
    from apps.retention.tasks import auto_close_retention_on_checkin, reverse_auto_close_retention

    _, sub, _, checkin = recorded_visit
    sub.student.status = "at_risk"
    sub.student.save()
    with patch("apps.retention.services.auto_close_retention_tasks") as close:
        auto_close_retention_on_checkin(checkin.id, sub.club_id)
        close.assert_not_called()
    with patch("apps.retention.services.reopen_retention_tasks_auto_closed") as reopen:
        reverse_auto_close_retention(checkin.id, sub.club_id)
        reopen.assert_not_called()
    sub.student.refresh_from_db()
    assert sub.student.status == "at_risk"


@pytest.fixture
def attendance_command(recorded_visit, settings):
    from apps.attendance.models import ScheduleEnrollment
    from apps.billing.models import Subscription
    from apps.clubs.models import ClubMembership
    from apps.trainers.tests.factories import TrainerRateFactory

    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    actor, sub, component, previous = recorded_visit
    sub.student.status = "active"
    sub.student.save(update_fields=["status"])
    TrainerRateFactory(club=sub.club, trainer=previous.trainer, location=previous.location,
                       training_type=previous.training_type)
    ClubMembership.objects.create(club=sub.club, user=actor, role="owner", is_active=True)
    ScheduleEnrollment.objects.create(
        club=sub.club, schedule=previous.schedule, student=sub.student,
        starts_on=previous.date - timedelta(days=30), status="active", created_from="manual",
    )
    Subscription.objects.filter(id=sub.id).update(activated_at=timezone.now() - timedelta(days=30))
    previous.soft_delete()
    return dict(club_id=sub.club_id, actor_user_id=actor.id, student_id=sub.student_id,
                schedule_id=previous.schedule_id, checkin_date=previous.date,
                subscription_id=sub.id, component_id=component.id)


def test_single_attendance_exact_expired_source_replay_and_scalar_effect(attendance_command, settings):
    from apps.attendance.models import GroupSession, StudentAttendanceCorrection
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import Subscription, SubscriptionComponent

    Subscription.objects.filter(id=attendance_command["subscription_id"]).update(
        status="expired", expires_at=timezone.now() - timedelta(days=1),
    )
    preview = preview_student_attendance(**attendance_command)
    command = dict(**attendance_command, expected_fingerprint=preview["fingerprint"],
                   command_key="one-visit", reason="Проверено по журналу")
    receipt = record_student_attendance(**command)
    component = SubscriptionComponent.objects.get(id=attendance_command["component_id"])
    assert (component.credits_left, component.credits_used) == (6, 2)
    assert Subscription.objects.get(id=component.subscription_id).status == "expired"
    assert not GroupSession.objects.exists()
    assert receipt.checkin.notification_policy == "silent_correction"
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    assert record_student_attendance(**command).id == receipt.id
    assert StudentAttendanceCorrection.objects.count() == 1


def test_single_attendance_stale_preview_does_not_deduct(attendance_command):
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import SubscriptionComponent

    preview = preview_student_attendance(**attendance_command)
    SubscriptionComponent.objects.filter(id=attendance_command["component_id"]).update(credits_left=6, credits_used=2)
    with pytest.raises(BusinessLogicError) as error:
        record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                  command_key="stale", reason="Проверено")
    assert error.value.code == "attendance_preview_stale"
    assert SubscriptionComponent.objects.get(id=attendance_command["component_id"]).credits_left == 6


def test_single_cancel_receipt_restores_exact_source_and_drains_disabled_gate(attendance_command, settings):
    from apps.attendance.models import StudentAttendanceCorrection
    from apps.attendance.services.student_corrections import (
        cancel_student_attendance,
        preview_student_attendance,
        record_student_attendance,
    )
    from apps.billing.models import SubscriptionComponent

    preview = preview_student_attendance(**attendance_command)
    receipt = record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                       command_key="record", reason="По журналу")
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    command = dict(club_id=receipt.club_id, actor_user_id=receipt.actor_id,
                   student_id=attendance_command["student_id"], checkin_id=receipt.checkin_id,
                   command_key="cancel", reason="Отмечен другой ученик")
    cancelled = cancel_student_attendance(**command)
    assert cancelled.action == "cancel" and cancelled.reason == command["reason"]
    assert cancel_student_attendance(**command).id == cancelled.id
    assert StudentAttendanceCorrection.objects.count() == 2
    component = SubscriptionComponent.objects.get(id=attendance_command["component_id"])
    assert (component.credits_left, component.credits_used) == (7, 1)


@pytest.mark.parametrize("change,code", [
    ("future", "attendance_future_date"),
    ("activation", "attendance_entitlement_needs_review"),
    ("debt", "attendance_debt_confirmation_required"),
    ("exhausted", "attendance_entitlement_exhausted"),
])
def test_single_attendance_rejects_unproven_right(attendance_command, change, code):
    from apps.attendance.services.student_corrections import preview_student_attendance
    from apps.billing.models import Subscription, SubscriptionComponent

    if change == "future":
        attendance_command["checkin_date"] = timezone.now().date() + timedelta(days=2)
    elif change == "activation":
        Subscription.objects.filter(id=attendance_command["subscription_id"]).update(activated_at=timezone.now())
    elif change == "debt":
        attendance_command.update(subscription_id=None, component_id=None)
    elif change == "exhausted":
        SubscriptionComponent.objects.filter(id=attendance_command["component_id"]).update(credits_left=0)
    with pytest.raises(BusinessLogicError) as error:
        preview_student_attendance(**attendance_command)
    assert error.value.code == code


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("writer", ["replay", "freeze"])
def test_postgres_single_attendance_waits_then_revalidates(attendance_command, monkeypatch, writer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from django.db import close_old_connections, connection, transaction

    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import Subscription, SubscriptionComponent
    from apps.billing.service_modules.freezes import freeze_subscription
    from apps.clubs.models import Club, ClubSettings

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL row locks")
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    if writer == "freeze":
        ClubSettings.objects.update_or_create(club_id=attendance_command["club_id"], defaults={"freeze_enabled": True})
    preview = preview_student_attendance(**attendance_command)
    command = dict(**attendance_command, command_key="concurrent", reason="По журналу",
                   expected_fingerprint=preview["fingerprint"])
    locked, release, waiting = Event(), Event(), Event()
    pids = {}

    def run(first):
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[first] = cursor.fetchone()[0]
                if first:
                    if writer == "freeze":
                        Subscription.objects.for_club(command["club_id"]).select_for_update().get(
                            id=command["subscription_id"],
                        )
                    else:
                        Club.objects.select_for_update(no_key=True).get(id=command["club_id"])
                    locked.set()
                    assert release.wait(15)
                else:
                    waiting.set()
                try:
                    if first and writer == "freeze":
                        return freeze_subscription(
                            club_id=command["club_id"], subscription_id=command["subscription_id"],
                            days=1, reason="vacation", frozen_by_id=command["actor_user_id"],
                        ).id
                    return record_student_attendance(**command).id
                except BusinessLogicError as error:
                    return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, True)
        assert locked.wait(10)
        second = executor.submit(run, False)
        assert waiting.wait(10)
        try:
            observed = False
            deadline = monotonic() + 10
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [pids[True], pids[False]])
                    observed = cursor.fetchone()[0]
                if observed:
                    break
                sleep(0.02)
            assert observed, "Attendance did not wait for the competing writer"
        finally:
            release.set()
        winner, loser = first.result(timeout=15), second.result(timeout=15)
    assert isinstance(winner, int), winner
    component = SubscriptionComponent.objects.get(id=command["component_id"])
    if writer == "replay":
        assert winner == loser
        assert (component.credits_left, component.credits_used) == (6, 2)
    else:
        assert loser == "attendance_entitlement_unavailable"
        assert (component.credits_left, component.credits_used) == (7, 1)


def test_single_correction_preserves_other_attendees_and_closed_session(attendance_command):
    from apps.attendance.models import GroupSession, Schedule
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.attendance.tasks import update_group_analytics

    schedule = Schedule.objects.get(id=attendance_command["schedule_id"])
    schedule.training_type.kind = "group"
    schedule.training_type.save()
    other = CheckinFactory(club=schedule.club, schedule=schedule, date=attendance_command["checkin_date"])
    session = GroupSession.objects.create(club=schedule.club, schedule=schedule, date=other.date,
                                          trainer=schedule.trainer, attendee_count=1, closed_at=timezone.now(),
                                          notes="Сохранить запись тренера", close_source="trainer_review")
    preview = preview_student_attendance(**attendance_command)
    receipt = record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                       command_key="closed", reason="По журналу")
    update_group_analytics(receipt.checkin_id, club_id=receipt.club_id)
    session.refresh_from_db()
    other.refresh_from_db()
    assert session.attendee_count == 2
    assert session.notes == "Сохранить запись тренера" and session.close_source == "trainer_review"
    assert other.deleted_at is None and other.cancelled_at is None


def test_explicit_debt_branch_is_audited(attendance_command):
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import Debt, Subscription

    Subscription.objects.filter(id=attendance_command["subscription_id"]).update(status="expired")
    attendance_command.update(subscription_id=None, component_id=None, allow_debt=True)
    preview = preview_student_attendance(**attendance_command)
    receipt = record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                       command_key="debt", reason="Согласовано занятие в долг")
    assert receipt.checkin.is_debt and receipt.checkin.subscription_id is None
    assert Debt.objects.filter(checkin_id=receipt.checkin_id, reason="no_subscription").count() == 1


def test_single_attendance_owner_authorization_is_server_resolved(attendance_command):
    from apps.attendance.services.student_corrections import preview_student_attendance

    attendance_command["actor_user_id"] = UserFactory().id
    with pytest.raises(BusinessLogicError) as error:
        preview_student_attendance(**attendance_command)
    assert error.value.code == "actor_not_authorized"


def test_single_attendance_respects_closed_payroll(attendance_command):
    from apps.attendance.services.student_corrections import preview_student_attendance
    from apps.trainers.services import close_trainer_payroll_period

    close_trainer_payroll_period(club_id=attendance_command["club_id"],
                                period_start=attendance_command["checkin_date"],
                                period_end=attendance_command["checkin_date"],
                                actor_user_id=attendance_command["actor_user_id"], reason="Сверено")
    with pytest.raises(BusinessLogicError) as error:
        preview_student_attendance(**attendance_command)
    assert error.value.code == "payroll_period_closed"


def test_single_personal_salary_keeps_original_unit_basis(attendance_command):
    from decimal import Decimal

    from apps.attendance.models import CheckinCascadeEvent
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import SubscriptionComponent

    component = SubscriptionComponent.objects.get(id=attendance_command["component_id"])
    component.paid_amount_basis_snapshot = Decimal("8000")
    component.unit_amount_basis_snapshot = Decimal("1000")
    component.trainer_payout_policy_snapshot = "on_checkin"
    component.save()
    preview = preview_student_attendance(**attendance_command)
    receipt = record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                       command_key="personal", reason="По журналу")
    event = CheckinCascadeEvent.objects.get(checkin_id=receipt.checkin_id, effect="salary")
    assert event.expected
    assert Decimal(event.payload["subscription_price_snapshot"]) == Decimal("1000")
    assert Decimal(event.payload["component_paid_amount_basis_snapshot"]) == Decimal("8000")
    component.refresh_from_db()
    assert component.unit_amount_basis_snapshot == Decimal("1000")


def test_dated_delivery_completes_cancellation_that_ran_during_send(recorded_visit):
    from apps.attendance.tasks import log_parent_event, reverse_parent_checkin_push

    _, sub, _, checkin = recorded_visit
    sub.student.is_child = True
    sub.student.parent_user = UserFactory()
    sub.student.save()
    checkin.notification_policy = "dated_correction"
    checkin.save()
    sent_types = []

    def send(**kwargs):
        sent_types.append(kwargs["notification_type"])
        if kwargs["notification_type"] == "parent_checkin":
            Checkin.objects.filter(id=checkin.id).update(cancelled_at=timezone.now(), deleted_at=timezone.now())
            reverse_parent_checkin_push(checkin.id, club_id=sub.club_id)
        return True

    with patch("apps.notifications.services.send_parent_notification", side_effect=send):
        log_parent_event(checkin.id, club_id=sub.club_id)
        reverse_parent_checkin_push(checkin.id, club_id=sub.club_id)
    checkin.refresh_from_db()
    assert sent_types == ["parent_checkin", "parent_checkin_cancelled"]
    assert checkin.parent_notified_at and checkin.parent_cancellation_notified_at


@pytest.mark.parametrize("opening", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_s4_expiry_correction_covers_today_without_rewriting_past(attendance_command, opening, historical):
    from apps.attendance.models import GroupSession, Schedule
    from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
    from apps.billing.models import OpeningEntitlementSnapshot, Subscription
    from apps.billing.service_modules.subscription_corrections import (
        correct_subscription,
        preview_subscription_correction,
    )
    from apps.clubs.timezones import club_localdate

    sub = Subscription.objects.get(id=attendance_command["subscription_id"])
    today = club_localdate(sub.club)
    sub.status = "expired"
    sub.expires_at = timezone.now() - timedelta(days=3)
    sub.save()
    if opening:
        sub = SubscriptionFactory(
            club=sub.club, student=sub.student, tariff=sub.tariff, status="expired",
            expires_at=sub.expires_at, trainings_left=7, trainings_used=1,
        )
        component = SubscriptionComponentFactory(
            club=sub.club, subscription=sub, credits_total=8, credits_left=7, credits_used=1,
        )
        attendance_command.update(subscription_id=sub.id, component_id=component.id)
        payment = PaymentFactory(
            club=sub.club, subscription=sub, student=sub.student, tariff=sub.tariff,
            origin="opening", status="confirmed", opening_effective_on=today - timedelta(days=30),
            opening_source_namespace="extension", opening_source_key="paid",
            opening_provenance={"reviewed": True}, verified_at=timezone.now(),
        )
        OpeningEntitlementSnapshot.objects.create(
            club=sub.club, subscription=sub, component_id=attendance_command["component_id"], payment=payment,
            actor_id=attendance_command["actor_user_id"], source_namespace="extension", student_source_key="student",
            entitlement_source_key="package", payload_fingerprint="a" * 64, channel="test",
            started_on=today - timedelta(days=30), expires_on=today - timedelta(days=3),
            covered_through=timezone.now() - timedelta(days=5),
            operational_cutover=timezone.now() - timedelta(days=4),
            original_total=8, original_used=1, original_left=7, history_only=False,
            reviewed_input={"reviewed": True}, student_transition={"from": "active", "to": "active"},
        )
    correction_args = dict(club_id=sub.club_id, actor_user_id=attendance_command["actor_user_id"],
                           subscription_id=sub.id, component_id=attendance_command["component_id"],
                           desired_expires_on=today + timedelta(days=7))
    correction_preview = preview_subscription_correction(**correction_args)
    correct_subscription(**correction_args, command_key="extend", reason="Сверен срок",
                         expected_fingerprint=correction_preview.fingerprint, channel="admin")
    sub.refresh_from_db()
    assert sub.status == "active"
    if not historical:
        schedule = Schedule.objects.get(id=attendance_command["schedule_id"])
        schedule.one_time_date = today
        schedule.day_of_week = today.weekday()
        schedule.save()
        attendance_command["checkin_date"] = today
        GroupSession.objects.create(club=sub.club, schedule=schedule, date=today,
                                    trainer=schedule.trainer, closed_at=timezone.now())
    if historical:
        with pytest.raises(BusinessLogicError) as error:
            preview_student_attendance(**attendance_command)
        assert error.value.code == "attendance_entitlement_needs_review"
    else:
        preview = preview_student_attendance(**attendance_command)
        receipt = record_student_attendance(**attendance_command, expected_fingerprint=preview["fingerprint"],
                                           command_key="after-extension", reason="По журналу")
        assert receipt.checkin.subscription_id == sub.id


@pytest.mark.parametrize("late_fact", ["activation", "payment"])
def test_historical_visit_needs_right_before_occurrence_not_just_same_day(attendance_command, late_fact):
    from datetime import datetime

    from apps.attendance.models import Schedule
    from apps.attendance.services.student_corrections import preview_student_attendance
    from apps.billing.models import Payment, Subscription
    from apps.clubs.timezones import club_zoneinfo

    schedule = Schedule.objects.get(id=attendance_command["schedule_id"])
    late = timezone.make_aware(datetime.combine(attendance_command["checkin_date"], schedule.start_time),
                               club_zoneinfo(schedule.club)) + timedelta(minutes=1)
    if late_fact == "activation":
        Subscription.objects.filter(id=attendance_command["subscription_id"]).update(activated_at=late)
    else:
        Payment.objects.filter(subscription_id=attendance_command["subscription_id"]).update(verified_at=late)
    with pytest.raises(BusinessLogicError) as error:
        preview_student_attendance(**attendance_command)
    assert error.value.code == "attendance_entitlement_needs_review"
