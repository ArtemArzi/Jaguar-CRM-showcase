from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from apps.attendance.models import Checkin, ScheduleEnrollment
from apps.attendance.services import create_checkin
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Debt, OpeningEntitlementSnapshot, Payment, Subscription, Tariff
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(club):
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone"])
    student = StudentFactory(club=club, status="active")
    tariff = TariffFactory(club=club, training_type__club=club, training_type__kind="group")
    subscription = SubscriptionFactory(
        club=club, student=student, tariff=tariff, trainings_left=7, trainings_used=5,
        expires_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    component = SubscriptionComponentFactory(
        club=club, subscription=subscription, credits_total=12, credits_left=7, credits_used=5,
        trainer_payout_policy_snapshot=Tariff.PayoutPolicy.NONE,
        paid_amount_basis_snapshot=Decimal("6500"),
    )
    payment = PaymentFactory(
        club=club, student=student, tariff=tariff, subscription=subscription,
        origin=Payment.Origin.OPENING, status=Payment.Status.CONFIRMED,
        opening_effective_on=date(2026, 9, 1), opening_source_namespace="test",
        opening_source_key="pay", opening_provenance={"reviewed": True},
        verified_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    snapshot = OpeningEntitlementSnapshot.objects.create(
        club=club, subscription=subscription, component=component, payment=payment,
        actor=payment.recorded_by, source_namespace="test", student_source_key="student",
        entitlement_source_key="package", payload_fingerprint="a" * 64, channel="test",
        started_on=date(2026, 9, 1), expires_on=date(2026, 9, 30),
        covered_through=datetime(2026, 9, 7, 5, tzinfo=UTC),
        operational_cutover=datetime(2026, 9, 7, 7, tzinfo=UTC),
        original_total=12, original_used=5, original_left=7, history_only=False,
        reviewed_input={"reviewed": True}, student_transition={"from": "active", "to": "active"},
    )
    schedule = ScheduleFactory(
        club=club, training_type=tariff.training_type, start_time=time(10), end_time=time(11),
    )
    ScheduleEnrollment.objects.create(
        club=club, student=student, schedule=schedule, starts_on=date(2026, 9, 1),
    )
    return snapshot, schedule


@pytest.mark.parametrize("hour", [9, 10])
@pytest.mark.parametrize("status", [Subscription.Status.ACTIVE, Subscription.Status.EXPIRED])
@pytest.mark.parametrize("channel", [Checkin.Source.MANUAL, Checkin.Source.KIOSK, Checkin.Source.BATCH])
def test_covered_visit_never_consumes_or_creates_debt(club, source, hour, status, channel):
    snapshot, schedule = source
    schedule.start_time = time(hour)
    schedule.end_time = time(hour + 1)
    schedule.save()
    subscription = snapshot.subscription
    subscription.status = status
    subscription.save()
    with pytest.raises(BusinessLogicError) as error:
        create_checkin(
            club_id=club.id, student_id=subscription.student_id, schedule_id=schedule.id,
            training_type_id=schedule.training_type_id, source=channel,
            checkin_date=date(2026, 9, 7),
        )
    assert error.value.code == "opening_attendance_already_covered"
    assert not Checkin.objects.for_club(club).exists()
    assert not Debt.objects.for_club(club).exists()
    snapshot.component.refresh_from_db()
    assert (snapshot.component.credits_used, snapshot.component.credits_left) == (5, 7)


def test_after_cutoff_consumes_once_and_keeps_source_facts(club, source):
    snapshot, schedule = source
    with patch("apps.attendance.services.checkin.async_task"):
        args = dict(
            club_id=club.id, student_id=snapshot.subscription.student_id, schedule_id=schedule.id,
            training_type_id=schedule.training_type_id, source=Checkin.Source.MANUAL,
            checkin_date=date(2026, 9, 14),
        )
        first = create_checkin(**args)
        second = create_checkin(**args)
    assert first["checkin_id"] == second["checkin_id"]
    snapshot.component.refresh_from_db()
    snapshot.refresh_from_db()
    assert (snapshot.component.credits_used, snapshot.component.credits_left) == (6, 6)
    assert (snapshot.original_used, snapshot.original_left) == (5, 7)


def test_existing_covered_checkin_replays_before_cutoff_guard(club, source):
    snapshot, schedule = source
    existing = CheckinFactory(
        club=club, student=snapshot.subscription.student, schedule=schedule,
        training_type=schedule.training_type, date=date(2026, 9, 7),
    )
    result = create_checkin(
        club_id=club.id, student_id=existing.student_id, schedule_id=schedule.id,
        training_type_id=schedule.training_type_id, source=Checkin.Source.MANUAL,
        checkin_date=existing.date,
    )
    assert result["checkin_id"] == existing.id
    snapshot.component.refresh_from_db()
    assert snapshot.component.credits_left == 7


def test_snapshot_rejects_mutation_and_inconsistent_source(club, source):
    snapshot, _ = source
    with pytest.raises(ValidationError):
        OpeningEntitlementSnapshot.objects.for_club(club).filter(id=snapshot.id).update(original_used=6)
    with pytest.raises(ValidationError):
        snapshot.delete()
    snapshot.covered_through += timedelta(days=1)
    with pytest.raises(ValidationError):
        snapshot.save()


def test_offline_covered_visit_is_terminal_and_repeat_safe(club, source):
    from ninja.testing import TestClient

    from apps.attendance.services import activate_kiosk, generate_kiosk_pin
    from config.api import api

    snapshot, schedule = source
    token = activate_kiosk(pin=generate_kiosk_pin(club_id=club.id))["token"]
    client = TestClient(api)
    payload = {"checkins": [{
        "client_id": "synthetic-offline", "idempotency_key": "synthetic-cutoff",
        "student_id": snapshot.subscription.student_id, "schedule_id": schedule.id,
        "training_type_id": schedule.training_type_id, "checkin_date": "2026-09-07",
    }]}
    for _ in range(2):
        result = client.post("/checkins/sync/", json=payload, headers={"X-Kiosk-Token": token})
        assert result.status_code == 200
        row = result.json()["results"][0]
        assert row["error"] == "opening_attendance_already_covered" and row["retryable"] is False
    assert not Checkin.objects.for_club(club).exists()
    assert not Debt.objects.for_club(club).exists()


def test_cancel_after_cutoff_restores_only_new_consumption(club, source):
    from apps.attendance.services import cancel_checkin

    snapshot, schedule = source
    with patch("apps.attendance.services.checkin.async_task"):
        result = create_checkin(
            club_id=club.id, student_id=snapshot.subscription.student_id, schedule_id=schedule.id,
            training_type_id=schedule.training_type_id, source=Checkin.Source.MANUAL,
            checkin_date=date(2026, 9, 14),
        )
        cancel_checkin(
            checkin_id=result["checkin_id"], club_id=club.id,
            cancelled_by_user_id=snapshot.actor_id, user_role="owner",
        )
    snapshot.component.refresh_from_db()
    snapshot.subscription.refresh_from_db()
    assert (snapshot.component.credits_used, snapshot.component.credits_left) == (5, 7)
    assert (snapshot.subscription.trainings_used, snapshot.subscription.trainings_left) == (5, 7)


def test_selected_opening_cannot_consume_before_original_start(club, source):
    snapshot, schedule = source
    ScheduleEnrollment.objects.for_club(club).filter(schedule=schedule).update(starts_on=date(2026, 8, 1))
    with pytest.raises(BusinessLogicError) as error:
        create_checkin(
            club_id=club.id, student_id=snapshot.subscription.student_id, schedule_id=schedule.id,
            training_type_id=schedule.training_type_id, source=Checkin.Source.MANUAL,
            checkin_date=date(2026, 8, 31),
        )
    assert error.value.code == "opening_attendance_already_covered"
    snapshot.component.refresh_from_db()
    assert (snapshot.component.credits_used, snapshot.component.credits_left) == (5, 7)
    assert not Checkin.objects.for_club(club).exists()
