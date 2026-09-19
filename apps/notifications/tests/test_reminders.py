from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment, ScheduleException
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    ClubSettingsFactory,
    UserFactory,
)
from apps.notifications.models import NotificationTemplate, SentNotification
from apps.notifications.selectors import get_students_for_training_reminder
from apps.notifications.services import get_habitual_schedules
from apps.notifications.tasks import (
    check_missed_trainings,
    send_training_reminders,
    send_training_reminders_24h,
)
from apps.notifications.tests.factories import (
    NotificationTemplateFactory,
    PushSubscriptionFactory,
)
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
class TestGetHabitualSchedules:
    def test_get_habitual_schedules_returns_frequent(self):
        """Student with 3 checkins in 4 weeks -> schedule included."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        today = timezone.now().date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i),
            )

        result = get_habitual_schedules(
            student_id=student.id, club_id=club.id
        )
        assert schedule.id in result

    def test_get_habitual_schedules_excludes_infrequent(self):
        """Student with 1 checkin in 4 weeks -> not included."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            date=timezone.now().date() - timedelta(days=7),
        )

        result = get_habitual_schedules(
            student_id=student.id, club_id=club.id
        )
        assert schedule.id not in result

    def test_get_habitual_schedules_empty_for_new_student(self):
        """New student with 0 checkins -> empty list."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")

        result = get_habitual_schedules(
            student_id=student.id, club_id=club.id
        )
        assert result == []

    def test_get_habitual_schedules_ignores_deleted_checkins(self):
        """Soft-deleted checkins are not counted."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        today = timezone.now().date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i),
                deleted_at=timezone.now(),  # soft-deleted
            )

        result = get_habitual_schedules(
            student_id=student.id, club_id=club.id
        )
        assert result == []

    def test_tenant_isolation(self):
        """Checkins from club A don't affect club B schedule detection."""
        club_a = ClubFactory()
        club_b = ClubFactory()
        student_a = StudentFactory(club=club_a, status="active")
        schedule_a = ScheduleFactory(club=club_a)

        today = timezone.now().date()
        for i in range(3):
            CheckinFactory(
                club=club_a,
                student=student_a,
                schedule=schedule_a,
                date=today - timedelta(weeks=i),
            )

        # Club B student should not see club A schedules
        student_b = StudentFactory(club=club_b, status="active")
        result = get_habitual_schedules(
            student_id=student_b.id, club_id=club_b.id
        )
        assert result == []


@pytest.mark.django_db
class TestGetStudentsForTrainingReminder:
    def test_includes_enrolled_active_and_trial_students_without_checkins(self):
        club = ClubFactory()
        target_date = timezone.now().date()
        schedule = ScheduleFactory(club=club)
        active_student = StudentFactory(club=club, status="active")
        trial_enrolled_student = StudentFactory(club=club, status="active")

        ScheduleEnrollment.objects.create(
            club=club,
            student=active_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=trial_enrolled_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=target_date,
        )

        result = get_students_for_training_reminder(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert {student.id for student in result} == {
            active_student.id,
            trial_enrolled_student.id,
        }

    def test_get_students_for_training_reminder(self):
        """Returns active students with habitual attendance."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        today = timezone.now().date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i),
            )

        result = get_students_for_training_reminder(
            club=club, schedule_id=schedule.id, target_date=today
        )
        assert len(result) == 1
        assert result[0].id == student.id

    def test_cancelled_and_ended_enrollments_suppress_legacy_checkins(self):
        club = ClubFactory()
        target_date = timezone.now().date()
        schedule = ScheduleFactory(club=club)
        cancelled_student = StudentFactory(club=club, status="active")
        ended_student = StudentFactory(club=club, status="active")

        for student in (cancelled_student, ended_student):
            for i in range(3):
                CheckinFactory(
                    club=club,
                    student=student,
                    schedule=schedule,
                    date=target_date - timedelta(weeks=i + 1),
                )
        ScheduleEnrollment.objects.create(
            club=club,
            student=cancelled_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=target_date - timedelta(days=30),
            ends_on=target_date - timedelta(days=1),
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=ended_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date - timedelta(days=30),
            ends_on=target_date - timedelta(days=1),
        )

        result = get_students_for_training_reminder(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert result == []

    def test_tenant_isolation_for_enrolled_students(self):
        club = ClubFactory()
        other_club = ClubFactory()
        target_date = timezone.now().date()
        schedule = ScheduleFactory(club=club)
        other_schedule = ScheduleFactory(club=other_club)
        own_student = StudentFactory(club=club, status="active")
        other_student = StudentFactory(club=other_club, status="active")

        ScheduleEnrollment.objects.create(
            club=club,
            student=own_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        ScheduleEnrollment.objects.create(
            club=other_club,
            student=other_student,
            schedule=other_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        result = get_students_for_training_reminder(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert [student.id for student in result] == [own_student.id]

    def test_excludes_churned_students(self):
        """Churned students are excluded from reminders."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="churned")
        schedule = ScheduleFactory(club=club)

        today = timezone.now().date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i),
            )

        result = get_students_for_training_reminder(
            club=club, schedule_id=schedule.id
        )
        assert len(result) == 0

    def test_excludes_churned_and_lost_enrolled_students(self):
        club = ClubFactory()
        target_date = timezone.now().date()
        schedule = ScheduleFactory(club=club)
        churned_student = StudentFactory(club=club, status="churned")
        lost_student = StudentFactory(club=club, status="lost")

        for student in (churned_student, lost_student):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=target_date,
            )

        result = get_students_for_training_reminder(
            club=club,
            schedule_id=schedule.id,
            target_date=target_date,
        )

        assert result == []

    def test_excludes_infrequent_attendees(self):
        """Students with < 2 checkins in 4 weeks not included."""
        club = ClubFactory()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            date=timezone.now().date() - timedelta(days=7),
        )

        result = get_students_for_training_reminder(
            club=club, schedule_id=schedule.id
        )
        assert len(result) == 0


@pytest.mark.django_db
class TestSendTrainingReminders:
    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_sends_push_for_enrolled_student_without_checkins(self, mock_async):
        club = ClubFactory(timezone="UTC")
        now = timezone.now()
        dow = now.weekday()
        target_time = (now + timedelta(minutes=60)).time()

        schedule = ScheduleFactory(
            club=club, day_of_week=dow, start_time=target_time
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=now.date(),
        )

        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder",
            title_template="{name}, скоро тренировка",
            body_template="{group} в {time}",
        )

        result = send_training_reminders()
        assert result["reminders_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_crosses_midnight_with_tomorrow_enrollment(self, mock_async):
        club = ClubFactory(timezone="Europe/Moscow")
        frozen_now = timezone.make_aware(datetime(2026, 4, 6, 23, 30))
        tomorrow = frozen_now.date() + timedelta(days=1)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=tomorrow.weekday(),
            start_time=time(0, 15),
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=tomorrow,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder",
            title_template="{name}, скоро тренировка",
            body_template="{group} в {time}",
        )

        with patch("apps.notifications.tasks.timezone.now", return_value=frozen_now):
            result = send_training_reminders()

        assert result["reminders_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_sends_push(self, mock_async):
        """Reminder sent for habitual student when schedule in 1h window."""
        club = ClubFactory(timezone="UTC")
        now = timezone.now()
        dow = now.weekday()
        target_time = (now + timedelta(minutes=60)).time()

        schedule = ScheduleFactory(
            club=club, day_of_week=dow, start_time=target_time
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)

        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder",
            title_template="{name}, скоро тренировка",
            body_template="{group} в {time}",
        )

        today = now.date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i + 1),
            )

        result = send_training_reminders()
        assert result["reminders_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_respects_quiet_hours(self, mock_async):
        """No reminders during quiet hours."""
        club = ClubFactory(timezone="UTC")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(0, 0),
            quiet_hours_end=time(23, 59),
        )

        result = send_training_reminders()
        assert result["reminders_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_respects_budget(self, mock_async):
        """No push when anti-spam budget exhausted."""
        club = ClubFactory(timezone="UTC")
        ClubSettingsFactory(club=club, max_push_per_week=0)
        now = timezone.now()
        dow = now.weekday()
        target_time = (now + timedelta(minutes=60)).time()

        schedule = ScheduleFactory(
            club=club, day_of_week=dow, start_time=target_time
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)

        NotificationTemplateFactory(
            club=club, trigger_type="training_reminder",
            body_template="test",
        )

        today = now.date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i + 1),
            )

        result = send_training_reminders()
        assert result["reminders_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_send_training_reminders_skips_new_students(self, mock_async):
        """Students with < 2 checkins not reminded."""
        club = ClubFactory(timezone="UTC")
        now = timezone.now()
        dow = now.weekday()
        target_time = (now + timedelta(minutes=60)).time()

        ScheduleFactory(
            club=club, day_of_week=dow, start_time=target_time
        )
        StudentFactory(club=club, status="active")

        NotificationTemplateFactory(
            club=club, trigger_type="training_reminder",
            body_template="test",
        )

        result = send_training_reminders()
        assert result["reminders_sent"] == 0


@pytest.mark.django_db
class TestSendTrainingReminders24h:
    @patch("apps.notifications.services.async_task")
    def test_sends_push_for_tomorrow_schedule(self, mock_async):
        """Reminder sent for habitual student with schedule tomorrow."""
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        frozen_now = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
        tomorrow = datetime(2026, 7, 16).date()
        tomorrow_dow = tomorrow.weekday()

        schedule = ScheduleFactory(
            club=club, day_of_week=tomorrow_dow, start_time=time(20, 0)
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)

        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder_24h",
            title_template="{name}, завтра тренировка",
            body_template="{group} в {time}",
        )

        today = datetime(2026, 7, 15).date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i + 1),
            )

        with (
            patch("apps.notifications.tasks.timezone.now", return_value=frozen_now),
            patch("apps.notifications.services.timezone.now", return_value=frozen_now),
        ):
            result = send_training_reminders_24h()
        assert result["reminders_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_skips_schedules_not_tomorrow(self, mock_async):
        """No reminders for schedules that aren't tomorrow."""
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        frozen_now = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
        # Use day after tomorrow
        day_after = datetime(2026, 7, 17).date()
        day_after_dow = day_after.weekday()

        schedule = ScheduleFactory(
            club=club, day_of_week=day_after_dow, start_time=time(18, 0)
        )
        student = StudentFactory(club=club, status="active")

        NotificationTemplateFactory(
            club=club, trigger_type="training_reminder_24h",
            body_template="test",
        )

        today = datetime(2026, 7, 15).date()
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=today - timedelta(weeks=i + 1),
            )

        with (
            patch("apps.notifications.tasks.timezone.now", return_value=frozen_now),
            patch("apps.notifications.services.timezone.now", return_value=frozen_now),
        ):
            result = send_training_reminders_24h()
        assert result["reminders_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_respects_club_local_quiet_hours(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(0, 0),
            quiet_hours_end=time(23, 59),
        )
        frozen_now = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
        occurrence_date = datetime(2026, 7, 16).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(20, 0),
        )
        _enroll_push_student(
            club=club,
            schedule=schedule,
            starts_on=occurrence_date,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder_24h",
        )

        with patch("apps.notifications.tasks.timezone.now", return_value=frozen_now):
            result = send_training_reminders_24h()

        assert result["reminders_sent"] == 0
        mock_async.assert_not_called()


@pytest.mark.django_db
class TestCheckMissedTrainings:
    @patch("apps.notifications.services.async_task")
    def test_check_missed_trainings_sends_push_for_trial_enrollment_without_checkins(self, mock_async):
        club = ClubFactory()
        yesterday = timezone.now().date() - timedelta(days=1)
        yesterday_dow = yesterday.weekday()

        schedule = ScheduleFactory(club=club, day_of_week=yesterday_dow)
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=yesterday,
        )

        NotificationTemplateFactory(
            club=club,
            trigger_type="missed_training",
            title_template="{name}, пропустили тренировку?",
            body_template="{group} {missed_day}",
        )

        result = check_missed_trainings()
        assert result["pushes_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_check_missed_trainings_sends_push(self, mock_async):
        """Student who missed habitual training gets push next day."""
        club = ClubFactory()
        yesterday = timezone.now().date() - timedelta(days=1)
        yesterday_dow = yesterday.weekday()

        schedule = ScheduleFactory(club=club, day_of_week=yesterday_dow)
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)

        NotificationTemplateFactory(
            club=club,
            trigger_type="missed_training",
            title_template="{name}, пропустили тренировку?",
            body_template="{group} {missed_day}",
        )

        # 3 past checkins to establish habitual pattern
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=yesterday - timedelta(weeks=i + 1),
            )
        # No checkin yesterday -- missed

        result = check_missed_trainings()
        assert result["pushes_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_check_missed_trainings_skips_present(self, mock_async):
        """Student who checked in yesterday is not pushed."""
        club = ClubFactory()
        yesterday = timezone.now().date() - timedelta(days=1)
        yesterday_dow = yesterday.weekday()

        schedule = ScheduleFactory(club=club, day_of_week=yesterday_dow)
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)

        NotificationTemplateFactory(
            club=club, trigger_type="missed_training",
            body_template="{name}",
        )

        # 3 past checkins + yesterday checkin
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=yesterday - timedelta(weeks=i + 1),
            )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            date=yesterday,
        )

        result = check_missed_trainings()
        assert result["pushes_sent"] == 0


def _enroll_push_student(*, club, schedule, starts_on):
    user = UserFactory()
    student = StudentFactory(club=club, status="active", user=user)
    ClubMembershipFactory(user=user, club=club, role="student")
    PushSubscriptionFactory(user=user)
    ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=starts_on,
    )
    return student


@pytest.mark.django_db
class TestCanonicalOccurrenceReminderLifecycle:
    @patch("apps.notifications.services.async_task")
    def test_one_hour_window_is_derived_per_club_timezone(self, mock_async):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        local_date = datetime(2026, 7, 15).date()
        yekaterinburg = ClubFactory(timezone="Asia/Yekaterinburg")
        moscow = ClubFactory(timezone="Europe/Moscow")
        yekaterinburg_schedule = ScheduleFactory(
            club=yekaterinburg,
            day_of_week=local_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Yekaterinburg 18",
        )
        moscow_schedule = ScheduleFactory(
            club=moscow,
            day_of_week=local_date.weekday(),
            start_time=time(16, 0),
            end_time=time(17, 0),
            group_name="Moscow 16",
        )
        _enroll_push_student(
            club=yekaterinburg,
            schedule=yekaterinburg_schedule,
            starts_on=local_date,
        )
        _enroll_push_student(
            club=moscow,
            schedule=moscow_schedule,
            starts_on=local_date,
        )
        for club in (yekaterinburg, moscow):
            NotificationTemplateFactory(
                club=club,
                trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
                title_template="Soon",
                body_template="{group} at {time}",
            )

        with (
            patch("apps.notifications.tasks.timezone.now", return_value=now),
            patch("apps.notifications.services.timezone.now", return_value=now),
        ):
            result = send_training_reminders()

        assert result == {"clubs_checked": 2, "reminders_sent": 2}
        bodies = {call.args[3] for call in mock_async.call_args_list}
        assert bodies == {
            "Yekaterinburg 18 at 18:00",
            "Moscow 16 at 16:00",
        }

    @patch("apps.notifications.services.async_task")
    def test_cancelled_occurrence_sends_no_reminder_or_missed_push(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        occurrence_date = datetime(2026, 7, 15).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        _enroll_push_student(club=club, schedule=schedule, starts_on=occurrence_date)
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=occurrence_date,
            exception_type=ScheduleException.ExceptionType.CANCELLED,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder_24h",
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.MISSED_TRAINING,
        )

        one_hour_now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        day_before_now = datetime(2026, 7, 14, 13, 0, tzinfo=UTC)
        day_after_now = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=one_hour_now),
            patch("apps.notifications.services.timezone.now", return_value=one_hour_now),
        ):
            one_hour = send_training_reminders()
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=day_before_now),
            patch("apps.notifications.services.timezone.now", return_value=day_before_now),
        ):
            twenty_four_hour = send_training_reminders_24h()
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=day_after_now),
            patch("apps.notifications.services.timezone.now", return_value=day_after_now),
        ):
            missed = check_missed_trainings()

        assert one_hour["reminders_sent"] == 0
        assert twenty_four_hour["reminders_sent"] == 0
        assert missed["pushes_sent"] == 0
        mock_async.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_rescheduled_occurrence_uses_effective_date_time_and_original_identity(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        original_date = datetime(2026, 7, 15).date()
        effective_date = datetime(2026, 7, 16).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=original_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Rescheduled group",
        )
        student = _enroll_push_student(
            club=club,
            schedule=schedule,
            starts_on=original_date,
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=original_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=effective_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="Soon",
            body_template="{group} at {time}",
        )

        original_now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        effective_now = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=original_now),
            patch("apps.notifications.services.timezone.now", return_value=original_now),
        ):
            original_result = send_training_reminders()
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=effective_now),
            patch("apps.notifications.services.timezone.now", return_value=effective_now),
        ):
            effective_result = send_training_reminders()

        assert original_result["reminders_sent"] == 0
        assert effective_result["reminders_sent"] == 1
        assert mock_async.call_args.args[3] == "Rescheduled group at 20:00"
        ledger = SentNotification.objects.get(student=student)
        assert ledger.occurrence_schedule_id == schedule.id
        assert ledger.occurrence_date == original_date
        assert ledger.delivery_stage == "one_hour"
        assert ledger.delivery_state == "queued"

    @patch("apps.notifications.services.async_task")
    def test_substitute_keeps_occurrence_time_and_enrolled_recipient(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        occurrence_date = datetime(2026, 7, 15).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Substitute group",
        )
        student = _enroll_push_student(
            club=club,
            schedule=schedule,
            starts_on=occurrence_date,
        )
        substitute = TrainerFactory(club=club)
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=occurrence_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
            substitute_trainer=substitute,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="Soon",
            body_template="{group} at {time}",
        )
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

        with patch("apps.notifications.tasks.timezone.now", return_value=now):
            result = send_training_reminders()

        assert result["reminders_sent"] == 1
        assert mock_async.call_args.args[3] == "Substitute group at 18:00"
        assert SentNotification.objects.filter(
            student=student,
            occurrence_schedule=schedule,
            occurrence_date=occurrence_date,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_two_same_day_occurrences_each_get_one_stage_and_replay_is_idempotent(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        occurrence_date = datetime(2026, 7, 15).date()
        first = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="First group",
        )
        second = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(18, 15),
            end_time=time(19, 15),
            group_name="Second group",
        )
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        for schedule in (first, second):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=occurrence_date,
            )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="Soon",
            body_template="{group} at {time}",
        )
        now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

        with (
            patch("apps.notifications.tasks.timezone.now", return_value=now),
            patch("apps.notifications.services.timezone.now", return_value=now),
        ):
            first_run = send_training_reminders()
            replay = send_training_reminders()

        assert first_run["reminders_sent"] == 2
        assert replay["reminders_sent"] == 0
        assert mock_async.call_count == 2
        rows = SentNotification.objects.filter(student=student).order_by("occurrence_schedule_id")
        assert rows.count() == 2
        assert {row.delivery_stage for row in rows} == {"one_hour"}
        assert {row.delivery_state for row in rows} == {"queued"}

    @patch("apps.notifications.services.async_task")
    def test_one_hour_and_twenty_four_hour_use_distinct_templates_and_ledger_stages(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        occurrence_date = datetime(2026, 7, 16).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(20, 0),
            end_time=time(21, 0),
            group_name="Stage group",
        )
        student = _enroll_push_student(
            club=club,
            schedule=schedule,
            starts_on=occurrence_date,
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
            title_template="One hour",
            body_template="one-hour {time}",
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type="training_reminder_24h",
            title_template="Tomorrow",
            body_template="twenty-four-hour {time}",
        )
        day_before_now = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
        one_hour_now = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)

        with (
            patch("apps.notifications.tasks.timezone.now", return_value=day_before_now),
            patch("apps.notifications.services.timezone.now", return_value=day_before_now),
        ):
            twenty_four = send_training_reminders_24h()
        with (
            patch("apps.notifications.tasks.timezone.now", return_value=one_hour_now),
            patch("apps.notifications.services.timezone.now", return_value=one_hour_now),
        ):
            one_hour = send_training_reminders()

        assert twenty_four["reminders_sent"] == 1
        assert one_hour["reminders_sent"] == 1
        assert [call.args[3] for call in mock_async.call_args_list] == [
            "twenty-four-hour 20:00",
            "one-hour 20:00",
        ]
        rows = SentNotification.objects.filter(student=student).order_by("delivery_stage")
        assert rows.count() == 2
        assert {row.notification_type for row in rows} == {
            "training_reminder",
            "training_reminder_24h",
        }
        assert {row.delivery_stage for row in rows} == {"one_hour", "twenty_four_hour"}

    @patch("apps.notifications.services.async_task")
    def test_missed_scan_waits_for_first_allowed_club_local_hour_and_sends_once(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        ClubSettingsFactory(
            club=club,
            quiet_hours_start=time(21, 0),
            quiet_hours_end=time(9, 0),
        )
        missed_date = datetime(2026, 7, 15).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=missed_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        _enroll_push_student(club=club, schedule=schedule, starts_on=missed_date)
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.MISSED_TRAINING,
            title_template="Missed",
            body_template="{group}",
        )
        quiet_now = datetime(2026, 7, 16, 3, 30, tzinfo=UTC)
        allowed_now = datetime(2026, 7, 16, 4, 30, tzinfo=UTC)
        replay_now = datetime(2026, 7, 16, 5, 30, tzinfo=UTC)

        results = []
        for frozen_now in (quiet_now, allowed_now, replay_now):
            with (
                patch("apps.notifications.tasks.timezone.now", return_value=frozen_now),
                patch("apps.notifications.services.timezone.now", return_value=frozen_now),
            ):
                results.append(check_missed_trainings())

        assert [result["pushes_sent"] for result in results] == [0, 1, 0]
        assert mock_async.call_count == 1

    @patch("apps.notifications.services.async_task")
    def test_cancelled_checkin_does_not_hide_a_missed_occurrence(self, mock_async):
        club = ClubFactory(timezone="Asia/Yekaterinburg")
        missed_date = datetime(2026, 7, 15).date()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=missed_date.weekday(),
            start_time=time(18, 0),
        )
        student = _enroll_push_student(
            club=club,
            schedule=schedule,
            starts_on=missed_date,
        )
        CheckinFactory(
            club=club,
            schedule=schedule,
            student=student,
            date=missed_date,
            cancelled_at=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.MISSED_TRAINING,
        )
        now = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)

        with patch("apps.notifications.tasks.timezone.now", return_value=now):
            result = check_missed_trainings()

        assert result["pushes_sent"] == 1
        assert mock_async.call_count == 1

    @patch("apps.notifications.services.async_task")
    def test_check_missed_trainings_skips_inactive_schedule(self, mock_async):
        """No push for inactive schedules."""
        club = ClubFactory()
        yesterday = timezone.now().date() - timedelta(days=1)
        yesterday_dow = yesterday.weekday()

        ScheduleFactory(
            club=club, day_of_week=yesterday_dow, is_active=False
        )

        NotificationTemplateFactory(
            club=club, trigger_type="missed_training",
            body_template="test",
        )

        result = check_missed_trainings()
        assert result["pushes_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_check_missed_trainings_suppresses_cancelled_enrollment_legacy_checkins(self, mock_async):
        club = ClubFactory()
        yesterday = timezone.now().date() - timedelta(days=1)
        yesterday_dow = yesterday.weekday()

        schedule = ScheduleFactory(club=club, day_of_week=yesterday_dow)
        user = UserFactory()
        student = StudentFactory(club=club, status="active", user=user)
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        NotificationTemplateFactory(
            club=club,
            trigger_type="missed_training",
            title_template="{name}, пропустили тренировку?",
            body_template="{group} {missed_day}",
        )
        for i in range(3):
            CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                date=yesterday - timedelta(weeks=i + 1),
            )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=yesterday - timedelta(days=30),
            ends_on=yesterday - timedelta(days=1),
        )

        result = check_missed_trainings()
        assert result["pushes_sent"] == 0
