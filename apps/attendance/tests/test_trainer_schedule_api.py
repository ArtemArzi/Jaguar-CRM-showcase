"""Tests for trainer schedule permissions, ownership guard, and one_time_date enforcement."""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import Checkin, GroupSession, ScheduleEnrollment
from apps.attendance.tests.factories import CheckinFactory, ScheduleExceptionFactory, ScheduleFactory
from apps.billing.models import TrainingType
from apps.billing.tests.factories import TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory, LocationFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


def _future_weekday(weekday: int, *, min_days: int = 7) -> date:
    today = timezone.localdate()
    delta = (weekday - today.weekday()) % 7
    if delta < min_days:
        delta += 7
    return today + timedelta(days=delta)


def _future_monday() -> date:
    return _future_weekday(0)


def _future_tuesday() -> date:
    return _future_monday() + timedelta(days=1)


def _future_wednesday() -> date:
    return _future_monday() + timedelta(days=2)


def _finished_session_now(target_date: date):
    return datetime.combine(target_date, time(12, 0), tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.fixture
def trainer_with_user(club, trainer_user):
    """Trainer record linked to trainer_user."""
    return TrainerFactory(club=club, user=trainer_user)


@pytest.fixture
def other_trainer(club):
    """Another trainer in the same club (different user)."""
    return TrainerFactory(club=club)


@pytest.fixture
def location(club):
    return LocationFactory(club=club)


@pytest.fixture
def training_type(club):
    return TrainingTypeFactory(club=club)


@pytest.mark.django_db
class TestTrainerCreateSchedule:
    """Trainer can create one-time schedules only."""

    def test_trainer_create_one_time_schedule(self, club, trainer_user, trainer_with_user, location, training_type):
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Extra Session",
                "trainer_id": trainer_with_user.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "one_time_date": _future_monday().isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["group_name"] == "Extra Session"
        assert data["trainer_id"] == trainer_with_user.id
        assert data["training_type_id"] == training_type.id
        assert data["one_time_date"] == _future_monday().isoformat()

    def test_trainer_create_without_one_time_date_rejected(
        self, club, trainer_user, trainer_with_user, location, training_type
    ):
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Recurring Attempt",
                "trainer_id": trainer_with_user.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403
        assert "one-time" in response.json()["detail"].lower()

    def test_trainer_create_forces_own_trainer_id(
        self, club, trainer_user, trainer_with_user, other_trainer, location, training_type
    ):
        """Even if trainer sends another trainer_id, the system forces their own."""
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Sneaky Session",
                "trainer_id": other_trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "one_time_date": _future_tuesday().isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201
        data = response.json()
        # Trainer ID forced to the authenticated trainer
        assert data["trainer_id"] == trainer_with_user.id


@pytest.mark.django_db
class TestTrainerUpdateSchedule:
    """Trainer can update own schedules, not others."""

    def test_trainer_update_own_schedule(self, club, trainer_user, trainer_with_user, location):
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location)
        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"group_name": "Updated Name"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 200
        assert response.json()["group_name"] == "Updated Name"

    def test_trainer_cannot_reassign_own_schedule(self, club, trainer_user, trainer_with_user, other_trainer, location):
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location)

        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"trainer_id": other_trainer.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        schedule.refresh_from_db()
        assert schedule.trainer_id == trainer_with_user.id

    def test_trainer_update_other_schedule_rejected(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"group_name": "Hijacked"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestTrainerCancelSession:
    """Trainer can cancel own schedules, not others."""

    def test_trainer_cancel_own_session(self, club, trainer_user, trainer_with_user, location):
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location)
        response = client.post(
            f"/schedules/{schedule.id}/cancel/",
            json={"date": _future_monday().isoformat(), "reason": "Sick"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201

    def test_trainer_cancel_other_session_rejected(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.post(
            f"/schedules/{schedule.id}/cancel/",
            json={"date": _future_monday().isoformat(), "reason": "Not my class"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestTrainerRescheduleSession:
    """Trainer can reschedule own schedules, not others."""

    def test_trainer_reschedule_own_session(self, club, trainer_user, trainer_with_user, location):
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location)
        response = client.post(
            f"/schedules/{schedule.id}/reschedule/",
            json={
                "date": _future_monday().isoformat(),
                "new_date": _future_tuesday().isoformat(),
                "new_start_time": "12:00:00",
                "new_end_time": "13:00:00",
                "reason": "Conflict",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 201

    def test_trainer_reschedule_other_session_rejected(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.post(
            f"/schedules/{schedule.id}/reschedule/",
            json={
                "date": _future_monday().isoformat(),
                "new_date": _future_tuesday().isoformat(),
                "new_start_time": "12:00:00",
                "new_end_time": "13:00:00",
                "reason": "Not mine",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestTrainerScheduleVisibility:
    """Trainer list endpoints should only expose effective sessions they own."""

    def test_trainer_cannot_list_raw_schedules(self, club, trainer_user):
        response = client.get(
            "/schedules/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_today_sessions_only_return_owned_occurrences(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        today = _future_monday()  # Monday
        owned = ScheduleFactory(club=club, trainer=trainer_with_user, location=location, day_of_week=0)
        foreign = ScheduleFactory(club=club, trainer=other_trainer, location=location, day_of_week=0)

        with patch("apps.attendance.api.club_localdate", return_value=today):
            response = client.get(
                "/schedules/today/",
                **_auth_params(trainer_user, club, role="trainer"),
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["schedule_id"] == owned.id
        assert data[0]["trainer_id"] == trainer_with_user.id
        assert data[0]["schedule_id"] != foreign.id

    def test_trainer_by_date_sessions_only_return_owned_occurrences(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        target_date = _future_monday()  # Monday
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        owned = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            day_of_week=0,
            training_type=training_type,
        )
        ScheduleFactory(club=club, trainer=other_trainer, location=location, day_of_week=0)

        response = client.get(
            f"/schedules/by-date/?date={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["schedule_id"] == owned.id
        assert data[0]["trainer_id"] == trainer_with_user.id
        assert data[0]["training_type_kind"] == TrainingType.Kind.GROUP

    def test_trainer_unclosed_sessions_only_return_owned_occurrences(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        target_date = _future_monday()  # Monday
        owned = ScheduleFactory(club=club, trainer=trainer_with_user, location=location, day_of_week=0)
        ScheduleFactory(club=club, trainer=other_trainer, location=location, day_of_week=0)

        response = client.get(
            f"/schedules/unclosed/?date={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["schedule_id"] == owned.id
        assert data[0]["trainer_id"] == trainer_with_user.id

    def test_trainer_unclosed_sessions_require_explicit_session_close_after_checkin(
        self, club, trainer_user, trainer_with_user, location
    ):
        target_date = _future_monday()  # Monday
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location, day_of_week=0)
        student = StudentFactory(club=club, status="active")
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=schedule.training_type,
            date=target_date,
        )

        response = client.get(
            f"/schedules/unclosed/?date={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["schedule_id"] == schedule.id

        with patch("apps.attendance.selectors.timezone.now", return_value=_finished_session_now(target_date)):
            close_response = client.post(
                f"/schedules/{schedule.id}/sessions/close/",
                json={"date": target_date.isoformat()},
                **_auth_params(trainer_user, club, role="trainer"),
            )

        assert close_response.status_code == 200

        response = client.get(
            f"/schedules/unclosed/?date={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.django_db
class TestTrainerForeignDeepLinks:
    """Foreign schedule deep links must be forbidden for trainers."""

    def test_trainer_cannot_view_foreign_schedule_detail(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.get(
            f"/schedules/{schedule.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_trainer_cannot_view_foreign_schedule_students(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.get(
            f"/schedules/{schedule.id}/students/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_trainer_cannot_view_foreign_schedule_checked_in(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        response = client.get(
            f"/schedules/{schedule.id}/checked-in/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_trainer_cannot_batch_checkin_foreign_schedule(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        schedule = ScheduleFactory(club=club, trainer=other_trainer, location=location)
        training_type = TrainingTypeFactory(club=club)
        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": _future_monday().isoformat(),
                "present_student_ids": [],
                "training_type_id": training_type.id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_trainer_cannot_batch_checkin_own_schedule(
        self, club, trainer_user, trainer_with_user, location
    ):
        target_date = _future_monday()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": target_date.isoformat(),
                "present_student_ids": [student.id],
                "training_type_id": training_type.id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not Checkin.objects.filter(student=student, schedule=schedule, date=target_date).exists()

    def test_trainer_session_detail_shows_roster_status_without_manual_attendance(
        self, club, trainer_user, trainer_with_user, location
    ):
        target_date = _future_monday()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        checked_student = StudentFactory(club=club, first_name="Checked", status=Student.Status.ACTIVE)
        waiting_student = StudentFactory(club=club, first_name="Waiting", status=Student.Status.ACTIVE)
        frozen_student = StudentFactory(club=club, first_name="Frozen", status=Student.Status.ACTIVE)
        for student, status in [
            (checked_student, ScheduleEnrollment.Status.ACTIVE),
            (waiting_student, ScheduleEnrollment.Status.ACTIVE),
            (frozen_student, ScheduleEnrollment.Status.FROZEN),
        ]:
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=status,
                starts_on=target_date,
            )
        CheckinFactory(
            club=club,
            student=checked_student,
            schedule=schedule,
            training_type=training_type,
            date=target_date,
            source=Checkin.Source.KIOSK,
        )

        response = client.get(
            f"/schedules/{schedule.id}/session-detail/?date={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["checked_in_count"] == 1
        assert data["summary"]["waiting_count"] == 1
        assert data["summary"]["blocked_count"] == 1
        statuses = {student["first_name"]: student["checkin_status"] for student in data["roster"]}
        assert statuses == {
            "Checked": "checked_in",
            "Waiting": "waiting",
            "Frozen": "blocked",
        }
        assert data["is_closed"] is False

    def test_trainer_closes_session_summary_without_creating_checkins(
        self, club, trainer_user, trainer_with_user, location
    ):
        target_date = _future_monday()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=target_date,
            source=Checkin.Source.KIOSK,
        )
        checkin_count = Checkin.objects.filter(club=club, schedule=schedule, date=target_date).count()

        with patch("apps.attendance.selectors.timezone.now", return_value=_finished_session_now(target_date)):
            response = client.post(
                f"/schedules/{schedule.id}/sessions/close/",
                json={
                    "date": target_date.isoformat(),
                    "topic_tags": ["guard"],
                    "notes": "Reviewed in trainer app",
                },
                **_auth_params(trainer_user, club, role="trainer"),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["attendee_count"] == 1
        assert data["close_source"] == "trainer_review"
        assert Checkin.objects.filter(club=club, schedule=schedule, date=target_date).count() == checkin_count
        session = GroupSession.objects.get(club=club, schedule=schedule, date=target_date)
        assert session.closed_by_id == trainer_user.id
        assert session.closed_at is not None

    def test_trainer_cannot_book_guest_on_foreign_schedule(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        target_date = _future_monday()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            location=location,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "student_id": student.id,
                "origin": "planned_session_action",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not ScheduleEnrollment.objects.filter(
            student=student,
            schedule=schedule,
            starts_on=target_date,
            ends_on=target_date,
        ).exists()

    def test_trainer_guest_lead_search_and_booking_are_limited_to_assigned_leads(
        self, club, trainer_user, trainer_with_user, other_trainer, location
    ):
        target_date = _future_monday()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        assigned_lead = StudentFactory(
            club=club,
            first_name="Nina",
            status=Student.Status.LEAD,
            assigned_trainer=trainer_with_user,
        )
        other_lead = StudentFactory(
            club=club,
            first_name="Nina",
            status=Student.Status.LEAD,
            assigned_trainer=other_trainer,
        )
        unassigned_lead = StudentFactory(
            club=club,
            first_name="Nina",
            status=Student.Status.LEAD,
            assigned_trainer=None,
        )

        response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={target_date.isoformat()}&q=Nina"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )
        other_response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "lead_id": other_lead.id,
                "origin": "planned_session_action",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        unassigned_response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "lead_id": unassigned_lead.id,
                "origin": "planned_session_action",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assigned_response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "lead_id": assigned_lead.id,
                "origin": "planned_session_action",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [candidate["id"] for candidate in response.json()] == [assigned_lead.id]
        assert other_response.status_code == 403
        assert unassigned_response.status_code == 403
        assert assigned_response.status_code == 201
        assert assigned_response.json()["student_id"] == assigned_lead.id


@pytest.mark.django_db
class TestTrainerSubstituteAccess:
    """Effective substitute trainer should be able to use related deep links."""

    def test_trainer_can_open_own_non_today_schedule_without_date(
        self, club, trainer_user, trainer_with_user, location
    ):
        future_date = _future_wednesday()
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            one_time_date=future_date,
        )

        detail_response = client.get(
            f"/schedules/{schedule.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        students_response = client.get(
            f"/schedules/{schedule.id}/students/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert detail_response.status_code == 200
        assert students_response.status_code == 200

    def test_substitute_trainer_can_open_related_schedule_endpoints(
        self, club, trainer_user, trainer_with_user, location
    ):
        substitute_user = UserFactory()
        substitute_trainer = TrainerFactory(club=club, user=substitute_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            training_type=training_type,
        )
        target_date = _future_monday()
        student = StudentFactory(club=club, status="active")
        ScheduleExceptionFactory(
            schedule=schedule,
            date=target_date,
            exception_type="substitute",
            substitute_trainer=substitute_trainer,
        )

        with patch("apps.attendance.api.date_cls") as mock_date:
            mock_date.today.return_value = target_date
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

            detail_response = client.get(
                f"/schedules/{schedule.id}/?date={target_date.isoformat()}",
                **_auth_params(substitute_user, club, role="trainer"),
            )
            students_response = client.get(
                f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
                **_auth_params(substitute_user, club, role="trainer"),
            )
            checked_in_response = client.get(
                f"/schedules/{schedule.id}/checked-in/?date={target_date.isoformat()}",
                **_auth_params(substitute_user, club, role="trainer"),
            )
            candidate_response = client.get(
                (
                    f"/schedules/{schedule.id}/guest-visit-candidates/"
                    f"?date={target_date.isoformat()}&q={student.first_name}"
                ),
                **_auth_params(substitute_user, club, role="trainer"),
            )
            guest_response = client.post(
                f"/schedules/{schedule.id}/guest-visits/",
                json={
                    "date": target_date.isoformat(),
                    "student_id": student.id,
                    "origin": "walk_in_checkin",
                },
                **_auth_params(substitute_user, club, role="trainer"),
            )
            batch_response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": target_date.isoformat(),
                    "present_student_ids": [],
                    "training_type_id": schedule.training_type_id,
                    "topic_tags": [],
                    "notes": "",
                },
                **_auth_params(substitute_user, club, role="trainer"),
            )

        assert detail_response.status_code == 200
        assert students_response.status_code == 200
        assert checked_in_response.status_code == 200
        assert candidate_response.status_code == 200
        assert candidate_response.json()[0]["id"] == student.id
        assert batch_response.status_code == 403
        assert guest_response.status_code == 201
        assert guest_response.json()["student_id"] == student.id

    def test_trainer_cannot_search_guest_candidates_for_other_trainer_occurrence(
        self, club, trainer_user, other_trainer, location
    ):
        target_date = _future_monday()
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            location=location,
            day_of_week=target_date.weekday(),
        )
        StudentFactory(club=club, first_name="Nina", status="active")

        response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={target_date.isoformat()}&q=Nina"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_substitute_trainer_can_access_rescheduled_session_on_new_date(
        self, club, trainer_user, trainer_with_user, location
    ):
        substitute_user = UserFactory()
        substitute_trainer = TrainerFactory(club=club, user=substitute_user)
        schedule = ScheduleFactory(club=club, trainer=trainer_with_user, location=location, day_of_week=0)
        old_date = _future_monday()
        new_date = _future_tuesday()
        ScheduleExceptionFactory(
            schedule=schedule,
            date=old_date,
            exception_type="rescheduled",
            new_date=new_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=new_date,
            exception_type="substitute",
            substitute_trainer=substitute_trainer,
        )
        list_response = client.get(
            f"/schedules/by-date/?date={new_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )
        detail_response = client.get(
            f"/schedules/{schedule.id}/?date={new_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )
        students_response = client.get(
            f"/schedules/{schedule.id}/students/?date={new_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )
        checked_in_response = client.get(
            f"/schedules/{schedule.id}/checked-in/?date={new_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )
        batch_response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": new_date.isoformat(),
                "present_student_ids": [],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(substitute_user, club, role="trainer"),
        )

        assert list_response.status_code == 200
        assert list_response.json()[0]["trainer_id"] == substitute_trainer.id
        assert list_response.json()[0]["is_rescheduled"] is True
        assert list_response.json()[0]["is_substitute"] is True
        assert detail_response.status_code == 200
        assert students_response.status_code == 200
        assert checked_in_response.status_code == 200
        assert batch_response.status_code == 403

    def test_substitute_trainer_can_open_non_today_schedule_endpoints(
        self, club, trainer_user, trainer_with_user, location
    ):
        substitute_user = UserFactory()
        substitute_trainer = TrainerFactory(club=club, user=substitute_user)
        target_date = _future_wednesday()
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            location=location,
            one_time_date=target_date,
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=target_date,
            exception_type="substitute",
            substitute_trainer=substitute_trainer,
        )

        detail_response = client.get(
            f"/schedules/{schedule.id}/?date={target_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )
        students_response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(substitute_user, club, role="trainer"),
        )

        assert detail_response.status_code == 200
        assert students_response.status_code == 200


@pytest.mark.django_db
class TestOwnerRegressionSchedule:
    """Owner can still do everything (no one_time_date restriction, any schedule)."""

    def test_owner_create_recurring_schedule(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Regular Class",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        assert response.json()["one_time_date"] is None
        assert response.json()["training_type_id"] == training_type.id

    def test_owner_update_any_schedule(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer, location=location)
        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"group_name": "Owner Changed"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["group_name"] == "Owner Changed"

    def test_owner_today_sessions_see_club_wide_occurrences(
        self, club, owner_user, trainer_with_user, other_trainer, location
    ):
        today = _future_monday()  # Monday
        owned = ScheduleFactory(club=club, trainer=trainer_with_user, location=location, day_of_week=0)
        foreign = ScheduleFactory(club=club, trainer=other_trainer, location=location, day_of_week=0)

        with patch("apps.attendance.api.club_localdate", return_value=today):
            response = client.get(
                "/schedules/today/",
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert {item["schedule_id"] for item in data} == {owned.id, foreign.id}


@pytest.mark.django_db
class TestLocationsEndpoint:
    """GET /api/clubs/locations/ returns club locations for trainer role."""

    def test_trainer_list_locations(self, club, trainer_user):
        LocationFactory(club=club, name="Main Hall")
        LocationFactory(club=club, name="Ring Room")
        # Other club location should not appear
        other_club = ClubFactory()
        LocationFactory(club=other_club, name="Other Place")

        response = client.get(
            "/clubs/locations/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = {item["name"] for item in data}
        assert names == {"Main Hall", "Ring Room"}

    def test_owner_list_locations(self, club, owner_user):
        LocationFactory(club=club, name="Gym A")
        response = client.get(
            "/clubs/locations/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert len(response.json()) == 1
