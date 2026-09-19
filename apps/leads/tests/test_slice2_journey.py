from datetime import timedelta
from decimal import Decimal
from threading import Event, Thread
from time import monotonic

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    ScheduleEnrollment,
)
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import BankPaymentOrder, Payment, Subscription, TrainingType
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.leads.selectors import get_lead_action_context
from apps.leads.services import (
    LeadClaimConflictError,
    book_trial,
    claim_lead,
    lose_lead,
    record_contact_outcome,
    reopen_lead,
    update_lead_status,
)
from apps.leads.tests.factories import LeadFactory
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _enable_unified(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={"unified_client_journey_enabled": True},
    )


@pytest.mark.django_db
def test_action_context_uses_payment_personal_trial_task_precedence(club, owner_user):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)
    now = timezone.now()
    RetentionTask.objects.create(
        club=club,
        student=lead,
        trainer=trainer,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        due_date=timezone.localdate(),
    )
    trial_schedule = ScheduleFactory(club=club, trainer=trainer)
    ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=trial_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        trial_at=now + timedelta(days=3),
    )
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type)
    reservation = PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=lead,
        trainer=trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
        starts_at=now + timedelta(days=2),
        ends_at=now + timedelta(days=2, hours=1),
        status=PersonalBookingPaymentReservation.Status.BOOKED,
        expires_at=now + timedelta(days=1),
        created_by=owner_user,
    )
    PaymentFactory(
        club=club,
        student=lead,
        tariff=tariff,
        status=Payment.Status.PENDING,
        payment_method=Payment.Method.CASH,
        recorded_by=owner_user,
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == (
        "personal_booking_reservation"
    )

    manual_payment = PaymentFactory(
        club=club,
        student=lead,
        tariff=tariff,
        status=Payment.Status.PENDING,
        payment_method=Payment.Method.CASH,
        recorded_by=owner_user,
        target_schedule=trial_schedule,
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "payment"
    assert context["primary_action"]["target_resource_id"] == manual_payment.id
    assert context["active_context"] == "payment"

    subscription = SubscriptionFactory(
        club=club,
        student=lead,
        tariff=tariff,
        status=Subscription.Status.PENDING,
        paid_amount=Decimal("5000"),
    )
    online_payment = PaymentFactory(
        club=club,
        student=lead,
        tariff=tariff,
        subscription=subscription,
        status=Payment.Status.PENDING,
        payment_method=Payment.Method.ONLINE,
        recorded_by=owner_user,
    )
    order = BankPaymentOrder.objects.create(
        club=club,
        payment=online_payment,
        subscription=subscription,
        student=lead,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.TRAINER,
        status=BankPaymentOrder.Status.FAILED,
        amount_snapshot=Decimal("5000"),
        purpose_snapshot="Personal session",
        expires_at=now + timedelta(hours=1),
        created_by=owner_user,
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "bank_payment_order"
    assert context["primary_action"]["target_resource_id"] == order.id

    order.status = BankPaymentOrder.Status.CANCELLED
    order.save(update_fields=["status", "updated_at"])
    manual_payment.status = Payment.Status.REJECTED
    manual_payment.save(update_fields=["status", "updated_at"])
    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "personal_booking_reservation"
    assert context["primary_action"]["target_resource_id"] == reservation.id
    assert context["active_context"] == "personal_booking"


@pytest.mark.django_db
def test_action_context_reads_exact_pay_at_visit_and_personal_enrollment(
    club,
    owner_user,
):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type)
    starts_on = timezone.localdate() + timedelta(days=2)
    schedule = ScheduleFactory(
        club=club,
        trainer=trainer,
        training_type=training_type,
        one_time_date=starts_on,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=starts_on,
        ends_on=starts_on,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "schedule_enrollment"
    assert context["active_context"] == "personal_booking"

    booking = PersonalDropInBooking.objects.create(
        club=club,
        enrollment=enrollment,
        tariff=tariff,
        tariff_name_snapshot=tariff.name,
        price_snapshot=Decimal("5000"),
        created_by=owner_user,
        idempotency_key="slice2-pay-at-visit",
    )
    enrollment.created_from = ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN
    enrollment.save(update_fields=["created_from", "updated_at"])

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == (
        "personal_drop_in_booking"
    )
    assert context["primary_action"]["target_resource_id"] == booking.id
    assert context["active_context"] == "payment"


@pytest.mark.django_db
def test_action_context_requires_exact_trial_done_event(club):
    lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)

    legacy_context = get_lead_action_context(club=club, lead=lead)
    assert legacy_context["primary_action"]["kind"] == "contact_lead"
    assert legacy_context["primary_action"]["context"] == "lead_task_or_stage"
    assert "не подтверждён" in legacy_context["primary_action"]["supporting_text"]

    LeadLifecycleEvent.objects.create(
        club=club,
        student=lead,
        event_type=LeadLifecycleEvent.EventType.TRIAL_DONE,
        old_lead_status=Student.LeadStatus.TRIAL_BOOKED,
        new_lead_status=Student.LeadStatus.TRIAL_DONE,
    )
    exact_context = get_lead_action_context(club=club, lead=lead)
    assert exact_context["primary_action"]["kind"] == "sell_training"
    assert exact_context["primary_action"]["context"] == "trial_done"

    LeadLifecycleEvent.objects.create(
        club=club,
        student=lead,
        event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
        old_lead_status=Student.LeadStatus.TRIAL_DONE,
        new_lead_status=Student.LeadStatus.TRIAL_DONE,
        metadata={"outcome": "sell_group"},
    )
    preserved_exact_context = get_lead_action_context(club=club, lead=lead)
    assert preserved_exact_context["primary_action"]["kind"] == "sell_training"

    LeadLifecycleEvent.objects.create(
        club=club,
        student=lead,
        event_type=LeadLifecycleEvent.EventType.STATUS_CHANGED,
        old_lead_status=Student.LeadStatus.TRIAL_BOOKED,
        new_lead_status=Student.LeadStatus.TRIAL_DONE,
    )
    later_legacy_context = get_lead_action_context(club=club, lead=lead)
    assert later_legacy_context["primary_action"]["kind"] == "contact_lead"
    assert later_legacy_context["primary_action"]["context"] == "lead_task_or_stage"


@pytest.mark.django_db
def test_action_context_ignores_expired_pending_reservation_without_hiding_enrollment(
    club,
    owner_user,
):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)
    starts_on = club_localdate(club) + timedelta(days=2)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    schedule = ScheduleFactory(
        club=club,
        trainer=trainer,
        training_type=training_type,
        one_time_date=starts_on,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=lead,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=starts_on,
        ends_on=starts_on,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    now = timezone.now()
    PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=lead,
        trainer=trainer,
        location=schedule.location,
        training_type=training_type,
        tariff=tariff,
        enrollment=enrollment,
        schedule=schedule,
        starts_at=now + timedelta(days=2),
        ends_at=now + timedelta(days=2, hours=1),
        status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
        expires_at=now - timedelta(minutes=5),
        created_by=owner_user,
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "schedule_enrollment"
    assert context["primary_action"]["target_resource_id"] == enrollment.id


@pytest.mark.django_db
def test_action_context_exact_due_task_beats_generic_stage(club):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)
    task = RetentionTask.objects.create(
        club=club,
        student=lead,
        trainer=trainer,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        due_date=club_localdate(club) + timedelta(days=2),
    )

    context = get_lead_action_context(club=club, lead=lead)
    assert context["primary_action"]["target_resource_type"] == "retention_task"
    assert context["primary_action"]["target_resource_id"] == task.id


@pytest.mark.django_db
def test_contact_outcomes_reuse_one_task_and_terminal_close_it(club, owner_user):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)

    first, _ = record_contact_outcome(
        club_id=club.id,
        student_id=lead.id,
        outcome="no_answer",
        due_date=club_localdate(club) + timedelta(days=1),
        actor_user_id=owner_user.id,
    )
    second, next_flow = record_contact_outcome(
        club_id=club.id,
        student_id=lead.id,
        outcome="book_trial",
        actor_user_id=owner_user.id,
    )

    assert first.id == second.id == lead.id
    assert next_flow == "book_trial"
    assert RetentionTask.objects.for_club(club).filter(
        student=lead,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        resolved_at__isnull=True,
    ).count() == 1
    assert RetentionTask.objects.for_club(club).get(
        student=lead,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        resolved_at__isnull=True,
    ).due_date == club_localdate(club)
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=lead,
        event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
    ).count() == 2

    lost, _ = record_contact_outcome(
        club_id=club.id,
        student_id=lead.id,
        outcome="lost",
        loss_reason=Student.LossReason.TOO_FAR,
        actor_user_id=owner_user.id,
    )
    task = RetentionTask.objects.for_club(club).get(
        student=lead,
        task_type=RetentionTask.TaskType.NEW_LEAD,
    )
    assert lost.status == Student.Status.LOST
    assert lost.lead_status is None
    assert task.resolution == RetentionTask.Resolution.LEAD_CLOSED
    assert task.resolved_at is not None


@pytest.mark.django_db
def test_no_answer_requires_explicit_next_attempt_date(club, owner_user):
    trainer = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=trainer)

    with pytest.raises(BusinessLogicError) as exc_info:
        record_contact_outcome(
            club_id=club.id,
            student_id=lead.id,
            outcome="no_answer",
            actor_user_id=owner_user.id,
        )

    assert exc_info.value.code == "contact_due_date_required"
    assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()


@pytest.mark.django_db
def test_trainer_scope_is_revalidated_by_all_lead_mutation_services(club):
    expected = TrainerFactory(club=club)
    current = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=current)

    mutation_calls = (
        lambda: update_lead_status(
            club_id=club.id,
            student_id=lead.id,
            new_status=Student.LeadStatus.CONTACTED,
            required_assigned_trainer_id=expected.id,
        ),
        lambda: book_trial(
            club_id=club.id,
            student_id=lead.id,
            schedule_id=999999,
            occurrence_date=club_localdate(club),
            required_assigned_trainer_id=expected.id,
        ),
        lambda: lose_lead(
            club_id=club.id,
            student_id=lead.id,
            loss_reason=Student.LossReason.TOO_FAR,
            required_assigned_trainer_id=expected.id,
        ),
        lambda: record_contact_outcome(
            club_id=club.id,
            student_id=lead.id,
            outcome="contacted",
            required_assigned_trainer_id=expected.id,
        ),
    )
    for mutation in mutation_calls:
        with pytest.raises(BusinessLogicError) as exc_info:
            mutation()
        assert exc_info.value.code == "not_your_lead"

    archived = LeadFactory(
        club=club,
        assigned_trainer=current,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        reopen_lead(
            club_id=club.id,
            student_id=archived.id,
            required_assigned_trainer_id=expected.id,
        )
    assert exc_info.value.code == "not_your_lead"


@pytest.mark.django_db
def test_manual_trial_done_is_flag_gated(settings, club, owner_user):
    _enable_unified(settings, club)
    lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_BOOKED)

    blocked = client.post(
        f"/leads/{lead.id}/status",
        json={"status": Student.LeadStatus.TRIAL_DONE},
        **_auth_params(owner_user, club),
    )
    assert blocked.status_code == 400
    assert blocked.json()["code"] == "trial_done_requires_checkin"
    lead.refresh_from_db()
    assert lead.lead_status == Student.LeadStatus.TRIAL_BOOKED

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    allowed = client.post(
        f"/leads/{lead.id}/status",
        json={"status": Student.LeadStatus.TRIAL_DONE},
        **_auth_params(owner_user, club),
    )
    assert allowed.status_code == 200
    assert allowed.json()["lead_status"] == Student.LeadStatus.TRIAL_DONE


@pytest.mark.django_db
def test_archive_list_and_reopen_preserve_same_student_id(settings, club, trainer_user):
    _enable_unified(settings, club)
    trainer = TrainerFactory(club=club, user=trainer_user)
    archived = LeadFactory(
        club=club,
        assigned_trainer=trainer,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )

    listed = client.get(
        "/leads/?workspace=archived&scope=mine",
        **_auth_params(trainer_user, club, role="trainer"),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [archived.id]

    reopened = client.post(
        f"/leads/{archived.id}/reopen",
        **_auth_params(trainer_user, club, role="trainer"),
    )
    assert reopened.status_code == 200
    assert reopened.json()["id"] == archived.id
    archived.refresh_from_db()
    assert archived.status == Student.Status.LEAD
    assert archived.lead_status == Student.LeadStatus.NEW
    assert archived.became_student_at is None


@pytest.mark.django_db
def test_unified_direct_lead_reload_returns_generic_404_for_other_trainer(
    settings,
    club,
    trainer_user,
):
    _enable_unified(settings, club)
    TrainerFactory(club=club, user=trainer_user)
    other = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=other)

    response = client.get(
        f"/leads/{lead.id}",
        **_auth_params(trainer_user, club, role="trainer"),
    )

    assert response.status_code == 404
    mutation = client.post(
        f"/leads/{lead.id}/contact-outcomes/",
        json={"outcome": "contacted"},
        **_auth_params(trainer_user, club, role="trainer"),
    )
    assert mutation.status_code == 404
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student=lead,
        event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
    ).exists()


@pytest.mark.django_db
def test_claim_rejects_non_lead_and_cross_tenant_ids_without_identity_disclosure(
    settings,
    club,
    trainer_user,
):
    _enable_unified(settings, club)
    trainer = TrainerFactory(club=club, user=trainer_user)
    other_club = ClubFactory()
    candidates = [
        StudentFactory(
            club=club,
            assigned_trainer=None,
            status=Student.Status.TRIAL,
            lead_status=None,
            became_student_at=timezone.now(),
        ),
        StudentFactory(
            club=club,
            assigned_trainer=None,
            status=Student.Status.LOST,
            lead_status=None,
            became_student_at=None,
        ),
        LeadFactory(club=other_club, assigned_trainer=None),
    ]

    for candidate in candidates:
        response = client.post(
            f"/leads/{candidate.id}/claim",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 404
        if candidate.phone:
            assert candidate.phone not in response.content.decode()

    for candidate in candidates[:2]:
        candidate.refresh_from_db()
        assert candidate.assigned_trainer_id is None
    assert trainer.club_id == club.id


@pytest.mark.django_db
def test_contact_outcome_reassignment_race_returns_generic_404(
    settings,
    club,
    trainer_user,
    monkeypatch,
):
    _enable_unified(settings, club)
    former = TrainerFactory(club=club, user=trainer_user)
    current = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=former)

    def raced_contact_outcome(**kwargs):
        Student.objects.for_club(club).filter(id=lead.id).update(
            assigned_trainer_id=current.id
        )
        return record_contact_outcome(**kwargs)

    monkeypatch.setattr("apps.leads.api.record_contact_outcome", raced_contact_outcome)
    response = client.post(
        f"/leads/{lead.id}/contact-outcomes/",
        json={"outcome": "contacted"},
        **_auth_params(trainer_user, club, role="trainer"),
    )

    assert response.status_code == 404
    lead.refresh_from_db()
    assert lead.lead_status == Student.LeadStatus.NEW
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student=lead,
        event_type=LeadLifecycleEvent.EventType.CONTACT_OUTCOME_RECORDED,
    ).exists()


@pytest.mark.django_db
def test_reopen_and_claim_race_loser_returns_conflict(
    settings,
    club,
    trainer_user,
    monkeypatch,
):
    _enable_unified(settings, club)
    TrainerFactory(club=club, user=trainer_user)
    archived = LeadFactory(
        club=club,
        assigned_trainer=None,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )

    def already_claimed(**_kwargs):
        raise BusinessLogicError("Lead is not archived", code="lead_not_archived")

    monkeypatch.setattr("apps.leads.api.reopen_lead", already_claimed)
    response = client.post(
        f"/leads/{archived.id}/reopen-and-claim",
        **_auth_params(trainer_user, club, role="trainer"),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Lead was already claimed"


def _claim_in_thread(*, club_id: int, student_id: int, trainer_id: int) -> str:
    close_old_connections()
    try:
        claim_lead(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
        )
        return "claimed"
    except LeadClaimConflictError:
        return "conflict"
    finally:
        close_old_connections()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_pool_claim_race_has_one_winner(club):
    first = TrainerFactory(club=club)
    second = TrainerFactory(club=club)
    lead = LeadFactory(club=club, assigned_trainer=None)
    first_row_locked = Event()
    release_first_claim = Event()
    first_finished = Event()
    second_started = Event()
    second_finished = Event()
    backend_pids = {}
    results = {}
    errors = {}

    def current_backend_pid():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def second_backend_waits_on_first_transaction():
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

    def wait_for_second_backend_to_block_on_first_transaction():
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if second_backend_waits_on_first_transaction():
                return True
        return False

    def hold_exact_lead_lock_then_claim():
        close_old_connections()
        try:
            with transaction.atomic():
                locked_lead = (
                    Student.objects.for_club(club.id)
                    .select_for_update()
                    .get(id=lead.id, deleted_at__isnull=True)
                )
                assert locked_lead.id == lead.id
                backend_pids["first"] = current_backend_pid()
                first_row_locked.set()
                if not release_first_claim.wait(timeout=5):
                    raise TimeoutError("test did not release first claim")
                claim_lead(
                    club_id=club.id,
                    student_id=lead.id,
                    trainer_id=first.id,
                )
                results["first"] = "claimed"
        except BaseException as exc:
            errors["first"] = exc
        finally:
            first_finished.set()
            close_old_connections()

    def contend_for_claim():
        close_old_connections()
        try:
            backend_pids["second"] = current_backend_pid()
            second_started.set()
            claim_lead(
                club_id=club.id,
                student_id=lead.id,
                trainer_id=second.id,
            )
            results["second"] = "claimed"
        except LeadClaimConflictError:
            results["second"] = "conflict"
        except BaseException as exc:
            errors["second"] = exc
        finally:
            second_finished.set()
            close_old_connections()

    first_thread = Thread(target=hold_exact_lead_lock_then_claim)
    first_thread.start()
    assert first_row_locked.wait(timeout=5)

    second_thread = Thread(target=contend_for_claim)
    second_thread.start()
    assert second_started.wait(timeout=5)
    try:
        assert wait_for_second_backend_to_block_on_first_transaction()
    finally:
        release_first_claim.set()
    assert first_finished.wait(timeout=5)
    assert second_finished.wait(timeout=5)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == {}
    assert sorted(results.values()) == ["claimed", "conflict"]
    lead.refresh_from_db()
    assert lead.assigned_trainer_id in {first.id, second.id}
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=lead,
        event_type=LeadLifecycleEvent.EventType.LEAD_CLAIMED,
    ).count() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_archived_reopen_and_claim_race_has_one_winner(club):
    first = TrainerFactory(club=club)
    second = TrainerFactory(club=club)
    archived = LeadFactory(
        club=club,
        assigned_trainer=None,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )
    first_row_locked = Event()
    release_first_reopen = Event()
    first_finished = Event()
    second_started = Event()
    second_finished = Event()
    backend_pids = {}
    results = {}
    errors = {}

    def current_backend_pid():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def second_backend_waits_on_first_transaction():
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

    def wait_for_second_backend_to_block_on_first_transaction():
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if second_backend_waits_on_first_transaction():
                return True
        return False

    def hold_exact_archived_lead_lock_then_reopen():
        close_old_connections()
        try:
            with transaction.atomic():
                locked_lead = (
                    Student.objects.for_club(club.id)
                    .select_for_update()
                    .get(id=archived.id, deleted_at__isnull=True)
                )
                assert locked_lead.id == archived.id
                assert locked_lead.status == Student.Status.LOST
                assert locked_lead.lead_status is None
                backend_pids["first"] = current_backend_pid()
                first_row_locked.set()
                if not release_first_reopen.wait(timeout=5):
                    raise TimeoutError("test did not release first reopen")
                reopen_lead(
                    club_id=club.id,
                    student_id=archived.id,
                    claim_trainer_id=first.id,
                )
                results["first"] = "claimed"
        except BaseException as exc:
            errors["first"] = exc
        finally:
            first_finished.set()
            close_old_connections()

    def contend_for_archived_reopen():
        close_old_connections()
        try:
            backend_pids["second"] = current_backend_pid()
            second_started.set()
            reopen_lead(
                club_id=club.id,
                student_id=archived.id,
                claim_trainer_id=second.id,
            )
            results["second"] = "claimed"
        except LeadClaimConflictError:
            results["second"] = "conflict"
        except BusinessLogicError as exc:
            if exc.code == "lead_not_archived":
                results["second"] = "conflict"
            else:
                errors["second"] = exc
        except BaseException as exc:
            errors["second"] = exc
        finally:
            second_finished.set()
            close_old_connections()

    first_thread = Thread(target=hold_exact_archived_lead_lock_then_reopen)
    first_thread.start()
    assert first_row_locked.wait(timeout=5)

    second_thread = Thread(target=contend_for_archived_reopen)
    second_thread.start()
    assert second_started.wait(timeout=5)
    try:
        assert wait_for_second_backend_to_block_on_first_transaction()
    finally:
        release_first_reopen.set()
    assert first_finished.wait(timeout=5)
    assert second_finished.wait(timeout=5)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == {}
    assert sorted(results.values()) == ["claimed", "conflict"]
    archived.refresh_from_db()
    assert archived.assigned_trainer_id == first.id
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=archived,
        event_type=LeadLifecycleEvent.EventType.LEAD_REOPENED,
    ).count() == 1
    task = RetentionTask.objects.for_club(club).get(
        student=archived,
        task_type=RetentionTask.TaskType.NEW_LEAD,
        resolved_at__isnull=True,
    )
    assert task.trainer_id == first.id
    assert task.due_date == club_localdate(club)
