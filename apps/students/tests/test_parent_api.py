from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import CheckinFactory, ScheduleExceptionFactory, ScheduleFactory
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentReconciliationAttempt,
    Payment,
    Subscription,
    SubscriptionFreeze,
    TrainingType,
)
from apps.billing.services import create_bank_payment_order
from apps.billing.tests.factories import (
    DebtFactory,
    SubscriptionFactory,
    SubscriptionFreezeFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, ClubSettingsFactory, UserFactory
from apps.clubs.timezones import club_local_day_start
from apps.common import auth_tokens
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.documents.tests.factories import DocumentTypeFactory
from apps.feedback.tests.factories import FeedbackFormFactory, FeedbackQuestionFactory, FeedbackResponseFactory
from apps.students.models import AccountAccess, ParentInvite
from apps.students.tests.factories import ParentInviteFactory, StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.mark.django_db
def test_parent_profile_uses_strict_dst_expiry_boundary_for_active_subscription(club, parent_user):
    club.timezone = "America/New_York"
    club.save(update_fields=["timezone"])
    child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
    tariff = TariffFactory(club=club, trainings_limit=4)
    expiry = club_local_day_start(club, date(2027, 3, 16))
    SubscriptionFactory(
        club=club,
        student=child,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=expiry,
    )

    with patch("apps.students.parent_selectors.timezone.now", return_value=expiry - timedelta(seconds=1)):
        last_valid = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
    with patch("apps.students.parent_selectors.timezone.now", return_value=expiry):
        expiry_day = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

    assert last_valid.status_code == expiry_day.status_code == 200
    assert last_valid.json()["active_subscription"]["status"] == Subscription.Status.ACTIVE
    assert expiry_day.json()["active_subscription"] is None


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestListMyChildren:
    def test_parent_can_list_children(self, club, parent_user):
        child1 = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        child2 = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        # Another child not linked to this parent
        StudentFactory(club=club, is_child=True)

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        ids = {item["id"] for item in data}
        assert ids == {child1.id, child2.id}

    def test_parent_children_are_returned_in_stable_name_order(self, club, parent_user):
        z_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            first_name="Zoya",
            last_name="Beta",
        )
        a_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            first_name="Anna",
            last_name="Alpha",
        )
        m_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            first_name="Misha",
            last_name="Beta",
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [a_child.id, m_child.id, z_child.id]

    def test_child_summary_exposes_status_and_last_visit_date(self, club, parent_user):
        child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            status="active",
            last_visit_date=date(2026, 4, 15),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data == [
            {
                "id": child.id,
                "first_name": child.first_name,
                "last_name": child.last_name,
                "status": "active",
                "is_child": True,
                "grade_name": None,
                "subscription_remaining": None,
                "subscription_total": None,
                "subscription_status": None,
                "subscription_freeze_status": None,
                "last_visit_date": "2026-04-15",
                "next_training_day_of_week": None,
                "next_training_start_time": None,
                "next_training_group_name": None,
                "next_training_trainer_name": None,
                "next_training_is_rescheduled": None,
                "next_training_is_substitute": None,
            }
        ]

    def test_child_summary_exposes_schedule_preview_from_own_attendance(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=2,
            start_time=time(17, 30),
            end_time=time(18, 30),
            group_name="Kids Muay Thai",
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["next_training_day_of_week"] == 2
        assert data[0]["next_training_start_time"] == "17:30"
        assert data[0]["next_training_group_name"] == "Kids Muay Thai"
        assert data[0]["next_training_trainer_name"] == (
            f"{schedule.trainer.first_name} {schedule.trainer.last_name}"
        )
        assert data[0]["next_training_is_rescheduled"] is False
        assert data[0]["next_training_is_substitute"] is False

    def test_child_summary_does_not_show_club_local_yesterday_as_next_training(
        self,
        club,
        parent_user,
        monkeypatch,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        monkeypatch.setattr(
            "apps.clubs.timezones.timezone.now",
            lambda: datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
        )
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        yesterday = date(2026, 6, 28)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=yesterday.weekday(),
            start_time=time(17, 30),
            group_name="Yesterday Kids Muay Thai",
            one_time_date=yesterday,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            starts_on=yesterday,
            status=ScheduleEnrollment.Status.ACTIVE,
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["next_training_day_of_week"] is None
        assert data[0]["next_training_group_name"] is None

    def test_child_summary_skips_already_attended_today_occurrence(self, club, parent_user):
        today = timezone.localdate()
        future_date = today + timedelta(days=2)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        today_schedule = ScheduleFactory(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(10, 0),
            group_name="Today Kids Muay Thai",
            one_time_date=today,
        )
        future_schedule = ScheduleFactory(
            club=club,
            day_of_week=future_date.weekday(),
            start_time=time(17, 30),
            group_name="Future Kids Muay Thai",
            one_time_date=future_date,
        )
        for schedule, starts_on in ((today_schedule, today), (future_schedule, future_date)):
            ScheduleEnrollment.objects.create(
                club=club,
                student=child,
                schedule=schedule,
                starts_on=starts_on,
                status=ScheduleEnrollment.Status.ACTIVE,
            )
        CheckinFactory(
            club=club,
            student=child,
            schedule=today_schedule,
            training_type=today_schedule.training_type,
            date=today,
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["next_training_day_of_week"] == future_date.weekday()
        assert data[0]["next_training_start_time"] == "17:30"
        assert data[0]["next_training_group_name"] == "Future Kids Muay Thai"

    def test_child_summary_uses_next_effective_occurrence(self, club, parent_user):
        today = timezone.localdate()
        original_date = today + timedelta(days=1)
        moved_date = original_date + timedelta(days=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=original_date.weekday(),
            start_time=time(17, 30),
            group_name="Kids Muay Thai",
        )
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=schedule.training_type,
            date=today - timedelta(days=7),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=original_date,
            exception_type="rescheduled",
            new_date=moved_date,
            new_start_time=time(19, 0),
            new_end_time=time(20, 0),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["next_training_day_of_week"] == moved_date.weekday()
        assert data[0]["next_training_start_time"] == "19:00"
        assert data[0]["next_training_group_name"] == "Kids Muay Thai"
        assert data[0]["next_training_is_rescheduled"] is True
        assert data[0]["next_training_is_substitute"] is False

    def test_child_summary_ignores_soft_deleted_active_subscription(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=3,
            expires_at=timezone.now() + timedelta(days=10),
            deleted_at=timezone.now(),
        )
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=6,
            expires_at=timezone.now() + timedelta(days=5),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["subscription_remaining"] == 6
        assert data[0]["subscription_total"] == 8
        assert data[0]["subscription_status"] == Subscription.Status.ACTIVE

    def test_child_summary_marks_unlimited_active_subscription(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=None)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=None,
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["subscription_remaining"] is None
        assert data[0]["subscription_total"] is None
        assert data[0]["subscription_status"] == Subscription.Status.ACTIVE

    def test_child_summary_ignores_date_expired_active_subscription(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=4,
            expires_at=timezone.now() - timedelta(days=1),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["subscription_remaining"] is None
        assert data[0]["subscription_status"] is None

    def test_child_summary_surfaces_parent_visible_subscription_states(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=8,
            expires_at=timezone.now() + timedelta(days=20),
        )
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=timezone.now() + timedelta(days=10),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["subscription_remaining"] == 4
        assert data[0]["subscription_status"] == Subscription.Status.FROZEN
        assert data[0]["subscription_freeze_status"] is None

    def test_child_summary_surfaces_pending_freeze_request(self, club, parent_user, trainer_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=8,
            expires_at=timezone.now() + timedelta(days=30),
        )
        subscription = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=5,
            expires_at=timezone.now() + timedelta(days=20),
        )
        SubscriptionFreezeFactory(
            subscription=subscription,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert data[0]["subscription_status"] == Subscription.Status.ACTIVE
        assert data[0]["subscription_freeze_status"] == SubscriptionFreeze.FreezeStatus.PENDING

    def test_non_parent_role_denied(self, club, trainer_user):
        response = client.get("/parents/children/", **_auth_params(trainer_user, club, role="trainer"))
        assert response.status_code == 403

    def test_parent_list_excludes_non_child_student_even_if_parent_linked(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        StudentFactory(club=club, is_child=False, parent_user=parent_user)

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [child.id]

    def test_parent_list_does_not_expand_to_other_children_in_same_group(self, club, parent_user):
        own_child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_parent = UserFactory()
        other_child = StudentFactory(club=club, is_child=True, parent_user=other_parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        CheckinFactory(club=club, student=own_child, schedule=schedule, training_type=training_type)
        CheckinFactory(club=club, student=other_child, schedule=schedule, training_type=training_type)

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [own_child.id]

    def test_parent_list_excludes_soft_deleted_child(self, club, parent_user):
        visible_child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            deleted_at=timezone.now(),
        )

        response = client.get("/parents/children/", **_auth_params(parent_user, club, role="parent"))

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [visible_child.id]


@pytest.mark.django_db
class TestChildProfile:
    def test_parent_can_view_child_profile(self, club, parent_user):
        child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            status="active",
        )
        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == child.id
        assert data["status"] == "active"
        assert "attendance_count" in data
        assert "grade_progress" in data
        assert "schedule" in data

    def test_child_profile_includes_schedule(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, name="Kids Boxing")
        schedule = ScheduleFactory(club=club, training_type=training_type)
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 200
        data = response.json()
        assert "schedule" in data
        assert len(data["schedule"]) == 1
        assert data["schedule"][0]["group_name"] == schedule.group_name
        assert data["schedule"][0]["training_type_id"] == training_type.id
        assert data["schedule"][0]["training_type_name"] == "Kids Boxing"
        assert data["schedule"][0]["training_type_kind"] == training_type.kind
        assert data["schedule"][0]["one_time_date"] is None

    def test_child_profile_schedule_includes_booking_metadata_for_self_booking(
        self, club, parent_user
    ):
        today = timezone.localdate()
        target_date = today + timedelta(days=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Parent Self Booked Group",
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedule"]) == 1
        occurrence = data["schedule"][0]["upcoming_occurrences"][0]
        assert occurrence["enrollment_id"] == enrollment.id
        assert occurrence["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING

    def test_child_profile_schedule_exposes_personal_one_time_metadata(
        self, club, parent_user
    ):
        target_date = timezone.localdate() + timedelta(days=1)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(
            club=club,
            name="Personal Boxing",
            kind=TrainingType.Kind.PERSONAL,
        )
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_date.weekday(),
            one_time_date=target_date,
            group_name="Персоналка",
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedule"]) == 1
        item = data["schedule"][0]
        assert item["training_type_kind"] == TrainingType.Kind.PERSONAL
        assert item["one_time_date"] == target_date.isoformat()
        occurrence = item["upcoming_occurrences"][0]
        assert occurrence["enrollment_id"] == enrollment.id
        assert occurrence["training_type_kind"] == TrainingType.Kind.PERSONAL
        assert occurrence["one_time_date"] == target_date.isoformat()

    def test_child_profile_schedule_exposes_upcoming_exception_state(self, club, parent_user):
        today = timezone.localdate()
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        substitute = TrainerFactory(club=club, first_name="Alex", last_name="Backup")
        cancelled_date = today + timedelta(days=1)
        rescheduled_date = today + timedelta(days=2)
        substitute_date = today + timedelta(days=3)
        moved_date = rescheduled_date + timedelta(days=1)
        cancelled_schedule = ScheduleFactory(
            club=club,
            day_of_week=cancelled_date.weekday(),
            group_name="Cancelled Kids",
        )
        rescheduled_schedule = ScheduleFactory(
            club=club,
            day_of_week=rescheduled_date.weekday(),
            start_time=time(17, 0),
            end_time=time(18, 0),
            group_name="Rescheduled Kids",
        )
        substitute_schedule = ScheduleFactory(
            club=club,
            day_of_week=substitute_date.weekday(),
            group_name="Substitute Kids",
        )
        for schedule in [cancelled_schedule, rescheduled_schedule, substitute_schedule]:
            ScheduleEnrollment.objects.create(
                club=club,
                student=child,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=today,
            )
            CheckinFactory(
                club=club,
                student=child,
                schedule=schedule,
                training_type=schedule.training_type,
                date=today - timedelta(days=7),
            )
        ScheduleExceptionFactory(
            club=club,
            schedule=cancelled_schedule,
            date=cancelled_date,
            exception_type="cancelled",
            reason="Tournament",
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=rescheduled_schedule,
            date=rescheduled_date,
            exception_type="rescheduled",
            new_date=moved_date,
            new_start_time=time(19, 0),
            new_end_time=time(20, 0),
            reason="Hall busy",
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=substitute_schedule,
            date=substitute_date,
            exception_type="substitute",
            substitute_trainer=substitute,
            reason="Trainer sick",
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        schedules = {item["group_name"]: item for item in response.json()["schedule"]}
        cancelled_exception = schedules["Cancelled Kids"]["upcoming_exceptions"][0]
        assert cancelled_exception["exception_type"] == "cancelled"
        assert cancelled_exception["date"] == cancelled_date.isoformat()
        assert cancelled_exception["reason"] == "Tournament"

        rescheduled_exception = schedules["Rescheduled Kids"]["upcoming_exceptions"][0]
        assert rescheduled_exception["exception_type"] == "rescheduled"
        assert rescheduled_exception["new_date"] == moved_date.isoformat()
        assert rescheduled_exception["new_start_time"] == "19:00:00"
        assert schedules["Rescheduled Kids"]["upcoming_occurrences"][0]["is_rescheduled"] is True
        assert schedules["Rescheduled Kids"]["upcoming_occurrences"][0]["effective_date"] == moved_date.isoformat()

        substitute_exception = schedules["Substitute Kids"]["upcoming_exceptions"][0]
        assert substitute_exception["exception_type"] == "substitute"
        assert substitute_exception["substitute_trainer_name"] == "Alex Backup"
        assert schedules["Substitute Kids"]["upcoming_occurrences"][0]["is_substitute"] is True
        assert schedules["Substitute Kids"]["upcoming_occurrences"][0]["trainer_name"] == "Alex Backup"

    def test_child_profile_schedule_filters_exceptions_outside_enrollment_window(self, club, parent_user):
        today = timezone.localdate()
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        exception_date = today + timedelta(days=1)
        schedule = ScheduleFactory(club=club, day_of_week=exception_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today + timedelta(days=30),
        )
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=schedule.training_type,
            date=today - timedelta(days=7),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=exception_date,
            exception_type="cancelled",
            reason="Before enrollment",
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedule"]) == 1
        assert data["schedule"][0]["upcoming_exceptions"] == []

    def test_child_profile_schedule_keeps_frozen_enrollment_exceptions_visible(self, club, parent_user):
        today = timezone.localdate()
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        exception_date = today + timedelta(days=1)
        schedule = ScheduleFactory(club=club, day_of_week=exception_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=child,
            schedule=schedule,
            status=ScheduleEnrollment.Status.FROZEN,
            starts_on=today,
        )
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=schedule.training_type,
            date=today - timedelta(days=7),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=exception_date,
            exception_type="cancelled",
            reason="Tournament",
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedule"]) == 1
        exception = data["schedule"][0]["upcoming_exceptions"][0]
        assert exception["exception_type"] == "cancelled"
        assert exception["reason"] == "Tournament"

    def test_child_profile_schedule_keeps_legacy_checkin_exceptions_visible(self, club, parent_user):
        today = timezone.localdate()
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        exception_date = today + timedelta(days=1)
        schedule = ScheduleFactory(club=club, day_of_week=exception_date.weekday())
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=schedule.training_type,
            date=today - timedelta(days=7),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=exception_date,
            exception_type="cancelled",
            reason="Legacy visible",
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedule"]) == 1
        exception = data["schedule"][0]["upcoming_exceptions"][0]
        assert exception["exception_type"] == "cancelled"
        assert exception["reason"] == "Legacy visible"

    def test_child_profile_schedule_empty_without_checkins(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["schedule"] == []

    def test_child_profile_serializes_active_subscription_with_null_expiry(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=12)
        subscription = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_used=2,
            expires_at=None,
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["active_subscription"] == {
            "id": subscription.id,
            "tariff_id": tariff.id,
            "tariff_name": tariff.name,
            "trainings_used": 2,
            "trainings_total": 12,
            "trainings_left": subscription.trainings_left,
            "expires_at": None,
            "status": Subscription.Status.ACTIVE,
            "freeze_status": None,
            "renewal_target_tariff_id": tariff.id,
            "renewal_target_tariff_name": tariff.name,
            "renewal_target_price": f"{tariff.price:.2f}",
        }

    def test_parent_exact_renewal_rejects_client_tariff_and_debt_terms(self, settings, club, parent_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        source = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        debt = DebtFactory(
            club=club,
            student=child,
            checkin=CheckinFactory(club=club, student=child),
        )
        tariff_only = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )
        assert tariff_only.status_code == 400
        assert tariff_only.json()["code"] == "renewal_source_required"

        response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={
                "renewed_from_subscription_id": source.id,
                "idempotency_key": "parent-exact-renewal-terms",
                "tariff_id": tariff.id,
                "debt_ids": [debt.id],
            },
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "renewal_client_terms_forbidden"

    def test_parent_can_create_child_renewal_bank_payment_order(self, settings, club, parent_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )

        response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201, response.json()
        data = response.json()
        order = BankPaymentOrder.objects.for_club(club).get(id=data["id"])
        assert data["source"] == BankPaymentOrder.Source.PARENT
        assert order.student_id == child.id
        assert order.payment.payment_method == Payment.Method.ONLINE

    def test_parent_can_list_child_live_bank_payment_order_from_trainer_source_without_cancel(
        self,
        settings,
        club,
        parent_user,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=child.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.AUTHORIZED
        order.save(update_fields=["status", "updated_at"])

        response = client.get(
            f"/parents/children/{child.id}/bank-payment-orders/?status=live",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [order.id]
        assert data[0]["source"] == BankPaymentOrder.Source.TRAINER
        assert data[0]["status"] == BankPaymentOrder.Status.AUTHORIZED
        assert data[0]["can_cancel"] is False

    def test_parent_exact_order_is_child_scoped_and_non_enumerating(
        self,
        settings,
        club,
        parent_user,
        owner_user,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_child = StudentFactory(club=club, is_child=True)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=child, tariff=tariff, status=Subscription.Status.ACTIVE)
        SubscriptionFactory(club=club, student=other_child, tariff=tariff, status=Subscription.Status.ACTIVE)
        own_order = create_bank_payment_order(
            club_id=club.id,
            student_id=child.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        other_order = create_bank_payment_order(
            club_id=club.id,
            student_id=other_child.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        response = client.get(
            f"/parents/children/{child.id}/bank-payment-orders/{own_order.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        hidden_order = client.get(
            f"/parents/children/{child.id}/bank-payment-orders/{other_order.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        hidden_child = client.get(
            f"/parents/children/{other_child.id}/bank-payment-orders/{other_order.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        hidden_role = client.get(
            f"/parents/children/{child.id}/bank-payment-orders/{own_order.id}/",
            **_auth_params(student_user, club, role="student"),
        )
        cross_source_refresh = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/{own_order.id}/refresh/",
            json={},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == own_order.id
        assert data["can_pay"] is True
        assert data["can_share"] is False
        assert data["can_cancel"] is False
        assert cross_source_refresh.status_code == 200
        assert BankPaymentReconciliationAttempt.objects.for_club(club).filter(order=own_order).count() == 1
        assert hidden_order.status_code == 404
        assert hidden_order.json()["detail"] == "Not found"
        assert hidden_child.status_code == 404
        assert hidden_child.json()["detail"] == "Not found"
        assert hidden_role.status_code == 404
        assert hidden_role.json()["detail"] == "Not found"

    def test_parent_refresh_coalesces_exact_child_source_without_provider_io(
        self,
        settings,
        club,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_child = StudentFactory(club=club, is_child=True)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=child, tariff=tariff, status=Subscription.Status.ACTIVE)
        SubscriptionFactory(club=club, student=other_child, tariff=tariff, status=Subscription.Status.ACTIVE)
        own_order = create_bank_payment_order(
            club_id=club.id,
            student_id=child.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.PARENT,
            created_by_id=parent_user.id,
        )
        foreign_order = create_bank_payment_order(
            club_id=club.id,
            student_id=other_child.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.PARENT,
            created_by_id=parent_user.id,
        )

        with patch("apps.billing.payment_providers.get_payment_provider") as get_provider:
            first = client.post(
                f"/parents/children/{child.id}/bank-payment-orders/{own_order.id}/refresh/",
                json={},
                **_auth_params(parent_user, club, role="parent"),
            )
            second = client.post(
                f"/parents/children/{child.id}/bank-payment-orders/{own_order.id}/refresh/",
                json={},
                **_auth_params(parent_user, club, role="parent"),
            )
            wrong_child = client.post(
                f"/parents/children/{child.id}/bank-payment-orders/{foreign_order.id}/refresh/",
                json={},
                **_auth_params(parent_user, club, role="parent"),
            )
            missing = client.post(
                f"/parents/children/{child.id}/bank-payment-orders/999999/refresh/",
                json={},
                **_auth_params(parent_user, club, role="parent"),
            )

        assert first.status_code == second.status_code == 200
        assert BankPaymentReconciliationAttempt.objects.for_club(club).filter(order=own_order).count() == 1
        assert wrong_child.status_code == missing.status_code == 404
        assert wrong_child.json() == missing.json()
        get_provider.assert_not_called()

    def test_parent_can_cancel_child_pending_bank_payment_order(self, settings, club, parent_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        create_response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )
        order = BankPaymentOrder.objects.for_club(club).get(id=create_response.json()["id"])

        response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/{order.id}/cancel/",
            json={},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert data["status"] == BankPaymentOrder.Status.CANCELLED
        assert data["can_cancel"] is False
        assert order.status == BankPaymentOrder.Status.CANCELLED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None

    def test_parent_cannot_create_bank_payment_order_for_other_child(self, settings, club, parent_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        other_child = StudentFactory(club=club, is_child=True)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=other_child, tariff=tariff)

        response = client.post(
            f"/parents/children/{other_child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404
        assert BankPaymentOrder.objects.for_club(club).count() == 0

    def test_parent_self_service_rejects_child_debt_ids(self, settings, club, parent_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=child, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=child,
            training_type=tariff.training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=child, checkin=checkin)

        response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id, "debt_ids": [debt.id]},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "self_service_debt_payment_not_supported"
        assert BankPaymentOrder.objects.for_club(club).count() == 0

    def test_parent_self_service_rejects_tariff_without_child_subscription(
        self,
        settings,
        club,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)

        response = client.post(
            f"/parents/children/{child.id}/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "self_service_tariff_not_allowed"
        assert BankPaymentOrder.objects.for_club(club).count() == 0

    def test_child_profile_serializes_all_active_subscriptions(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        boxing_type = TrainingTypeFactory(club=club, name="Boxing", slug="boxing")
        bjj_type = TrainingTypeFactory(club=club, name="BJJ", slug="bjj")
        boxing_tariff = TariffFactory(
            club=club,
            training_type=boxing_type,
            name="Kids boxing",
            trainings_limit=8,
        )
        bjj_tariff = TariffFactory(
            club=club,
            training_type=bjj_type,
            name="Kids BJJ",
            trainings_limit=12,
        )
        boxing_sub = SubscriptionFactory(
            club=club,
            student=child,
            tariff=boxing_tariff,
            status=Subscription.Status.ACTIVE,
            trainings_used=2,
            trainings_left=6,
            expires_at=timezone.now() + timedelta(days=10),
        )
        bjj_sub = SubscriptionFactory(
            club=club,
            student=child,
            tariff=bjj_tariff,
            status=Subscription.Status.ACTIVE,
            trainings_used=9,
            trainings_left=3,
            expires_at=timezone.now() + timedelta(days=20),
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data["active_subscriptions"]] == [
            bjj_sub.id,
            boxing_sub.id,
        ]
        assert data["active_subscription"] == data["active_subscriptions"][0]
        assert {
            item["tariff_name"]: item["trainings_left"]
            for item in data["active_subscriptions"]
        } == {
            "Kids BJJ": 3,
            "Kids boxing": 6,
        }

    def test_child_profile_serializes_parent_visible_subscription_states(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        now = timezone.now()
        active = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=5,
            expires_at=now + timedelta(days=20),
        )
        SubscriptionFreezeFactory(
            subscription=active,
            frozen_by=UserFactory(),
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )
        frozen = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=now + timedelta(days=10),
        )
        pending = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            trainings_left=8,
            expires_at=now + timedelta(days=30),
        )
        recent_expired = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
            trainings_left=0,
            expires_at=now - timedelta(days=7),
        )
        cancelled = SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.CANCELLED,
            trainings_left=3,
            expires_at=now + timedelta(days=15),
        )
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
            trainings_left=0,
            expires_at=now - timedelta(days=120),
        )
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.FROZEN,
            deleted_at=now,
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data["active_subscriptions"]] == [
            active.id,
            frozen.id,
            pending.id,
            recent_expired.id,
            cancelled.id,
        ]
        assert [item["status"] for item in data["active_subscriptions"]] == [
            Subscription.Status.ACTIVE,
            Subscription.Status.FROZEN,
            Subscription.Status.PENDING,
            Subscription.Status.EXPIRED,
            Subscription.Status.CANCELLED,
        ]
        assert data["active_subscription"]["id"] == active.id
        assert data["active_subscription"]["freeze_status"] == SubscriptionFreeze.FreezeStatus.PENDING

    def test_child_profile_hides_old_expired_subscriptions_when_no_current_state(
        self,
        club,
        parent_user,
    ):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        tariff = TariffFactory(club=club, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
            trainings_left=0,
            expires_at=timezone.now() - timedelta(days=120),
        )
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.EXPIRED,
            trainings_left=0,
            expires_at=timezone.now() - timedelta(days=180),
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["active_subscriptions"] == []
        assert data["active_subscription"] is None

    def test_child_profile_serializes_open_debts(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        training_type = TrainingTypeFactory(club=club, name="Kids Muay Thai")
        schedule = ScheduleFactory(club=club, training_type=training_type)
        checkin = CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            date=date(2026, 3, 1),
            is_debt=True,
        )
        DebtFactory(
            club=club,
            student=child,
            checkin=checkin,
            tariff_price=Decimal("1200"),
            reason="no_subscription",
        )
        other_checkin = CheckinFactory(
            club=club,
            student=other_child,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
        )
        DebtFactory(
            club=club,
            student=other_child,
            checkin=other_checkin,
            tariff_price=Decimal("900"),
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["open_debts"]) == 1
        debt = data["open_debts"][0]
        assert set(debt) == {
            "id",
            "checkin_id",
            "tariff_price",
            "reason",
            "training_type_name",
            "checkin_date",
            "created_at",
        }
        assert debt["checkin_id"] == checkin.id
        assert Decimal(debt["tariff_price"]) == Decimal("1200")
        assert debt["reason"] == "no_subscription"
        assert debt["training_type_name"] == "Kids Muay Thai"
        assert debt["checkin_date"] == "2026-03-01"
        assert debt["created_at"]

    def test_child_profile_includes_parent_safe_document_checklist(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        DocumentTypeFactory(club=club, name="Base Contract", scope="all")
        DocumentTypeFactory(club=club, name="Parent Consent", scope="children")
        DocumentTypeFactory(club=club, name="Adult Waiver", scope="adults")

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        names = [item["document_type"]["name"] for item in response.json()["document_checklist"]]
        assert names == ["Base Contract", "Parent Consent"]

    def test_child_profile_attendance_count_ignores_soft_deleted_checkins(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(club=club, student=child, schedule=schedule, training_type=training_type)
        CheckinFactory(
            club=club,
            student=child,
            schedule=ScheduleFactory(club=club),
            training_type=training_type,
            deleted_at=timezone.now(),
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json()["attendance_count"] == 1

    def test_parent_cannot_view_others_child(self, club, parent_user):
        other_child = StudentFactory(club=club, is_child=True)  # no parent_user
        response = client.get(
            f"/parents/children/{other_child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 404

    def test_parent_cannot_view_other_parent_child_in_same_group(self, club, parent_user):
        own_child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_parent = UserFactory()
        other_child = StudentFactory(club=club, is_child=True, parent_user=other_parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(club=club, student=own_child, schedule=schedule, training_type=training_type)
        CheckinFactory(club=club, student=other_child, schedule=schedule, training_type=training_type)

        response = client.get(
            f"/parents/children/{other_child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_view_non_child_student_even_if_parent_linked(self, club, parent_user):
        adult_student = StudentFactory(club=club, is_child=False, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{adult_student.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_view_soft_deleted_child(self, club, parent_user):
        deleted_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            deleted_at=timezone.now(),
        )

        response = client.get(
            f"/parents/children/{deleted_child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_invalid_child_profile_returns_404(self, club, parent_user):
        response = client.get(
            "/parents/children/999999/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestChildAttendance:
    def test_child_attendance_returns_empty_list(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{child.id}/attendance/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_child_attendance_uses_actual_checkin_trainer(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        substitute = TrainerFactory(club=club, first_name="Alex", last_name="Backup")
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            trainer=substitute,
        )

        response = client.get(
            f"/parents/children/{child.id}/attendance/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json()[0]["trainer_name"] == "Alex Backup"

    def test_child_attendance_supports_bounded_limit_and_offset(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            date=date(2026, 4, 1),
        )
        expected = CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            date=date(2026, 4, 2),
        )
        CheckinFactory(
            club=club,
            student=child,
            schedule=schedule,
            training_type=training_type,
            date=date(2026, 4, 3),
        )

        response = client.get(
            f"/parents/children/{child.id}/attendance/?limit=1&offset=1",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["date"] == expected.date.isoformat()

    @pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
    def test_child_attendance_validates_pagination(self, club, parent_user, query):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{child.id}/attendance/?{query}",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 400

    def test_parent_cannot_view_other_parent_child_attendance_in_same_group(self, club, parent_user):
        own_child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        other_parent = UserFactory()
        other_child = StudentFactory(club=club, is_child=True, parent_user=other_parent)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(club=club, student=own_child, schedule=schedule, training_type=training_type)
        CheckinFactory(club=club, student=other_child, schedule=schedule, training_type=training_type)

        response = client.get(
            f"/parents/children/{other_child.id}/attendance/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_view_non_child_student_attendance_even_if_parent_linked(self, club, parent_user):
        adult_student = StudentFactory(club=club, is_child=False, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{adult_student.id}/attendance/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_view_soft_deleted_child_attendance(self, club, parent_user):
        deleted_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            deleted_at=timezone.now(),
        )

        response = client.get(
            f"/parents/children/{deleted_child.id}/attendance/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestParentChildFeedback:
    def test_parent_gets_active_feedback_form_for_own_child(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(
            club=club,
            form=form,
            question_type="yes_no",
            order=1,
            is_required=True,
        )

        response = client.get(
            f"/parents/children/{child.id}/feedback/form/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == form.id
        assert data["questions"][0]["id"] == question.id

    def test_parent_gets_null_when_no_feedback_form(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)

        response = client.get(
            f"/parents/children/{child.id}/feedback/form/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json() is None

    def test_parent_submits_feedback_for_own_child(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        rating = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)
        text = FeedbackQuestionFactory(club=club, form=form, question_type="text", order=2)

        response = client.post(
            f"/parents/children/{child.id}/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [
                    {"question_id": rating.id, "rating_value": 5},
                    {"question_id": text.id, "text_value": "Спасибо"},
                ],
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        data = response.json()
        assert response.status_code == 201
        assert data["student_id"] == child.id
        assert data["already_submitted"] is False

    def test_parent_duplicate_feedback_returns_existing_response(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        existing = FeedbackResponseFactory(club=club, form=form, student=child)

        response = client.post(
            f"/parents/children/{child.id}/feedback/submit/",
            json={"form_id": form.id, "answers": []},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == existing.id
        assert data["already_submitted"] is True

    def test_parent_duplicate_feedback_still_rejects_foreign_question_id(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, order=1)
        FeedbackResponseFactory(club=club, form=form, student=child)

        response = client.post(
            f"/parents/children/{child.id}/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": foreign_question.id, "rating_value": 5}],
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_question"

    def test_parent_cannot_access_other_parent_child_feedback(self, club, parent_user):
        other_parent = UserFactory()
        other_child = StudentFactory(club=club, is_child=True, parent_user=other_parent)

        response = client.get(
            f"/parents/children/{other_child.id}/feedback/form/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_submit_other_club_child_feedback(self, club, other_club, parent_user):
        other_child = StudentFactory(club=other_club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)

        response = client.post(
            f"/parents/children/{other_child.id}/feedback/submit/",
            json={"form_id": form.id, "answers": []},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_submit_deleted_child_feedback(self, club, parent_user):
        deleted_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            deleted_at=timezone.now(),
        )
        form = FeedbackFormFactory(club=club, is_active=True)

        response = client.post(
            f"/parents/children/{deleted_child.id}/feedback/submit/",
            json={"form_id": form.id, "answers": []},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_feedback_rejects_foreign_question_id(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, order=1)

        response = client.post(
            f"/parents/children/{child.id}/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": foreign_question.id, "rating_value": 5}],
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_question"


@pytest.mark.django_db
class TestCreateInvite:
    def test_owner_can_create_invite(self, club, owner_user):
        child = StudentFactory(club=club, is_child=True)
        response = client.post(
            "/parents/invite/",
            json={"student_id": child.id},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert "token" in data
        assert data["student_id"] == child.id
        invite = ParentInvite.objects.for_club(club).get(student=child)
        assert data["token"] == str(invite.token)

    def test_admin_can_create_invite(self, club, admin_user):
        child = StudentFactory(club=club, is_child=True)

        response = client.post(
            "/parents/invite/",
            json={"student_id": child.id},
            **_auth_params(admin_user, club, role="admin"),
        )

        assert response.status_code == 201
        assert response.json()["student_id"] == child.id

    def test_trainer_cannot_create_invite(self, club, trainer_user):
        child = StudentFactory(club=club, is_child=True)

        response = client.post(
            "/parents/invite/",
            json={"student_id": child.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not ParentInvite.objects.for_club(club).filter(student=child).exists()

    def test_parent_cannot_create_invite(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True)
        response = client.post(
            "/parents/invite/",
            json={"student_id": child.id},
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 403

    def test_owner_cannot_create_invite_for_foreign_child(self, club, other_club, owner_user):
        foreign_child = StudentFactory(club=other_club, is_child=True)

        response = client.post(
            "/parents/invite/",
            json={"student_id": foreign_child.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 404

    def test_owner_cannot_create_invite_for_adult_student(self, club, owner_user):
        adult = StudentFactory(club=club, is_child=False)

        response = client.post(
            "/parents/invite/",
            json={"student_id": adult.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "not_child"


@pytest.mark.django_db
class TestAcceptInvite:
    def test_accept_invite_binds_parent(self, club):
        child = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=child)
        new_user = UserFactory()

        # For accept-invite, user is authenticated but not necessarily in a club.
        # We simulate raw JWT auth by passing auth dict + dummy club/membership.
        other_club = ClubFactory()
        response = client.post(
            "/parents/accept-invite/",
            json={"token": str(invite.token)},
            **_auth_params(new_user, other_club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["student_id"] == child.id
        assert data["club_id"] == club.id
        assert ClubMembership.objects.filter(user=new_user, club=club, role="parent").exists()
        assert not AccountAccess.objects.for_club(club).filter(student=child).exists()

    def test_accept_invite_returns_access_token_for_accepted_parent_club(self, club, monkeypatch):
        child = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=child)
        new_user = UserFactory()
        other_club = ClubFactory()
        api_client = TestClient(api)
        api_client.cookies[auth_tokens.REFRESH_COOKIE_NAME] = "current-refresh-token"
        captured = {}

        def fake_switch_refresh_token_membership(*, refresh_token, user_id, club_id, role):
            captured.update(
                {
                    "refresh_token": refresh_token,
                    "user_id": user_id,
                    "club_id": club_id,
                    "role": role,
                }
            )
            return "parent-access-token", "next-refresh-token"

        monkeypatch.setattr(
            auth_tokens,
            "switch_refresh_token_membership",
            fake_switch_refresh_token_membership,
        )

        response = api_client.post(
            "/parents/accept-invite/",
            json={"token": str(invite.token)},
            **_auth_params(new_user, other_club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["student_id"] == child.id
        assert data["club_id"] == club.id
        assert data["access_token"] == "parent-access-token"
        assert response.cookies[auth_tokens.REFRESH_COOKIE_NAME].value == "next-refresh-token"
        assert captured == {
            "refresh_token": "current-refresh-token",
            "user_id": new_user.id,
            "club_id": club.id,
            "role": ClubMembership.Role.PARENT,
        }

    def test_accept_invite_allows_authenticated_user_without_current_club(self, club):
        child = StudentFactory(club=club, is_child=True)
        invite = ParentInviteFactory(club=club, student=child)
        new_user = UserFactory()

        response = client.post(
            "/parents/accept-invite/",
            json={"token": str(invite.token)},
            user=new_user,
            auth={"user_id": new_user.id},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["student_id"] == child.id
        assert data["club_id"] == club.id
        assert ClubMembership.objects.filter(user=new_user, club=club, role="parent").exists()


@pytest.mark.django_db
class TestParentAPITenantIsolation:
    def test_parent_sees_only_own_club_children(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        parent = UserFactory()
        ClubMembershipFactory(user=parent, club=club_a, role="parent")

        StudentFactory(club=club_a, is_child=True, parent_user=parent)
        StudentFactory(club=club_b, is_child=True, parent_user=parent)

        response = client.get("/parents/children/", **_auth_params(parent, club_a, role="parent"))
        assert response.status_code == 200
        assert len(response.json()) == 1
