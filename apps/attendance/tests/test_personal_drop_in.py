from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Barrier, local
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.api import (
    _personal_drop_in_booking_payload,
    _personal_drop_in_booking_result_for_existing,
)
from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    Schedule,
    ScheduleEnrollment,
)
from apps.attendance.selectors import get_student_upcoming_personal_bookings
from apps.attendance.services import (
    PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX,
    batch_checkin,
    book_personal_drop_in,
    book_personal_session,
    cancel_checkin,
    cancel_personal_drop_in_booking,
    cancel_schedule_enrollment,
    create_checkin,
    create_personal_drop_in_bank_payment_order,
    create_personal_drop_in_payment,
    delete_exception,
    enroll_student_in_schedule,
    freeze_schedule_enrollment,
    mark_personal_drop_in_no_show,
    record_personal_drop_in_attendance_correction,
    reschedule_session,
    substitute_trainer,
    transfer_schedule_enrollment,
    unfreeze_schedule_enrollment,
)
from apps.attendance.services import drop_in as drop_in_service
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import BankPaymentOrder, Debt, Payment, Subscription, Tariff, TariffComponent, TrainingType
from apps.billing.services import (
    create_bank_payment_order,
    create_payment,
    create_subscription,
    update_tariff,
    update_training_type,
    verify_payment,
)
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.leads.services import book_trial, complete_booked_trial_after_checkin
from apps.students.api import _student_personal_booking_out
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarningAdjustment, TrainerLocation, TrainerRate
from apps.trainers.services import close_trainer_payroll_period
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


def _drop_in_context(*, club, owner_user, student_status=Student.Status.LEAD):
    trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=Decimal("1000.00"),
        trial_free=True,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("1000.00"),
        trainings_limit=1,
        duration_days=30,
        scope=Tariff.Scope.CLUB,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        scope=Tariff.Scope.CLUB,
        location=None,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=Decimal("1000.00"),
    )
    TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
    TrainerRate.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        percent=Decimal("50.00"),
    )
    student = StudentFactory(
        club=club,
        status=student_status,
        assigned_trainer=trainer if student_status == Student.Status.LEAD else None,
    )
    target_date = timezone.localtime(timezone.now(), club_zoneinfo(club)).date() + timedelta(days=7)
    starts_at = timezone.make_aware(datetime.combine(target_date, time(10, 0)), club_zoneinfo(club))
    return {
        "student": student,
        "trainer": trainer,
        "location": location,
        "training_type": training_type,
        "tariff": tariff,
        "starts_at": starts_at,
        "ends_at": starts_at + timedelta(hours=1),
        "owner_user": owner_user,
    }


def _book(context, *, idempotency_key="drop-in-test"):
    return book_personal_drop_in(
        club_id=context["student"].club_id,
        student_id=context["student"].id,
        trainer_id=context["trainer"].id,
        starts_at=context["starts_at"],
        ends_at=context["ends_at"],
        location_id=context["location"].id,
        training_type_id=context["training_type"].id,
        tariff_id=context["tariff"].id,
        actor_user_id=context["owner_user"].id,
        idempotency_key=idempotency_key,
    )


def _book_complete_v1(context, *, day_offset: int, idempotency_key: str):
    starts_at = context["starts_at"] + timedelta(days=day_offset)
    schedule = Schedule.objects.create(
        club_id=context["student"].club_id,
        day_of_week=starts_at.date().weekday(),
        start_time=starts_at.time().replace(tzinfo=None),
        end_time=(starts_at + timedelta(hours=1)).time().replace(tzinfo=None),
        group_name=f"Historical personal {day_offset}",
        trainer=context["trainer"],
        location=context["location"],
        training_type=context["training_type"],
        one_time_date=starts_at.date(),
    )
    enrollment = ScheduleEnrollment.objects.create(
        club_id=context["student"].club_id,
        student=context["student"],
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=starts_at.date(),
        ends_on=starts_at.date(),
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
    )
    booking = PersonalDropInBooking.objects.create(
        club_id=context["student"].club_id,
        enrollment=enrollment,
        tariff=context["tariff"],
        tariff_name_snapshot=context["tariff"].name,
        price_snapshot=context["tariff"].price,
        created_by=context["owner_user"],
        idempotency_key=idempotency_key,
    )
    component = context["tariff"].components.get()
    PersonalServiceTermsSnapshot.objects.create(
        club_id=context["student"].club_id,
        booking=booking,
        terms_version=PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V1,
        tariff_id_snapshot=context["tariff"].id,
        tariff_name_snapshot=context["tariff"].name,
        training_type_id_snapshot=context["training_type"].id,
        training_type_name_snapshot=context["training_type"].name,
        base_amount=context["tariff"].price,
        payable_amount=context["tariff"].price,
        duration_days=context["tariff"].duration_days,
        scope=context["tariff"].scope,
        component_name_snapshot=context["tariff"].name,
        component_training_type_id_snapshot=context["training_type"].id,
        component_training_type_name_snapshot=context["training_type"].name,
        component_entitlement_kind=component.entitlement_kind,
        component_credits_total=component.credits_total,
        component_scope=component.scope,
        tariff_trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        component_trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        component_paid_amount_basis=context["tariff"].price,
        component_unit_amount_basis=context["tariff"].price,
    )
    return booking


def _confirm_drop_in_payment(*, club, owner_user, booking, idempotency_key):
    with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
        link = create_personal_drop_in_payment(
            club_id=club.id,
            booking_id=booking.id,
            payment_method=Payment.Method.CASH,
            created_by_id=owner_user.id,
            idempotency_key=idempotency_key,
        )
        verify_payment(
            payment_id=link.payment_id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
    return Payment.objects.for_club(club).select_related("subscription").get(id=link.payment_id)


@pytest.mark.django_db
class TestPersonalDropInBooking:
    def test_complete_v1_fixture_remains_usable_by_payment_bank_checkin_debt_cancel_and_payroll(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        context = _drop_in_context(club=club, owner_user=owner_user)

        manual_booking = _book_complete_v1(
            context,
            day_offset=0,
            idempotency_key="complete-v1-manual",
        )
        manual_link = create_personal_drop_in_payment(
            club_id=club.id,
            booking_id=manual_booking.id,
            payment_method=Payment.Method.CASH,
            created_by_id=owner_user.id,
            idempotency_key="complete-v1-manual-payment",
        )
        assert manual_link.payment.amount == Decimal("1000.00")

        bank_booking = _book_complete_v1(
            context,
            day_offset=1,
            idempotency_key="complete-v1-bank",
        )
        bank_link = create_personal_drop_in_bank_payment_order(
            club_id=club.id,
            booking_id=bank_booking.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            idempotency_key="complete-v1-bank-order",
        )
        assert bank_link.bank_payment_order.amount_snapshot == Decimal("1000.00")

        visit_booking = _book_complete_v1(
            context,
            day_offset=2,
            idempotency_key="complete-v1-visit",
        )
        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            checked_in = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=visit_booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=visit_booking.enrollment.starts_on,
            )
        visit_booking.refresh_from_db()
        assert visit_booking.state == PersonalDropInBooking.State.ATTENDED
        assert visit_booking.debt.tariff_price == Decimal("1000.00")
        salary_event = CheckinCascadeEvent.objects.for_club(club).get(
            checkin_id=checked_in["checkin_id"],
            effect=CheckinCascadeEvent.Effect.SALARY,
        )
        assert salary_event.payload["subscription_price_snapshot"] == "1000.00"

        cancelled_booking = _book_complete_v1(
            context,
            day_offset=3,
            idempotency_key="complete-v1-cancel",
        )
        cancel_personal_drop_in_booking(
            club_id=club.id,
            booking_id=cancelled_booking.id,
            actor_user_id=owner_user.id,
            reason="Historical complete contract cancellation",
        )
        cancelled_booking.refresh_from_db()
        assert cancelled_booking.state == PersonalDropInBooking.State.CANCELLED

    def test_booking_snapshots_price_is_idempotent_and_rejects_cross_tenant_inputs(self, club, other_club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)

        created = _book(context, idempotency_key="drop-in-idempotent")
        replay = _book(context, idempotency_key="drop-in-idempotent")

        assert created.created is True
        assert replay.created is False
        assert created.booking.id == replay.booking.id
        assert created.booking.price_snapshot == Decimal("1000.00")
        assert PersonalDropInBooking.objects.for_club(club).count() == 1

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_drop_in(
                club_id=other_club.id,
                student_id=context["student"].id,
                trainer_id=context["trainer"].id,
                starts_at=context["starts_at"],
                ends_at=context["ends_at"],
                location_id=context["location"].id,
                training_type_id=context["training_type"].id,
                tariff_id=context["tariff"].id,
                actor_user_id=context["owner_user"].id,
                idempotency_key="cross-tenant",
            )

        assert exc_info.value.code == "trainer_club_mismatch"
        assert PersonalDropInBooking.objects.for_club(other_club).count() == 0

    def test_wrong_tariff_contract_is_rejected_before_booking(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        wrong_tariff = TariffFactory(
            club=club,
            training_type=context["training_type"],
            price=Decimal("999.00"),
            trainings_limit=1,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            book_personal_drop_in(
                club_id=club.id,
                student_id=context["student"].id,
                trainer_id=context["trainer"].id,
                starts_at=context["starts_at"],
                ends_at=context["ends_at"],
                location_id=context["location"].id,
                training_type_id=context["training_type"].id,
                tariff_id=wrong_tariff.id,
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "personal_drop_in_tariff_invalid"
        assert PersonalDropInBooking.objects.for_club(club).count() == 0

    def test_rescheduled_occurrence_into_slot_blocks_personal_drop_in(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        source_date = context["starts_at"].date() + timedelta(days=1)
        schedule = ScheduleFactory(
            club=club,
            trainer=context["trainer"],
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=source_date.weekday(),
            start_time=context["starts_at"].time().replace(tzinfo=None),
            end_time=context["ends_at"].time().replace(tzinfo=None),
        )
        reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=source_date,
            new_date=context["starts_at"].date(),
            new_start_time=context["starts_at"].time().replace(tzinfo=None),
            new_end_time=context["ends_at"].time().replace(tzinfo=None),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            _book(context, idempotency_key="drop-in-rescheduled-conflict")

        assert exc_info.value.code == "personal_booking_slot_conflict"
        assert PersonalDropInBooking.objects.for_club(club).count() == 0

    def test_incoming_substitute_occurrence_blocks_personal_drop_in(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        other_trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=context["starts_at"].date().weekday(),
            start_time=context["starts_at"].time().replace(tzinfo=None),
            end_time=context["ends_at"].time().replace(tzinfo=None),
        )
        substitute_trainer(
            club_id=club.id,
            schedule_id=schedule.id,
            date=context["starts_at"].date(),
            substitute_trainer_id=context["trainer"].id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            _book(context, idempotency_key="drop-in-substitute-conflict")

        assert exc_info.value.code == "personal_booking_slot_conflict"
        assert PersonalDropInBooking.objects.for_club(club).count() == 0

    def test_reschedule_cannot_create_overlap_with_existing_personal_drop_in(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        _book(context, idempotency_key="drop-in-before-reschedule")
        source_date = context["starts_at"].date() + timedelta(days=1)
        schedule = ScheduleFactory(
            club=club,
            trainer=context["trainer"],
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=source_date.weekday(),
            start_time=context["starts_at"].time().replace(tzinfo=None),
            end_time=context["ends_at"].time().replace(tzinfo=None),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            reschedule_session(
                club_id=club.id,
                schedule_id=schedule.id,
                date=source_date,
                new_date=context["starts_at"].date(),
                new_start_time=context["starts_at"].time().replace(tzinfo=None),
                new_end_time=context["ends_at"].time().replace(tzinfo=None),
            )

        assert exc_info.value.code == "personal_drop_in_slot_conflict"

    def test_substitute_cannot_create_overlap_with_existing_personal_drop_in(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        _book(context, idempotency_key="drop-in-before-substitute")
        other_trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=context["starts_at"].date().weekday(),
            start_time=context["starts_at"].time().replace(tzinfo=None),
            end_time=context["ends_at"].time().replace(tzinfo=None),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            substitute_trainer(
                club_id=club.id,
                schedule_id=schedule.id,
                date=context["starts_at"].date(),
                substitute_trainer_id=context["trainer"].id,
            )

        assert exc_info.value.code == "personal_drop_in_slot_conflict"

    def test_substitute_on_rescheduled_occurrence_uses_effective_time_for_drop_in_conflict(
        self, club, owner_user
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        _book(context, idempotency_key="drop-in-before-rescheduled-substitute")
        other_trainer = TrainerFactory(club=club)
        source_date = context["starts_at"].date() + timedelta(days=1)
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=source_date.weekday(),
            start_time=time(8, 0),
            end_time=time(9, 0),
        )
        reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=source_date,
            new_date=context["starts_at"].date(),
            new_start_time=time(10, 30),
            new_end_time=time(11, 30),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            substitute_trainer(
                club_id=club.id,
                schedule_id=schedule.id,
                date=context["starts_at"].date(),
                substitute_trainer_id=context["trainer"].id,
            )

        assert exc_info.value.code == "personal_drop_in_slot_conflict"

    def test_delete_substitute_on_rescheduled_occurrence_uses_effective_time_for_drop_in_conflict(
        self, club, owner_user
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        substitute = TrainerFactory(club=club)
        source_date = context["starts_at"].date() + timedelta(days=1)
        schedule = ScheduleFactory(
            club=club,
            trainer=context["trainer"],
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=source_date.weekday(),
            start_time=time(8, 0),
            end_time=time(9, 0),
        )
        reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=source_date,
            new_date=context["starts_at"].date(),
            new_start_time=time(10, 30),
            new_end_time=time(11, 30),
        )
        substitute_trainer(
            club_id=club.id,
            schedule_id=schedule.id,
            date=context["starts_at"].date(),
            substitute_trainer_id=substitute.id,
        )
        _book(context, idempotency_key="drop-in-before-rescheduled-substitute-delete")

        with pytest.raises(BusinessLogicError) as exc_info:
            delete_exception(
                club_id=club.id,
                schedule_id=schedule.id,
                exception_date=context["starts_at"].date(),
            )

        assert exc_info.value.code == "personal_drop_in_slot_conflict"

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_same_idempotency_key_replays_after_trainer_lock(self, club, owner_user):
        if connection.vendor != "postgresql":
            pytest.skip("This regression requires PostgreSQL row-lock behavior")

        context = _drop_in_context(club=club, owner_user=owner_user)
        initial_lookup_barrier = Barrier(2)
        thread_state = local()
        original_lookup = drop_in_service._get_idempotent_drop_in_booking_or_raise

        def synchronize_initial_lookup(**kwargs):
            booking = original_lookup(**kwargs)
            if not getattr(thread_state, "completed_initial_lookup", False):
                thread_state.completed_initial_lookup = True
                initial_lookup_barrier.wait(timeout=10)
            return booking

        def create_or_replay():
            close_old_connections()
            try:
                return _book(context, idempotency_key="concurrent-drop-in-key")
            finally:
                close_old_connections()

        with patch.object(
            drop_in_service,
            "_get_idempotent_drop_in_booking_or_raise",
            side_effect=synchronize_initial_lookup,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: create_or_replay(), range(2)))

        assert sorted(result.created for result in results) == [False, True]
        assert len({result.booking.id for result in results}) == 1
        assert PersonalDropInBooking.objects.for_club(club).count() == 1
        assert Schedule.objects.for_club(club).count() == 1
        assert ScheduleEnrollment.objects.for_club(club).count() == 1

    def test_admin_attendance_correction_uses_confirmed_subscription_and_preserves_first_reason(
        self,
        club,
        owner_user,
        admin_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="attendance-correction-covered").booking
        payment = _confirm_drop_in_payment(
            club=club,
            owner_user=owner_user,
            booking=booking,
            idempotency_key="attendance-correction-payment",
        )
        subscription = payment.subscription
        assert subscription is not None
        completed_at = context["ends_at"] + timedelta(minutes=5)

        with (
            patch("apps.attendance.selectors.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.checkin.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.async_task"),
        ):
            first = record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason="  Тренер   не отметил  ",
            )
            replay = record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=admin_user.id,
                reason="Другая причина",
            )

        booking.refresh_from_db()
        subscription.refresh_from_db()
        session = GroupSession.objects.for_club(club).get(
            schedule_id=booking.enrollment.schedule_id,
            date=context["starts_at"].date(),
        )
        assert first.created is True
        assert replay.created is False
        assert replay.checkin.id == first.checkin.id
        assert first.checkin.source == Checkin.Source.BATCH
        assert booking.state == PersonalDropInBooking.State.ATTENDED
        assert subscription.trainings_left == 0
        assert session.closed_by_id == owner_user.id
        assert session.close_source == GroupSession.CloseSource.BATCH
        assert session.notes == f"{PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX}Тренер не отметил"
        assert Checkin.objects.for_club(club).filter(
            student=context["student"],
            schedule=booking.enrollment.schedule,
            date=context["starts_at"].date(),
        ).count() == 1

    @pytest.mark.parametrize(
        ("reason", "expected_code"),
        [
            ("   ", "attendance_correction_reason_required"),
            ("x" * 501, "attendance_correction_reason_too_long"),
        ],
    )
    def test_attendance_correction_rejects_invalid_reason_without_side_effects(
        self,
        club,
        owner_user,
        reason,
        expected_code,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key=f"invalid-reason-{expected_code}").booking

        with pytest.raises(BusinessLogicError) as exc_info:
            record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason=reason,
            )

        booking.refresh_from_db()
        assert exc_info.value.code == expected_code
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert not Checkin.objects.for_club(club).exists()
        assert not GroupSession.objects.for_club(club).exists()

    def test_attendance_correction_rejects_closed_payroll_without_partial_effects(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="attendance-correction-closed-payroll").booking
        payment = _confirm_drop_in_payment(
            club=club,
            owner_user=owner_user,
            booking=booking,
            idempotency_key="attendance-correction-closed-payroll-payment",
        )
        subscription = payment.subscription
        assert subscription is not None
        initial_trainings_left = subscription.trainings_left
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=context["starts_at"].date(),
            period_end=context["starts_at"].date(),
            reason="Период уже выплачен",
            actor_user_id=owner_user.id,
        )
        completed_at = context["ends_at"] + timedelta(minutes=5)

        with (
            patch("apps.attendance.selectors.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.checkin.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.async_task"),
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason="Тренер не отметил",
            )

        booking.refresh_from_db()
        subscription.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert subscription.trainings_left == initial_trainings_left
        assert not Checkin.objects.for_club(club).exists()
        assert not CheckinCascadeEvent.objects.for_club(club).exists()
        assert not GroupSession.objects.for_club(club).exists()

    def test_attendance_correction_keeps_debt_only_branch_available_in_closed_payroll(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="attendance-correction-closed-payroll-debt").booking
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=context["starts_at"].date(),
            period_end=context["starts_at"].date(),
            reason="Период уже выплачен",
            actor_user_id=owner_user.id,
        )
        completed_at = context["ends_at"] + timedelta(minutes=5)

        with (
            patch("apps.attendance.selectors.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.checkin.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.async_task"),
        ):
            result = record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason="Тренер не отметил",
            )

        booking.refresh_from_db()
        assert result.created is True
        assert result.checkin.is_debt is True
        assert booking.state == PersonalDropInBooking.State.ATTENDED
        assert booking.debt_id is not None
        assert result.group_session is not None

    def test_attendance_correction_rejects_before_end_without_partial_effects(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="attendance-correction-before-end").booking

        with pytest.raises(BusinessLogicError) as exc_info:
            record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason="Тренер не отметил",
            )

        booking.refresh_from_db()
        assert exc_info.value.code == "session_close_not_allowed_yet"
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert not Checkin.objects.for_club(club).exists()
        assert not GroupSession.objects.for_club(club).exists()

    def test_attendance_correction_rejects_terminal_drop_in_states(
        self,
        club,
        owner_user,
    ):
        cancelled_context = _drop_in_context(club=club, owner_user=owner_user)
        cancelled = _book(
            cancelled_context,
            idempotency_key="attendance-correction-cancelled",
        ).booking
        cancel_personal_drop_in_booking(
            club_id=club.id,
            booking_id=cancelled.id,
            actor_user_id=owner_user.id,
            reason="Клиент отменил",
        )

        no_show_context = _drop_in_context(club=club, owner_user=owner_user)
        no_show = _book(
            no_show_context,
            idempotency_key="attendance-correction-no-show",
        ).booking
        with patch(
            "apps.attendance.services.drop_in.timezone.now",
            return_value=no_show_context["ends_at"] + timedelta(minutes=5),
        ):
            mark_personal_drop_in_no_show(
                club_id=club.id,
                booking_id=no_show.id,
                actor_user_id=owner_user.id,
                reason="Не пришёл",
            )

        for terminal_booking in (cancelled, no_show):
            with pytest.raises(BusinessLogicError) as exc_info:
                record_personal_drop_in_attendance_correction(
                    club_id=club.id,
                    booking_id=terminal_booking.id,
                    actor_user_id=owner_user.id,
                    reason="Тренер не отметил",
                )
            assert exc_info.value.code == "personal_drop_in_attendance_terminal"

        assert not Checkin.objects.for_club(club).exists()
        assert not GroupSession.objects.for_club(club).exists()

    def test_attendance_correction_rejects_already_closed_session_without_checkin(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="attendance-correction-closed-session").booking
        GroupSession.objects.create(
            club=club,
            schedule=booking.enrollment.schedule,
            date=context["starts_at"].date(),
            trainer=context["trainer"],
            attendee_count=0,
            closed_at=context["ends_at"],
            closed_by=owner_user,
            close_source=GroupSession.CloseSource.TRAINER_REVIEW,
            notes="Закрыто без отметки",
        )
        completed_at = context["ends_at"] + timedelta(minutes=5)

        with (
            patch("apps.attendance.selectors.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.checkin.timezone.now", return_value=completed_at),
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            record_personal_drop_in_attendance_correction(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner_user.id,
                reason="Тренер не отметил",
            )

        booking.refresh_from_db()
        assert exc_info.value.code == "group_session_closed"
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert not Checkin.objects.for_club(club).exists()
        session = GroupSession.objects.for_club(club).get()
        assert session.notes == "Закрыто без отметки"

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_attendance_corrections_keep_first_actor_reason_and_single_cascade(
        self,
        club,
        owner_user,
        admin_user,
    ):
        if connection.vendor != "postgresql":
            pytest.skip("This regression requires PostgreSQL row-lock behavior")

        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="concurrent-attendance-correction").booking
        payment = _confirm_drop_in_payment(
            club=club,
            owner_user=owner_user,
            booking=booking,
            idempotency_key="concurrent-attendance-correction-payment",
        )
        subscription_id = payment.subscription_id
        completed_at = context["ends_at"] + timedelta(minutes=5)
        start_barrier = Barrier(2)

        def synchronized_batch_checkin(**kwargs):
            start_barrier.wait(timeout=10)
            return batch_checkin(**kwargs)

        actors_and_reasons = [
            (owner_user.id, "Причина владельца"),
            (admin_user.id, "Причина администратора"),
        ]

        def record_correction(actor_and_reason):
            close_old_connections()
            try:
                actor_id, reason = actor_and_reason
                return record_personal_drop_in_attendance_correction(
                    club_id=club.id,
                    booking_id=booking.id,
                    actor_user_id=actor_id,
                    reason=reason,
                )
            finally:
                close_old_connections()

        with (
            patch(
                "apps.attendance.services.checkin.batch_checkin",
                side_effect=synchronized_batch_checkin,
            ),
            patch("apps.attendance.selectors.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.checkin.timezone.now", return_value=completed_at),
            patch("apps.attendance.services.async_task"),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(record_correction, actors_and_reasons))

        session = GroupSession.objects.for_club(club).get(
            schedule_id=booking.enrollment.schedule_id,
            date=context["starts_at"].date(),
        )
        subscription = Subscription.objects.for_club(club).get(id=subscription_id)
        expected_note_by_actor = {
            owner_user.id: f"{PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX}Причина владельца",
            admin_user.id: f"{PERSONAL_DROP_IN_ATTENDANCE_NOTE_PREFIX}Причина администратора",
        }
        cascade_events = CheckinCascadeEvent.objects.for_club(club)

        assert sorted(result.created for result in results) == [False, True]
        assert Checkin.objects.for_club(club).count() == 1
        assert subscription.trainings_left == 0
        assert session.notes == expected_note_by_actor[session.closed_by_id]
        assert session.close_source == GroupSession.CloseSource.BATCH
        assert cascade_events.count() == cascade_events.values("effect").distinct().count()

    def test_delete_exception_cannot_restore_overlap_with_existing_personal_drop_in(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        target_date = context["starts_at"].date()
        schedule = ScheduleFactory(
            club=club,
            trainer=context["trainer"],
            location=context["location"],
            training_type=context["training_type"],
            day_of_week=target_date.weekday(),
            start_time=context["starts_at"].time().replace(tzinfo=None),
            end_time=context["ends_at"].time().replace(tzinfo=None),
        )
        reschedule_session(
            club_id=club.id,
            schedule_id=schedule.id,
            date=target_date,
            new_date=target_date + timedelta(days=1),
            new_start_time=context["starts_at"].time().replace(tzinfo=None),
            new_end_time=context["ends_at"].time().replace(tzinfo=None),
        )
        _book(context, idempotency_key="drop-in-before-exception-delete")

        with pytest.raises(BusinessLogicError) as exc_info:
            delete_exception(
                club_id=club.id,
                schedule_id=schedule.id,
                exception_date=target_date,
            )

        assert exc_info.value.code == "personal_drop_in_slot_conflict"

    def test_hybrid_personal_component_blocks_drop_in_but_covers_strict_personal_booking(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user, student_status=Student.Status.ACTIVE)
        parent_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        hybrid_tariff = TariffFactory(
            club=club,
            training_type=parent_training_type,
            price=Decimal("5000.00"),
        )
        personal_component = TariffComponentFactory(
            club=club,
            tariff=hybrid_tariff,
            training_type=context["training_type"],
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            scope=Tariff.Scope.CLUB,
            location=None,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis=Decimal("1000.00"),
        )
        subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=hybrid_tariff,
            status=Subscription.Status.ACTIVE,
        )
        subscription_component = SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=personal_component,
            credits_total=1,
            credits_left=1,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            _book(context, idempotency_key="drop-in-hybrid-entitlement")

        assert exc_info.value.code == "personal_subscription_already_available"
        assert PersonalDropInBooking.objects.for_club(club).count() == 0

        strict_booking = book_personal_session(
            club_id=club.id,
            student_id=context["student"].id,
            trainer_id=context["trainer"].id,
            starts_at=context["starts_at"],
            ends_at=context["ends_at"],
            location_id=context["location"].id,
            training_type_id=context["training_type"].id,
            subscription_id=subscription.id,
            actor_user_id=owner_user.id,
            idempotency_key="strict-hybrid-component-booking",
        )
        with patch("apps.attendance.services.async_task"):
            checkin = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=strict_booking.schedule.id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        subscription_component.refresh_from_db()
        assert strict_booking.created is True
        assert checkin["subscription_id"] == subscription.id
        assert subscription_component.credits_left == 0

    @pytest.mark.parametrize("entitlement_kind", ["legacy", "component"])
    def test_personal_entitlement_expiry_is_strict_at_club_local_day_start_across_dst(
        self,
        club,
        owner_user,
        entitlement_kind,
    ):
        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        context = _drop_in_context(club=club, owner_user=owner_user, student_status=Student.Status.ACTIVE)
        target_date = date(2027, 3, 14)
        day_start = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time()),
            club_zoneinfo(club),
        )
        subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            status=Subscription.Status.ACTIVE,
            expires_at=day_start,
        )
        if entitlement_kind == "component":
            SubscriptionComponentFactory(
                club=club,
                subscription=subscription,
                tariff_component=TariffComponent.objects.get(tariff=context["tariff"]),
                credits_total=1,
                credits_left=1,
            )

        assert not drop_in_service._usable_personal_entitlement_exists(
            club=club,
            student_id=context["student"].id,
            training_type_id=context["training_type"].id,
            location_id=context["location"].id,
            target_date=target_date,
        )

        subscription.expires_at = day_start + timedelta(seconds=1)
        subscription.save(update_fields=["expires_at", "updated_at"])

        assert drop_in_service._usable_personal_entitlement_exists(
            club=club,
            student_id=context["student"].id,
            training_type_id=context["training_type"].id,
            location_id=context["location"].id,
            target_date=target_date,
        )

    def test_strict_personal_booking_consumes_the_selected_hybrid_subscription(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user, student_status=Student.Status.ACTIVE)
        parent_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        hybrid_tariff = TariffFactory(
            club=club,
            training_type=parent_training_type,
            price=Decimal("5000.00"),
        )
        personal_component = TariffComponentFactory(
            club=club,
            tariff=hybrid_tariff,
            training_type=context["training_type"],
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            scope=Tariff.Scope.CLUB,
            location=None,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis=Decimal("1000.00"),
        )
        earlier_subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=hybrid_tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=context["starts_at"] + timedelta(days=10),
        )
        selected_subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=hybrid_tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=context["starts_at"] + timedelta(days=20),
        )
        earlier_component = SubscriptionComponentFactory(
            club=club,
            subscription=earlier_subscription,
            tariff_component=personal_component,
            credits_total=1,
            credits_left=1,
        )
        selected_component = SubscriptionComponentFactory(
            club=club,
            subscription=selected_subscription,
            tariff_component=personal_component,
            credits_total=1,
            credits_left=1,
        )

        strict_booking = book_personal_session(
            club_id=club.id,
            student_id=context["student"].id,
            trainer_id=context["trainer"].id,
            starts_at=context["starts_at"],
            ends_at=context["ends_at"],
            location_id=context["location"].id,
            training_type_id=context["training_type"].id,
            subscription_id=selected_subscription.id,
            actor_user_id=owner_user.id,
            idempotency_key="strict-selected-hybrid-subscription",
        )
        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=strict_booking.schedule.id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        earlier_component.refresh_from_db()
        selected_component.refresh_from_db()
        assert result["subscription_id"] == selected_subscription.id
        assert earlier_component.credits_left == 1
        assert selected_component.credits_left == 0

    def test_strict_personal_checkin_rejects_when_selected_subscription_became_unavailable(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user, student_status=Student.Status.ACTIVE)
        earlier_subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
            expires_at=context["starts_at"] + timedelta(days=10),
        )
        selected_subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
            expires_at=context["starts_at"] + timedelta(days=20),
        )
        strict_booking = book_personal_session(
            club_id=club.id,
            student_id=context["student"].id,
            trainer_id=context["trainer"].id,
            starts_at=context["starts_at"],
            ends_at=context["ends_at"],
            location_id=context["location"].id,
            training_type_id=context["training_type"].id,
            subscription_id=selected_subscription.id,
            actor_user_id=owner_user.id,
            idempotency_key="strict-selected-subscription-unavailable",
        )
        selected_subscription.trainings_left = 0
        selected_subscription.save(update_fields=["trainings_left", "updated_at"])

        with patch("apps.attendance.services.async_task"), pytest.raises(BusinessLogicError) as exc_info:
            create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=strict_booking.schedule.id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        earlier_subscription.refresh_from_db()
        assert exc_info.value.code == "selected_subscription_not_available"
        assert earlier_subscription.trainings_left == 1

    def test_exact_lead_checkin_creates_snapshot_debt_despite_trial_free(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context).booking

        with patch("apps.attendance.services.async_task"):
            result = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        booking.refresh_from_db()
        debt = Debt.objects.for_club(club).get(id=booking.debt_id)
        assert result["is_debt"] is True
        assert booking.state == PersonalDropInBooking.State.ATTENDED
        assert debt.tariff_price == Decimal("1000.00")
        assert debt.required_tariff_id == context["tariff"].id
        assert debt.reason == "personal_drop_in"

    def test_drop_in_lead_checkin_never_enqueues_post_trial_workflow(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context).booking

        with (
            patch("apps.attendance.services.async_task") as async_task,
            patch("apps.leads.services._trigger_trial_done_side_effects") as trigger_trial_done,
        ):
            result = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        post_trial = CheckinCascadeEvent.objects.for_club(club).get(
            checkin_id=result["checkin_id"],
            effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
        )
        context["student"].refresh_from_db()
        assert post_trial.expected is False
        assert context["student"].lead_status != Student.LeadStatus.TRIAL_DONE
        assert not any(
            call.args[0] == "apps.retention.tasks.create_post_trial_task" for call in async_task.call_args_list
        )
        trigger_trial_done.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    def test_personal_drop_in_payment_path_accepts_scheduled_booking_then_settles_attended_debt(
        self, club, owner_user
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context).booking
        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            link = create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="pending-payment",
            )
            create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            booking.refresh_from_db()
            debt = Debt.objects.for_club(club).get(id=booking.debt_id)
            assert debt.settlement_payment_id == link.payment_id
            assert debt.resolved_at is None

            verify_payment(
                payment_id=link.payment_id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        debt.refresh_from_db()
        link.payment.refresh_from_db()
        assert link.payment.status == Payment.Status.CONFIRMED
        assert debt.resolved_at is not None
        assert debt.resolution_type == "payment"

    def test_generic_payment_cannot_bypass_booking_payment_link(self, settings, club, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="drop-in-generic-payment-guard").booking

        with pytest.raises(BusinessLogicError) as scheduled_exc:
            create_payment(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                seller_trainer_id=context["trainer"].id,
                package_owner_trainer_id=context["trainer"].id,
            )
        assert scheduled_exc.value.code == "personal_drop_in_use_booking_payment"

        with pytest.raises(BusinessLogicError) as bank_exc:
            create_bank_payment_order(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
                seller_trainer_id=context["trainer"].id,
                package_owner_trainer_id=context["trainer"].id,
            )
        assert bank_exc.value.code == "personal_drop_in_use_booking_payment"
        assert BankPaymentOrder.objects.for_club(club).count() == 0
        assert PersonalDropInPaymentLink.objects.for_club(club).count() == 0
        assert Payment.objects.for_club(club).count() == 0

        with pytest.raises(BusinessLogicError) as direct_subscription_exc:
            create_subscription(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                seller_trainer_id=context["trainer"].id,
                package_owner_trainer_id=context["trainer"].id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
            )
        assert direct_subscription_exc.value.code == "personal_drop_in_use_booking_payment"
        assert Subscription.objects.for_club(club).count() == 0

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
        booking.refresh_from_db()

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                debt_ids=[booking.debt_id],
                recorded_by_id=owner_user.id,
                seller_trainer_id=context["trainer"].id,
                package_owner_trainer_id=context["trainer"].id,
            )

        assert exc_info.value.code == "personal_drop_in_use_booking_payment"
        assert PersonalDropInPaymentLink.objects.for_club(club).count() == 0
        assert Payment.objects.for_club(club).count() == 0

        with pytest.raises(BusinessLogicError) as attended_direct_subscription_exc:
            create_subscription(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                seller_trainer_id=context["trainer"].id,
                package_owner_trainer_id=context["trainer"].id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
            )

        assert attended_direct_subscription_exc.value.code == "personal_drop_in_use_booking_payment"
        assert Subscription.objects.for_club(club).count() == 0

    def test_attended_booking_covered_by_later_entitlement_cannot_be_charged_again(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        context = _drop_in_context(club=club, owner_user=owner_user, student_status=Student.Status.ACTIVE)
        booking = _book(context, idempotency_key="drop-in-later-entitlement").booking
        SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
        )

        with pytest.raises(BusinessLogicError) as manual_exc:
            create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="drop-in-stale-manual-payment",
            )
        with pytest.raises(BusinessLogicError) as online_exc:
            create_personal_drop_in_bank_payment_order(
                club_id=club.id,
                booking_id=booking.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
                idempotency_key="drop-in-stale-online-payment",
            )

        assert manual_exc.value.code == "active_subscription_exists"
        assert online_exc.value.code == "active_subscription_exists"
        assert PersonalDropInPaymentLink.objects.for_club(club).count() == 0
        assert Payment.objects.for_club(club).count() == 0
        assert BankPaymentOrder.objects.for_club(club).count() == 0

        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
        booking.refresh_from_db()
        assert booking.state == PersonalDropInBooking.State.ATTENDED
        assert booking.debt_id is None

        with pytest.raises(BusinessLogicError) as exc_info:
            create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="drop-in-covered-stale-payment",
            )

        assert exc_info.value.code == "personal_drop_in_payment_covered"
        assert Payment.objects.for_club(club).count() == 0

    @pytest.mark.django_db(transaction=True)
    def test_scheduled_drop_in_blocks_training_type_contract_change_with_safe_lock(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        _book(context, idempotency_key="drop-in-training-type-contract-lock")

        with pytest.raises(BusinessLogicError) as exc_info:
            update_training_type(
                training_type_id=context["training_type"].id,
                club_id=club.id,
                drop_in_price=Decimal("1200.00"),
            )

        assert exc_info.value.code == "personal_drop_in_contract_change_blocked"

    @pytest.mark.django_db(transaction=True)
    def test_scheduled_drop_in_blocks_tariff_contract_change_with_safe_lock(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        _book(context, idempotency_key="drop-in-tariff-contract-lock")

        with pytest.raises(BusinessLogicError) as exc_info:
            update_tariff(
                tariff_id=context["tariff"].id,
                club_id=club.id,
                price=Decimal("1200.00"),
            )

        assert exc_info.value.code == "personal_drop_in_contract_change_blocked"

    def test_contract_noop_is_allowed_while_open_and_covered_visit_is_terminal(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="drop-in-terminal-contract").booking

        update_training_type(
            training_type_id=context["training_type"].id,
            club_id=club.id,
            drop_in_price=context["training_type"].drop_in_price,
        )
        update_tariff(
            tariff_id=context["tariff"].id,
            club_id=club.id,
            price=context["tariff"].price,
        )

        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            payment_link = create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="drop-in-terminal-contract-payment",
            )
            verify_payment(
                payment_id=payment_link.payment_id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )
            create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        booking.refresh_from_db()
        assert booking.state == PersonalDropInBooking.State.ATTENDED
        assert booking.debt_id is None

        update_training_type(
            training_type_id=context["training_type"].id,
            club_id=club.id,
            drop_in_price=Decimal("1200.00"),
        )
        update_tariff(
            tariff_id=context["tariff"].id,
            club_id=club.id,
            price=Decimal("1200.00"),
        )
        context["training_type"].refresh_from_db()
        context["tariff"].refresh_from_db()
        assert context["training_type"].drop_in_price == Decimal("1200.00")
        assert context["tariff"].price == Decimal("1200.00")

    def test_cancel_and_no_show_keep_unattended_booking_free_of_debt(self, club, owner_user, monkeypatch):
        context = _drop_in_context(club=club, owner_user=owner_user)
        cancelled = _book(context, idempotency_key="cancel-me").booking
        cancel_personal_drop_in_booking(
            club_id=club.id,
            booking_id=cancelled.id,
            actor_user_id=owner_user.id,
            reason="client cancelled",
        )
        cancelled.refresh_from_db()
        assert cancelled.state == PersonalDropInBooking.State.CANCELLED
        assert cancelled.debt_id is None
        assert cancelled.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert cancelled.enrollment.schedule.is_active is False
        assert (
            _personal_drop_in_booking_payload(
                _personal_drop_in_booking_result_for_existing(cancelled)
            )["financial_state"]
            == "not_due"
        )

        context["starts_at"] += timedelta(days=1)
        context["ends_at"] += timedelta(days=1)
        no_show = _book(context, idempotency_key="no-show-me").booking
        monkeypatch.setattr(
            "apps.attendance.services.drop_in.timezone.now",
            lambda: context["ends_at"] + timedelta(minutes=1),
        )
        mark_personal_drop_in_no_show(
            club_id=club.id,
            booking_id=no_show.id,
            actor_user_id=owner_user.id,
            reason="did not attend",
        )
        no_show.refresh_from_db()
        assert no_show.state == PersonalDropInBooking.State.NO_SHOW
        assert no_show.debt_id is None
        assert no_show.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert (
            _personal_drop_in_booking_payload(
                _personal_drop_in_booking_result_for_existing(no_show)
            )["financial_state"]
            == "not_due"
        )

    def test_cancelled_drop_in_is_visible_in_existing_personal_bookings_list(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="list-cancelled-drop-in").booking
        cancel_personal_drop_in_booking(
            club_id=club.id,
            booking_id=booking.id,
            actor_user_id=owner_user.id,
            reason="client cancelled",
        )

        [enrollment] = list(
            get_student_upcoming_personal_bookings(
                club=club,
                student_id=context["student"].id,
            )
        )
        payload = _student_personal_booking_out(enrollment)

        assert payload.booking_id == booking.id
        assert payload.booking_kind == "drop_in"
        assert payload.attendance_state == PersonalDropInBooking.State.CANCELLED
        assert payload.financial_state == "not_due"
        assert payload.price_snapshot == "1000.00"
        assert payload.tariff_id == context["tariff"].id
        assert payload.debt_id is None
        assert payload.can_cancel is False
        assert payload.can_mark_no_show is False
        assert payload.next_action_label is None

    def test_personal_bookings_list_exposes_only_linked_online_payment_attempt(
        self,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="list-linked-online-attempt").booking
        link = create_personal_drop_in_bank_payment_order(
            club_id=club.id,
            booking_id=booking.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            idempotency_key="list-linked-online-attempt-payment",
        )

        [enrollment] = list(
            get_student_upcoming_personal_bookings(
                club=club,
                student_id=context["student"].id,
            )
        )
        payload = _student_personal_booking_out(enrollment)

        assert payload.financial_state == "payment_pending"
        assert payload.payment_id == link.payment_id
        assert payload.bank_payment_order_id == link.bank_payment_order_id
        assert payload.payment_status == Payment.Status.PENDING
        assert payload.order_status == BankPaymentOrder.Status.PENDING
        assert payload.provider_payment_url == link.bank_payment_order.provider_payment_url
        assert payload.can_cancel_payment is True

    def test_staff_exact_drop_in_payment_link_is_trainer_scoped_and_non_enumerating(
        self,
        settings,
        club,
        owner_user,
        trainer_user,
        bypass_jwt_auth,
    ):
        del bypass_jwt_auth
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="exact-drop-in-payment-link").booking
        link = create_personal_drop_in_bank_payment_order(
            club_id=club.id,
            booking_id=booking.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            idempotency_key="exact-drop-in-payment-link-order",
        )
        TrainerFactory(club=club, user=trainer_user)

        owner_response = client.get(
            f"/personal-drop-in-bookings/{booking.id}/payment-link/",
            **_auth_params(owner_user, club, role="owner"),
        )
        hidden_trainer = client.get(
            f"/personal-drop-in-bookings/{booking.id}/payment-link/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert owner_response.status_code == 200
        assert owner_response.json()["bank_payment_order_id"] == link.bank_payment_order_id
        assert owner_response.json()["order_status"] == BankPaymentOrder.Status.PENDING
        assert hidden_trainer.status_code == 404
        assert hidden_trainer.json()["detail"] == "Not found"

    def test_cancelled_drop_in_can_be_rebooked_with_new_key_but_replays_terminal_key(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        original = _book(context, idempotency_key="drop-in-terminal-key")
        cancel_personal_drop_in_booking(
            club_id=club.id,
            booking_id=original.booking.id,
            actor_user_id=owner_user.id,
            reason="client cancelled",
        )

        replay = _book(context, idempotency_key="drop-in-terminal-key")
        replacement = _book(context, idempotency_key="drop-in-rebook-key")

        assert replay.created is False
        assert replay.booking.id == original.booking.id
        assert replay.booking.state == PersonalDropInBooking.State.CANCELLED
        assert replacement.created is True
        assert replacement.booking.id != original.booking.id
        assert replacement.booking.state == PersonalDropInBooking.State.SCHEDULED

    def test_generic_enrollment_mutations_cannot_bypass_drop_in_lifecycle(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="drop-in-generic-enrollment-guard").booking
        other_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        target_schedule = ScheduleFactory(club=club)

        guarded_actions = (
            lambda: enroll_student_in_schedule(
                club_id=club.id,
                student_id=other_student.id,
                schedule_id=booking.enrollment.schedule_id,
            ),
            lambda: cancel_schedule_enrollment(
                club_id=club.id,
                enrollment_id=booking.enrollment_id,
                ends_on=context["starts_at"].date(),
            ),
            lambda: transfer_schedule_enrollment(
                club_id=club.id,
                enrollment_id=booking.enrollment_id,
                target_schedule_id=target_schedule.id,
                ends_on=context["starts_at"].date(),
            ),
            lambda: freeze_schedule_enrollment(
                club_id=club.id,
                enrollment_id=booking.enrollment_id,
            ),
            lambda: unfreeze_schedule_enrollment(
                club_id=club.id,
                enrollment_id=booking.enrollment_id,
            ),
        )

        for action in guarded_actions:
            with pytest.raises(BusinessLogicError) as exc_info:
                action()
            assert exc_info.value.code == "personal_drop_in_use_booking_action"

        booking.refresh_from_db()
        booking.enrollment.refresh_from_db()
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert booking.enrollment.status == ScheduleEnrollment.Status.ACTIVE
        assert (
            ScheduleEnrollment.objects.for_club(club)
            .filter(schedule_id=booking.enrollment.schedule_id)
            .count()
            == 1
        )

    def test_late_settlement_closed_cancel_and_replacement_leave_one_visit_net_payroll(
        self,
        club,
        owner_user,
    ):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context).booking
        with (
            patch("apps.attendance.services.async_task"),
            patch("django_q.tasks.async_task") as django_async_task,
        ):
            original_result = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            original_checkin_id = original_result["checkin_id"]
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=context["starts_at"].date(),
                period_end=context["starts_at"].date(),
                reason="Closed personal drop-in period",
                actor_user_id=owner_user.id,
            )
            link = create_personal_drop_in_payment(
                club_id=club.id,
                booking_id=booking.id,
                payment_method=Payment.Method.CASH,
                created_by_id=owner_user.id,
                idempotency_key="late-settlement-payment",
            )
            verify_payment(
                payment_id=link.payment_id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
                verified_at=context["ends_at"] + timedelta(days=1),
            )
            salary_event = CheckinCascadeEvent.objects.for_club(club).get(
                checkin_id=original_checkin_id,
                effect=CheckinCascadeEvent.Effect.SALARY,
            )
            assert salary_event.expected is False
            assert not any(
                call.args[:2] == ("apps.attendance.tasks.calculate_salary", original_checkin_id)
                for call in django_async_task.call_args_list
            )

            original_credit = TrainerEarningAdjustment.objects.for_club(club).get(
                source_checkin_id=original_checkin_id,
                kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
            )
            cancel_checkin(
                checkin_id=original_checkin_id,
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            cancellation_debit = TrainerEarningAdjustment.objects.for_club(club).get(
                reversal_of_id=original_credit.id,
                kind=TrainerEarningAdjustment.Kind.CHECKIN_CANCELLATION_DEBIT,
            )

            booking.refresh_from_db()
            assert booking.state == PersonalDropInBooking.State.SCHEDULED
            assert booking.checkin_id is None
            assert booking.debt_id is None
            assert cancellation_debit.payable_amount_delta == -original_credit.payable_amount_delta

            # Cancellation is idempotent and does not create a second debit.
            cancel_checkin(
                checkin_id=original_checkin_id,
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            assert (
                TrainerEarningAdjustment.objects.for_club(club)
                .filter(
                    reversal_of_id=original_credit.id,
                    kind=TrainerEarningAdjustment.Kind.CHECKIN_CANCELLATION_DEBIT,
                )
                .count()
                == 1
            )

            replacement_result = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        replacement_credit = TrainerEarningAdjustment.objects.for_club(club).get(
            source_checkin_id=replacement_result["checkin_id"],
            kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
        )
        assert replacement_result["checkin_id"] != original_checkin_id
        assert (
            TrainerEarningAdjustment.objects.for_club(club)
            .filter(
                source_checkin_id=replacement_result["checkin_id"],
                kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
            )
            .count()
            == 1
        )
        total = sum(
            TrainerEarningAdjustment.objects.for_club(club)
            .filter(affects_payroll=True)
            .values_list("payable_amount_delta", flat=True)
        )
        assert total == replacement_credit.payable_amount_delta

    def test_closed_replacement_with_unrelated_legacy_entitlement_posts_open_period_credit(
        self,
        club,
        owner_user,
        settings,
        monkeypatch,
    ):
        from apps.attendance.tasks import calculate_salary
        from apps.trainers.settlement_selectors import get_trainer_settlement_summary
        from apps.trainers.settlement_services import record_trainer_settlement

        context = _drop_in_context(
            club=club,
            owner_user=owner_user,
            student_status=Student.Status.ACTIVE,
        )
        booking = _book(context, idempotency_key="drop-in-closed-legacy-replacement").booking
        legacy_tariff = TariffFactory(
            club=club,
            training_type=context["training_type"],
            price=Decimal("1500.00"),
            trainings_limit=1,
            scope=Tariff.Scope.CLUB,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        legacy_subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=legacy_tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
            trainings_used=0,
        )

        settings.TRAINER_SETTLEMENTS_ENABLED = True
        earned_on = context["starts_at"].date()
        monkeypatch.setattr(timezone, "now", lambda: context["ends_at"] + timedelta(hours=1))
        record_trainer_settlement(
            club_id=club.id, actor_user_id=owner_user.id, trainer_id=context["trainer"].id,
            kind="opening", effective_on=earned_on - timedelta(days=1), balance_delta=Decimal("0"),
            reason="Opening for closed-period regression", source_namespace="test", source_key="closed-drop-in",
        )

        def balance(as_of):
            return get_trainer_settlement_summary(
                club=club, trainer_id=context["trainer"].id,
                date_from=earned_on - timedelta(days=1), date_to=as_of,
            )["balance"]

        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            original = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            calculate_salary(original["checkin_id"], club.id)
            original_balance = balance(earned_on)
            assert original_balance > 0
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=context["starts_at"].date(),
                period_end=context["starts_at"].date(),
                reason="Closed legacy-covered drop-in period",
                actor_user_id=owner_user.id,
            )
            monkeypatch.setattr(timezone, "now", lambda: context["ends_at"] + timedelta(days=1))
            cancel_checkin(
                checkin_id=original["checkin_id"],
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            assert balance(earned_on) == original_balance
            assert balance(earned_on + timedelta(days=1)) == Decimal("0")
            from apps.trainers.selectors import get_salary_summary, get_trainers_with_stats
            from apps.trainers.services import _build_payroll_close_snapshot

            for as_of, expected in [(earned_on, original_balance), (earned_on + timedelta(days=1), Decimal("0"))]:
                rows = get_salary_summary(club=club, date_from=earned_on, date_to=as_of)
                assert next(row["total"] for row in rows if row["trainer_id"] == context["trainer"].id) == expected
                stats = get_trainers_with_stats(club=club, date_from=earned_on, date_to=as_of)
                assert stats.get(id=context["trainer"].id).monthly_salary == expected
                total, _ = _build_payroll_close_snapshot(club_id=club.id, period_start=earned_on, period_end=as_of)
                assert total == expected
            replacement = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        legacy_subscription.refresh_from_db()
        replacement_credit = TrainerEarningAdjustment.objects.for_club(club).get(
            source_checkin_id=replacement["checkin_id"],
            source_payment__isnull=True,
            kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
        )
        assert replacement["subscription_id"] == legacy_subscription.id
        assert replacement_credit.effective_date != context["starts_at"].date()
        assert PersonalDropInPaymentLink.objects.for_club(club).count() == 0

    def test_closed_replacement_with_non_checkin_component_does_not_post_credit(self, club, owner_user):
        context = _drop_in_context(
            club=club,
            owner_user=owner_user,
            student_status=Student.Status.ACTIVE,
        )
        booking = _book(context, idempotency_key="drop-in-closed-none-component").booking
        parent_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        hybrid_tariff = TariffFactory(
            club=club,
            training_type=parent_training_type,
            price=Decimal("5000.00"),
        )
        component = TariffComponentFactory(
            club=club,
            tariff=hybrid_tariff,
            training_type=context["training_type"],
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            scope=Tariff.Scope.CLUB,
            location=None,
            trainer_payout_policy=Tariff.PayoutPolicy.NONE,
            paid_amount_basis=Decimal("1000.00"),
        )
        subscription = SubscriptionFactory(
            club=club,
            student=context["student"],
            tariff=hybrid_tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
            trainings_used=0,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=component,
            credits_total=1,
            credits_left=1,
        )

        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            original = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=context["starts_at"].date(),
                period_end=context["starts_at"].date(),
                reason="Closed non-checkin component period",
                actor_user_id=owner_user.id,
            )
            cancel_checkin(
                checkin_id=original["checkin_id"],
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            replacement = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )

        assert replacement["subscription_id"] == subscription.id
        assert not TrainerEarningAdjustment.objects.for_club(club).filter(
            source_checkin_id=replacement["checkin_id"],
            kind=TrainerEarningAdjustment.Kind.LATE_DROP_IN_CREDIT,
        ).exists()

    def test_drop_in_allows_only_one_replacement_after_cancellations(self, club, owner_user):
        context = _drop_in_context(club=club, owner_user=owner_user)
        booking = _book(context, idempotency_key="drop-in-one-replacement").booking

        with patch("apps.attendance.services.async_task"), patch("django_q.tasks.async_task"):
            original = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            cancel_checkin(
                checkin_id=original["checkin_id"],
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            replacement = create_checkin(
                club_id=club.id,
                student_id=context["student"].id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=context["training_type"].id,
                source="kiosk",
                checkin_date=context["starts_at"].date(),
            )
            cancel_checkin(
                checkin_id=replacement["checkin_id"],
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )
            with pytest.raises(BusinessLogicError) as exc_info:
                create_checkin(
                    club_id=club.id,
                    student_id=context["student"].id,
                    schedule_id=booking.enrollment.schedule_id,
                    training_type_id=context["training_type"].id,
                    source="kiosk",
                    checkin_date=context["starts_at"].date(),
                )

        assert exc_info.value.code == "personal_drop_in_replacement_limit_reached"


@pytest.mark.django_db
def test_personal_trial_is_rejected_and_legacy_trial_completion_is_exact(club, owner_user):
    lead = StudentFactory(
        club=club,
        status=Student.Status.TRIAL,
        lead_status=Student.LeadStatus.TRIAL_BOOKED,
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        book_trial(club_id=club.id, student_id=lead.id, mode="personal")
    assert exc_info.value.code == "personal_trial_not_supported"

    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    exact_schedule = ScheduleFactory(club=club, training_type=training_type)
    other_schedule = ScheduleFactory(club=club, training_type=training_type)
    target_date = timezone.localdate() + timedelta(days=3)
    ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=exact_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    unrelated = CheckinFactory(
        club=club,
        student=lead,
        schedule=other_schedule,
        training_type=training_type,
        date=target_date,
    )
    exact = CheckinFactory(
        club=club,
        student=lead,
        schedule=exact_schedule,
        training_type=training_type,
        date=target_date,
    )

    with patch("apps.leads.services._trigger_trial_done_side_effects"):
        assert (
            complete_booked_trial_after_checkin(
                club_id=club.id,
                student_id=lead.id,
                checkin=unrelated,
            )
            is False
        )
        assert (
            complete_booked_trial_after_checkin(
                club_id=club.id,
                student_id=lead.id,
                checkin=exact,
            )
            is True
        )


@pytest.mark.django_db
def test_unrelated_trial_checkin_does_not_enqueue_or_complete_post_trial_follow_up(club):
    lead = StudentFactory(
        club=club,
        status=Student.Status.TRIAL,
        lead_status=Student.LeadStatus.TRIAL_BOOKED,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    target_date = timezone.localdate() + timedelta(days=3)
    exact_schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=target_date.weekday(),
    )
    unrelated_schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=target_date.weekday(),
    )
    ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=exact_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=unrelated_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
    )

    with (
        patch("apps.attendance.services.async_task") as async_task,
        patch("apps.leads.services._trigger_trial_done_side_effects") as trigger_trial_done,
    ):
        result = create_checkin(
            club_id=club.id,
            student_id=lead.id,
            schedule_id=unrelated_schedule.id,
            training_type_id=training_type.id,
            source="kiosk",
            checkin_date=target_date,
        )

    post_trial = CheckinCascadeEvent.objects.for_club(club).get(
        checkin_id=result["checkin_id"],
        effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
    )
    lead.refresh_from_db()
    assert post_trial.expected is False
    assert lead.lead_status == Student.LeadStatus.TRIAL_BOOKED
    assert not any(
        call.args[0] == "apps.retention.tasks.create_post_trial_task" for call in async_task.call_args_list
    )
    trigger_trial_done.assert_not_called()


@pytest.mark.django_db
def test_grandfathered_personal_trial_completes_once_after_exact_checkin(club, owner_user):
    lead = StudentFactory(
        club=club,
        status=Student.Status.TRIAL,
        lead_status=Student.LeadStatus.TRIAL_BOOKED,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    target_date = timezone.localdate() + timedelta(days=3)
    schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=target_date.weekday(),
    )
    ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )

    with (
        patch("apps.attendance.services.async_task"),
        patch("apps.leads.services._trigger_trial_done_side_effects") as trigger_trial_done,
    ):
        created = create_checkin(
            club_id=club.id,
            student_id=lead.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source="kiosk",
            checkin_date=target_date,
        )
        replay = create_checkin(
            club_id=club.id,
            student_id=lead.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source="kiosk",
            checkin_date=target_date,
        )

    lead.refresh_from_db()
    post_trial = CheckinCascadeEvent.objects.for_club(club).get(
        checkin_id=created["checkin_id"],
        effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
    )
    assert created["created"] is True
    assert replay["created"] is False
    assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
    assert post_trial.expected is True
    assert (
        LeadLifecycleEvent.objects.for_club(club)
        .filter(student=lead, event_type=LeadLifecycleEvent.EventType.TRIAL_DONE)
        .count()
        == 1
    )
    trigger_trial_done.assert_called_once_with(club_id=club.id, student_id=lead.id)
