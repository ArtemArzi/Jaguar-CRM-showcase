from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from time import monotonic

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone
from ninja.testing import TestClient

import apps.attendance.services.personal_reschedule as personal_reschedule_service
from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalDropInBooking,
    PersonalServiceTermsSnapshot,
    ScheduleBookingEvent,
)
from apps.attendance.personal_offers import personal_offer_payload
from apps.attendance.services.enrollment import book_personal_availability_slot
from apps.attendance.services.personal_reschedule import reschedule_personal_exact_booking
from apps.attendance.services.staff_intents import (
    get_personal_commercial_context,
    get_staff_direct_personal_offer,
    submit_staff_personal_intent,
)
from apps.billing.models import BankPaymentOrder, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.tests.factories import (
    DiscountFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubFactory, LocationFactory, UserFactory
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _enable_unified_personal(*, settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})


def _complete_personal_tariff(*, club, training_type, location, trainer, price):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=price,
        trainings_limit=1,
        duration_days=30,
        scope=Tariff.Scope.LOCATION,
        location=location,
        personal_booking_trainer=trainer,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=price,
    )
    update_tariff(tariff_id=tariff.id, club_id=club.id, is_personal_booking_default=True)
    return tariff


def _availability_slot(*, club, trainer, location, training_type, day_offset, hour):
    starts_at = (timezone.now() + timedelta(days=day_offset)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )


def _paid_drop_in_context(*, settings, club, owner):
    _enable_unified_personal(settings=settings, club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_personal_tariff(
        club=club,
        training_type=training_type,
        location=location,
        trainer=trainer,
        price=Decimal("1800.00"),
    )
    discount = DiscountFactory(
        club=club,
        name="Personal reschedule discount",
        discount_type="fixed",
        value=Decimal("300.00"),
    )
    student = StudentFactory(club=club, status=Student.Status.LEAD)
    source_slot = _availability_slot(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_offset=8,
        hour=10,
    )
    destination_slot = _availability_slot(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_offset=9,
        hour=14,
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
        discount_id=discount.id,
    )
    receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=source_slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=personal_offer_payload(slot=source_slot, offer=offer)["offer_digest"],
        discount_id=discount.id,
        idempotency_key="personal-reschedule-paid-source",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    booking = PersonalDropInBooking.objects.get(id=receipt["booking_id"])
    return {
        "training_type": training_type,
        "location": location,
        "trainer": trainer,
        "student": student,
        "source_slot": source_slot,
        "destination_slot": destination_slot,
        "booking": booking,
    }


@pytest.mark.django_db
def test_reschedule_paid_discounted_drop_in_preserves_terms_and_receipt_time(settings, club, monkeypatch):
    owner = UserFactory()
    context = _paid_drop_in_context(settings=settings, club=club, owner=owner)
    booking = context["booking"]
    terms = PersonalServiceTermsSnapshot.objects.get(booking=booking)
    payment = booking.payment_links.select_related("payment__subscription").get().payment
    source_starts_at = context["source_slot"].starts_at
    original_terms = (
        terms.terms_version,
        terms.tariff_id_snapshot,
        terms.base_amount,
        terms.discount_amount,
        terms.discount_id_snapshot,
        terms.payable_amount,
        payment.id,
        payment.subscription_id,
        payment.amount,
    )
    ordered_lock_calls = []
    original_lock = personal_reschedule_service.lock_complete_personal_scopes

    def capture_complete_personal_lock(**kwargs):
        ordered_lock_calls.append(kwargs)
        return original_lock(**kwargs)

    monkeypatch.setattr(
        personal_reschedule_service,
        "lock_complete_personal_scopes",
        capture_complete_personal_lock,
    )

    result = reschedule_personal_exact_booking(
        club_id=club.id,
        enrollment_id=booking.enrollment_id,
        destination_slot_id=context["destination_slot"].id,
        actor_user_id=owner.id,
        reason="Клиент попросил другое время",
        idempotency_key="personal-reschedule-paid-v2",
    )

    assert ordered_lock_calls == [
        {
            "club_id": club.id,
            "booking_ids": [booking.id],
            "extra_slot_ids": [context["destination_slot"].id],
        }
    ]

    booking.refresh_from_db()
    terms.refresh_from_db()
    payment.refresh_from_db()
    context["source_slot"].refresh_from_db()
    context["destination_slot"].refresh_from_db()
    result.enrollment.refresh_from_db()
    result.schedule.refresh_from_db()
    assert result.created is True
    assert result.booking is not None
    assert result.booking.id == booking.id
    assert (
        terms.terms_version,
        terms.tariff_id_snapshot,
        terms.base_amount,
        terms.discount_amount,
        terms.discount_id_snapshot,
        terms.payable_amount,
        payment.id,
        payment.subscription_id,
        payment.amount,
    ) == original_terms
    assert context["source_slot"].status == PersonalAvailabilitySlot.Status.PUBLISHED
    assert context["source_slot"].booked_enrollment_id is None
    assert context["destination_slot"].status == PersonalAvailabilitySlot.Status.BOOKED
    assert context["destination_slot"].booked_enrollment_id == booking.enrollment_id
    destination_local_starts_at = timezone.localtime(context["destination_slot"].starts_at, club_zoneinfo(club))
    assert result.enrollment.starts_on == destination_local_starts_at.date()
    assert result.enrollment.ends_on == destination_local_starts_at.date()
    assert result.schedule.one_time_date == destination_local_starts_at.date()
    assert result.schedule.start_time == destination_local_starts_at.time().replace(tzinfo=None)
    event = ScheduleBookingEvent.objects.get(
        enrollment_id=booking.enrollment_id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
    )
    assert event.actor_id == owner.id
    assert event.metadata["source_availability_slot_id"] == context["source_slot"].id
    assert event.metadata["destination_availability_slot_id"] == context["destination_slot"].id
    assert event.metadata["source_starts_at"] == timezone.localtime(
        source_starts_at,
        club_zoneinfo(club),
    ).isoformat()
    context_receipt = get_personal_commercial_context(
        club_id=club.id,
        student_id=context["student"].id,
    )
    receipt = next(item for item in context_receipt if item["booking_id"] == booking.id)
    assert receipt["slot_id"] == context["destination_slot"].id
    assert receipt["starts_at"] == context["destination_slot"].starts_at

    replay = client.post(
        f"/personal-drop-in-bookings/{booking.id}/reschedule/",
        json={
            "destination_slot_id": context["destination_slot"].id,
            "reason": "Клиент попросил другое время",
            "idempotency_key": "personal-reschedule-paid-v2",
        },
        **make_auth_params(owner, club, role="owner"),
    )
    assert replay.status_code == 200
    assert replay.json()["availability_slot_id"] == context["destination_slot"].id
    assert (
        ScheduleBookingEvent.objects.filter(
            enrollment_id=booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
        ).count()
        == 1
    )

    another_destination = _availability_slot(
        club=club,
        trainer=context["trainer"],
        location=context["location"],
        training_type=context["training_type"],
        day_offset=10,
        hour=16,
    )
    with pytest.raises(BusinessLogicError) as destination_conflict:
        reschedule_personal_exact_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=another_destination.id,
            actor_user_id=owner.id,
            reason="Клиент попросил другое время",
            idempotency_key="personal-reschedule-paid-v2",
        )
    assert destination_conflict.value.code == "personal_reschedule_idempotency_conflict"
    with pytest.raises(BusinessLogicError) as reason_conflict:
        reschedule_personal_exact_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=context["destination_slot"].id,
            actor_user_id=owner.id,
            reason="Другая причина",
            idempotency_key="personal-reschedule-paid-v2",
        )
    assert reason_conflict.value.code == "personal_reschedule_idempotency_conflict"
    another_destination.refresh_from_db()
    assert another_destination.status == PersonalAvailabilitySlot.Status.PUBLISHED


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL observable transaction wait semantics",
)
def test_postgresql_two_reschedules_to_one_destination_have_one_observable_winner(
    settings,
    club,
    monkeypatch,
):
    owner = UserFactory()
    context = _paid_drop_in_context(settings=settings, club=club, owner=owner)
    first_booking = context["booking"]
    second_student = StudentFactory(club=club, status=Student.Status.LEAD)
    second_source = _availability_slot(
        club=club,
        trainer=context["trainer"],
        location=context["location"],
        training_type=context["training_type"],
        day_offset=8,
        hour=12,
    )
    second_offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=context["trainer"].id,
        training_type_id=context["training_type"].id,
        location_id=context["location"].id,
    )
    second_receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=second_source.id,
        student_id=second_student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=personal_offer_payload(slot=second_source, offer=second_offer)["offer_digest"],
        discount_id=None,
        idempotency_key="personal-reschedule-second-paid-source",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    second_booking = PersonalDropInBooking.objects.get(id=second_receipt["booking_id"])
    first_scope_locked = Event()
    release_first = Event()
    second_started = Event()
    backend_pids: dict[str, int] = {}
    original_locked_enrollment = personal_reschedule_service._locked_enrollment

    def current_backend_pid() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def second_waits_on_first() -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks AS waiter
                    JOIN pg_locks AS holder
                        ON holder.locktype = 'transactionid'
                        AND holder.transactionid = waiter.transactionid
                        AND holder.granted
                    WHERE waiter.pid = %s
                        AND holder.pid = %s
                        AND waiter.locktype = 'transactionid'
                        AND NOT waiter.granted
                )
                """,
                [backend_pids["second"], backend_pids["first"]],
            )
            return cursor.fetchone()[0]

    def wait_for_observable_contention() -> bool:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if second_waits_on_first():
                return True
        return False

    def pause_first_after_complete_scope(*, club_id, enrollment_id):
        if enrollment_id == first_booking.enrollment_id:
            backend_pids["first"] = current_backend_pid()
            first_scope_locked.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("test did not release first reschedule")
        return original_locked_enrollment(club_id=club_id, enrollment_id=enrollment_id)

    monkeypatch.setattr(personal_reschedule_service, "_locked_enrollment", pause_first_after_complete_scope)

    def run_reschedule(*, booking, key, worker_name):
        close_old_connections()
        try:
            if worker_name == "second":
                backend_pids["second"] = current_backend_pid()
                second_started.set()
            result = reschedule_personal_exact_booking(
                club_id=club.id,
                enrollment_id=booking.enrollment_id,
                destination_slot_id=context["destination_slot"].id,
                actor_user_id=owner.id,
                reason="Concurrent destination proof",
                idempotency_key=key,
            )
            return "rescheduled", result.enrollment.id
        except BusinessLogicError as exc:
            return "error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            run_reschedule,
            booking=first_booking,
            key="pg-personal-reschedule-first",
            worker_name="first",
        )
        assert first_scope_locked.wait(timeout=10)
        second_future = executor.submit(
            run_reschedule,
            booking=second_booking,
            key="pg-personal-reschedule-second",
            worker_name="second",
        )
        assert second_started.wait(timeout=10)
        assert wait_for_observable_contention()
        release_first.set()
        outcomes = [first_future.result(timeout=20), second_future.result(timeout=20)]

    assert ("rescheduled", first_booking.enrollment_id) in outcomes
    assert ("error", "personal_reschedule_destination_unavailable") in outcomes
    context["destination_slot"].refresh_from_db()
    assert context["destination_slot"].booked_enrollment_id == first_booking.enrollment_id


@pytest.mark.django_db
def test_reschedule_entitlement_and_direct_drop_in_source(settings, club, monkeypatch):
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = TariffFactory(club=club, training_type=training_type, scope=Tariff.Scope.LOCATION, location=location)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
    source_slot = _availability_slot(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_offset=8,
        hour=10,
    )
    destination_slot = _availability_slot(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_offset=9,
        hour=12,
    )
    entitlement = book_personal_availability_slot(
        club_id=club.id,
        slot_id=source_slot.id,
        student_id=student.id,
        actor_user_id=owner.id,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        subscription_id=subscription.id,
        idempotency_key="personal-reschedule-entitlement",
    )
    ordered_lock_calls = []
    original_lock = personal_reschedule_service.lock_personal_booking_enrollment_scope

    def capture_personal_booking_lock(**kwargs):
        ordered_lock_calls.append(kwargs)
        return original_lock(**kwargs)

    monkeypatch.setattr(
        personal_reschedule_service,
        "lock_personal_booking_enrollment_scope",
        capture_personal_booking_lock,
    )

    entitlement_result = reschedule_personal_exact_booking(
        club_id=club.id,
        enrollment_id=entitlement.enrollment.id,
        destination_slot_id=destination_slot.id,
        actor_user_id=owner.id,
        reason="Перенос по договоренности",
    )

    assert ordered_lock_calls == [
        {
            "club_id": club.id,
            "enrollment_id": entitlement.enrollment.id,
            "extra_slot_ids": [destination_slot.id],
        }
    ]

    entitlement_event = ScheduleBookingEvent.objects.get(
        enrollment_id=entitlement.enrollment.id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
    )
    assert entitlement_event.metadata["subscription_id"] == subscription.id
    assert entitlement_result.booking is None
    destination_slot.refresh_from_db()
    assert destination_slot.booked_enrollment_id == entitlement.enrollment.id

    _enable_unified_personal(settings=settings, club=club)
    _complete_personal_tariff(
        club=club,
        training_type=training_type,
        location=location,
        trainer=trainer,
        price=Decimal("1700.00"),
    )
    direct_student = StudentFactory(club=club, status=Student.Status.LEAD)
    direct_starts_at = (timezone.now() + timedelta(days=11)).replace(hour=16, minute=0, second=0, microsecond=0)
    from apps.attendance.services.staff_intents import submit_staff_direct_personal_intent

    direct_offer = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=direct_starts_at,
        ends_at=direct_starts_at + timedelta(hours=1),
        training_type_id=training_type.id,
        location_id=location.id,
    )

    direct_result = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=direct_student.id,
        trainer_id=trainer.id,
        starts_at=direct_starts_at,
        ends_at=direct_starts_at + timedelta(hours=1),
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=direct_offer["offer_digest"],
        discount_id=None,
        idempotency_key="personal-reschedule-direct-source",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    direct_booking_id = direct_result.receipt["booking_id"]
    direct_booking = PersonalDropInBooking.objects.get(id=direct_booking_id)
    direct_target = _availability_slot(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_offset=13,
        hour=16,
    )
    direct_reschedule = reschedule_personal_exact_booking(
        club_id=club.id,
        enrollment_id=direct_booking.enrollment_id,
        destination_slot_id=direct_target.id,
        actor_user_id=owner.id,
        reason="Перенос прямой записи",
    )
    assert direct_reschedule.source_slot_id is None
    direct_target.refresh_from_db()
    assert direct_target.booked_enrollment_id == direct_booking.enrollment_id
    assert direct_booking.enrollment.personal_availability_slots.count() == 1
    assert direct_booking.enrollment.personal_availability_slots.get().id == direct_target.id


@pytest.mark.django_db
def test_reschedule_conflict_rolls_back_and_trainer_cannot_enumerate_other_booking(settings, club):
    owner = UserFactory()
    context = _paid_drop_in_context(settings=settings, club=club, owner=owner)
    booking = context["booking"]
    destination = context["destination_slot"]
    destination.status = PersonalAvailabilitySlot.Status.HELD
    destination.save(update_fields=["status", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        reschedule_personal_exact_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=destination.id,
            actor_user_id=owner.id,
            reason="Занятый слот",
        )

    assert exc_info.value.code == "personal_reschedule_destination_unavailable"
    booking.refresh_from_db()
    booking.enrollment.schedule.refresh_from_db()
    context["source_slot"].refresh_from_db()
    destination.refresh_from_db()
    assert context["source_slot"].booked_enrollment_id == booking.enrollment_id
    assert destination.status == PersonalAvailabilitySlot.Status.HELD
    assert booking.enrollment.schedule.one_time_date == context["source_slot"].starts_at.date()
    assert not ScheduleBookingEvent.objects.filter(
        enrollment_id=booking.enrollment_id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
    ).exists()

    booking.state = PersonalDropInBooking.State.NO_SHOW
    booking.save(update_fields=["state", "updated_at"])
    with pytest.raises(BusinessLogicError) as terminal_exc:
        reschedule_personal_exact_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=destination.id,
            actor_user_id=owner.id,
            reason="Просроченная запись",
        )
    assert terminal_exc.value.code == "personal_reschedule_terminal"

    mini_group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
    booking.enrollment.schedule.training_type = mini_group_type
    booking.enrollment.schedule.save(update_fields=["training_type", "updated_at"])
    with pytest.raises(BusinessLogicError) as training_type_exc:
        reschedule_personal_exact_booking(
            club_id=club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=destination.id,
            actor_user_id=owner.id,
            reason="Не персональная запись",
        )
    assert training_type_exc.value.code == "personal_reschedule_source_invalid"

    foreign_club = ClubFactory()
    with pytest.raises(BusinessLogicError) as tenant_exc:
        reschedule_personal_exact_booking(
            club_id=foreign_club.id,
            enrollment_id=booking.enrollment_id,
            destination_slot_id=destination.id,
            actor_user_id=owner.id,
            reason="Чужой клуб",
        )
    assert tenant_exc.value.code == "personal_reschedule_not_found"

    other_trainer_user = UserFactory()
    TrainerFactory(club=club, user=other_trainer_user)
    denied = client.post(
        f"/personal-drop-in-bookings/{booking.id}/reschedule/",
        json={
            "destination_slot_id": destination.id,
            "reason": "Не моя запись",
            "idempotency_key": "foreign-trainer-reschedule",
        },
        **make_auth_params(other_trainer_user, club, role="trainer"),
    )
    assert denied.status_code == 404
