import io
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    ScheduleEnrollment,
    TrainingGroup,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.services import create_checkin
from apps.attendance.services.training_group_memberships import create_training_group_membership
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory, TrainingGroupMembershipFactory
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Debt,
    DebtSettlementEvent,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionRenewalEvent,
    TariffComponent,
    TrainingType,
)
from apps.billing.selectors import get_group_enrollment_options
from apps.billing.services import (
    cancel_bank_payment_order,
    create_bank_payment_order,
    create_payment,
    process_bank_payment_webhook,
    verify_payment,
)
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import ClubSettingsFactory, LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.retention.models import RetentionTask
from apps.retention.tests.factories import RetentionTaskFactory
from apps.students.models import Student
from apps.students.selectors import get_manual_operational_admission, is_account_access_eligible
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def _mapped_group_payment_context(*, club, target_start_date, slot_count=1):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    group = TrainingGroup.objects.create(
        club=club,
        name="Mapped manual admission group",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    schedules = [
        ScheduleFactory(
            club=club,
            training_group=group,
            trainer=trainer,
            location=location,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
        )
        for _ in range(slot_count)
    ]
    return {
        "group": group,
        "schedule": schedules[0],
        "schedules": schedules,
        "tariff": TariffFactory(club=club, training_type=training_type, trainings_limit=3),
        "student": StudentFactory(club=club, status="active"),
    }


def _set_rollout_mode(*, club, mode):
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=mode)


def _enable_unified_group_admission(*, club, settings):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettingsFactory(
        club=club,
        unified_client_journey_enabled=True,
        commercial_journey_protocol_version="v2",
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)


def _enable_legacy_group_admission(*, club, settings):
    """Make historical generic payment fixtures explicit about their v1 capability."""
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettingsFactory(
        club=club,
        unified_client_journey_enabled=True,
        commercial_journey_protocol_version="v1",
    )


@pytest.fixture(autouse=True)
def _enable_training_group_new_writes_for_existing_canonical_scenarios(settings):
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True


@pytest.mark.django_db
def test_flagged_group_payment_immediately_admits_lead_and_confirmation_is_idempotent(club, owner_user, settings):
    target_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    student = context["student"]
    student.status = Student.Status.LEAD
    student.lead_status = Student.LeadStatus.NEW
    student.save(update_fields=["status", "lead_status", "updated_at"])
    _enable_unified_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
    )
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None

    confirmed = verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    student.refresh_from_db()
    assert confirmed.status == Payment.Status.CONFIRMED
    assert student.status == Student.Status.ACTIVE
    assert student.became_student_at is not None


@pytest.mark.django_db
def test_group_manual_admission_keeps_v1_pending_lead_but_v2_immediately_admits(
    club,
    owner_user,
    settings,
):
    """Protocol version is the boundary between legacy snooze and v2 admission."""

    target_date = timezone.localdate() + timedelta(days=9)
    v1_context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    v1_student = v1_context["student"]
    v1_student.status = Student.Status.LEAD
    v1_student.lead_status = Student.LeadStatus.NEW
    v1_student.save(update_fields=["status", "lead_status", "updated_at"])
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    club_settings = ClubSettingsFactory(
        club=club,
        unified_client_journey_enabled=True,
        commercial_journey_protocol_version="v1",
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)

    with patch("django_q.tasks.async_task"):
        v1_payment = create_payment(
            club_id=club.id,
            student_id=v1_student.id,
            tariff_id=v1_context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=v1_context["schedule"].id,
            target_training_group_id=v1_context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
        )
        v1_replay = create_payment(
            club_id=club.id,
            student_id=v1_student.id,
            tariff_id=v1_context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=v1_context["schedule"].id,
            target_training_group_id=v1_context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
        )
    v1_student.refresh_from_db()
    assert v1_replay.id == v1_payment.id
    assert v1_student.status == Student.Status.LEAD
    assert v1_student.lead_status == Student.LeadStatus.NEW
    assert Payment.objects.for_club(club).filter(student=v1_student).count() == 1

    club_settings.commercial_journey_protocol_version = "v2"
    club_settings.save(update_fields=["commercial_journey_protocol_version", "updated_at"])
    v2_context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    v2_student = v2_context["student"]
    v2_student.status = Student.Status.LEAD
    v2_student.lead_status = Student.LeadStatus.NEW
    v2_student.save(update_fields=["status", "lead_status", "updated_at"])
    with patch("django_q.tasks.async_task"):
        v2_payment = create_payment(
            club_id=club.id,
            student_id=v2_student.id,
            tariff_id=v2_context["tariff"].id,
            payment_method=Payment.Method.TRANSFER,
            recorded_by_id=owner_user.id,
            target_schedule_id=v2_context["schedule"].id,
            target_training_group_id=v2_context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
        )
    v2_student.refresh_from_db()
    assert v2_student.status == Student.Status.ACTIVE
    assert v2_student.lead_status is None
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=v2_student,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        metadata__payment_id=v2_payment.id,
    ).exists()


@pytest.mark.django_db
def test_flagged_group_rejection_before_attendance_closes_membership_without_demoting_student(
    club,
    owner_user,
    settings,
):
    target_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    student = context["student"]
    student.status = Student.Status.LEAD
    student.lead_status = Student.LeadStatus.TRIAL_DONE
    student.save(update_fields=["status", "lead_status", "updated_at"])
    task = RetentionTaskFactory(
        club=club,
        student=student,
        trainer=context["group"].responsible_trainer,
        task_type=RetentionTask.TaskType.POST_TRIAL,
        status=RetentionTask.TaskStatus.OPEN,
        due_date=target_date,
    )
    _enable_unified_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
        )
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None

    rejected = verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="reject",
        rejection_reason="Pre-attendance rejection.",
    )
    student.refresh_from_db()
    task.refresh_from_db()
    assert rejected.status == Payment.Status.REJECTED
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert task.status == RetentionTask.TaskStatus.CLOSED
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student=student,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
        metadata__payment_id=payment.id,
    ).exists()


@pytest.mark.django_db
def test_flagged_exact_group_checkin_converts_lead_and_rejection_keeps_attended_state(
    club,
    owner_user,
    settings,
):
    target_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    student = context["student"]
    student.status = Student.Status.LEAD
    student.lead_status = Student.LeadStatus.NEW
    student.save(update_fields=["status", "lead_status", "updated_at"])
    _enable_unified_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            create_manual_operational_admission=True,
        )
    with patch("apps.attendance.services.async_task"):
        checkin = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=context["schedule"].id,
            training_type_id=context["group"].training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
    debt = Debt.objects.for_club(club).get(checkin_id=checkin["checkin_id"])
    student.refresh_from_db()
    assert debt.settlement_payment_id == payment.id
    assert debt.reason == "pending_manual_admission"
    assert student.status == Student.Status.ACTIVE

    rejected = verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="reject",
        rejection_reason="Post-attendance rejection.",
    )
    debt.refresh_from_db()
    student.refresh_from_db()
    assert rejected.status == Payment.Status.REJECTED
    assert debt.settlement_payment_id is None
    assert student.status == Student.Status.ACTIVE
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student=student,
        event_type=LeadLifecycleEvent.EventType.GROUP_ADMISSION_RESTORED,
        metadata__payment_id=payment.id,
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("wrong_group", "student_schedule_ineligible"),
        ("before_target_date", "schedule_occurrence_not_found"),
        ("other_student", "student_ineligible"),
        ("rejected_payment", "student_schedule_ineligible"),
        ("flag_off", "student_ineligible"),
    ],
)
def test_group_lead_checkin_exception_requires_exact_live_pending_admission(
    club,
    owner_user,
    settings,
    case,
    expected_code,
):
    target_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    student = context["student"]
    student.status = Student.Status.LEAD
    student.lead_status = Student.LeadStatus.NEW
    student.save(update_fields=["status", "lead_status", "updated_at"])

    attempt_student = student
    attempt_schedule = context["schedule"]
    attempt_date = target_date
    if case == "flag_off":
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    else:
        _enable_unified_group_admission(club=club, settings=settings)
        payment_student = student
        if case == "other_student":
            payment_student = StudentFactory(
                club=club,
                status=Student.Status.LEAD,
                lead_status=Student.LeadStatus.NEW,
            )
        with patch("django_q.tasks.async_task"):
            payment = create_payment(
                club_id=club.id,
                student_id=payment_student.id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=context["schedule"].id,
                target_training_group_id=context["group"].id,
                target_start_date=target_date,
                create_manual_operational_admission=True,
            )
        if case == "rejected_payment":
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="reject",
                rejection_reason="Terminal before check-in.",
            )
        elif case == "before_target_date":
            attempt_date = target_date - timedelta(days=1)
        elif case == "wrong_group":
            other_group = TrainingGroup.objects.create(
                club=club,
                name="Wrong group exact-admission denial",
                training_type=context["group"].training_type,
                location=context["group"].location,
                responsible_trainer=context["group"].responsible_trainer,
                status=TrainingGroup.Status.ACTIVE,
            )
            attempt_schedule = ScheduleFactory(
                club=club,
                training_group=other_group,
                trainer=other_group.responsible_trainer,
                location=other_group.location,
                training_type=other_group.training_type,
                day_of_week=target_date.weekday(),
            )

    with pytest.raises(BusinessLogicError) as exc_info:
        create_checkin(
            club_id=club.id,
            student_id=attempt_student.id,
            schedule_id=attempt_schedule.id,
            training_type_id=attempt_schedule.training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=attempt_date,
        )
    assert exc_info.value.code == expected_code
    assert not Checkin.objects.for_club(club).filter(student=attempt_student).exists()


@pytest.mark.django_db
def test_flagged_canonical_group_renewal_requires_and_uses_exact_subscription_source(
    club,
    owner_user,
    settings,
):
    target_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    _enable_unified_group_admission(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = context["student"]
    TrainingGroupMembershipFactory(
        club=club,
        student=student,
        training_group=context["group"],
        starts_on=target_date - timedelta(days=1),
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        expires_at=target_date + timedelta(days=30),
    )
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": context["tariff"].id,
        "payment_method": Payment.Method.CASH,
        "recorded_by_id": owner_user.id,
        "target_schedule_id": context["schedule"].id,
        "target_training_group_id": context["group"].id,
        "target_start_date": target_date,
        "create_manual_operational_admission": True,
    }
    with pytest.raises(BusinessLogicError) as exc_info:
        create_payment(**request)
    assert exc_info.value.code == "renewal_source_required"

    with patch("django_q.tasks.async_task"):
        manual = create_payment(**(request | {"renewed_from_subscription_id": source.id}))
    assert manual.subscription.renewed_from_id == source.id
    confirmed = verify_payment(
        payment_id=manual.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    assert confirmed.status == Payment.Status.CONFIRMED
    assert SubscriptionRenewalEvent.objects.for_club(club).filter(payment=manual).count() == 1

    sbp_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    TrainingGroupMembershipFactory(
        club=club,
        student=sbp_student,
        training_group=context["group"],
        starts_on=target_date - timedelta(days=1),
    )
    sbp_source = SubscriptionFactory(
        club=club,
        student=sbp_student,
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        expires_at=target_date + timedelta(days=30),
    )
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=sbp_student.id,
        tariff_id=None,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=context["schedule"].id,
        target_training_group_id=context["group"].id,
        target_start_date=target_date,
        renewed_from_subscription_id=sbp_source.id,
        command_idempotency_key="exact-group-sbp-source",
    )
    assert order.renewed_from_subscription_id == sbp_source.id
    assert order.payment.subscription.renewed_from_id == sbp_source.id


@pytest.mark.django_db
def test_last_credit_checkin_makes_future_expired_group_sources_renewable_without_carry(
    club,
    owner_user,
    settings,
):
    target_date = timezone.localdate()
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    _enable_unified_group_admission(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = context["student"]
    TrainingGroupMembershipFactory(
        club=club,
        student=student,
        training_group=context["group"],
        starts_on=target_date,
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        trainings_left=1,
        expires_at=timezone.now() + timedelta(days=30),
    )
    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=context["schedule"].id,
            training_type_id=context["tariff"].training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
    source.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED
    assert source.trainings_left == 0
    assert source.expires_at > timezone.now()
    options = get_group_enrollment_options(
        club=club,
        student_id=student.id,
        tariff_id=context["tariff"].id,
        canonical_cards_enabled=True,
    )
    assert options[0]["group_membership_action"] == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert options[0]["renewed_from_subscription_id"] == source.id

    with patch("django_q.tasks.async_task"):
        manual = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            renewed_from_subscription_id=source.id,
            allow_renewal=True,
        )
        verify_payment(
            payment_id=manual.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
    event = SubscriptionRenewalEvent.objects.for_club(club).get(payment=manual)
    manual.subscription.refresh_from_db()
    assert event.carry_snapshot["outcome"] == "exhausted"
    assert event.carry_snapshot["legacy_finite_credits"] == 0
    assert manual.subscription.trainings_left == context["tariff"].trainings_limit


@pytest.mark.django_db
def test_last_credit_checkin_sbp_confirmation_carries_no_exhausted_entitlement(
    club,
    owner_user,
    settings,
):
    target_date = timezone.localdate()
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    _enable_unified_group_admission(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = context["student"]
    TrainingGroupMembershipFactory(
        club=club,
        student=student,
        training_group=context["group"],
        starts_on=target_date,
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        trainings_left=1,
        expires_at=timezone.now() + timedelta(days=30),
    )
    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=context["schedule"].id,
            training_type_id=context["tariff"].training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
    source.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED
    assert source.trainings_left == 0

    with patch("django_q.tasks.async_task"):
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            renewed_from_subscription_id=source.id,
            command_idempotency_key="group-exhausted-sbp-k1",
        )
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(
                {
                    "webhookType": "acquiringInternetPayment",
                    "event_id": "group-exhausted-sbp-approved",
                    "status": "APPROVED",
                    "paymentLinkId": order.provider_payment_link_id,
                    "operationId": "group-exhausted-sbp-operation",
                    "amount": str(order.amount_snapshot),
                    "paid_at": timezone.now().isoformat(),
                }
            ).encode(),
            headers={},
            request_id="group-exhausted-sbp-request",
        )
    event = SubscriptionRenewalEvent.objects.for_club(club).get(payment=order.payment)
    order.subscription.refresh_from_db()
    assert event.carry_snapshot["outcome"] == "exhausted"
    assert event.carry_snapshot["legacy_finite_credits"] == 0
    assert order.subscription.trainings_left == context["tariff"].trainings_limit


@pytest.mark.django_db
def test_last_component_credit_checkin_makes_future_expired_group_source_renewable_without_carry(
    club,
    owner_user,
    settings,
):
    """The component ledger, not only legacy trainings_left, permits exhaustion renewal."""

    target_date = timezone.localdate()
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    context["tariff"].trainings_limit = None
    context["tariff"].save(update_fields=["trainings_limit", "updated_at"])
    _enable_unified_group_admission(club=club, settings=settings)
    student = context["student"]
    TrainingGroupMembershipFactory(
        club=club,
        student=student,
        training_group=context["group"],
        starts_on=target_date,
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        trainings_left=None,
        expires_at=timezone.now() + timedelta(days=30),
    )
    tariff_component = TariffComponentFactory(
        club=club,
        tariff=context["tariff"],
        training_type=context["tariff"].training_type,
        credits_total=1,
    )
    source_component = SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=tariff_component,
        credits_total=1,
        credits_left=1,
    )

    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=context["schedule"].id,
            training_type_id=context["tariff"].training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )
    source.refresh_from_db()
    source_component.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED
    assert source_component.credits_left == 0
    assert source.expires_at > timezone.now()
    options = get_group_enrollment_options(
        club=club,
        student_id=student.id,
        tariff_id=context["tariff"].id,
        canonical_cards_enabled=True,
    )
    assert options[0]["renewed_from_subscription_id"] == source.id

    with patch("django_q.tasks.async_task"):
        manual = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_date,
            renewed_from_subscription_id=source.id,
            allow_renewal=True,
        )
        verify_payment(
            payment_id=manual.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
    event = SubscriptionRenewalEvent.objects.for_club(club).get(payment=manual)
    renewed_component = SubscriptionComponent.objects.for_club(club).get(
        subscription=manual.subscription,
        tariff_component=tariff_component,
    )
    assert event.carry_snapshot["outcome"] == "exhausted"
    assert event.carry_snapshot["components"] == []
    assert renewed_component.credits_left == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "other_entitlement_kind",
    [
        TariffComponent.EntitlementKind.FINITE_CREDITS,
        TariffComponent.EntitlementKind.WEEKLY_LIMIT,
        TariffComponent.EntitlementKind.UNLIMITED,
    ],
)
def test_component_checkin_keeps_subscription_active_when_another_component_remains_usable(
    club,
    settings,
    other_entitlement_kind,
):
    target_date = timezone.localdate()
    context = _mapped_group_payment_context(club=club, target_start_date=target_date)
    context["tariff"].trainings_limit = None
    context["tariff"].save(update_fields=["trainings_limit", "updated_at"])
    source = SubscriptionFactory(
        club=club,
        student=context["student"],
        tariff=context["tariff"],
        status=Subscription.Status.ACTIVE,
        trainings_left=None,
        expires_at=timezone.now() + timedelta(days=30),
    )
    TrainingGroupMembershipFactory(
        club=club,
        student=context["student"],
        training_group=context["group"],
        starts_on=target_date,
    )
    exhausted_tariff_component = TariffComponentFactory(
        club=club,
        tariff=context["tariff"],
        training_type=context["tariff"].training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=exhausted_tariff_component,
        credits_total=1,
        credits_left=1,
    )
    live_tariff_component = TariffComponentFactory(
        club=club,
        tariff=context["tariff"],
        training_type=context["tariff"].training_type,
        entitlement_kind=other_entitlement_kind,
        credits_total=1 if other_entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
        weekly_limit=1 if other_entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT else None,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=live_tariff_component,
        credits_total=live_tariff_component.credits_total,
        credits_left=1 if other_entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
    )

    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=club.id,
            student_id=context["student"].id,
            schedule_id=context["schedule"].id,
            training_type_id=context["tariff"].training_type_id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_date,
        )

    source.refresh_from_db()
    assert source.status == Subscription.Status.ACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mode", "manual_admission_enabled", "expected_code", "expects_canonical_identity"),
    [
        # This stale schedule-only command has no durable ClubSettings protocol
        # marker.  The commercial capability projection therefore fails closed
        # before the legacy manual-admission switch or canonical rollout state.
        (TrainingGroupRolloutState.Mode.OFF, False, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.OFF, True, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.SHADOW, False, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.SHADOW, True, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.ACTIVE, False, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.ACTIVE, True, "commercial_journey_unavailable", None),
        (TrainingGroupRolloutState.Mode.CONTAINMENT, False, "training_group_writes_disabled", None),
        (TrainingGroupRolloutState.Mode.CONTAINMENT, True, "training_group_writes_disabled", None),
        (TrainingGroupRolloutState.Mode.RECONCILING, False, "training_group_reconciling", None),
        (TrainingGroupRolloutState.Mode.RECONCILING, True, "training_group_reconciling", None),
    ],
)
def test_mapped_stale_schedule_only_manual_request_obeys_rollout_and_kill_switch_matrix(
    club,
    owner_user,
    settings,
    mode,
    manual_admission_enabled,
    expected_code,
    expects_canonical_identity,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=mode)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = manual_admission_enabled

    payment_kwargs = {
        "club_id": club.id,
        "student_id": context["student"].id,
        "tariff_id": context["tariff"].id,
        "payment_method": Payment.Method.CASH,
        "recorded_by_id": owner_user.id,
        "target_schedule_id": context["schedule"].id,
        "target_start_date": target_start_date,
        "create_manual_operational_admission": True,
    }
    with patch("django_q.tasks.async_task"):
        if expected_code is not None:
            with pytest.raises(BusinessLogicError) as exc_info:
                create_payment(**payment_kwargs)

            assert exc_info.value.code == expected_code
            assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
            assert not TrainingGroupMembership.objects.for_club(club).filter(
                student=context["student"]
            ).exists()
            assert not ScheduleEnrollment.objects.for_club(club).filter(
                student=context["student"]
            ).exists()
            return

        payment = create_payment(**payment_kwargs)

    if expects_canonical_identity:
        membership = TrainingGroupMembership.objects.for_club(club).get(
            id=payment.conversion_group_membership_id
        )
        assert payment.target_training_group_id == context["group"].id
        assert payment.target_group_membership_id == membership.id
        assert payment.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
        assert payment.conversion_enrollment.training_group_membership_id == membership.id
        assert payment.conversion_enrollment.created_from == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
    else:
        assert payment.target_training_group_id is None
        assert payment.target_group_membership_id is None
        assert payment.conversion_group_membership_id is None
        assert payment.group_membership_action_snapshot == ""
        assert payment.conversion_enrollment.schedule_id == context["schedule"].id
        assert payment.conversion_enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION


@pytest.mark.django_db
def test_mapped_manual_admission_requires_rollout_state_before_financial_or_identity_rows(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    TrainingGroupRolloutState.objects.for_club(club).filter(club=club).delete()
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

    with patch("django_q.tasks.async_task"):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=context["schedule"].id,
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )

    assert exc_info.value.code == "training_group_rollout_state_missing"
    assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Subscription.objects.for_club(club).filter(student=context["student"]).exists()
    assert not TrainingGroupMembership.objects.for_club(club).filter(
        student=context["student"]
    ).exists()
    assert not ScheduleEnrollment.objects.for_club(club).filter(
        student=context["student"]
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("include_explicit_group_id", [False, True])
def test_active_canonical_manual_sale_switch_false_rejects_stale_or_explicit_intent_before_roots(
    club,
    owner_user,
    settings,
    include_explicit_group_id,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False

    with patch("django_q.tasks.async_task"):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=context["schedule"].id,
                target_training_group_id=(
                    context["group"].id if include_explicit_group_id else None
                ),
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )

    assert exc_info.value.code == "training_group_writes_disabled"
    assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Subscription.objects.for_club(club).filter(student=context["student"]).exists()
    assert not TrainingGroupMembership.objects.for_club(club).filter(student=context["student"]).exists()
    assert not ScheduleEnrollment.objects.for_club(club).filter(student=context["student"]).exists()


@pytest.mark.django_db
def test_active_canonical_bank_sale_switch_false_rejects_before_order_or_payment_roots(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False

    with pytest.raises(BusinessLogicError) as exc_info:
        create_bank_payment_order(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_start_date,
        )

    assert exc_info.value.code == "training_group_writes_disabled"
    assert not BankPaymentOrder.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Subscription.objects.for_club(club).filter(student=context["student"]).exists()


@pytest.mark.django_db
def test_existing_canonical_manual_payment_can_confirm_or_reject_after_switch_is_disabled(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    confirmed_context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    rejected_context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        confirmed_payment = create_payment(
            club_id=club.id,
            student_id=confirmed_context["student"].id,
            tariff_id=confirmed_context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=confirmed_context["schedule"].id,
            target_training_group_id=confirmed_context["group"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
        rejected_payment = create_payment(
            club_id=club.id,
            student_id=rejected_context["student"].id,
            tariff_id=rejected_context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=rejected_context["schedule"].id,
            target_training_group_id=rejected_context["group"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
        confirmed = verify_payment(
            payment_id=confirmed_payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        rejected = verify_payment(
            payment_id=rejected_payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="Owner rejected the existing pending payment.",
        )

    assert confirmed.status == Payment.Status.CONFIRMED
    assert rejected.status == Payment.Status.REJECTED
    confirmed.refresh_from_db()
    rejected.refresh_from_db()
    assert confirmed.target_training_group_id == confirmed_context["group"].id
    assert confirmed.target_group_membership_id is not None
    assert rejected.target_training_group_id == rejected_context["group"].id


@pytest.mark.django_db
def test_existing_canonical_bank_order_can_cancel_after_switch_is_disabled(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=context["student"].id,
        tariff_id=context["tariff"].id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=context["schedule"].id,
        target_training_group_id=context["group"].id,
        target_start_date=target_start_date,
    )

    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
    cancelled = cancel_bank_payment_order(
        club_id=club.id,
        order_id=order.id,
        actor_user_id=owner_user.id,
    )

    assert cancelled.status == BankPaymentOrder.Status.CANCELLED
    order.payment.refresh_from_db()
    assert order.payment.status == Payment.Status.REJECTED


@pytest.mark.django_db
def test_mapped_manual_admission_rejects_explicit_group_mismatch_before_mutation(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.ACTIVE)
    wrong_group = TrainingGroup.objects.create(
        club=club,
        name="Wrong requested group",
        training_type=context["group"].training_type,
        location=context["group"].location,
        responsible_trainer=context["group"].responsible_trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

    with patch("django_q.tasks.async_task"):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=context["schedule"].id,
                target_training_group_id=wrong_group.id,
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )

    assert exc_info.value.code == "target_training_group_mismatch"
    assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Subscription.objects.for_club(club).filter(student=context["student"]).exists()
    assert not TrainingGroupMembership.objects.for_club(club).filter(
        student=context["student"]
    ).exists()
    assert not ScheduleEnrollment.objects.for_club(club).filter(
        student=context["student"]
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        (TrainingGroupRolloutState.Mode.OFF, "training_group_writes_disabled"),
        (TrainingGroupRolloutState.Mode.SHADOW, "training_group_writes_disabled"),
        (TrainingGroupRolloutState.Mode.CONTAINMENT, "training_group_writes_disabled"),
        (TrainingGroupRolloutState.Mode.RECONCILING, "training_group_reconciling"),
    ],
)
def test_explicit_canonical_group_manual_admission_never_downgrades_when_rollout_is_unavailable(
    club,
    owner_user,
    settings,
    mode,
    expected_code,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(club=club, target_start_date=target_start_date)
    _set_rollout_mode(club=club, mode=mode)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

    with pytest.raises(BusinessLogicError) as exc_info:
        create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.TRANSFER,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_training_group_id=context["group"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    assert exc_info.value.code == expected_code
    assert not Payment.objects.for_club(club).filter(student=context["student"]).exists()
    assert not Subscription.objects.for_club(club).filter(student=context["student"]).exists()
    assert not TrainingGroupMembership.objects.for_club(club).filter(student=context["student"]).exists()
    assert not ScheduleEnrollment.objects.for_club(club).filter(student=context["student"]).exists()


@pytest.mark.django_db
def test_unmapped_schedule_in_shadow_retains_legacy_manual_admission(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)
    unmapped_schedule = ScheduleFactory(
        club=club,
        trainer=context["group"].responsible_trainer,
        location=context["group"].location,
        training_type=context["group"].training_type,
        day_of_week=target_start_date.weekday(),
    )

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=unmapped_schedule.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    assert payment.target_training_group_id is None
    assert payment.target_group_membership_id is None
    assert payment.conversion_group_membership_id is None
    assert payment.conversion_enrollment.schedule_id == unmapped_schedule.id
    assert payment.conversion_enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION


@pytest.mark.django_db
def test_canonical_renewal_without_paid_or_pending_owner_admission_is_not_account_ready(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    membership = create_training_group_membership(
        club_id=club.id,
        student_id=context["student"].id,
        training_group_id=context["group"].id,
        starts_on=target_start_date,
        source=TrainingGroupMembership.Source.MANUAL,
        actor_user_id=owner_user.id,
        idempotency_key="test-renewal-only-account-readiness",
    )

    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    assert renewal.target_training_group_id == context["group"].id
    assert renewal.target_group_membership_id == membership.id
    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.conversion_group_membership_id is None
    assert renewal.conversion_enrollment_id is None
    assert get_manual_operational_admission(club=club, student=context["student"]) is None
    assert is_account_access_eligible(club=club, student=context["student"]) is False


@pytest.mark.django_db
def test_canonical_terminal_owner_remains_truthful_but_loses_pending_account_readiness(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="reject",
        rejection_reason="operator rejected before confirmation",
    )

    admission = get_manual_operational_admission(club=club, student=context["student"])
    assert admission is not None
    assert admission.payment_id == payment.id
    assert admission.payment_status == Payment.Status.REJECTED
    assert admission.is_qualifying is False
    assert is_account_access_eligible(club=club, student=context["student"]) is False


@pytest.mark.django_db
def test_canonical_pending_renewal_does_not_replace_confirmed_owner_readiness_truth(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
        slot_count=2,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        owner_payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    verify_payment(
        payment_id=owner_payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )

    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            renewed_from_subscription_id=owner_payment.subscription_id,
            create_manual_operational_admission=True,
        )

    admission = get_manual_operational_admission(club=club, student=context["student"])
    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.conversion_group_membership_id is None
    assert renewal.conversion_enrollment_id is None
    assert admission is not None
    assert admission.payment_id == owner_payment.id
    assert admission.payment_status == Payment.Status.CONFIRMED
    assert admission.is_qualifying is False
    assert is_account_access_eligible(club=club, student=context["student"]) is True


@pytest.mark.django_db
def test_canonical_renewal_rejection_leaves_the_existing_owned_membership_and_projections_unchanged(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
        slot_count=2,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        owner_payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    verify_payment(
        payment_id=owner_payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    membership = TrainingGroupMembership.objects.for_club(club).get(
        id=owner_payment.conversion_group_membership_id
    )
    membership_before = TrainingGroupMembership.objects.for_club(club).filter(id=membership.id).values(
        "id",
        "student_id",
        "training_group_id",
        "status",
        "starts_on",
        "ends_on",
        "source",
        "authority",
        "created_by_id",
    ).get()
    membership_event_ids_before = list(
        TrainingGroupMembershipEvent.objects.for_club(club)
        .filter(membership=membership)
        .order_by("id")
        .values_list("id", flat=True)
    )
    projection_rows_before = list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(training_group_membership=membership)
        .order_by("schedule_id", "id")
        .values(
            "id",
            "student_id",
            "schedule_id",
            "status",
            "starts_on",
            "ends_on",
            "created_from",
            "training_group_membership_id",
        )
    )

    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            renewed_from_subscription_id=owner_payment.subscription_id,
            create_manual_operational_admission=True,
        )
    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.target_group_membership_id == membership.id
    assert renewal.conversion_group_membership_id is None
    assert renewal.conversion_enrollment_id is None

    rejected = verify_payment(
        payment_id=renewal.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="reject",
        rejection_reason="Owner rejected the renewal before confirmation.",
    )

    membership.refresh_from_db()
    assert rejected.status == Payment.Status.REJECTED
    assert membership.status == TrainingGroupMembership.Status.ACTIVE
    assert list(
        TrainingGroupMembership.objects.for_club(club)
        .filter(id=membership.id)
        .values(
            "id",
            "student_id",
            "training_group_id",
            "status",
            "starts_on",
            "ends_on",
            "source",
            "authority",
            "created_by_id",
        )
    ) == [membership_before]
    assert list(
        TrainingGroupMembershipEvent.objects.for_club(club)
        .filter(membership=membership)
        .order_by("id")
        .values_list("id", flat=True)
    ) == membership_event_ids_before
    assert list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(training_group_membership=membership)
        .order_by("schedule_id", "id")
        .values(
            "id",
            "student_id",
            "schedule_id",
            "status",
            "starts_on",
            "ends_on",
            "created_from",
            "training_group_membership_id",
        )
    ) == projection_rows_before
    assert rejected.target_group_membership_id == membership.id
    assert rejected.conversion_group_membership_id is None
    assert rejected.conversion_enrollment_id is None


@pytest.mark.django_db
def test_operational_admission_audit_accepts_canonical_new_and_pending_renewal(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        owner_payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    verify_payment(
        payment_id=owner_payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            renewed_from_subscription_id=owner_payment.subscription_id,
            create_manual_operational_admission=True,
        )

    stdout = io.StringIO()
    call_command(
        "audit_operational_admissions",
        "--fail-on-invalid",
        club_id=club.id,
        stdout=stdout,
    )
    report = json.loads(stdout.getvalue())

    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.conversion_group_membership_id is None
    assert renewal.conversion_enrollment_id is None
    assert report["canonical_group_action_counts"] == {"new_admission": 1, "renewal": 1}
    assert report["invalid_state_counts"] == {}
    assert report["clean"] is True


@pytest.mark.django_db
def test_operational_admission_audit_rejects_reserved_debt_from_another_group_slot(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    other_group = TrainingGroup.objects.create(
        club=club,
        name="Other group debt slot",
        training_type=context["group"].training_type,
        location=context["group"].location,
        responsible_trainer=context["group"].responsible_trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    other_schedule = ScheduleFactory(
        club=club,
        training_group=other_group,
        trainer=context["group"].responsible_trainer,
        location=context["group"].location,
        training_type=context["group"].training_type,
        day_of_week=target_start_date.weekday(),
    )
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    checkin = CheckinFactory(
        club=club,
        student=context["student"],
        schedule=other_schedule,
        trainer=other_schedule.trainer,
        location=other_schedule.location,
        training_type=other_schedule.training_type,
        date=target_start_date,
    )
    debt = DebtFactory(
        club=club,
        student=context["student"],
        checkin=checkin,
        required_tariff=context["tariff"],
        settlement_payment=payment,
    )
    DebtSettlementEvent.objects.create(
        club=club,
        debt=debt,
        payment=payment,
        event_type=DebtSettlementEvent.EventType.RESERVED,
    )

    stdout = io.StringIO()
    call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
    report = json.loads(stdout.getvalue())

    assert report["invalid_state_counts"]["debt_schedule_group_mismatch"] == 1
    assert report["clean"] is False


@pytest.mark.django_db
def test_operational_admission_audit_flags_confirmed_canonical_new_missing_owned_family(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)
    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    Payment.objects.for_club(club).filter(id=payment.id).update(
        target_group_membership=None,
        conversion_group_membership=None,
        conversion_enrollment=None,
    )

    stdout = io.StringIO()
    call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
    invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

    assert invalid["group_payment_owned_membership_missing"] == 1
    assert invalid["group_payment_anchor_missing"] == 1
    assert invalid["orphan_payment_owned_group_membership"] == 1


@pytest.mark.django_db
def test_operational_admission_audit_flags_canonical_group_ownership_and_anchor_mismatches(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
        slot_count=2,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    wrong_group = TrainingGroup.objects.create(
        club=club,
        name="Wrong audit group",
        training_type=context["group"].training_type,
        location=context["group"].location,
        responsible_trainer=context["group"].responsible_trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    _enable_legacy_group_admission(club=club, settings=settings)
    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    membership = TrainingGroupMembership.objects.for_club(club).get(
        id=payment.conversion_group_membership_id
    )
    non_anchor_projection = ScheduleEnrollment.objects.for_club(club).get(
        training_group_membership=membership,
        schedule=context["schedules"][1],
    )
    Payment.objects.for_club(club).filter(id=payment.id).update(
        target_training_group=wrong_group,
        target_group_membership=None,
        conversion_enrollment=non_anchor_projection,
    )

    stdout = io.StringIO()
    call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
    invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

    assert invalid["group_target_schedule_mismatch"] == 1
    assert invalid["group_payment_ownership_mismatch"] == 1
    assert invalid["group_projection_anchor_mismatch"] == 1
    assert invalid["target_schedule_mismatch"] == 1


@pytest.mark.django_db
def test_operational_admission_audit_flags_renewal_with_foreign_target_and_owned_artifacts(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    membership = create_training_group_membership(
        club_id=club.id,
        student_id=context["student"].id,
        training_group_id=context["group"].id,
        starts_on=target_start_date,
        source=TrainingGroupMembership.Source.MANUAL,
        actor_user_id=owner_user.id,
        idempotency_key="test-audit-renewal-owner",
    )
    foreign_membership = create_training_group_membership(
        club_id=club.id,
        student_id=StudentFactory(club=club, status="active").id,
        training_group_id=context["group"].id,
        starts_on=target_start_date,
        source=TrainingGroupMembership.Source.MANUAL,
        actor_user_id=owner_user.id,
        idempotency_key="test-audit-renewal-foreign",
    )
    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    projection = ScheduleEnrollment.objects.for_club(club).get(
        training_group_membership=membership,
        schedule=context["schedule"],
    )
    Payment.objects.for_club(club).filter(id=renewal.id).update(
        target_group_membership=foreign_membership,
        conversion_group_membership=membership,
        conversion_enrollment=projection,
    )

    stdout = io.StringIO()
    call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
    invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

    assert invalid["group_renewal_target_membership_mismatch"] == 1
    assert invalid["group_renewal_owns_admission"] == 1


@pytest.mark.django_db
def test_operational_admission_audit_fails_all_clubs_for_deferred_provider_event(club):
    BankPaymentProviderEvent.objects.create(
        club=club,
        provider=BankPaymentOrder.Provider.MOCK,
        event_type="acquiringInternetPayment",
        provider_event_id="audit-deferred-event",
        received_at=timezone.now(),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
    )

    stdout = io.StringIO()
    with pytest.raises(CommandError):
        call_command("audit_operational_admissions", "--all-clubs", "--fail-on-invalid", stdout=stdout)
    report = json.loads(stdout.getvalue())

    assert report["aggregate_invalid_state_counts"]["deferred_provider_event"] == 1
    assert report["clean"] is False


@pytest.mark.django_db(transaction=True)
def test_operational_admission_audit_reports_reused_payment_owned_membership_after_constraint_corruption(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    context = _mapped_group_payment_context(
        club=club,
        target_start_date=target_start_date,
    )
    _set_rollout_mode(club=club, mode=TrainingGroupRolloutState.Mode.SHADOW)
    _enable_legacy_group_admission(club=club, settings=settings)
    with patch("django_q.tasks.async_task"):
        owner_payment = create_payment(
            club_id=club.id,
            student_id=context["student"].id,
            tariff_id=context["tariff"].id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=context["schedule"].id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    membership = TrainingGroupMembership.objects.for_club(club).get(
        id=owner_payment.conversion_group_membership_id
    )
    unique_constraint = next(
        constraint
        for constraint in Payment._meta.constraints
        if constraint.name == "uniq_payment_conversion_group_membership"
    )
    duplicate = None
    with connection.schema_editor() as schema_editor:
        schema_editor.remove_constraint(Payment, unique_constraint)
    try:
        duplicate = PaymentFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            subscription=SubscriptionFactory(
                club=club,
                student=context["student"],
                tariff=context["tariff"],
                status=Subscription.Status.PENDING,
                paid_amount=None,
                expires_at=None,
            ),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            target_schedule=context["schedule"],
            target_training_group=context["group"],
            target_group_membership=membership,
            conversion_group_membership=membership,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
            target_start_date=target_start_date,
            conversion_enrollment=owner_payment.conversion_enrollment,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

        assert invalid["reused_payment_owned_group_membership"] == 1
    finally:
        if duplicate is not None:
            Payment.objects.for_club(club).filter(id=duplicate.id).delete()
        with connection.schema_editor() as schema_editor:
            schema_editor.add_constraint(Payment, unique_constraint)


@pytest.mark.django_db
def test_payment_owned_group_admission_projects_every_slot_and_covers_non_anchor_pending_checkin(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    group = TrainingGroup.objects.create(
        club=club,
        name="Two-slot payment admission",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    anchor = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
    )
    non_anchor = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
    )
    tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=3)
    student = StudentFactory(club=club, status="active")
    _enable_legacy_group_admission(club=club, settings=settings)

    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=anchor.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    membership = TrainingGroupMembership.objects.for_club(club).get(id=payment.conversion_group_membership_id)
    assert payment.conversion_enrollment.schedule_id == anchor.id
    assert set(
        ScheduleEnrollment.objects.for_club(club)
        .filter(training_group_membership=membership)
        .values_list("schedule_id", flat=True)
    ) == {anchor.id, non_anchor.id}

    group.name = "Current canonical group label"
    group.save(update_fields=["name", "updated_at"])
    admission = get_manual_operational_admission(club=club, student=student)
    assert admission is not None
    assert admission.group_label == group.name
    assert admission.training_group_id == group.id
    assert admission.group_membership_id == membership.id

    non_anchor_projection = ScheduleEnrollment.objects.for_club(club).get(
        training_group_membership=membership,
        schedule=non_anchor,
    )
    payment.conversion_enrollment = non_anchor_projection
    payment.save(update_fields=["conversion_enrollment", "updated_at"])
    assert get_manual_operational_admission(club=club, student=student) is None
    assert is_account_access_eligible(club=club, student=student) is False
    payment.conversion_enrollment = ScheduleEnrollment.objects.for_club(club).get(
        training_group_membership=membership,
        schedule=anchor,
    )
    payment.save(update_fields=["conversion_enrollment", "updated_at"])

    with patch("apps.attendance.services.async_task"):
        checkin = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=non_anchor.id,
            training_type_id=training_type.id,
            source=Checkin.Source.KIOSK,
            checkin_date=target_start_date,
        )
    debt = Debt.objects.for_club(club).get(checkin_id=checkin["checkin_id"])
    assert debt.settlement_payment_id == payment.id

    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    with patch("django_q.tasks.async_task"):
        renewal = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=anchor.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
            renewed_from_subscription_id=payment.subscription_id,
            create_manual_operational_admission=True,
        )
    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.conversion_group_membership_id is None
    assert renewal.conversion_enrollment_id is None


@pytest.mark.django_db
def test_operational_admission_audit_reports_canonical_group_aggregate_counters(
    club,
    owner_user,
    settings,
):
    target_start_date = timezone.localdate() + timedelta(days=7)
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    group = TrainingGroup.objects.create(
        club=club,
        name="Audit-safe group counters",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
    )
    _enable_legacy_group_admission(club=club, settings=settings)
    with patch("django_q.tasks.async_task"):
        create_payment(
            club_id=club.id,
            student_id=StudentFactory(club=club, status="active").id,
            tariff_id=TariffFactory(club=club, training_type=training_type).id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=group.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    stdout = io.StringIO()
    call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
    report = json.loads(stdout.getvalue())

    assert report["canonical_group_payment_count"] == 1
    assert report["canonical_group_action_counts"] == {"new_admission": 1}
    assert report["payment_owned_group_membership_count"] == 1
    assert report["payment_owned_group_projection_count"] == 1
    assert report["deferred_provider_event_count"] == 0
    assert "student" not in stdout.getvalue()
