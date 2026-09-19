"""T3 — student reactivation on checkin."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.attendance.services import create_checkin, enroll_student_in_schedule
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Subscription
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory
from apps.students.models import Student
from apps.students.services import reactivate_student
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestReactivateStudent:
    def test_churned_with_active_sub_to_active(self, club):
        student = StudentFactory(club=club, status=Student.Status.CHURNED)
        tariff = TariffFactory(club=club)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        result = reactivate_student(student_id=student.id, club_id=club.id)
        assert result is not None
        assert result.status == Student.Status.ACTIVE

    def test_churned_without_sub_to_trial(self, club):
        student = StudentFactory(club=club, status=Student.Status.CHURNED)
        result = reactivate_student(student_id=student.id, club_id=club.id)
        assert result.status == Student.Status.TRIAL

    def test_at_risk_with_active_sub_to_active(self, club):
        student = StudentFactory(club=club, status=Student.Status.AT_RISK)
        tariff = TariffFactory(club=club)
        SubscriptionFactory(
            club=club, student=student, tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        result = reactivate_student(student_id=student.id, club_id=club.id)
        assert result.status == Student.Status.ACTIVE

    def test_lost_to_trial(self, club):
        student = StudentFactory(club=club, status=Student.Status.LOST)
        result = reactivate_student(student_id=student.id, club_id=club.id)
        assert result.status == Student.Status.TRIAL

    def test_active_unchanged_returns_none(self, club):
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        result = reactivate_student(student_id=student.id, club_id=club.id)
        assert result is None
        student.refresh_from_db()
        assert student.status == Student.Status.ACTIVE

    def test_lead_unchanged(self, club):
        student = StudentFactory(club=club, status=Student.Status.LEAD)
        assert reactivate_student(student_id=student.id, club_id=club.id) is None


@pytest.mark.django_db
class TestCheckinReactivationCascade:
    def test_checkin_reactivates_churned_student(self, club):
        student = StudentFactory(club=club, status=Student.Status.CHURNED)
        tariff = TariffFactory(club=club, trainings_limit=10)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=5,
            expires_at=timezone.now() + timedelta(days=30),
        )
        today = timezone.localdate()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=today.weekday(),
            training_type=tariff.training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status="active",
            starts_on=today,
        )

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source="kiosk",
                checkin_date=today,
            )

        student.refresh_from_db()
        assert student.status == Student.Status.ACTIVE

    def test_checkin_active_student_unchanged(self, club):
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, trainings_limit=10)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=5,
            expires_at=timezone.now() + timedelta(days=30),
        )
        today = timezone.localdate()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=today.weekday(),
            training_type=tariff.training_type,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            status="active",
            starts_on=today,
        )

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source="kiosk",
                checkin_date=today,
            )

        student.refresh_from_db()
        assert student.status == Student.Status.ACTIVE
