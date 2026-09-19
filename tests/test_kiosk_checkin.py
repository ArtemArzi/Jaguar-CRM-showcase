"""Tests for kiosk checkin endpoints: lookup, checkin, sync, idempotency."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.test import Client, override_settings

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    KioskDevice,
    ScheduleEnrollment,
    TrainingGroupRolloutState,
)
from apps.attendance.services import activate_kiosk, create_checkin, generate_kiosk_pin
from apps.attendance.tests.factories import ScheduleExceptionFactory, ScheduleFactory
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import Debt, Payment, TrainingType
from apps.billing.services import create_payment
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory, UserFactory
from apps.grades.tests.factories import GradeFactory, GradeSystemFactory, StudentGradeFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


@pytest.fixture
def kiosk_club(db):
    return ClubFactory()


@pytest.fixture
def kiosk_device(kiosk_club):
    pin = generate_kiosk_pin(club_id=kiosk_club.id)
    result = activate_kiosk(pin=pin)
    return KioskDevice.objects.get(token=result["token"])


@pytest.fixture
def kiosk_client(kiosk_device):
    """Django test client with kiosk device token header."""
    client = Client()
    client.defaults["HTTP_X_KIOSK_TOKEN"] = kiosk_device.token
    return client


@pytest.fixture
def kiosk_training_type(kiosk_club):
    return TrainingTypeFactory(club=kiosk_club)


@pytest.fixture
def kiosk_schedule(kiosk_club, kiosk_training_type):
    return ScheduleFactory(
        club=kiosk_club,
        day_of_week=date.today().weekday(),
        training_type=kiosk_training_type,
    )


@pytest.fixture
def kiosk_students(kiosk_club, kiosk_schedule):
    students = [
        StudentFactory(club=kiosk_club, phone="+79001234567", first_name="Ivan", status="active"),
        StudentFactory(club=kiosk_club, phone="+79009994567", first_name="Petr", status="active"),
        StudentFactory(club=kiosk_club, phone="+79001239999", first_name="Oleg", status="active"),
    ]
    for student in students:
        _enroll_for_kiosk(club=kiosk_club, student=student, schedule=kiosk_schedule)
    return students


def _enroll_for_kiosk(*, club, student, schedule, status=ScheduleEnrollment.Status.ACTIVE, starts_on=None):
    return ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=status,
        starts_on=starts_on or date.today(),
    )


@pytest.mark.django_db
class TestKioskLookup:
    def test_lookup_returns_matching_students(self, kiosk_client, kiosk_students):
        resp = kiosk_client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "4567"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.json()
        # Ivan and Petr both end with 4567
        assert len(data) == 2
        names = {s["first_name"] for s in data}
        assert names == {"Ivan", "Petr"}
        for item in data:
            assert "phone" not in item
            assert "email" not in item
            assert item["lookup_suffix"] == "4567"
            assert item["masked_phone"].endswith("4567")

    def test_lookup_no_match(self, kiosk_client, kiosk_students):
        resp = kiosk_client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "0000"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_lookup_excludes_ineligible_leads(self, kiosk_client, kiosk_club):
        StudentFactory(club=kiosk_club, phone="+79001234567", status="lead")

        resp = kiosk_client.post(
            "/api/checkins/kiosk/lookup/",
            {"phone_suffix": "4567"},
            content_type="application/json",
        )

        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.django_db
class TestKioskScheduleEndpoint:
    def test_schedule_endpoint_returns_normalized_device_auth_fields(
        self, kiosk_client, kiosk_schedule, kiosk_training_type
    ):
        resp = kiosk_client.get("/api/checkins/kiosk/schedules/today/")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        item = data[0]
        assert item["schedule_id"] == kiosk_schedule.id
        assert item["effective_date"] == str(date.today())
        assert item["start_time"] == "10:00:00"
        assert item["end_time"] == "11:00:00"
        assert item["group_name"] == kiosk_schedule.group_name
        assert item["trainer_name"]
        assert item["location_name"]
        assert item["training_type_id"] == kiosk_training_type.id
        assert item["training_type_name"] == kiosk_training_type.name
        assert "effective_start_time" not in item
        assert "effective_end_time" not in item

    def test_schedule_endpoint_hides_legacy_null_training_type(
        self, kiosk_client, kiosk_club, kiosk_schedule
    ):
        legacy_schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=date.today().weekday(),
            training_type=None,
            group_name="Legacy Null Type",
        )

        resp = kiosk_client.get("/api/checkins/kiosk/schedules/today/")

        assert resp.status_code == 200
        schedule_ids = {item["schedule_id"] for item in resp.json()}
        assert kiosk_schedule.id in schedule_ids
        assert legacy_schedule.id not in schedule_ids


@pytest.mark.django_db
class TestKioskRosterEndpoint:
    def test_roster_endpoint_returns_safe_lookup_fields_only(self, kiosk_client, kiosk_students):
        resp = kiosk_client.get("/api/checkins/kiosk/roster/")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        for item in data:
            assert set(item) >= {"id", "first_name", "last_name", "lookup_suffix", "masked_phone"}
            assert "phone" not in item
            assert "email" not in item
            assert item["masked_phone"].endswith(item["lookup_suffix"])
            assert len(item["lookup_suffix"]) == 4


@pytest.mark.django_db
class TestKioskBrandingEndpoint:
    def test_branding_endpoint_returns_safe_device_auth_fields(self, kiosk_client, kiosk_club):
        ClubSettingsFactory(
            club=kiosk_club,
            primary_color="#112233",
            accent_color="#445566",
            club_name_display="Jaguar Tablet",
            logo_url="https://example.com/logo.png",
        )

        resp = kiosk_client.get("/api/checkins/kiosk/branding/")

        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "primary_color": "#112233",
            "accent_color": "#445566",
            "club_name_display": "Jaguar Tablet",
            "logo_url": "https://example.com/logo.png",
        }


@pytest.mark.django_db
class TestKioskCheckin:
    @patch("apps.attendance.services.async_task")
    def test_checkin_creates_record(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        student = kiosk_students[0]
        resp = kiosk_client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": kiosk_schedule.id,
                "training_type_id": kiosk_training_type.id,
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["checkin_id"] > 0
        assert data["student_id"] == student.id
        assert data["created"] is True
        assert data["duplicate"] is False
        assert data["salary_queued"] is False
        assert data["parent_notification_queued"] is False
        assert data["grade_progress_queued"] is False
        assert Checkin.objects.filter(id=data["checkin_id"], student=student).exists()

    @patch("apps.attendance.services.async_task")
    def test_checkin_flags_only_real_parent_and_grade_side_effects(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        parent = UserFactory()
        student = StudentFactory(
            club=kiosk_club,
            phone="+79001230001",
            first_name="Child",
            status="active",
            is_child=True,
            parent_user=parent,
        )
        grade_system = GradeSystemFactory(club=kiosk_club)
        grade = GradeFactory(club=kiosk_club, grade_system=grade_system)
        StudentGradeFactory(
            club=kiosk_club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
        )
        kiosk_training_type.grade_system = grade_system
        kiosk_training_type.save(update_fields=["grade_system"])
        tariff = TariffFactory(club=kiosk_club, training_type=kiosk_training_type)
        SubscriptionFactory(club=kiosk_club, student=student, tariff=tariff)
        _enroll_for_kiosk(club=kiosk_club, student=student, schedule=kiosk_schedule)

        resp = kiosk_client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": kiosk_schedule.id,
                "training_type_id": kiosk_training_type.id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["salary_queued"] is True
        assert data["parent_notification_queued"] is True
        assert data["grade_progress_queued"] is True

    @patch("apps.attendance.services.async_task")
    def test_checkin_flags_use_recorded_cascade_events(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        parent = UserFactory()
        student = StudentFactory(
            club=kiosk_club,
            phone="+79001230002",
            status="active",
            is_child=True,
            parent_user=parent,
        )
        grade_system = GradeSystemFactory(club=kiosk_club)
        grade = GradeFactory(club=kiosk_club, grade_system=grade_system)
        StudentGradeFactory(
            club=kiosk_club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
        )
        kiosk_training_type.grade_system = grade_system
        kiosk_training_type.save(update_fields=["grade_system"])
        tariff = TariffFactory(club=kiosk_club, training_type=kiosk_training_type)
        SubscriptionFactory(club=kiosk_club, student=student, tariff=tariff)
        _enroll_for_kiosk(club=kiosk_club, student=student, schedule=kiosk_schedule)

        def create_then_mutate(*args, **kwargs):
            result = create_checkin(*args, **kwargs)
            Student.objects.filter(id=student.id).update(
                is_child=False,
                parent_user=None,
            )
            TrainingType.objects.filter(id=kiosk_training_type.id).update(
                grade_system=None,
            )
            return result

        with patch("apps.attendance.api.create_checkin", side_effect=create_then_mutate):
            resp = kiosk_client.post(
                "/api/checkins/kiosk/",
                {
                    "student_id": student.id,
                    "schedule_id": kiosk_schedule.id,
                    "training_type_id": kiosk_training_type.id,
                },
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["salary_queued"] is True
        assert data["parent_notification_queued"] is True
        assert data["grade_progress_queued"] is True

    @patch("apps.attendance.services.async_task")
    def test_checkin_rejects_frozen_enrollment_without_side_effects(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        student = StudentFactory(club=kiosk_club, phone="+79001230003", status="active")
        _enroll_for_kiosk(
            club=kiosk_club,
            student=student,
            schedule=kiosk_schedule,
            status=ScheduleEnrollment.Status.FROZEN,
        )

        resp = kiosk_client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": kiosk_schedule.id,
                "training_type_id": kiosk_training_type.id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "enrollment_frozen"
        assert not Checkin.objects.filter(student=student, schedule=kiosk_schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=kiosk_club).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_duplicate_checkin_is_idempotent(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        student = kiosk_students[0]
        payload = {
            "student_id": student.id,
            "schedule_id": kiosk_schedule.id,
            "training_type_id": kiosk_training_type.id,
        }
        resp1 = kiosk_client.post("/api/checkins/kiosk/", payload, content_type="application/json")
        assert resp1.status_code == 200

        # Second attempt -- should not create duplicate (IntegrityError caught)
        resp2 = kiosk_client.post("/api/checkins/kiosk/", payload, content_type="application/json")
        # Should still succeed (idempotent) or return error gracefully
        assert resp2.status_code in (200, 400)
        if resp2.status_code == 200:
            data = resp2.json()
            assert data["created"] is False
            assert data["duplicate"] is True
            assert data["salary_queued"] is False

        # Only 1 checkin should exist
        count = Checkin.objects.filter(
            student=student, schedule=kiosk_schedule, date=date.today(), deleted_at__isnull=True
        ).count()
        assert count == 1

    @patch("apps.attendance.services.async_task")
    def test_integrity_error_duplicate_uses_payload_checkin_date(
        self, mock_async, kiosk_client, kiosk_club, kiosk_students, kiosk_training_type
    ):
        checkin_date = date.today() - timedelta(days=1)
        schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=checkin_date.weekday(),
            training_type=kiosk_training_type,
        )
        _enroll_for_kiosk(
            club=kiosk_club,
            student=kiosk_students[0],
            schedule=schedule,
            starts_on=checkin_date,
        )
        existing = create_checkin(
            club_id=kiosk_club.id,
            student_id=kiosk_students[0].id,
            schedule_id=schedule.id,
            training_type_id=kiosk_training_type.id,
            source="kiosk",
            checkin_date=checkin_date,
        )

        with patch("apps.attendance.api.create_checkin", side_effect=IntegrityError):
            resp = kiosk_client.post(
                "/api/checkins/kiosk/",
                {
                    "student_id": kiosk_students[0].id,
                    "schedule_id": schedule.id,
                    "training_type_id": kiosk_training_type.id,
                    "checkin_date": str(checkin_date),
                },
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["checkin_id"] == existing["checkin_id"]
        assert data["duplicate"] is True
        assert data["created"] is False


@pytest.mark.django_db
class TestOfflineSync:
    def test_reconciling_keeps_offline_item_retryable_without_checkin(
        self,
        kiosk_client,
        kiosk_club,
        kiosk_schedule,
        kiosk_training_type,
        kiosk_students,
    ):
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(kiosk_club),
            mode=TrainingGroupRolloutState.Mode.RECONCILING,
        )
        item = {
            "student_id": kiosk_students[0].id,
            "schedule_id": kiosk_schedule.id,
            "training_type_id": kiosk_training_type.id,
            "checkin_date": str(date.today()),
            "client_id": "reconciling-retry",
        }

        response = kiosk_client.post("/api/checkins/sync/", {"checkins": [item]}, content_type="application/json")

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["success"] is False
        assert result["error"] == "training_group_reconciling"
        assert result["retryable"] is True
        assert Checkin.objects.for_club(kiosk_club).count() == 0

    @patch("apps.attendance.services.async_task")
    def test_sync_processes_multiple_checkins(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        payload = {
            "checkins": [
                {
                    "student_id": kiosk_students[0].id,
                    "schedule_id": kiosk_schedule.id,
                    "training_type_id": kiosk_training_type.id,
                    "checkin_date": str(date.today()),
                    "client_id": "offline-1",
                },
                {
                    "student_id": kiosk_students[1].id,
                    "schedule_id": kiosk_schedule.id,
                    "training_type_id": kiosk_training_type.id,
                    "checkin_date": str(date.today()),
                    "client_id": "offline-2",
                },
            ]
        }
        resp = kiosk_client.post("/api/checkins/sync/", payload, content_type="application/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 2
        assert data["failed"] == 0
        assert {item["client_id"] for item in data["results"]} == {"offline-1", "offline-2"}

    @patch("apps.attendance.services.async_task")
    def test_sync_deduplicates(self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type):
        item = {
            "student_id": kiosk_students[0].id,
            "schedule_id": kiosk_schedule.id,
            "training_type_id": kiosk_training_type.id,
            "checkin_date": str(date.today()),
            "client_id": "offline-dup",
        }
        payload = {"checkins": [item, item]}
        resp = kiosk_client.post("/api/checkins/sync/", payload, content_type="application/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 2
        assert data["failed"] == 0
        assert data["results"][1]["success"] is True
        assert data["results"][1]["duplicate"] is True
        assert data["results"][1]["client_id"] == "offline-dup"

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_pending_manual_admission_offline_retry_keeps_one_reserved_debt(
        self,
        _mock_payment_async,
        _mock_checkin_async,
        kiosk_client,
        kiosk_club,
        kiosk_schedule,
        kiosk_training_type,
    ):
        kiosk_training_type.kind = TrainingType.Kind.GROUP
        kiosk_training_type.drop_in_price = None
        kiosk_training_type.save(update_fields=["kind", "drop_in_price", "updated_at"])
        student = StudentFactory(club=kiosk_club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(
            club=kiosk_club,
            training_type=kiosk_training_type,
            trainings_limit=2,
            duration_days=7,
        )
        ClubSettings.objects.update_or_create(
            club=kiosk_club,
            defaults={
                "unified_client_journey_enabled": True,
                "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
            },
        )
        with override_settings(
            UNIFIED_CLIENT_JOURNEY_ENABLED=True,
            MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        ):
            payment = create_payment(
                club_id=kiosk_club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=UserFactory().id,
                target_schedule_id=kiosk_schedule.id,
                target_start_date=date.today(),
                create_manual_operational_admission=True,
            )
        item = {
            "student_id": student.id,
            "schedule_id": kiosk_schedule.id,
            "training_type_id": kiosk_training_type.id,
            "checkin_date": str(date.today()),
            "client_id": "pending-admission-offline-retry",
        }

        first = kiosk_client.post("/api/checkins/sync/", {"checkins": [item]}, content_type="application/json")
        retry = kiosk_client.post("/api/checkins/sync/", {"checkins": [item]}, content_type="application/json")

        assert first.status_code == retry.status_code == 200
        assert first.json()["results"][0]["duplicate"] is False
        assert retry.json()["results"][0]["duplicate"] is True
        debts = Debt.objects.for_club(kiosk_club).filter(student=student, settlement_payment=payment)
        assert debts.count() == 1
        assert debts.get().tariff_price is None

    @patch("apps.attendance.services.async_task")
    def test_sync_preserves_original_checkin_date_and_echoes_client_id(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        original_date = date.today() - timedelta(days=1)
        schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=original_date.weekday(),
            training_type=kiosk_training_type,
        )
        _enroll_for_kiosk(
            club=kiosk_club,
            student=kiosk_students[0],
            schedule=schedule,
            starts_on=original_date,
        )

        resp = kiosk_client.post(
            "/api/checkins/sync/",
            {
                "checkins": [
                    {
                        "student_id": kiosk_students[0].id,
                        "schedule_id": schedule.id,
                        "training_type_id": kiosk_training_type.id,
                        "checkin_date": str(original_date),
                        "client_id": "offline-yesterday-1",
                    }
                ]
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 1
        assert data["failed"] == 0
        assert data["results"][0]["client_id"] == "offline-yesterday-1"
        checkin = Checkin.objects.get(id=data["results"][0]["checkin_id"])
        assert checkin.date == original_date

    @patch("apps.attendance.services.async_task")
    def test_sync_integrity_error_duplicate_is_terminal_success(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        checkin_date = date.today() - timedelta(days=1)
        schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=checkin_date.weekday(),
            training_type=kiosk_training_type,
        )
        _enroll_for_kiosk(
            club=kiosk_club,
            student=kiosk_students[0],
            schedule=schedule,
            starts_on=checkin_date,
        )
        existing = create_checkin(
            club_id=kiosk_club.id,
            student_id=kiosk_students[0].id,
            schedule_id=schedule.id,
            training_type_id=kiosk_training_type.id,
            source="kiosk",
            checkin_date=checkin_date,
        )

        with patch("apps.attendance.api.create_checkin", side_effect=IntegrityError):
            resp = kiosk_client.post(
                "/api/checkins/sync/",
                {
                    "checkins": [
                        {
                            "student_id": kiosk_students[0].id,
                            "schedule_id": schedule.id,
                            "training_type_id": kiosk_training_type.id,
                            "checkin_date": str(checkin_date),
                            "client_id": "race-duplicate-1",
                        }
                    ]
                },
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 1
        assert data["failed"] == 0
        assert data["results"][0]["client_id"] == "race-duplicate-1"
        assert data["results"][0]["checkin_id"] == existing["checkin_id"]
        assert data["results"][0]["duplicate"] is True

    @patch("apps.attendance.services.async_task")
    def test_sync_integrity_error_duplicate_without_date_uses_today(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        existing = create_checkin(
            club_id=kiosk_schedule.club_id,
            student_id=kiosk_students[0].id,
            schedule_id=kiosk_schedule.id,
            training_type_id=kiosk_training_type.id,
            source="kiosk",
            checkin_date=date.today(),
        )

        with patch("apps.attendance.api.create_checkin", side_effect=IntegrityError):
            resp = kiosk_client.post(
                "/api/checkins/sync/",
                {
                    "checkins": [
                        {
                            "student_id": kiosk_students[0].id,
                            "schedule_id": kiosk_schedule.id,
                            "training_type_id": kiosk_training_type.id,
                            "client_id": "race-duplicate-today",
                        }
                    ]
                },
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 1
        assert data["failed"] == 0
        assert data["results"][0]["client_id"] == "race-duplicate-today"
        assert data["results"][0]["checkin_id"] == existing["checkin_id"]
        assert data["results"][0]["duplicate"] is True

    @patch("apps.attendance.services.async_task")
    def test_sync_returns_frozen_checkin_as_failed_business_item(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        student = StudentFactory(club=kiosk_club, phone="+79001230004", status="active")
        _enroll_for_kiosk(
            club=kiosk_club,
            student=student,
            schedule=kiosk_schedule,
            status=ScheduleEnrollment.Status.FROZEN,
        )

        resp = kiosk_client.post(
            "/api/checkins/sync/",
            {
                "checkins": [
                    {
                        "student_id": student.id,
                        "schedule_id": kiosk_schedule.id,
                        "training_type_id": kiosk_training_type.id,
                        "checkin_date": str(date.today()),
                        "client_id": "offline-frozen-1",
                        "idempotency_key": "offline-frozen-1",
                    }
                ]
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 0
        assert data["failed"] == 1
        assert data["results"] == [
            {
                "client_id": "offline-frozen-1",
                "idempotency_key": "offline-frozen-1",
                "student_id": student.id,
                "success": False,
                "checkin_id": None,
                "duplicate": False,
                "error": "enrollment_frozen",
                "retryable": False,
            }
        ]
        assert not Checkin.objects.filter(student=student, schedule=kiosk_schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=kiosk_club).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_sync_returns_stable_code_for_missing_student(
        self, mock_async, kiosk_client, kiosk_schedule, kiosk_training_type
    ):
        resp = kiosk_client.post(
            "/api/checkins/sync/",
            {
                "checkins": [
                    {
                        "student_id": 999_999,
                        "schedule_id": kiosk_schedule.id,
                        "training_type_id": kiosk_training_type.id,
                        "checkin_date": str(date.today()),
                        "client_id": "offline-missing-student",
                    }
                ]
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 0
        assert data["failed"] == 1
        assert data["results"][0]["error"] == "student_not_found"
        assert "matching query" not in data["results"][0]["error"]
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_sync_returns_stable_code_for_missing_schedule(
        self, mock_async, kiosk_client, kiosk_students, kiosk_training_type
    ):
        resp = kiosk_client.post(
            "/api/checkins/sync/",
            {
                "checkins": [
                    {
                        "student_id": kiosk_students[0].id,
                        "schedule_id": 999_999,
                        "training_type_id": kiosk_training_type.id,
                        "checkin_date": str(date.today()),
                        "client_id": "offline-missing-schedule",
                    }
                ]
            },
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 0
        assert data["failed"] == 1
        assert data["results"][0]["error"] == "schedule_not_found"
        assert "matching query" not in data["results"][0]["error"]
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_sync_returns_stable_code_for_missing_training_type(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        with patch(
            "apps.attendance.api.create_checkin",
            side_effect=TrainingType.DoesNotExist("unstable private model text"),
        ):
            resp = kiosk_client.post(
                "/api/checkins/sync/",
                {
                    "checkins": [
                        {
                            "student_id": kiosk_students[0].id,
                            "schedule_id": kiosk_schedule.id,
                            "training_type_id": kiosk_training_type.id,
                            "checkin_date": str(date.today()),
                            "client_id": "offline-missing-training-type",
                        }
                    ]
                },
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 0
        assert data["failed"] == 1
        assert data["results"][0]["error"] == "training_type_not_found"
        assert "unstable private model text" not in data["results"][0]["error"]
        mock_async.assert_not_called()


# ──────────────────────────────────────────────
# B-11: Unexpected error propagates (not silently caught)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestOfflineSyncErrorPropagation:
    @patch("apps.attendance.services.async_task")
    def test_offline_sync_unexpected_error_propagates(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        """RuntimeError should NOT be caught — it should propagate (not silently swallowed)."""
        payload = {
            "checkins": [
                {
                    "student_id": kiosk_students[0].id,
                    "schedule_id": kiosk_schedule.id,
                    "training_type_id": kiosk_training_type.id,
                }
            ]
        }
        with patch("apps.attendance.api.create_checkin", side_effect=RuntimeError("unexpected")):
            # Django test client re-raises exceptions by default; disable to get 500 response
            kiosk_client.raise_request_exception = False
            resp = kiosk_client.post(
                "/api/checkins/sync/", payload, content_type="application/json",
            )
            kiosk_client.raise_request_exception = True
            # Unexpected exception propagates as 500, not silently caught
            assert resp.status_code == 500


# ──────────────────────────────────────────────
# B-12: Idempotent sync (pre-existing checkin)
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestOfflineSyncIdempotency:
    @patch("apps.attendance.services.async_task")
    def test_offline_sync_idempotent_key_prevents_duplicate(
        self, mock_async, kiosk_client, kiosk_club, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        """If a checkin already exists, sync returns terminal duplicate success."""
        student = kiosk_students[0]

        # Pre-create checkin
        create_checkin(
            club_id=kiosk_club.id,
            student_id=student.id,
            schedule_id=kiosk_schedule.id,
            training_type_id=kiosk_training_type.id,
            source="kiosk",
        )

        # Sync the same student+schedule again
        payload = {
            "checkins": [
                {
                    "student_id": student.id,
                    "schedule_id": kiosk_schedule.id,
                    "training_type_id": kiosk_training_type.id,
                    "checkin_date": str(date.today()),
                    "client_id": "existing-checkin-1",
                }
            ]
        }
        resp = kiosk_client.post("/api/checkins/sync/", payload, content_type="application/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 1
        assert data["failed"] == 0
        assert data["results"][0]["success"] is True
        assert data["results"][0]["duplicate"] is True
        assert data["results"][0]["client_id"] == "existing-checkin-1"

        # Only 1 checkin exists
        count = Checkin.objects.filter(
            student=student,
            schedule=kiosk_schedule,
            date=date.today(),
            deleted_at__isnull=True,
        ).count()
        assert count == 1


@pytest.mark.django_db
class TestKioskScheduleValidation:
    def _post_checkin(self, client, *, student, schedule, training_type):
        return client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
            },
            content_type="application/json",
        )

    @patch("apps.attendance.services.async_task")
    def test_rejects_unenrolled_student_without_side_effects(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        student = StudentFactory(club=kiosk_club, status="active")

        resp = self._post_checkin(
            kiosk_client,
            student=student,
            schedule=kiosk_schedule,
            training_type=kiosk_training_type,
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "student_schedule_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=kiosk_schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=kiosk_club).exists()

    @patch("apps.attendance.services.async_task")
    def test_rejects_wrong_schedule_for_enrolled_student(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        wrong_schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=date.today().weekday(),
            training_type=kiosk_training_type,
        )

        resp = self._post_checkin(
            kiosk_client,
            student=kiosk_students[0],
            schedule=wrong_schedule,
            training_type=kiosk_training_type,
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "student_schedule_ineligible"
        assert not Checkin.objects.filter(student=kiosk_students[0], schedule=wrong_schedule).exists()

    @patch("apps.attendance.services.async_task")
    def test_rejects_wrong_weekday_schedule(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        wrong_weekday = (date.today().weekday() + 1) % 7
        schedule = ScheduleFactory(club=kiosk_club, day_of_week=wrong_weekday, training_type=kiosk_training_type)

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=schedule, training_type=kiosk_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_cancelled_schedule(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        ScheduleExceptionFactory(schedule=kiosk_schedule, date=date.today(), exception_type="cancelled")

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=kiosk_schedule, training_type=kiosk_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_inactive_schedule(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=date.today().weekday(),
            is_active=False,
            training_type=kiosk_training_type,
        )

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=schedule, training_type=kiosk_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_original_rescheduled_date(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_training_type
    ):
        ScheduleExceptionFactory(
            schedule=kiosk_schedule,
            date=date.today(),
            exception_type="rescheduled",
            new_date=date.today() + timedelta(days=1),
        )

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=kiosk_schedule, training_type=kiosk_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_training_type_mismatch(
        self, mock_async, kiosk_client, kiosk_students, kiosk_schedule, kiosk_club
    ):
        other_training_type = TrainingTypeFactory(club=kiosk_club)

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=kiosk_schedule, training_type=other_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_schedule_without_training_type(
        self, mock_async, kiosk_client, kiosk_students, kiosk_club, kiosk_training_type
    ):
        schedule = ScheduleFactory(
            club=kiosk_club,
            day_of_week=date.today().weekday(),
            training_type=None,
        )

        resp = self._post_checkin(
            kiosk_client, student=kiosk_students[0], schedule=schedule, training_type=kiosk_training_type
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0


@pytest.mark.django_db
class TestKioskStudentEligibility:
    @patch("apps.attendance.services.async_task")
    @pytest.mark.parametrize("status", ["lead", "lost"])
    def test_rejects_ineligible_same_club_student(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type, status
    ):
        student = StudentFactory(club=kiosk_club, status=status)

        resp = kiosk_client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": kiosk_schedule.id,
                "training_type_id": kiosk_training_type.id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0

    @patch("apps.attendance.services.async_task")
    def test_rejects_deleted_same_club_student(
        self, mock_async, kiosk_client, kiosk_club, kiosk_schedule, kiosk_training_type
    ):
        student = StudentFactory(club=kiosk_club, status="active")
        student.soft_delete()

        resp = kiosk_client.post(
            "/api/checkins/kiosk/",
            {
                "student_id": student.id,
                "schedule_id": kiosk_schedule.id,
                "training_type_id": kiosk_training_type.id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert Checkin.objects.count() == 0
