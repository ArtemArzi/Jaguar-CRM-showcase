from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.utils import timezone

import apps.leads.service_modules.ownership as ownership_lifecycle
from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment, ScheduleException
from apps.attendance.selectors import get_expected_student_ids_for_schedule_date
from apps.attendance.services import create_checkin
from apps.attendance.tests.factories import CheckinFactory, ScheduleExceptionFactory, ScheduleFactory
from apps.billing.models import Payment, Subscription, TrainingType
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.leads.selectors import get_lead_funnel_stats, get_leads
from apps.leads.services import (
    book_trial,
    complete_booked_trial_after_checkin,
    convert_lead,
    convert_lead_after_subscription_payment,
    convert_lead_for_manual_operational_admission,
    create_lead,
    lose_lead,
    record_contact_outcome,
    restore_lead_after_terminal_personal_payment,
    snooze_lead_for_pending_personal_payment,
    update_lead_status,
)
from apps.leads.tests.factories import LeadFactory
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("student_status", "lead_status", "task_type"),
    [
        (
            Student.Status.TRIAL,
            Student.LeadStatus.TRIAL_BOOKED,
            RetentionTask.TaskType.NEW_LEAD,
        ),
        (
            Student.Status.ACTIVE,
            Student.LeadStatus.TRIAL_DONE,
            RetentionTask.TaskType.POST_TRIAL,
        ),
    ],
)
def test_pending_personal_payment_preserves_active_trial_lead_provenance(
    club,
    student_status,
    lead_status,
    task_type,
):
    trainer = TrainerFactory(club=club)
    student = StudentFactory(
        club=club,
        status=student_status,
        lead_status=lead_status,
        assigned_trainer=trainer,
    )
    task = RetentionTask.objects.create(
        club=club,
        student=student,
        trainer=trainer,
        task_type=task_type,
        level="",
        due_date=timezone.localdate() + timedelta(days=2),
    )

    snooze_lead_for_pending_personal_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=987654,
        actor_user_id=None,
    )
    task.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.SNOOZED

    restore_lead_after_terminal_personal_payment(
        club_id=club.id,
        student_id=student.id,
        payment_id=987654,
        actor_user_id=None,
        outcome="rejected_before_attendance",
    )
    task.refresh_from_db()
    student.refresh_from_db()
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert student.status == student_status
    assert student.lead_status == lead_status


@pytest.mark.django_db
class TestCreateLead:
    def test_create_lead(self, club):
        student = create_lead(
            club_id=club.id,
            first_name="Ivan",
            last_name="Petrov",
            phone="+79001234567",
        )
        assert student.status == Student.Status.LEAD
        assert student.lead_status == Student.LeadStatus.NEW
        assert student.first_name == "Ivan"
        assert student.club_id == club.id

    def test_create_lead_keeps_optional_date_of_birth_for_child_identity(self, club):
        student = create_lead(
            club_id=club.id,
            first_name="Masha",
            phone="+79001234567",
            is_child=True,
            date_of_birth=date(2017, 5, 6),
        )

        assert student.date_of_birth == date(2017, 5, 6)

    def test_create_lead_allows_same_named_siblings_with_distinct_birth_dates(self, club):
        first = create_lead(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="",
            guardian_phone="+79001234567",
            is_child=True,
            date_of_birth=date(2017, 5, 6),
        )

        sibling = create_lead(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="",
            guardian_phone="+79001234567",
            is_child=True,
            date_of_birth=date(2018, 5, 6),
        )

        assert {first.date_of_birth, sibling.date_of_birth} == {
            date(2017, 5, 6),
            date(2018, 5, 6),
        }
        assert Student.objects.for_club(club).filter(guardian_phone="+79001234567").count() == 2

    def test_create_lead_duplicate_phone(self, club):
        create_lead(club_id=club.id, first_name="A", phone="+79001234567")
        with pytest.raises(BusinessLogicError, match="already exists"):
            create_lead(club_id=club.id, first_name="B", phone="+79001234567")

    def test_create_lead_rejects_soft_deleted_exact_identity_without_recreating_it(self, club):
        deleted = StudentFactory(club=club, phone="+79001234566")
        deleted.soft_delete()

        with pytest.raises(BusinessLogicError) as exc_info:
            create_lead(club_id=club.id, first_name="Retry", phone="+79001234566")

        assert exc_info.value.code == "duplicate_phone"
        assert Student.objects.for_club(club).filter(phone="+79001234566").count() == 1

    def test_create_lead_rejects_adult_phone_matching_child_guardian(self, club):
        create_lead(
            club_id=club.id,
            first_name="Child",
            phone="+79001234570",
            is_child=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_lead(club_id=club.id, first_name="Adult", phone="+79001234570")

        assert exc_info.value.code == "duplicate_phone"

    def test_create_lead_allows_second_child_on_same_guardian_phone(self, club):
        first = create_lead(
            club_id=club.id,
            first_name="Masha",
            phone="+79001234570",
            is_child=True,
        )
        second = create_lead(
            club_id=club.id,
            first_name="Petr",
            phone="+79001234570",
            is_child=True,
        )

        assert first.phone == ""
        assert second.phone == ""
        assert first.guardian_phone == "+79001234570"
        assert second.guardian_phone == "+79001234570"

    def test_create_lead_maps_same_child_without_birth_date_to_legacy_duplicate(self, club):
        create_lead(
            club_id=club.id,
            first_name="Masha",
            phone="+79001234570",
            is_child=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_lead(
                club_id=club.id,
                first_name="Masha",
                phone="+79001234570",
                is_child=True,
            )

        assert exc_info.value.code == "duplicate_phone"

    def test_create_lead_accepts_same_club_assigned_trainer(self, club):
        trainer = TrainerFactory(club=club)

        lead = create_lead(
            club_id=club.id,
            first_name="Assigned",
            phone="+79001234568",
            assigned_trainer_id=trainer.id,
        )

        assert lead.assigned_trainer_id == trainer.id
        assert not RetentionTask.objects.for_club(club).filter(
            student=lead,
            task_type=RetentionTask.TaskType.NEW_LEAD,
        ).exists()

    def test_create_lead_rejects_assigned_trainer_wrong_club(self, club, other_club):
        trainer = TrainerFactory(club=other_club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_lead(
                club_id=club.id,
                first_name="Wrong Club Trainer",
                phone="+79001234569",
                assigned_trainer_id=trainer.id,
            )

        assert exc_info.value.code == "trainer_club_mismatch"

    def test_create_lead_delegates_duplicate_arbitration_to_shared_identity_service(self, club, monkeypatch):
        from apps.leads import services

        calls = []
        original = services.resolve_staff_intake_person_identity

        def recording_arbitration(**kwargs):
            calls.append(kwargs["confirm_distinct_child"])
            return original(**kwargs)

        monkeypatch.setattr(services, "resolve_staff_intake_person_identity", recording_arbitration)

        create_lead(
            club_id=club.id,
            first_name="Shared",
            phone="8 (917) 400-20-41",
        )

        assert calls == [False]


@pytest.mark.django_db
class TestUpdateLeadStatus:
    def test_update_lead_status_valid(self, club):
        lead = LeadFactory(club=club, lead_status="new")
        updated = update_lead_status(club_id=club.id, student_id=lead.id, new_status="contacted")
        assert updated.lead_status == "contacted"

        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.STATUS_CHANGED
        assert event.old_lead_status == Student.LeadStatus.NEW
        assert event.new_lead_status == Student.LeadStatus.CONTACTED

    def test_update_lead_status_rejects_trial_booking_bypass(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_lead_status(
                club_id=club.id,
                student_id=lead.id,
                new_status=Student.LeadStatus.TRIAL_BOOKED,
            )

        lead.refresh_from_db()
        assert exc_info.value.code == "trial_booking_requires_book_trial"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.NEW
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.filter(student=lead).exists()

    def test_update_lead_status_invalid(self, club):
        lead = LeadFactory(club=club, lead_status="new")
        with pytest.raises(BusinessLogicError, match="Cannot transition"):
            update_lead_status(club_id=club.id, student_id=lead.id, new_status="trial_done")

    def test_trial_done_uses_facade_unified_journey_provider(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_BOOKED)

        with patch(
            "apps.leads.services.is_unified_client_journey_enabled",
            return_value=True,
        ):
            with pytest.raises(BusinessLogicError) as exc_info:
                update_lead_status(
                    club_id=club.id,
                    student_id=lead.id,
                    new_status=Student.LeadStatus.TRIAL_DONE,
                )

        assert exc_info.value.code == "trial_done_requires_checkin"

    @pytest.mark.django_db(transaction=True)
    def test_trainer_scope_revalidation_follows_lock_inside_atomic(self, club, monkeypatch):
        assigned_trainer = TrainerFactory(club=club)
        expected_trainer = TrainerFactory(club=club)
        lead = LeadFactory(club=club, assigned_trainer=assigned_trainer)
        observations = []
        original_get_lead_for_update = ownership_lifecycle._get_lead_for_update
        original_ensure_expected_trainer_assignment = (
            ownership_lifecycle._ensure_expected_trainer_assignment
        )

        def record_get_lead_for_update(**kwargs):
            observations.append(("lock", connection.in_atomic_block))
            return original_get_lead_for_update(**kwargs)

        def record_ensure_expected_trainer_assignment(
            student,
            *,
            required_assigned_trainer_id,
        ):
            observations.append(("scope", connection.in_atomic_block))
            return original_ensure_expected_trainer_assignment(
                student,
                required_assigned_trainer_id=required_assigned_trainer_id,
            )

        assert not connection.in_atomic_block
        monkeypatch.setattr(ownership_lifecycle, "_get_lead_for_update", record_get_lead_for_update)
        monkeypatch.setattr(
            ownership_lifecycle,
            "_ensure_expected_trainer_assignment",
            record_ensure_expected_trainer_assignment,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            update_lead_status(
                club_id=club.id,
                student_id=lead.id,
                new_status=Student.LeadStatus.CONTACTED,
                required_assigned_trainer_id=expected_trainer.id,
            )

        assert exc_info.value.code == "not_your_lead"
        assert observations == [("lock", True), ("scope", True)]


@pytest.mark.django_db
class TestUpdateLeadStatusTrialFeedback:
    def test_trial_done_triggers_feedback(self, club):
        """Transitioning to trial_done auto-schedules post-trial survey."""
        from unittest.mock import patch

        lead = LeadFactory(club=club, lead_status="trial_booked")
        with patch("apps.feedback.services.schedule_trial_feedback") as mock_feedback:
            update_lead_status(club_id=club.id, student_id=lead.id, new_status="trial_done")
            mock_feedback.assert_called_once_with(club_id=club.id, student_id=lead.id)

    def test_non_trial_done_does_not_trigger_feedback(self, club):
        """Other transitions do not trigger feedback."""
        from unittest.mock import patch

        lead = LeadFactory(club=club, lead_status="new")
        with patch("apps.feedback.services.schedule_trial_feedback") as mock_feedback:
            update_lead_status(club_id=club.id, student_id=lead.id, new_status="contacted")
            mock_feedback.assert_not_called()

    def test_trial_done_callback_runs_inside_outer_transaction_before_rollback(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_BOOKED)
        callback_observations = []

        def record_callback(*, club_id, student_id):
            callback_observations.append(
                (
                    club_id,
                    student_id,
                    connection.in_atomic_block,
                    Student.objects.get(id=student_id).lead_status,
                )
            )

        with patch(
            "apps.leads.services._trigger_trial_done_side_effects",
            side_effect=record_callback,
        ):
            with pytest.raises(RuntimeError, match="rollback outer transaction"):
                with transaction.atomic():
                    update_lead_status(
                        club_id=club.id,
                        student_id=lead.id,
                        new_status=Student.LeadStatus.TRIAL_DONE,
                    )
                    assert callback_observations == [
                        (club.id, lead.id, True, Student.LeadStatus.TRIAL_DONE),
                    ]
                    raise RuntimeError("rollback outer transaction")

        lead.refresh_from_db()
        assert lead.lead_status == Student.LeadStatus.TRIAL_BOOKED


@pytest.mark.django_db
class TestBookTrial:
    @staticmethod
    def _assert_trial_booking_untouched(lead):
        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status in {
            Student.LeadStatus.NEW,
            Student.LeadStatus.CONTACTED,
            Student.LeadStatus.THINKING,
        }
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.for_club(lead.club).filter(student=lead).exists()
        assert not LeadLifecycleEvent.objects.for_club(lead.club).filter(student=lead).exists()

    @staticmethod
    def _personal_trial_context(club):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
            trial_free=True,
        )
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer,
            lead_status=Student.LeadStatus.CONTACTED,
        )
        return lead, trainer, location, training_type

    @staticmethod
    def _future_group_context(club, *, trainer=None, days: int = 7):
        trial_day = club_localdate(club) + timedelta(days=days)
        start_time = time(10, 0)
        trial_at = timezone.make_aware(
            datetime.combine(trial_day, start_time),
            club_zoneinfo(club),
        )
        schedule_kwargs = {
            "club": club,
            "day_of_week": trial_day.weekday(),
            "start_time": start_time,
            "end_time": time(11, 0),
        }
        if trainer is not None:
            schedule_kwargs["trainer"] = trainer
        return trial_at, ScheduleFactory(**schedule_kwargs)

    @staticmethod
    def _future_personal_times(club, *, days: int = 7):
        trial_day = club_localdate(club) + timedelta(days=days)
        zone = club_zoneinfo(club)
        starts_at = timezone.make_aware(datetime.combine(trial_day, time(16, 0)), zone)
        ends_at = timezone.make_aware(datetime.combine(trial_day, time(17, 0)), zone)
        return starts_at, ends_at

    def test_group_trial_requires_schedule_and_occurrence_identity_without_mutation(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=None,
            )

        assert exc_info.value.code == "trial_schedule_required"
        self._assert_trial_booking_untouched(lead)

    @pytest.mark.parametrize(
        ("occurrence_date", "start_time", "now_local"),
        [
            (date(2026, 7, 14), time(18, 0), datetime(2026, 7, 15, 12, 0)),
            (date(2026, 7, 15), time(11, 59), datetime(2026, 7, 15, 12, 0)),
        ],
    )
    def test_group_trial_rejects_prior_or_started_club_local_occurrence_without_mutation(
        self,
        occurrence_date,
        start_time,
        now_local,
        club,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=start_time,
            end_time=(datetime.combine(occurrence_date, start_time) + timedelta(hours=1)).time(),
        )
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
        now = timezone.make_aware(now_local, club_zoneinfo(club))

        with patch("apps.leads.services.timezone.now", return_value=now):
            with pytest.raises(BusinessLogicError) as exc_info:
                book_trial(
                    club_id=club.id,
                    student_id=lead.id,
                    trial_date=None,
                    schedule_id=schedule.id,
                    occurrence_date=occurrence_date,
                )

        assert exc_info.value.code == "trial_start_not_future"
        self._assert_trial_booking_untouched(lead)

    def test_group_trial_rejects_client_time_that_differs_from_canonical_start(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        occurrence_date = date(2026, 7, 16)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.CONTACTED)
        now = timezone.make_aware(datetime(2026, 7, 15, 23, 0), club_zoneinfo(club))
        supplied_time = timezone.make_aware(
            datetime.combine(occurrence_date, time(18, 30)),
            club_zoneinfo(club),
        )

        with patch("apps.leads.services.timezone.now", return_value=now):
            with pytest.raises(BusinessLogicError) as exc_info:
                book_trial(
                    club_id=club.id,
                    student_id=lead.id,
                    trial_date=supplied_time,
                    schedule_id=schedule.id,
                    occurrence_date=occurrence_date,
                )

        assert exc_info.value.code == "trial_time_mismatch"
        self._assert_trial_booking_untouched(lead)

    def test_rescheduled_substitute_occurrence_uses_effective_slot_and_trainer(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        base_trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(club=club)
        original_date = date(2026, 7, 20)
        effective_date = date(2026, 7, 21)
        schedule = ScheduleFactory(
            club=club,
            trainer=base_trainer,
            day_of_week=original_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=original_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=effective_date,
            new_start_time=time(20, 15),
            new_end_time=time(21, 15),
        )
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=effective_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
            substitute_trainer=substitute,
        )
        substitute_lead = LeadFactory(
            club=club,
            assigned_trainer=substitute,
            lead_status=Student.LeadStatus.THINKING,
        )
        base_lead = LeadFactory(
            club=club,
            assigned_trainer=base_trainer,
            lead_status=Student.LeadStatus.THINKING,
        )
        original_date_lead = LeadFactory(
            club=club,
            assigned_trainer=substitute,
            lead_status=Student.LeadStatus.THINKING,
        )
        now = timezone.make_aware(datetime(2026, 7, 15, 12, 0), club_zoneinfo(club))
        canonical_start = timezone.make_aware(
            datetime.combine(effective_date, time(20, 15)),
            club_zoneinfo(club),
        )

        with patch("apps.leads.services.timezone.now", return_value=now):
            updated = book_trial(
                club_id=club.id,
                student_id=substitute_lead.id,
                trial_date=None,
                schedule_id=schedule.id,
                occurrence_date=effective_date,
                required_trainer_id=substitute.id,
            )
            with pytest.raises(BusinessLogicError) as base_exc:
                book_trial(
                    club_id=club.id,
                    student_id=base_lead.id,
                    trial_date=None,
                    schedule_id=schedule.id,
                    occurrence_date=effective_date,
                    required_trainer_id=base_trainer.id,
                )
            with pytest.raises(BusinessLogicError) as original_exc:
                book_trial(
                    club_id=club.id,
                    student_id=original_date_lead.id,
                    trial_date=None,
                    schedule_id=schedule.id,
                    occurrence_date=original_date,
                    required_trainer_id=substitute.id,
                )

        enrollment = ScheduleEnrollment.objects.for_club(club).get(
            student=substitute_lead,
            schedule=schedule,
        )
        assert updated.trial_date == canonical_start
        assert enrollment.starts_on == effective_date
        assert enrollment.ends_on == effective_date
        assert enrollment.trial_at == canonical_start
        assert base_exc.value.code == "schedule_trainer_mismatch"
        assert original_exc.value.code == "schedule_occurrence_not_found"
        self._assert_trial_booking_untouched(base_lead)
        self._assert_trial_booking_untouched(original_date_lead)

    def test_future_group_trial_near_club_midnight_persists_canonical_start(self, club):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        occurrence_date = date(2026, 7, 16)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=occurrence_date.weekday(),
            start_time=time(0, 5),
            end_time=time(1, 5),
        )
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
        now = timezone.make_aware(datetime(2026, 7, 15, 23, 55), club_zoneinfo(club))
        canonical_start = timezone.make_aware(
            datetime.combine(occurrence_date, time(0, 5)),
            club_zoneinfo(club),
        )

        with patch("apps.leads.services.timezone.now", return_value=now):
            updated = book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=None,
                schedule_id=schedule.id,
                occurrence_date=occurrence_date,
            )

        enrollment = ScheduleEnrollment.objects.for_club(club).get(student=lead, schedule=schedule)
        assert updated.trial_date == canonical_start
        assert enrollment.trial_at == canonical_start

    def test_personal_trial_is_rejected_before_mutation(self, club):
        lead, trainer, location, training_type = self._personal_trial_context(club)
        starts_at, ends_at = self._future_personal_times(club)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=starts_at,
                mode="personal",
                starts_at=starts_at,
                ends_at=ends_at,
                trainer_id=trainer.id,
                location_id=location.id,
                training_type_id=training_type.id,
            )

        assert exc_info.value.code == "personal_trial_not_supported"
        self._assert_trial_booking_untouched(lead)
        assert not Schedule.objects.for_club(club).filter(
            group_name__startswith="Пробная персоналка",
        ).exists()

    def test_book_trial(self, club):
        lead = LeadFactory(club=club, lead_status="new")
        trial_dt, schedule = self._future_group_context(club)
        updated = book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_dt.date(),
        )
        assert updated.status == Student.Status.TRIAL
        assert updated.lead_status == "trial_booked"
        assert updated.trial_date == trial_dt

        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.TRIAL_BOOKED
        assert event.old_lead_status == Student.LeadStatus.NEW
        assert event.new_lead_status == Student.LeadStatus.TRIAL_BOOKED

    def test_book_trial_allows_thinking_lead(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
        trial_dt, schedule = self._future_group_context(club)

        updated = book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_dt.date(),
        )

        enrollment = ScheduleEnrollment.objects.get(club=club, student=lead, schedule=schedule)
        assert updated.status == Student.Status.TRIAL
        assert updated.lead_status == Student.LeadStatus.TRIAL_BOOKED
        assert updated.trial_date == trial_dt
        assert enrollment.status == ScheduleEnrollment.Status.TRIAL
        assert enrollment.starts_on == trial_dt.date()
        assert enrollment.ends_on == trial_dt.date()
        assert enrollment.trial_at == trial_dt

    def test_book_trial_with_schedule_makes_lead_expected_roster_checkin_eligible(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)
        trial_dt, schedule = self._future_group_context(club)
        trial_date = trial_dt.date()

        updated = book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_date,
        )

        assert get_expected_student_ids_for_schedule_date(
            club=club,
            schedule_id=schedule.id,
            target_date=trial_date,
        ) == {lead.id}

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=lead.id,
                schedule_id=schedule.id,
                training_type_id=schedule.training_type_id,
                source=Checkin.Source.BATCH,
                checkin_date=trial_date,
            )

        lead.refresh_from_db()
        assert updated.status == Student.Status.TRIAL
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        assert result["created"] is True

    def test_trial_checkin_completes_booked_trial_once(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)
        trial_dt, schedule = self._future_group_context(club)

        book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_dt.date(),
        )

        with (
            patch("apps.attendance.services.async_task"),
            patch("apps.feedback.services.schedule_trial_feedback") as mock_feedback,
            patch("apps.pipelines.services.trigger_pipeline") as mock_pipeline,
        ):
            first = create_checkin(
                club_id=club.id,
                student_id=lead.id,
                schedule_id=schedule.id,
                training_type_id=schedule.training_type_id,
                source=Checkin.Source.BATCH,
                checkin_date=trial_dt.date(),
            )
            second = create_checkin(
                club_id=club.id,
                student_id=lead.id,
                schedule_id=schedule.id,
                training_type_id=schedule.training_type_id,
                source=Checkin.Source.BATCH,
                checkin_date=trial_dt.date(),
            )

        lead.refresh_from_db()
        assert first["created"] is True
        assert second["created"] is False
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        mock_feedback.assert_called_once_with(club_id=club.id, student_id=lead.id)
        mock_pipeline.assert_called_once_with(
            club_id=club.id,
            student_id=lead.id,
            pipeline_type="follow_up",
        )

    def test_completion_callback_runs_inside_outer_transaction_before_rollback(self, club):
        lead = LeadFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
        )
        target_date = club_localdate(club) + timedelta(days=3)
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=lead,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        checkin = CheckinFactory(
            club=club,
            student=lead,
            schedule=schedule,
            training_type=schedule.training_type,
            date=target_date,
        )
        callback_observations = []

        def record_callback(*, club_id, student_id):
            callback_observations.append(
                (
                    club_id,
                    student_id,
                    connection.in_atomic_block,
                    Student.objects.get(id=student_id).lead_status,
                )
            )

        with patch(
            "apps.leads.services._trigger_trial_done_side_effects",
            side_effect=record_callback,
        ):
            with pytest.raises(RuntimeError, match="rollback outer transaction"):
                with transaction.atomic():
                    assert complete_booked_trial_after_checkin(
                        club_id=club.id,
                        student_id=lead.id,
                        checkin=checkin,
                    )
                    assert callback_observations == [
                        (club.id, lead.id, True, Student.LeadStatus.TRIAL_DONE),
                    ]
                    raise RuntimeError("rollback outer transaction")

        lead.refresh_from_db()
        assert lead.lead_status == Student.LeadStatus.TRIAL_BOOKED

    def test_active_student_checkin_does_not_trigger_trial_completion(self, club):
        checkin_date = timezone.localdate()
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        with (
            patch("apps.attendance.services.async_task"),
            patch("apps.feedback.services.schedule_trial_feedback") as mock_feedback,
            patch("apps.pipelines.services.trigger_pipeline") as mock_pipeline,
        ):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=schedule.training_type_id,
                source=Checkin.Source.BATCH,
                checkin_date=checkin_date,
            )

        student.refresh_from_db()
        assert result["created"] is True
        assert student.status == Student.Status.ACTIVE
        assert student.lead_status is None
        mock_feedback.assert_not_called()
        mock_pipeline.assert_not_called()

    def test_book_trial_with_schedule_creates_trial_enrollment(self, club):
        trainer = TrainerFactory(club=club)
        lead = LeadFactory(club=club, lead_status="new")
        trial_dt, schedule = self._future_group_context(club, trainer=trainer)

        updated = book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_dt.date(),
            required_trainer_id=trainer.id,
        )

        enrollment = ScheduleEnrollment.objects.get(club=club, student=lead, schedule=schedule)
        assert updated.lead_status == "trial_booked"
        assert updated.trial_date == trial_dt
        assert enrollment.status == ScheduleEnrollment.Status.TRIAL
        assert enrollment.starts_on == trial_dt.date()
        assert enrollment.ends_on == trial_dt.date()
        assert enrollment.trial_at == trial_dt
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.LEAD_BOOKING

    def test_book_trial_required_trainer_rejects_other_trainer_schedule(self, club):
        required_trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=club)
        lead = LeadFactory(
            club=club,
            assigned_trainer=required_trainer,
            lead_status=Student.LeadStatus.THINKING,
        )
        trial_dt, schedule = self._future_group_context(club, trainer=other_trainer)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=trial_dt,
                schedule_id=schedule.id,
                occurrence_date=trial_dt.date(),
                required_trainer_id=required_trainer.id,
            )

        lead.refresh_from_db()
        assert exc_info.value.code == "schedule_trainer_mismatch"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.filter(club=club, student=lead).exists()
        assert not LeadLifecycleEvent.objects.filter(student=lead).exists()

    def test_book_trial_with_schedule_does_not_mutate_existing_open_enrollment(self, club):
        lead = LeadFactory(club=club, lead_status="new")
        trial_dt, schedule = self._future_group_context(club)
        permanent = ScheduleEnrollment.objects.create(
            club=club,
            student=lead,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=trial_dt.date(),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        book_trial(
            club_id=club.id,
            student_id=lead.id,
            trial_date=trial_dt,
            schedule_id=schedule.id,
            occurrence_date=trial_dt.date(),
        )

        permanent.refresh_from_db()
        trial_enrollment = ScheduleEnrollment.objects.get(
            club=club,
            student=lead,
            schedule=schedule,
            starts_on=trial_dt.date(),
            ends_on=trial_dt.date(),
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        assert permanent.ends_on is None
        assert permanent.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert trial_enrollment.id != permanent.id
        assert trial_enrollment.status == ScheduleEnrollment.Status.TRIAL

    def test_book_trial_rejects_schedule_date_mismatch(self, club):
        lead = LeadFactory(club=club, lead_status="new")
        trial_day = club_localdate(club) + timedelta(days=7)
        trial_dt = timezone.make_aware(
            datetime.combine(trial_day, time(18, 0)),
            club_zoneinfo(club),
        )
        schedule = ScheduleFactory(club=club, day_of_week=(trial_dt.date().weekday() + 1) % 7)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=trial_dt,
                schedule_id=schedule.id,
                occurrence_date=trial_dt.date(),
            )

        lead.refresh_from_db()
        assert exc_info.value.code == "schedule_occurrence_not_found"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == "new"
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.filter(student=lead).exists()

    def test_book_trial_rejects_wrong_club_schedule(self, club, other_club):
        lead = LeadFactory(club=club, lead_status="new")
        schedule = ScheduleFactory(club=other_club)
        occurrence_date = club_localdate(club) + timedelta(days=7)

        with pytest.raises(BusinessLogicError) as exc_info:
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=timezone.now(),
                schedule_id=schedule.id,
                occurrence_date=occurrence_date,
            )

        lead.refresh_from_db()
        assert exc_info.value.code == "schedule_club_mismatch"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == "new"
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.filter(student=lead).exists()

    def test_book_trial_invalid_status(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)
        trial_dt, schedule = self._future_group_context(club)
        with pytest.raises(BusinessLogicError, match="Cannot book trial"):
            book_trial(
                club_id=club.id,
                student_id=lead.id,
                trial_date=trial_dt,
                schedule_id=schedule.id,
                occurrence_date=trial_dt.date(),
            )


@pytest.mark.django_db
class TestConvertLead:
    def test_convert_unpaid_lead_is_rejected(self, club):
        lead = LeadFactory(club=club, lead_status="thinking")

        with pytest.raises(BusinessLogicError) as exc_info:
            convert_lead(club_id=club.id, student_id=lead.id)

        lead.refresh_from_db()
        assert exc_info.value.code == "lead_conversion_requires_paid_subscription"
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert lead.status == Student.Status.LEAD
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    def test_convert_lead_with_paid_active_subscription(self, club):
        lead = LeadFactory(club=club, lead_status="thinking")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
        SubscriptionFactory(
            tariff=tariff,
            student=lead,
            status=Subscription.Status.ACTIVE,
            paid_amount=Decimal("5000"),
        )

        converted = convert_lead(club_id=club.id, student_id=lead.id)

        assert converted.lead_status is None
        assert converted.status == Student.Status.ACTIVE
        assert converted.became_student_at is not None

        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_CONVERTED
        assert event.old_lead_status == Student.LeadStatus.THINKING
        assert event.new_lead_status == ""

    @pytest.mark.django_db(transaction=True)
    def test_conversion_finalizer_runs_inside_outer_transaction_before_rollback(self, club):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
        SubscriptionFactory(
            tariff=tariff,
            student=lead,
            status=Subscription.Status.ACTIVE,
            paid_amount=Decimal("5000"),
        )
        finalizer_observations = []

        def observe_finalizer(*, club_id: int, student_id: int) -> None:
            observed_student = Student.objects.for_club(club_id).get(id=student_id)
            finalizer_observations.append(
                (
                    connection.in_atomic_block,
                    observed_student.status,
                    observed_student.lead_status,
                    LeadLifecycleEvent.objects.for_club(club_id)
                    .filter(
                        student_id=student_id,
                        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
                    )
                    .exists(),
                )
            )

        with patch(
            "apps.leads.services._finalize_lead_conversion_side_effects",
            side_effect=observe_finalizer,
        ):
            with pytest.raises(RuntimeError, match="rollback outer transaction"):
                with transaction.atomic():
                    converted = convert_lead(club_id=club.id, student_id=lead.id)
                    assert converted.status == Student.Status.ACTIVE
                    assert finalizer_observations == [(True, Student.Status.ACTIVE, None, True)]
                    raise RuntimeError("rollback outer transaction")

        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert lead.became_student_at is None
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    def test_paid_subscription_conversion_path_activates_lead(self, club):
        lead = LeadFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )

        converted = convert_lead_after_subscription_payment(club_id=club.id, student_id=lead.id)

        lead.refresh_from_db()
        assert converted is True
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert lead.became_student_at is not None
        event = LeadLifecycleEvent.objects.for_club(club).get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_CONVERTED
        assert event.old_lead_status == Student.LeadStatus.TRIAL_DONE

    @pytest.mark.django_db(transaction=True)
    def test_paid_conversion_finalizer_runs_inside_outer_transaction_before_rollback(self, club):
        lead = LeadFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        finalizer_observations = []

        def observe_finalizer(*, club_id: int, student_id: int) -> None:
            observed_student = Student.objects.for_club(club_id).get(id=student_id)
            finalizer_observations.append(
                (
                    connection.in_atomic_block,
                    observed_student.status,
                    observed_student.lead_status,
                    LeadLifecycleEvent.objects.for_club(club_id)
                    .filter(
                        student_id=student_id,
                        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
                    )
                    .exists(),
                )
            )

        with patch(
            "apps.leads.services._finalize_lead_conversion_side_effects",
            side_effect=observe_finalizer,
        ):
            with pytest.raises(RuntimeError, match="rollback outer transaction"):
                with transaction.atomic():
                    assert convert_lead_after_subscription_payment(
                        club_id=club.id,
                        student_id=lead.id,
                    )
                    assert finalizer_observations == [(True, Student.Status.ACTIVE, None, True)]
                    raise RuntimeError("rollback outer transaction")

        lead.refresh_from_db()
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        assert lead.became_student_at is None
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    @pytest.mark.parametrize("payment_status", [Payment.Status.PENDING, Payment.Status.REJECTED])
    def test_manual_operational_admission_without_confirmed_payment_keeps_provenance_null(
        self,
        club,
        payment_status,
    ):
        lead = LeadFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        tariff = TariffFactory(club=club)
        PaymentFactory(
            club=club,
            student=lead,
            tariff=tariff,
            status=payment_status,
        )

        first = convert_lead_for_manual_operational_admission(
            club_id=club.id,
            student_id=lead.id,
        )
        second = convert_lead_for_manual_operational_admission(
            club_id=club.id,
            student_id=lead.id,
        )

        lead.refresh_from_db()
        assert first is True
        assert second is False
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert lead.became_student_at is None
        assert convert_lead_for_manual_operational_admission(
            club_id=club.id,
            student_id=lead.id,
        ) is False
        lead.refresh_from_db()
        assert lead.became_student_at is None
        events = LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        )
        assert events.count() == 1
        assert events.get().metadata["source"] == "manual_operational_admission"

    def test_confirmed_conversion_after_manual_admission_sets_provenance_once(self, club):
        lead = LeadFactory(
            club=club,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )

        assert convert_lead_for_manual_operational_admission(
            club_id=club.id,
            student_id=lead.id,
        ) is True
        lead.refresh_from_db()
        assert lead.became_student_at is None

        assert convert_lead_after_subscription_payment(
            club_id=club.id,
            student_id=lead.id,
        ) is False
        lead.refresh_from_db()
        first_became_student_at = lead.became_student_at
        assert first_became_student_at is not None

        assert convert_lead_after_subscription_payment(
            club_id=club.id,
            student_id=lead.id,
        ) is False
        lead.refresh_from_db()
        assert lead.became_student_at == first_became_student_at

    def test_convert_non_lead_raises(self, club):
        student = LeadFactory(club=club, lead_status=None, status="active")
        with pytest.raises(BusinessLogicError, match="not a lead"):
            convert_lead(club_id=club.id, student_id=student.id)


@pytest.mark.django_db
class TestLoseLead:
    def test_lose_lead(self, club):
        lead = LeadFactory(club=club, lead_status="thinking")
        lost = lose_lead(club_id=club.id, student_id=lead.id, loss_reason="expensive")
        assert lost.status == Student.Status.LOST
        assert lost.loss_reason == "expensive"
        assert lost.lead_status is None

        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_LOST
        assert event.old_lead_status == Student.LeadStatus.THINKING
        assert event.new_lead_status == ""
        assert event.reason == "expensive"

    def test_lose_lead_invalid_reason(self, club):
        lead = LeadFactory(club=club, lead_status="thinking")
        with pytest.raises(BusinessLogicError, match="Invalid loss reason"):
            lose_lead(club_id=club.id, student_id=lead.id, loss_reason="invalid_reason")


@pytest.mark.django_db(transaction=True)
def test_direct_loss_commits_before_pipeline_cancellation_failure(club):
    lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
    cancellation_observations = []

    def fail_pipeline_cancellation(*, club_id, student_id):
        student = Student.objects.for_club(club_id).get(id=student_id)
        cancellation_observations.append(
            (
                connection.in_atomic_block,
                student.status,
                student.lead_status,
                LeadLifecycleEvent.objects.for_club(club_id)
                .filter(
                    student_id=student_id,
                    event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
                )
                .exists(),
            )
        )
        raise RuntimeError("pipeline cancellation failed")

    with patch(
        "apps.pipelines.services.cancel_pipeline",
        side_effect=fail_pipeline_cancellation,
    ):
        with pytest.raises(RuntimeError, match="pipeline cancellation failed"):
            lose_lead(
                club_id=club.id,
                student_id=lead.id,
                loss_reason=Student.LossReason.TOO_FAR,
            )

    assert cancellation_observations == [
        (False, Student.Status.LOST, None, True),
    ]
    lead.refresh_from_db()
    assert lead.status == Student.Status.LOST
    assert lead.lead_status is None


@pytest.mark.django_db(transaction=True)
def test_nested_loss_cancels_pipeline_inside_outer_transaction_and_rolls_back(club):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(
        club=club,
        lead_status=Student.LeadStatus.THINKING,
        assigned_trainer=trainer,
    )
    task = RetentionTask.objects.create(
        club=club,
        student=lead,
        trainer=trainer,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        due_date=club_localdate(club),
    )
    cancellation_observations = []

    def observe_pipeline_cancellation(*, club_id, student_id):
        student = Student.objects.for_club(club_id).get(id=student_id)
        cancellation_observations.append(
            (
                connection.in_atomic_block,
                student.status,
                student.lead_status,
                RetentionTask.objects.for_club(club_id)
                .get(id=task.id)
                .resolved_at,
            )
        )
        return 0

    with patch(
        "apps.pipelines.services.cancel_pipeline",
        side_effect=observe_pipeline_cancellation,
    ):
        with pytest.raises(RuntimeError, match="rollback outer transaction"):
            with transaction.atomic():
                record_contact_outcome(
                    club_id=club.id,
                    student_id=lead.id,
                    outcome="lost",
                    loss_reason=Student.LossReason.TOO_FAR,
                )
                assert cancellation_observations == [
                    (True, Student.Status.LOST, None, None),
                ]
                raise RuntimeError("rollback outer transaction")

    lead.refresh_from_db()
    task.refresh_from_db()
    assert lead.status == Student.Status.LEAD
    assert lead.lead_status == Student.LeadStatus.THINKING
    assert task.resolved_at is None
    assert task.status == RetentionTask.TaskStatus.OPEN
    assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()


@pytest.mark.django_db
class TestSelectors:
    def test_get_leads_filters(self, club):
        trainer = TrainerFactory(club=club)
        LeadFactory(club=club, lead_status="new", assigned_trainer=trainer)
        LeadFactory(club=club, lead_status="contacted")

        all_leads = get_leads(club=club)
        assert all_leads.count() == 2

        new_only = get_leads(club=club, status="new")
        assert new_only.count() == 1

        by_trainer = get_leads(club=club, assigned_trainer_id=trainer.id)
        assert by_trainer.count() == 1

    def test_get_lead_funnel_stats(self, club):
        LeadFactory(club=club, lead_status="new")
        LeadFactory(club=club, lead_status="new")
        LeadFactory(club=club, lead_status="contacted")

        stats = get_lead_funnel_stats(club=club)
        assert stats["new"] == 2
        assert stats["contacted"] == 1
        assert stats["trial_booked"] == 0

    def test_tenant_isolation(self, club, other_club):
        LeadFactory(club=club, lead_status="new")
        LeadFactory(club=other_club, lead_status="new")

        leads = get_leads(club=club)
        assert leads.count() == 1
        assert leads.first().club_id == club.id
