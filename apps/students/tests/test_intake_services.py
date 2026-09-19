import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from threading import Barrier
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.attendance.models import PersonalDropInBooking, ScheduleEnrollment, TrainingGroupMembership
from apps.attendance.selectors import get_expected_student_ids_for_schedule_date
from apps.attendance.services import book_personal_session, create_checkin
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Debt, Payment, Subscription, TrainingType
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubMembership
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadIntakeEvent, LeadLifecycleEvent
from apps.leads.services import create_landing_lead_intake
from apps.students.access_services import open_account_access_for_student
from apps.students.intake_services import submit_student_intake
from apps.students.models import AccountAccess, Student, StudentIntakeCommand
from apps.students.services import create_student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


def _submit(*, club, actor, role, key=None, trainer_id=None, **overrides):
    payload = {
        "actor_trainer_id": trainer_id,
        "idempotency_key": key or uuid4(),
        "intake_kind": "new_contact",
        "first_name": "Ivan",
        "last_name": "Petrov",
        "phone": "+79001234567",
        "guardian_phone": "",
        "date_of_birth": None,
        "is_child": False,
        "source": "other",
        "assigned_trainer_id": None,
        "confirm_distinct_child": False,
    }
    payload.update(overrides)
    return submit_student_intake(
        club_id=club.id,
        actor_user_id=actor.id,
        actor_role=role,
        **payload,
    )


@pytest.mark.django_db
def test_existing_student_intake_sets_provenance_without_finance_attendance_or_access(club, owner_user):
    result = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        intake_kind="existing_student",
        last_name="",
    )

    assert result.result_kind == "created_existing_student"
    assert result.target_workspace == "students"
    assert result.student_id is not None
    student = Student.objects.for_club(club).get(id=result.student_id)
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.last_name == ""
    assert student.crm_entry_kind == Student.CrmEntryKind.EXISTING_STUDENT
    assert student.crm_entered_by_id == owner_user.id
    assert student.became_student_at == student.created_at
    assert result.commercial_segment == "no_crm_entitlement"
    assert not Payment.objects.for_club(club).filter(student=student).exists()
    assert not Subscription.objects.for_club(club).filter(student=student).exists()
    assert not Debt.objects.for_club(club).filter(student=student).exists()
    assert not ScheduleEnrollment.objects.for_club(club).filter(student=student).exists()
    assert not TrainingGroupMembership.objects.for_club(club).filter(student=student).exists()
    assert not PersonalDropInBooking.objects.for_club(club).filter(enrollment__student=student).exists()
    assert not LeadLifecycleEvent.objects.for_club(club).filter(student=student).exists()
    assert not AccountAccess.objects.for_club(club).filter(student=student).exists()


@pytest.mark.django_db
def test_existing_student_intake_has_no_entitlement_for_roster_checkin_booking_or_account_access(club, owner_user):
    result = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        intake_kind="existing_student",
    )
    student = Student.objects.for_club(club).get(id=result.student_id)
    schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday())

    assert student.id not in get_expected_student_ids_for_schedule_date(
        club=club,
        schedule_id=schedule.id,
        target_date=date.today(),
    )
    # Direct check-in sees no roster row; disable task enqueue because no async
    # worker is part of this focused domain test.
    with pytest.raises(BusinessLogicError, match="не записан") as checkin_error:
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=schedule.training_type_id,
            source="manual",
            checkin_date=date.today(),
            _defer_async_until_commit=True,
        )
    assert checkin_error.value.code == "student_schedule_ineligible"

    tariff = TariffFactory(club=club, training_type=schedule.training_type)
    SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=8,
        trainings_used=0,
    )
    assert create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=schedule.id,
        training_type_id=schedule.training_type_id,
        source="manual",
        checkin_date=date.today(),
        _defer_async_until_commit=True,
    )["created"] is True

    legacy = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        crm_entry_kind=Student.CrmEntryKind.LEGACY_UNKNOWN,
    )
    assert create_checkin(
        club_id=club.id,
        student_id=legacy.id,
        schedule_id=schedule.id,
        training_type_id=schedule.training_type_id,
        source="manual",
        checkin_date=date.today(),
        _defer_async_until_commit=True,
    )["created"] is True

    trainer = TrainerFactory(club=club)
    personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    trainer_location = TrainerLocationFactory(club=club, trainer=trainer, location=schedule.location)
    starts_at = datetime.combine(date.today() + timedelta(days=2), time(hour=10))
    with pytest.raises(BusinessLogicError) as booking_error:
        book_personal_session(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            location_id=trainer_location.location_id,
            training_type_id=personal_type.id,
            subscription_id=None,
            actor_user_id=owner_user.id,
        )
    assert booking_error.value.code == "subscription_required_for_personal_booking"

    with pytest.raises(BusinessLogicError) as access_error:
        open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=owner_user.id,
        )
    assert access_error.value.code == "account_access_requires_paid_subscription"


@pytest.mark.django_db
def test_new_contact_trainer_derives_self_assignment_and_stores_safe_receipt(club, trainer_user):
    trainer = TrainerFactory(club=club, user=trainer_user)

    result = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
    )

    student = Student.objects.for_club(club).get(id=result.student_id)
    command = StudentIntakeCommand.objects.for_club(club).get(student=student)
    serialized_receipt = json.dumps(command.result_receipt)
    assert student.assigned_trainer_id == trainer.id
    assert student.status == Student.Status.LEAD
    assert student.lead_status == Student.LeadStatus.NEW
    assert student.crm_entry_kind == Student.CrmEntryKind.LEAD_INTAKE
    assert student.crm_entered_by_id == trainer_user.id
    assert command.request_fingerprint != student.phone
    assert student.phone not in serialized_receipt
    assert trainer_user.email not in serialized_receipt
    assert LeadLifecycleEvent.objects.filter(
        club=club,
        student=student,
        event_type=LeadLifecycleEvent.EventType.LEAD_ASSIGNED,
    ).exists()


@pytest.mark.django_db
def test_intake_same_key_replays_safe_receipt_and_different_payload_conflicts(club, owner_user):
    key = UUID("11111111-1111-4111-8111-111111111111")
    first = _submit(club=club, actor=owner_user, role=ClubMembership.Role.OWNER, key=key)
    replay = _submit(club=club, actor=owner_user, role=ClubMembership.Role.OWNER, key=key)
    mismatch = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        key=key,
        first_name="Other",
    )

    assert replay.replayed is True
    assert replay.as_receipt() == first.as_receipt()
    assert mismatch.code == "idempotency_conflict"
    assert StudentIntakeCommand.objects.for_club(club).count() == 1
    assert Student.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_intake_command_rejects_instance_and_queryset_mutation(club, owner_user):
    result = _submit(club=club, actor=owner_user, role=ClubMembership.Role.OWNER)
    command = StudentIntakeCommand.objects.for_club(club).get(student_id=result.student_id)

    command.result_kind = "rewritten"
    with pytest.raises(ValidationError, match="append-only"):
        command.save()
    with pytest.raises(ValidationError, match="append-only"):
        command.delete()
    with pytest.raises(ValidationError, match="append-only"):
        StudentIntakeCommand.objects.for_club(club).filter(id=command.id).update(result_kind="rewritten")
    with pytest.raises(ValidationError, match="append-only"):
        StudentIntakeCommand.objects.for_club(club).filter(id=command.id).delete()
    with pytest.raises(ValidationError, match="append-only"):
        StudentIntakeCommand.objects.unscoped().filter(id=command.id).update(
            result_kind="rewritten"
        )
    with pytest.raises(ValidationError, match="append-only"):
        StudentIntakeCommand.objects.unscoped().filter(id=command.id).delete()


@pytest.mark.django_db
def test_child_intake_requires_confirmation_for_same_name_without_birth_date_and_allows_siblings(club, owner_user):
    first = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        intake_kind="existing_student",
        first_name="Masha",
        last_name="Petrova",
        phone="",
        guardian_phone="+79001234568",
        is_child=True,
    )
    ambiguous_key = UUID("22222222-2222-4222-8222-222222222222")
    ambiguous = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        key=ambiguous_key,
        intake_kind="existing_student",
        first_name="Masha",
        last_name="Petrova",
        phone="",
        guardian_phone="+79001234568",
        is_child=True,
    )
    confirmed = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        key=UUID("33333333-3333-4333-8333-333333333333"),
        intake_kind="existing_student",
        first_name="Masha",
        last_name="Petrova",
        phone="",
        guardian_phone="+79001234568",
        is_child=True,
        confirm_distinct_child=True,
    )
    sibling = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        key=UUID("44444444-4444-4444-8444-444444444444"),
        intake_kind="existing_student",
        first_name="Petr",
        last_name="Petrov",
        phone="",
        guardian_phone="+79001234568",
        is_child=True,
    )

    assert first.result_kind == "created_existing_student"
    assert ambiguous.code == "possible_child_duplicate_confirmation_required"
    assert confirmed.result_kind == "created_existing_student"
    assert sibling.result_kind == "created_existing_student"
    assert Student.objects.for_club(club).filter(guardian_phone="+79001234568").count() == 3


@pytest.mark.django_db
def test_soft_deleted_identity_never_reveals_or_restores_for_trainer(club, trainer_user):
    trainer = TrainerFactory(club=club, user=trainer_user)
    deleted_student = StudentFactory(club=club, phone="+79001234567")
    deleted_student.soft_delete()

    result = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
    )

    command = StudentIntakeCommand.objects.for_club(club).get()
    assert result.code == "identity_requires_owner_review"
    assert result.student_id is None
    assert result.route is None
    assert result.identity_visibility == "none"
    assert result.allowed_action is None
    assert command.student_id == deleted_student.id
    assert Student.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_other_trainer_duplicate_has_no_identity_or_route(club, trainer_user, owner_user):
    current_trainer = TrainerFactory(club=club, user=trainer_user)
    other_trainer = TrainerFactory(club=club)
    StudentFactory(
        club=club,
        phone="+79001234567",
        assigned_trainer=other_trainer,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )

    result = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=current_trainer.id,
    )

    assert result.code == "duplicate_phone"
    assert result.student_id is None
    assert result.route is None
    assert result.identity_visibility == "none"
    assert result.allowed_action is None


@pytest.mark.django_db
def test_trainer_duplicate_privacy_matrix_covers_own_pool_archived_and_former_reentry(
    club,
    trainer_user,
):
    trainer = TrainerFactory(club=club, user=trainer_user)
    own_lead = StudentFactory(
        club=club,
        phone="+79001234561",
        assigned_trainer=trainer,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )
    own = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
        phone=own_lead.phone,
    )
    assert (own.identity_visibility, own.allowed_action, own.student_id) == ("full", "open", own_lead.id)
    assert own.route == f"/trainer/leads?lead={own_lead.id}"

    pool_lead = StudentFactory(
        club=club,
        phone="+79001234562",
        assigned_trainer=None,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )
    pool = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
        phone=pool_lead.phone,
    )
    assert (pool.identity_visibility, pool.allowed_action, pool.student_id, pool.route) == (
        "masked",
        "can_claim",
        None,
        None,
    )

    own_archived = StudentFactory(
        club=club,
        phone="+79001234563",
        assigned_trainer=trainer,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )
    archived = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
        phone=own_archived.phone,
    )
    assert (archived.target_workspace, archived.identity_visibility, archived.allowed_action) == (
        "leads_archived",
        "full",
        "reopen_lead",
    )
    assert archived.route is None

    pool_archived = StudentFactory(
        club=club,
        phone="+79001234564",
        assigned_trainer=None,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )
    archived_pool = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
        phone=pool_archived.phone,
    )
    assert (archived_pool.identity_visibility, archived_pool.allowed_action, archived_pool.student_id) == (
        "masked",
        "can_reopen_and_claim",
        None,
    )

    former = StudentFactory(
        club=club,
        phone="+79001234565",
        assigned_trainer=trainer,
        status=Student.Status.CHURNED,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    former_result = _submit(
        club=club,
        actor=trainer_user,
        role=ClubMembership.Role.TRAINER,
        trainer_id=trainer.id,
        phone=former.phone,
    )
    assert (former_result.target_workspace, former_result.identity_visibility, former_result.allowed_action) == (
        "students",
        "full",
        "open",
    )
    assert former_result.route == f"/trainer/students/{former.id}"


@pytest.mark.django_db
def test_duplicate_outside_workspace_truth_table_is_a_private_readiness_anomaly(club, owner_user):
    anomaly = StudentFactory(
        club=club,
        phone="+79001234566",
        status=Student.Status.CHURNED,
        lead_status=None,
        became_student_at=None,
    )

    result = _submit(
        club=club,
        actor=owner_user,
        role=ClubMembership.Role.OWNER,
        phone=anomaly.phone,
    )

    assert result.is_conflict is True
    assert (result.result_kind, result.target_workspace, result.code) == (
        "readiness_anomaly",
        "none",
        "workspace_truth_table_anomaly",
    )
    assert (result.student_id, result.route, result.identity_visibility, result.allowed_action) == (
        None,
        None,
        "none",
        None,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("first_name", "   ", "first_name_required"),
        ("first_name", "A" * 101, "first_name_too_long"),
        ("last_name", "B" * 101, "last_name_too_long"),
    ],
)
def test_intake_service_rejects_blank_or_overlong_names_with_stable_codes(
    club,
    owner_user,
    field,
    value,
    code,
):
    with pytest.raises(BusinessLogicError) as exc_info:
        _submit(
            club=club,
            actor=owner_user,
            role=ClubMembership.Role.OWNER,
            **{field: value},
        )

    assert exc_info.value.code == code


def _race_worker(*, barrier: Barrier, create):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        return create()
    except Exception as exc:  # pragma: no cover - assertion checks the concrete result
        return exc
    finally:
        close_old_connections()


@pytest.mark.django_db
def test_postgresql_intake_race_gate_requires_opt_in_disposable_database():
    database_url = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_URL", "")
    gate_required = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_GATE_REQUIRED") == "1"
    if not database_url and not gate_required:
        pytest.skip("unified client journey PostgreSQL race gate is opt-in")
    assert database_url, "UNIFIED_CLIENT_JOURNEY_POSTGRES_URL is required"
    parsed = urlparse(database_url)
    assert parsed.scheme in {"postgres", "postgresql"}
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    assert re.search(r"(?:^|[_-])(?:test|e2e|journey)(?:[_-]|$)", parsed.path.lstrip("/"))
    assert connection.vendor == "postgresql"


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_same_intake_key_replays_once_under_club_lock(club, owner_user):
    key = UUID("55555555-5555-4555-8555-555555555555")
    barrier = Barrier(2)

    def submit():
        return _submit(club=club, actor=owner_user, role=ClubMembership.Role.OWNER, key=key)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: _race_worker(barrier=barrier, create=submit), range(2)))

    assert all(not isinstance(result, Exception) for result in results)
    assert Student.objects.for_club(club).filter(phone="+79001234567").count() == 1
    assert StudentIntakeCommand.objects.for_club(club).filter(idempotency_key=key).count() == 1
    assert sum(result.replayed for result in results) == 1


def _public_child_submission(*, club_id: int, key: UUID):
    return create_landing_lead_intake(
        club_id=club_id,
        name="Masha",
        phone="+79001234568",
        goal="Group training",
        preferred_format="group",
        is_child=True,
        consent={
            "personal_data": True,
            "privacy_policy_version": "test",
            "consent_text_hash": "safe-hash",
        },
        source={},
        request_id="child-race",
        client_ip_hash="safe-hash",
        user_agent="test",
        idempotency_key=key,
    )


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_exact_child_same_intake_key_replays_once(club, owner_user):
    key = UUID("66666666-6666-4666-8666-666666666666")
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _value: _race_worker(
                    barrier=barrier,
                    create=lambda: _submit(
                        club=club,
                        actor=owner_user,
                        role=ClubMembership.Role.OWNER,
                        key=key,
                        intake_kind="existing_student",
                        first_name="Masha",
                        last_name="Petrova",
                        phone="",
                        guardian_phone="+79001234568",
                        is_child=True,
                        date_of_birth=date(2017, 5, 6),
                    ),
                ),
                range(2),
            )
        )

    assert all(not isinstance(result, Exception) for result in results)
    assert Student.objects.for_club(club).filter(guardian_phone="+79001234568").count() == 1
    assert StudentIntakeCommand.objects.for_club(club).filter(idempotency_key=key).count() == 1
    assert sum(result.replayed for result in results) == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_public_vs_staff_exact_child_creates_one_person(club, owner_user, monkeypatch):
    monkeypatch.setattr("apps.leads.services._queue_lead_intake_telegram", lambda **kwargs: None)
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda create: _race_worker(barrier=barrier, create=create),
                (
                    lambda: _public_child_submission(
                        club_id=club.id,
                        key=UUID("77777777-7777-4777-8777-777777777777"),
                    ),
                    lambda: _submit(
                        club=club,
                        actor=owner_user,
                        role=ClubMembership.Role.OWNER,
                        key=UUID("78787878-7878-4787-8787-787878787878"),
                        first_name="Masha",
                        last_name="Petrova",
                        phone="",
                        guardian_phone="+79001234568",
                        is_child=True,
                    ),
                ),
            )
        )

    assert all(not isinstance(result, Exception) for result in results)
    assert Student.objects.for_club(club).filter(guardian_phone="+79001234568").count() == 1
    command = StudentIntakeCommand.objects.for_club(club).get(
        idempotency_key=UUID("78787878-7878-4787-8787-787878787878"),
    )
    event = LeadIntakeEvent.objects.for_club(club).get(
        idempotency_key=UUID("77777777-7777-4777-8777-777777777777"),
    )
    assert StudentIntakeCommand.objects.for_club(club).count() == 1
    assert LeadIntakeEvent.objects.for_club(club).count() == 1
    assert (command.result_kind, event.is_repeat_submission) in {
        ("created_new_contact", True),
        ("child_confirmation_required", False),
    }
    if command.result_kind == "created_new_contact":
        assert command.student_id == event.student_id


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("is_child", "first_name", "guardian_phone"),
    [
        (False, "Adult", ""),
        (True, "Masha", "+79001234569"),
    ],
)
def test_postgresql_legacy_create_vs_intake_exact_identity_creates_once(
    club,
    owner_user,
    is_child,
    first_name,
    guardian_phone,
):
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda create: _race_worker(barrier=barrier, create=create),
                (
                    lambda: create_student(
                        club_id=club.id,
                        first_name=first_name,
                        last_name="Petrova" if is_child else "Petrov",
                        phone=guardian_phone or "+79001234569",
                        guardian_phone=guardian_phone,
                        is_child=is_child,
                    ),
                    lambda: _submit(
                        club=club,
                        actor=owner_user,
                        role=ClubMembership.Role.OWNER,
                        key=uuid4(),
                        first_name=first_name,
                        last_name="Petrova" if is_child else "Petrov",
                        phone="" if is_child else "+79001234569",
                        guardian_phone=guardian_phone,
                        is_child=is_child,
                    ),
                ),
            )
        )

    assert all(
        not isinstance(result, Exception) or isinstance(result, BusinessLogicError)
        for result in results
    )
    assert Student.objects.for_club(club).count() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_distinct_siblings_sharing_guardian_remain_possible(club, owner_user):
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda child: _race_worker(
                    barrier=barrier,
                    create=lambda: _submit(
                        club=club,
                        actor=owner_user,
                        role=ClubMembership.Role.OWNER,
                        key=uuid4(),
                        intake_kind="existing_student",
                        first_name=child,
                        phone="",
                        guardian_phone="+79001234567",
                        is_child=True,
                    ),
                ),
                ("Masha", "Petr"),
            )
        )

    assert all(not isinstance(result, Exception) for result in results)
    assert {result.result_kind for result in results} == {"created_existing_student"}
    assert Student.objects.for_club(club).filter(guardian_phone="+79001234567").count() == 2
