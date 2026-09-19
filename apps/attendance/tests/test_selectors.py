from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.attendance.models import (
    PersonalDropInBooking,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
)
from apps.attendance.selectors import (
    compute_checkin_alerts,
    get_session_detail,
    get_student_attendance_summary,
    get_students_for_schedule,
    lookup_by_phone_suffix,
)
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
)
from apps.billing.models import Payment, Subscription
from apps.billing.services import create_payment
from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubSettingsFactory, UserFactory
from apps.clubs.timezones import club_zoneinfo
from apps.grades.tests.factories import GradeFactory, GradeSystemFactory, StudentGradeFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


def _next_monday() -> date:
    today = timezone.localdate() + timedelta(days=7)
    return today + timedelta(days=(-today.weekday()) % 7)


@pytest.mark.django_db
class TestScheduleEnrollmentActiveOnDatePredicate:
    def test_public_predicate_matches_effective_membership_boundaries(self, club):
        from apps.attendance.selectors import schedule_enrollment_active_on_date_q

        target_date = date(2026, 7, 16)
        schedule = ScheduleFactory(club=club)
        expected_student_ids = set()
        cases = [
            (ScheduleEnrollment.Status.ACTIVE, None, None, True),
            (ScheduleEnrollment.Status.TRIAL, target_date, target_date, True),
            (ScheduleEnrollment.Status.FROZEN, None, None, True),
            (
                ScheduleEnrollment.Status.TRANSFERRED,
                target_date - timedelta(days=2),
                target_date,
                True,
            ),
            (ScheduleEnrollment.Status.ACTIVE, target_date + timedelta(days=1), None, False),
            (ScheduleEnrollment.Status.ACTIVE, None, target_date - timedelta(days=1), False),
            (
                ScheduleEnrollment.Status.CANCELLED,
                target_date,
                target_date,
                False,
            ),
        ]
        for status, starts_on, ends_on, expected in cases:
            student = StudentFactory(club=club)
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=status,
                starts_on=starts_on,
                ends_on=ends_on,
                created_from=(
                    ScheduleEnrollment.CreatedFrom.LEAD_BOOKING
                    if status == ScheduleEnrollment.Status.CANCELLED
                    else ScheduleEnrollment.CreatedFrom.MANUAL
                ),
            )
            if expected:
                expected_student_ids.add(student.id)

        actual_student_ids = set(
            ScheduleEnrollment.objects.for_club(club)
            .filter(schedule_enrollment_active_on_date_q(target_date))
            .values_list("student_id", flat=True)
        )

        assert actual_student_ids == expected_student_ids

    def test_terminal_paid_conversion_is_excluded_from_operational_roster_only(self, club):
        from apps.attendance.selectors import (
            get_expected_student_ids_by_schedule_date,
            get_student_attendance,
            get_student_schedule_occurrences_for_range,
            schedule_enrollment_active_on_date_q,
        )

        target_date = date(2026, 7, 16)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_date,
        )

        assert not ScheduleEnrollment.objects.for_club(club).filter(
            id=enrollment.id
        ).filter(schedule_enrollment_active_on_date_q(target_date)).exists()
        assert get_expected_student_ids_by_schedule_date(
            club=club,
            schedule_ids=[schedule.id],
            target_date=target_date,
        ) == {schedule.id: set()}
        assert get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student.id,
            date_from=target_date,
            date_to=target_date,
        ) == []
        assert list(get_student_attendance(club=club, student_id=student.id))


@pytest.mark.django_db
class TestLookupByPhoneSuffix:
    def test_lookup_by_phone_suffix_returns_matches(self, club):
        student = StudentFactory(club=club, phone="+79001234567", status="active")
        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")
        assert len(results) == 1
        assert results[0]["id"] == student.id
        assert results[0]["first_name"] == student.first_name

    def test_lookup_by_phone_suffix_returns_child_by_guardian_phone(self, club):
        child = StudentFactory(
            club=club,
            phone="",
            guardian_phone="+79001234567",
            is_child=True,
            status="active",
            lead_status=None,
        )

        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")

        assert len(results) == 1
        assert results[0]["id"] == child.id
        assert results[0]["lookup_suffix"] == "4567"
        assert results[0]["lookup_suffixes"] == ["4567"]
        assert results[0]["masked_phone"] == "+***4567"

    def test_lookup_by_shared_guardian_phone_returns_both_children_with_distinct_groups(self, club):
        first = StudentFactory(
            club=club,
            phone="",
            guardian_phone="+79001234567",
            is_child=True,
            status=Student.Status.ACTIVE,
            lead_status=None,
        )
        second = StudentFactory(
            club=club,
            phone="",
            guardian_phone="+79001234567",
            is_child=True,
            status=Student.Status.ACTIVE,
            lead_status=None,
        )
        first_group = TrainingGroupFactory(club=club, name="Дети младшие")
        second_group = TrainingGroupFactory(club=club, name="Дети старшие")
        TrainingGroupMembershipFactory(
            club=club,
            student=first,
            training_group=first_group,
            starts_on=timezone.localdate(),
        )
        TrainingGroupMembershipFactory(
            club=club,
            student=second,
            training_group=second_group,
            starts_on=timezone.localdate(),
        )

        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")

        assert {result["id"] for result in results} == {first.id, second.id}
        assert {result["group_name"] for result in results} == {
            "Дети младшие",
            "Дети старшие",
        }

    def test_lookup_by_phone_suffix_returns_guardian_suffix_when_student_has_own_phone(self, club):
        student = StudentFactory(
            club=club,
            phone="+79001112233",
            guardian_phone="+79001234567",
            is_child=True,
            status="active",
            lead_status=None,
        )

        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")

        assert len(results) == 1
        assert results[0]["id"] == student.id
        assert results[0]["lookup_suffix"] == "4567"
        assert results[0]["lookup_suffixes"] == ["2233", "4567"]
        assert results[0]["masked_phone"] == "+***4567"

    def test_lookup_by_phone_suffix_excludes_lost(self, club):
        StudentFactory(club=club, phone="+79001234567", status="lost")
        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")
        assert len(results) == 0

    def test_lookup_by_phone_suffix_tenant_isolation(self, club, other_club):
        StudentFactory(club=club, phone="+79001234567", status="active")
        StudentFactory(club=other_club, phone="+79001234567", status="active")
        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")
        assert len(results) == 1

    def test_lookup_by_phone_suffix_returns_safe_subscription_and_grade_summary(self, club):
        student = StudentFactory(club=club, phone="+79001234567", status="active")
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, name="Monthly")
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=7,
        )
        grade_system = GradeSystemFactory(club=club)
        grade = GradeFactory(club=club, grade_system=grade_system, name="Yellow belt")
        StudentGradeFactory(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
        )

        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")

        assert results == [
            {
                "id": student.id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "lookup_suffix": "4567",
                "lookup_suffixes": ["4567"],
                "masked_phone": "+***4567",
                "group_name": "",
                "grade_name": "Yellow belt",
                "subscription_name": "Monthly",
                "subscription_status": "active",
                "trainings_left": 7,
            }
        ]

    def test_lookup_by_phone_suffix_does_not_show_depleted_subscription_as_active(self, club):
        student = StudentFactory(club=club, phone="+79001234567", status="active")
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type, name="Monthly")
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=0,
        )

        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="4567")

        assert results[0]["subscription_name"] == ""
        assert results[0]["subscription_status"] == ""
        assert results[0]["trainings_left"] is None

    def test_lookup_by_phone_suffix_max_10(self, club):
        for i in range(15):
            StudentFactory(club=club, phone=f"+7900123{i:04d}", status="active")
        results = lookup_by_phone_suffix(club_id=club.id, phone_suffix="123")
        assert len(results) <= 10


@pytest.mark.django_db
class TestSessionDetailCloseState:
    def test_session_detail_blocks_close_before_effective_end_time(self, club):
        club.timezone = "Europe/Moscow"
        club.save(update_fields=["timezone"])
        session_date = date(2099, 1, 5)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=session_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        current_time = datetime(2099, 1, 5, 18, 30, tzinfo=ZoneInfo("Europe/Moscow"))

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            detail = get_session_detail(club=club, schedule_id=schedule.id, session_date=session_date)

        assert detail["can_close"] is False
        assert detail["close_block_reason"] == "session_not_finished"
        assert detail["close_allowed_at"] == datetime(2099, 1, 5, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    def test_session_detail_allows_close_after_effective_end_time(self, club):
        club.timezone = "Europe/Moscow"
        club.save(update_fields=["timezone"])
        session_date = date(2099, 1, 5)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=session_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        current_time = datetime(2099, 1, 5, 19, 1, tzinfo=ZoneInfo("Europe/Moscow"))

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            detail = get_session_detail(club=club, schedule_id=schedule.id, session_date=session_date)

        assert detail["can_close"] is True
        assert detail["close_block_reason"] == ""


@pytest.mark.django_db
class TestComputeCheckinAlerts:
    def _make_checkin(self, club, student=None, is_debt=False):
        schedule = ScheduleFactory(club=club)
        training_type = schedule.club.trainingtypes.first()
        if training_type is None:
            training_type = TrainingTypeFactory(club=club)
        if student is None:
            student = StudentFactory(club=club, status="active")
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=is_debt,
        )
        return checkin, student

    def test_alert_newcomer(self, club):
        checkin, student = self._make_checkin(club)
        # Only 1 checkin total -> newcomer
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "newcomer" in alert_types

    def test_alert_debtor(self, club):
        checkin, student = self._make_checkin(club, is_debt=True)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "debtor" in alert_types

    def test_alert_contraindication(self, club):
        student = StudentFactory(club=club, status="active", contraindications="Bad knee")
        checkin, _ = self._make_checkin(club, student=student)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "contraindications" in alert_types

    def test_alert_returned_after_pause(self, club):
        student = StudentFactory(
            club=club,
            status="active",
            last_visit_date=date.today() - timedelta(days=20),
        )
        checkin, _ = self._make_checkin(club, student=student)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "returned" in alert_types

    def test_alert_expiring_subscription(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type)
        sub = SubscriptionFactory(
            student=student,
            tariff=tariff,
            club=club,
            trainings_left=2,
        )
        checkin, _ = self._make_checkin(club, student=student)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=sub)
        alert_types = [a["type"] for a in alerts]
        assert "expiring" in alert_types

    def test_schedule_students_do_not_show_last_training_for_depleted_subscription(self, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="active",
            trainings_left=0,
        )

        rows = get_students_for_schedule(club=club, schedule_id=schedule.id)

        alert_types = [alert["type"] for alert in rows[0]["alerts"]]
        assert "last_training" not in alert_types

    def test_alert_birthday(self, club):
        # Birthday within 7 days
        bday = date.today() + timedelta(days=3)
        student = StudentFactory(
            club=club,
            status="active",
            date_of_birth=bday.replace(year=bday.year - 20),
        )
        checkin, _ = self._make_checkin(club, student=student)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "birthday" in alert_types

    def test_alert_first_after_grade(self, club):
        student = StudentFactory(club=club, status="active")
        grade_system = GradeSystemFactory(club=club)
        grade = GradeFactory(grade_system=grade_system, club=club)
        StudentGradeFactory(
            student=student,
            grade_system=grade_system,
            club=club,
            current_grade=grade,
            promoted_at=timezone.now() - timedelta(days=5),
        )
        # This is the first checkin after promotion -- no other checkins exist
        checkin, _ = self._make_checkin(club, student=student)
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        alert_types = [a["type"] for a in alerts]
        assert "first_after_grade" in alert_types

    def test_no_alerts_for_normal_student(self, club):
        student = StudentFactory(club=club, status="active")
        # Create 5 previous checkins to avoid newcomer
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        for i in range(5):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                training_type=training_type,
                date=date.today() - timedelta(days=i + 1),
            )
        # Recent visit to avoid returned_after_pause
        student.last_visit_date = date.today() - timedelta(days=1)
        student.save()

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        alerts = compute_checkin_alerts(checkin=checkin, student=student, subscription=None)
        assert len(alerts) == 0


@pytest.mark.django_db
class TestEffectiveScheduleEnrollmentRows:
    def test_roster_prefers_newer_open_enrollment_over_effective_transferred_history(self, club):
        reference_date = date(2026, 7, 22)
        schedule = ScheduleFactory(club=club, day_of_week=reference_date.weekday())
        student = StudentFactory(club=club, status="active")
        transferred = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRANSFERRED,
            starts_on=reference_date - timedelta(days=14),
            ends_on=reference_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        active = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=reference_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        rows = get_students_for_schedule(
            club=club,
            schedule_id=schedule.id,
            reference_date=reference_date,
        )

        assert len(rows) == 1
        assert rows[0]["enrollment_id"] == active.id
        assert rows[0]["enrollment_status"] == ScheduleEnrollment.Status.ACTIVE
        assert rows[0]["enrollment_id"] != transferred.id


@pytest.mark.django_db
class TestPendingManualAdmissionSelector:
    def _create_admission(self, *, club):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V1,
        )
        target_date = _next_monday()
        training_type = TrainingTypeFactory(club=club, kind="group", drop_in_price=None)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_date.weekday(),
            one_time_date=None,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=3,
            duration_days=10,
        )
        student = StudentFactory(club=club, status="active")
        owner = UserFactory()
        with override_settings(
            UNIFIED_CLIENT_JOURNEY_ENABLED=True,
            MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        ), patch("django_q.tasks.async_task"):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner.id,
                target_schedule_id=schedule.id,
                target_start_date=target_date,
                create_manual_operational_admission=True,
            )
        return student, schedule, tariff, payment, target_date, owner

    def test_selector_requires_owned_enrollment_start_to_match_payment_target(self, club):
        from apps.attendance.selectors import get_locked_pending_manual_admission_payment

        student, schedule, _tariff, payment, target_date, _owner = self._create_admission(club=club)
        payment.conversion_enrollment.starts_on = target_date + timedelta(days=7)
        payment.conversion_enrollment.save(update_fields=["starts_on", "updated_at"])

        with transaction.atomic():
            result = get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=target_date,
            )

        assert result is None

    def test_selector_fails_closed_when_two_pending_owned_admissions_qualify(self, club):
        from apps.attendance.selectors import get_locked_pending_manual_admission_payment

        student, schedule, tariff, payment, target_date, owner = self._create_admission(club=club)
        duplicate_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=duplicate_subscription,
            payment_method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING,
            recorded_by=owner,
            target_schedule=schedule,
            target_start_date=target_date,
            conversion_enrollment=payment.conversion_enrollment,
            target_training_type_id_snapshot=schedule.training_type_id,
            target_location_id_snapshot=schedule.location_id,
        )

        with transaction.atomic():
            result = get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=target_date,
            )

        assert result is None
        assert payment.conversion_enrollment.conversion_payments.count() == 2


@pytest.mark.django_db
class TestClubLocalExpiryDstBoundaries:
    def test_attendance_subscription_predicate_keeps_start_and_last_day_but_excludes_dst_expiry_day(self, club):
        from apps.attendance.selectors import _subscription_active_on_date_q
        from apps.clubs.timezones import club_local_day_start, club_zoneinfo

        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        tariff = TariffFactory(club=club)
        expiry_day = date(2027, 3, 16)
        subscription = SubscriptionFactory(
            club=club,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=club_local_day_start(club, expiry_day),
        )

        for eligible_date in (date(2027, 3, 14), date(2027, 3, 15)):
            assert Subscription.objects.for_club(club).filter(
                id=subscription.id,
            ).filter(
                _subscription_active_on_date_q(eligible_date, club_tz=club_zoneinfo(club))
            ).exists()
        assert not Subscription.objects.for_club(club).filter(
            id=subscription.id,
        ).filter(
            _subscription_active_on_date_q(expiry_day, club_tz=club_zoneinfo(club))
        ).exists()


@pytest.mark.django_db
class TestStudentAttendanceSummary:
    @staticmethod
    def _event(
        *,
        membership,
        action,
        effective_date,
        status,
        created_at,
        suffix="",
    ):
        with patch("django.utils.timezone.now", return_value=created_at):
            return TrainingGroupMembershipEvent.objects.create(
                club=membership.club,
                membership=membership,
                action=action,
                effective_date=effective_date,
                previous_state_snapshot={},
                new_state_snapshot={"status": status},
                rationale="attendance summary test",
                idempotency_key=(
                    f"attendance-summary-{membership.id}-{action}-"
                    f"{effective_date.isoformat()}-{suffix}"
                ),
            )

    def _canonical_group(
        self,
        *,
        club,
        student,
        starts_on,
        event_created_at=None,
        root_action="created",
    ):
        training_group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            training_group=training_group,
            training_type=training_group.training_type,
            trainer=training_group.responsible_trainer,
            location=training_group.location,
            day_of_week=starts_on.weekday(),
            start_time=time(10, 0),
        )
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=training_group,
            starts_on=starts_on,
            status=TrainingGroupMembership.Status.ACTIVE,
        )
        created_at = event_created_at or timezone.make_aware(
            datetime.combine(starts_on, time(8, 0)),
            club_zoneinfo(club),
        )
        self._event(
            membership=membership,
            action=root_action,
            effective_date=starts_on,
            status=TrainingGroupMembership.Status.ACTIVE,
            created_at=created_at,
        )
        return schedule, membership

    @staticmethod
    def _closed_session(*, club, schedule, session_date):
        return GroupSessionFactory(
            club=club,
            schedule=schedule,
            trainer=schedule.trainer,
            date=session_date,
            closed_at=timezone.now(),
        )

    @staticmethod
    def _no_show(*, club, student, booking_date, schedule=None):
        owner = UserFactory()
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=1,
        )
        schedule = schedule or ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=booking_date.weekday(),
            one_time_date=booking_date,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=booking_date,
            ends_on=booking_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        return PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.NO_SHOW,
            created_by=owner,
            idempotency_key=f"attendance-summary-no-show-{student.id}-{schedule.id}",
        )

    def test_live_checkins_across_formats_are_attended(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        group_schedule = ScheduleFactory(club=club)
        personal_schedule = ScheduleFactory(club=club)
        for schedule in (group_schedule, personal_schedule):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                training_type=schedule.training_type,
                trainer=schedule.trainer,
                location=schedule.location,
                date=target_date,
            )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.attended_count == 2
        assert summary.missed_count == 0
        assert summary.decided_count == 2
        assert summary.attendance_rate == 100

    def test_cancelled_checkin_on_closed_expected_session_becomes_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=7),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=schedule.training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_date,
            cancelled_at=timezone.now(),
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.attended_count == 0
        assert summary.missed_count == 1
        assert summary.attendance_rate == 0

    def test_valid_checkin_suppresses_closed_session_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=7),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=schedule.training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_date,
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.attended_count == 1
        assert summary.missed_count == 0
        assert summary.attendance_rate == 100

    def test_explicit_no_show_counts_once_and_cancelled_booking_does_not(self, club):
        student = StudentFactory(club=club)
        booking_date = timezone.localdate() - timedelta(days=1)
        no_show = self._no_show(
            club=club,
            student=student,
            booking_date=booking_date,
        )
        cancelled = self._no_show(
            club=club,
            student=student,
            booking_date=booking_date - timedelta(days=1),
        )
        cancelled.state = PersonalDropInBooking.State.CANCELLED
        cancelled.save(update_fields=["state", "updated_at"])

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert no_show.state == PersonalDropInBooking.State.NO_SHOW
        assert summary.attended_count == 0
        assert summary.missed_count == 1
        assert summary.decided_count == 1
        assert summary.attendance_rate == 0

    def test_cancelled_and_rescheduled_original_occurrences_are_not_misses(self, club):
        student = StudentFactory(club=club)
        first_date = timezone.localdate() - timedelta(days=8)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=first_date - timedelta(days=7),
        )
        second_date = first_date + timedelta(days=7)
        for session_date in (first_date, second_date):
            self._closed_session(
                club=club,
                schedule=schedule,
                session_date=session_date,
            )
        ScheduleException.objects.create(
            club=club,
            schedule=schedule,
            date=first_date,
            exception_type=ScheduleException.ExceptionType.CANCELLED,
        )
        ScheduleException.objects.create(
            club=club,
            schedule=schedule,
            date=second_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=second_date + timedelta(days=1),
            new_start_time=time(12, 0),
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 0
        assert summary.attendance_rate is None

    def test_backfill_created_after_same_day_session_does_not_create_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        event_created_at = timezone.make_aware(
            datetime.combine(target_date, time(12, 0)),
            club_zoneinfo(club),
        )
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=30),
            event_created_at=event_created_at,
            root_action="backfilled",
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 0
        assert summary.attendance_rate is None

    def test_source_linked_without_root_event_does_not_create_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        training_group = TrainingGroupFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            training_group=training_group,
            training_type=training_group.training_type,
            trainer=training_group.responsible_trainer,
            location=training_group.location,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
        )
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=training_group,
            starts_on=target_date - timedelta(days=30),
            status=TrainingGroupMembership.Status.ACTIVE,
        )
        self._event(
            membership=membership,
            action="source_linked",
            effective_date=membership.starts_on,
            status=TrainingGroupMembership.Status.ACTIVE,
            created_at=timezone.make_aware(
                datetime.combine(membership.starts_on, time(8, 0)),
                club_zoneinfo(club),
            ),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 0
        assert summary.attendance_rate is None

    def test_created_after_same_day_session_does_not_create_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=30),
            event_created_at=timezone.make_aware(
                datetime.combine(target_date, time(12, 0)),
                club_zoneinfo(club),
            ),
            root_action="created",
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 0
        assert summary.attendance_rate is None

    def test_freeze_after_same_day_session_does_not_erase_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=30),
        )
        self._event(
            membership=membership,
            action="frozen",
            effective_date=target_date,
            status=TrainingGroupMembership.Status.FROZEN,
            created_at=timezone.make_aware(
                datetime.combine(target_date, time(12, 0)),
                club_zoneinfo(club),
            ),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 1
        assert summary.attendance_rate == 0

    def test_rescheduled_new_start_time_controls_same_day_root_coverage(self, club):
        student = StudentFactory(club=club)
        original_date = timezone.localdate() - timedelta(days=2)
        rescheduled_date = original_date + timedelta(days=1)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=original_date - timedelta(days=30),
            event_created_at=timezone.make_aware(
                datetime.combine(rescheduled_date, time(11, 0)),
                club_zoneinfo(club),
            ),
        )
        ScheduleException.objects.create(
            club=club,
            schedule=schedule,
            date=original_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=rescheduled_date,
            new_start_time=time(12, 0),
        )
        self._closed_session(
            club=club,
            schedule=schedule,
            session_date=rescheduled_date,
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 1
        assert summary.attendance_rate == 0

    def test_unfreeze_after_same_day_session_does_not_create_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=30),
        )
        self._event(
            membership=membership,
            action="frozen",
            effective_date=target_date - timedelta(days=2),
            status=TrainingGroupMembership.Status.FROZEN,
            created_at=timezone.make_aware(
                datetime.combine(target_date - timedelta(days=2), time(9, 0)),
                club_zoneinfo(club),
            ),
        )
        self._event(
            membership=membership,
            action="unfrozen",
            effective_date=target_date,
            status=TrainingGroupMembership.Status.ACTIVE,
            created_at=timezone.make_aware(
                datetime.combine(target_date, time(12, 0)),
                club_zoneinfo(club),
            ),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 0

    def test_unclosed_and_frozen_sessions_do_not_create_misses(self, club):
        student = StudentFactory(club=club)
        first_date = timezone.localdate() - timedelta(days=8)
        schedule, membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=first_date - timedelta(days=7),
        )
        GroupSessionFactory(
            club=club,
            schedule=schedule,
            trainer=schedule.trainer,
            date=first_date,
            closed_at=None,
        )
        frozen_date = first_date + timedelta(days=7)
        self._event(
            membership=membership,
            action="frozen",
            effective_date=frozen_date,
            status=TrainingGroupMembership.Status.FROZEN,
            created_at=timezone.make_aware(
                datetime.combine(frozen_date, time(8, 0)),
                club_zoneinfo(club),
            ),
        )
        self._closed_session(
            club=club,
            schedule=schedule,
            session_date=frozen_date,
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.attended_count == 0
        assert summary.missed_count == 0
        assert summary.attendance_rate is None

    def test_full_membership_event_chain_preserves_only_evidenced_active_misses(self, club):
        student = StudentFactory(club=club)
        first_date = timezone.localdate() - timedelta(days=22)
        schedule, membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=first_date - timedelta(days=7),
        )
        frozen_date = first_date + timedelta(days=7)
        active_again_date = first_date + timedelta(days=14)
        self._event(
            membership=membership,
            action="frozen",
            effective_date=frozen_date,
            status=TrainingGroupMembership.Status.FROZEN,
            created_at=timezone.make_aware(
                datetime.combine(frozen_date, time(8, 0)),
                club_zoneinfo(club),
            ),
        )
        self._event(
            membership=membership,
            action="unfrozen",
            effective_date=active_again_date,
            status=TrainingGroupMembership.Status.ACTIVE,
            created_at=timezone.make_aware(
                datetime.combine(active_again_date, time(8, 0)),
                club_zoneinfo(club),
            ),
        )
        self._event(
            membership=membership,
            action="cancelled",
            effective_date=active_again_date,
            status=TrainingGroupMembership.Status.CANCELLED,
            created_at=timezone.make_aware(
                datetime.combine(active_again_date, time(12, 0)),
                club_zoneinfo(club),
            ),
        )
        membership.status = TrainingGroupMembership.Status.CANCELLED
        membership.ends_on = active_again_date
        membership.save(update_fields=["status", "ends_on", "updated_at"])
        for session_date in (
            first_date,
            frozen_date,
            active_again_date,
            active_again_date + timedelta(days=7),
        ):
            self._closed_session(
                club=club,
                schedule=schedule,
                session_date=session_date,
            )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 2
        assert summary.decided_count == 2

    def test_explicit_no_show_dedupes_with_derived_group_miss(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=7),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)
        self._no_show(
            club=club,
            student=student,
            booking_date=target_date,
            schedule=schedule,
        )

        summary = get_student_attendance_summary(club=club, student_id=student.id)

        assert summary.missed_count == 1

    def test_half_up_rounding_and_tenant_isolation(self, club, other_club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=8)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=7),
        )
        for offset in range(8):
            session_date = target_date + timedelta(days=offset)
            self._closed_session(
                club=club,
                schedule=schedule,
                session_date=session_date,
            )
            if offset == 0:
                CheckinFactory(
                    club=club,
                    student=student,
                    schedule=schedule,
                    training_type=schedule.training_type,
                    trainer=schedule.trainer,
                    location=schedule.location,
                    date=session_date,
                )
        foreign_student = StudentFactory(club=other_club)

        summary = get_student_attendance_summary(club=club, student_id=student.id)
        foreign_summary = get_student_attendance_summary(
            club=club,
            student_id=foreign_student.id,
        )

        assert summary.attended_count == 1
        assert summary.missed_count == 7
        assert summary.attendance_rate == 13
        assert foreign_summary.attended_count == 0
        assert foreign_summary.missed_count == 0
        assert foreign_summary.attendance_rate is None

    def test_query_count_does_not_grow_with_closed_sessions(self, club):
        student = StudentFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=120)
        schedule, _membership = self._canonical_group(
            club=club,
            student=student,
            starts_on=target_date - timedelta(days=1),
        )
        self._closed_session(club=club, schedule=schedule, session_date=target_date)

        with CaptureQueriesContext(connection) as one_session_queries:
            first = get_student_attendance_summary(club=club, student_id=student.id)

        for offset in range(1, 100):
            self._closed_session(
                club=club,
                schedule=schedule,
                session_date=target_date + timedelta(days=offset),
            )

        with CaptureQueriesContext(connection) as many_session_queries:
            many = get_student_attendance_summary(club=club, student_id=student.id)

        assert first.missed_count == 1
        assert many.missed_count == 100
        assert len(many_session_queries) == len(one_session_queries)
        assert len(many_session_queries) <= 8
