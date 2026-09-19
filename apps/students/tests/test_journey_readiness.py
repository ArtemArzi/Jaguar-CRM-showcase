import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    ScheduleEnrollment,
    TrainingGroup,
    TrainingGroupRolloutState,
)
from apps.attendance.tests.factories import PersonalAvailabilitySlotFactory, ScheduleFactory
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import BankPaymentOrder, Payment, Subscription, TrainingType
from apps.billing.services import create_payment
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.leads.models import LeadLifecycleEvent
from apps.students.journey_readiness import audit_unified_client_journey_readiness
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def _dimension(report: dict, name: str) -> dict:
    return next(dimension for dimension in report["clubs"][0]["dimensions"] if dimension["name"] == name)


def _personal_booking_default(
    *,
    club,
    location,
    training_type,
    personal_booking_trainer=None,
    price=Decimal("1000.00"),
    trainings_limit=1,
    is_personal_booking_default=True,
):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=price,
        trainings_limit=trainings_limit,
        duration_days=30,
        scope="location",
        location=location,
        personal_booking_trainer=personal_booking_trainer,
        trainer_payout_policy="on_checkin",
        is_personal_booking_default=is_personal_booking_default,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        name="One personal session",
        credits_total=1,
        scope="location",
        location=location,
        trainer_payout_policy="on_checkin",
        paid_amount_basis=price,
    )
    return tariff


def _linked_legacy_personal_payment(*, club, owner_user, suffix: str, booking_state: str):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    schedule = ScheduleFactory(club=club, training_type=training_type)
    target_date = timezone.localdate() + timedelta(days=2)
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.CANCELLED
        if booking_state == PersonalDropInBooking.State.NO_SHOW
        else ScheduleEnrollment.Status.ACTIVE,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
    )
    booking = PersonalDropInBooking.objects.create(
        club=club,
        enrollment=enrollment,
        tariff=tariff,
        tariff_name_snapshot=tariff.name,
        price_snapshot=tariff.price,
        state=booking_state,
        created_by=owner_user,
        idempotency_key=f"legacy-personal-booking-{suffix}",
    )
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.CONFIRMED,
        recorded_by=owner_user,
    )
    PersonalDropInPaymentLink.objects.create(
        club=club,
        booking=booking,
        payment=payment,
        created_by=owner_user,
        idempotency_key=f"legacy-personal-link-{suffix}",
    )
    PersonalServiceTermsSnapshot.objects.create(
        club=club,
        booking=booking,
        terms_version=PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL,
        tariff_id_snapshot=tariff.id,
        tariff_name_snapshot=tariff.name,
        base_amount=tariff.price,
        discount_amount=Decimal("0.00"),
        payable_amount=tariff.price,
    )
    return payment


def _manual_review_order(*, club, owner_user):
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    tariff = TariffFactory(club=club)
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.ONLINE,
        status=Payment.Status.PENDING,
        recorded_by=owner_user,
    )
    return BankPaymentOrder.objects.create(
        club=club,
        payment=payment,
        subscription=subscription,
        student=student,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.OWNER,
        status=BankPaymentOrder.Status.MANUAL_REVIEW,
        amount_snapshot=tariff.price,
        purpose_snapshot="audit",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=owner_user,
    )


def _online_drop_in_family(*, club, owner_user, suffix: str, with_order: bool = True):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    schedule = ScheduleFactory(club=club, training_type=training_type)
    target_date = timezone.localdate() + timedelta(days=2)
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
    )
    booking = PersonalDropInBooking.objects.create(
        club=club,
        enrollment=enrollment,
        tariff=tariff,
        tariff_name_snapshot=tariff.name,
        price_snapshot=tariff.price,
        created_by=owner_user,
        idempotency_key=f"readiness-booking-{suffix}",
    )
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.ONLINE,
        status=Payment.Status.PENDING,
        recorded_by=owner_user,
    )
    order = None
    if with_order:
        order = BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.OWNER,
            status=BankPaymentOrder.Status.CREATED,
            amount_snapshot=tariff.price,
            purpose_snapshot="personal drop-in audit",
            personal_drop_in_booking_id_snapshot=booking.id,
            expires_at=timezone.now() + timedelta(days=1),
            created_by=owner_user,
        )
    link = PersonalDropInPaymentLink.objects.create(
        club=club,
        booking=booking,
        payment=payment,
        bank_payment_order=order,
        created_by=owner_user,
        idempotency_key=f"readiness-link-{suffix}",
    )
    return booking, link, order


def _reconciliation_candidate(*, club, owner_user):
    target_start_date = timezone.localdate() + timedelta(days=7)
    target_start_date += timedelta(days=(-target_start_date.weekday()) % 7)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
        drop_in_price=None,
    )
    group = TrainingGroup.objects.create(
        club=club,
        name="Readiness reconciliation group",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    rollout = TrainingGroupRolloutState.objects.for_club(club).get()
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        location=location,
        trainer=trainer,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
        one_time_date=None,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("4000.00"),
        trainings_limit=3,
        duration_days=3,
    )
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": "v2",
        },
    )
    with override_settings(
        UNIFIED_CLIENT_JOURNEY_ENABLED=True,
        MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    ), patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_training_group_id=group.id,
            target_schedule_id=schedule.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
    Student.objects.for_club(club).filter(id=student.id).update(
        status=Student.Status.ACTIVE,
        lead_status=Student.LeadStatus.NEW,
        became_student_at=None,
    )
    return payment


@pytest.mark.django_db
def test_readiness_audit_reports_clean_empty_club_as_ready(club):
    report = audit_unified_client_journey_readiness(club_ids=(club.id,))

    assert report == {
        "is_ready": True,
        "audited_club_count": 1,
        "invalid_club_count": 0,
        "invalid_dimension_count": 0,
        "blocker_counts": {},
        "clubs": [
            {
                "club_id": club.id,
                "is_ready": True,
                "dimensions": [
                    {
                        "name": "workspace_provenance",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "live_student_count": 0,
                        "expected_null_legacy_count": 0,
                        "ambiguous_legacy_count": 0,
                    },
                    {
                        "name": "early_manual_admission",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "pending_manual_admission_candidate_count": 0,
                        "unsupported_manual_admission_count": 0,
                    },
                    {
                        "name": "personal_terms",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "legacy_partial_count": 0,
                        "missing_count": 0,
                    },
                    {
                        "name": "personal_default_tariff",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "missing_default_count": 0,
                        "ambiguous_default_count": 0,
                        "invalid_default_count": 0,
                        "legacy_price_mismatch_count": 0,
                    },
                    {
                        "name": "operational_admission",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "operational_admission_payment_count": 0,
                        "deferred_provider_event_count": 0,
                        "legacy_unlinked_pending_manual_count": 0,
                        "manual_admission_origin_counts": {},
                        "pending_row_classification_counts": {},
                        "legacy_pending_reconcile_count": 0,
                        "legacy_pending_drain_count": 0,
                        "unclassified_pending_manual_count": 0,
                    },
                    {
                        "name": "live_finance",
                        "status": "ready",
                        "candidate_count": 0,
                        "classification_state": "clear",
                        "blocker_codes": [],
                        "payment_status_counts": {},
                        "bank_order_status_counts": {},
                        "live_bank_order_count": 0,
                        "manual_review_queues": {
                            "manual_payment": {
                                "count": 0,
                                "age_bucket_counts": {
                                    "lt_1h": 0,
                                    "1h_to_24h": 0,
                                    "1d_to_7d": 0,
                                    "gte_7d": 0,
                                },
                                "max_age_seconds": None,
                            },
                            "online_order": {
                                "count": 0,
                                "age_bucket_counts": {
                                    "lt_1h": 0,
                                    "1h_to_24h": 0,
                                    "1d_to_7d": 0,
                                    "gte_7d": 0,
                                },
                                "max_age_seconds": None,
                            },
                        },
                    },
                ],
            }
        ],
    }


@pytest.mark.django_db
def test_readiness_provenance_treats_lead_trial_and_never_converted_lost_as_expected_null(club):
    StudentFactory(club=club, status=Student.Status.LEAD)
    StudentFactory(club=club, status=Student.Status.TRIAL)
    lost = StudentFactory(club=club, status=Student.Status.LOST)
    LeadLifecycleEvent.objects.create(
        club=club,
        student=lost,
        event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
        metadata={"student_status_from": Student.Status.LEAD},
    )

    dimension = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "workspace_provenance",
    )

    assert dimension["status"] == "ready"
    assert dimension["expected_null_legacy_count"] == 3
    assert dimension["ambiguous_legacy_count"] == 0


@pytest.mark.django_db
def test_readiness_reports_workspace_and_early_manual_admission_blockers(club):
    StudentFactory(club=club, status=Student.Status.ACTIVE)
    StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        crm_entry_kind=Student.CrmEntryKind.LEAD_INTAKE,
    )

    report = audit_unified_client_journey_readiness(club_ids=(club.id,))

    assert _dimension(report, "workspace_provenance")["blocker_codes"] == ["ambiguous_legacy_student_provenance"]
    assert _dimension(report, "early_manual_admission")["blocker_codes"] == [
        "early_manual_admission_without_durable_evidence"
    ]
    assert report["invalid_club_count"] == 1


@pytest.mark.django_db
def test_readiness_reports_personal_terms_default_and_operational_integrity_blockers(club, owner_user):
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type)
    slot_starts_at = timezone.now() + timedelta(days=1)
    slot = PersonalAvailabilitySlotFactory(
        club=club,
        location=location,
        training_type=training_type,
        starts_at=slot_starts_at,
        ends_at=slot_starts_at + timedelta(hours=1),
    )
    PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=StudentFactory(club=club),
        trainer=slot.trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        expires_at=timezone.now() + timedelta(hours=1),
        created_by=owner_user,
    )
    group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    schedule = ScheduleFactory(club=club, training_type=group_type)
    PaymentFactory(
        club=club,
        student=StudentFactory(club=club),
        tariff=TariffFactory(club=club, training_type=group_type),
        target_schedule=schedule,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        recorded_by=owner_user,
    )

    report = audit_unified_client_journey_readiness(club_ids=(club.id,))

    assert _dimension(report, "personal_terms")["blocker_codes"] == ["missing_personal_terms"]
    assert _dimension(report, "personal_default_tariff")["blocker_codes"] == ["personal_booking_tariff_not_configured"]
    operational_codes = _dimension(report, "operational_admission")["blocker_codes"]
    assert "missing_subscription" in operational_codes
    assert "missing_enrollment" in operational_codes
    finance_codes = _dimension(report, "live_finance")["blocker_codes"]
    assert "live_personal_reservation_missing_bank_order" in finance_codes
    assert "live_personal_reservation_missing_payment" in finance_codes
    assert "live_personal_reservation_missing_subscription" in finance_codes


@pytest.mark.django_db
def test_readiness_uses_model_live_order_states_and_checks_personal_order_binding(club, owner_user):
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.ONLINE,
        status=Payment.Status.PENDING,
        recorded_by=owner_user,
    )
    starts_at = timezone.now() + timedelta(days=1)
    slot = PersonalAvailabilitySlotFactory(
        club=club,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    reservation = PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=student,
        trainer=slot.trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
        availability_slot=slot,
        payment=payment,
        subscription=subscription,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        expires_at=timezone.now() + timedelta(hours=1),
        created_by=owner_user,
    )
    mismatched_order = BankPaymentOrder.objects.create(
        club=club,
        payment=payment,
        subscription=subscription,
        student=student,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.STUDENT,
        status=BankPaymentOrder.Status.CREATED,
        amount_snapshot=tariff.price,
        purpose_snapshot="personal audit",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=owner_user,
    )
    reservation.bank_payment_order = mismatched_order
    reservation.save(update_fields=["bank_payment_order", "updated_at"])
    approved_order = _manual_review_order(club=club, owner_user=owner_user)
    approved_order.status = BankPaymentOrder.Status.APPROVED
    approved_order.save(update_fields=["status", "updated_at"])

    finance = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "live_finance",
    )

    assert finance["bank_order_status_counts"] == {"approved": 1, "created": 1}
    assert finance["live_bank_order_count"] == 1
    assert finance["blocker_codes"] == ["live_personal_reservation_bank_order_mismatch"]


@pytest.mark.django_db
def test_readiness_blocks_live_orders_with_missing_personal_origins(club, owner_user):
    reservation_order = _manual_review_order(club=club, owner_user=owner_user)
    reservation_order.personal_booking_reservation_id_snapshot = 999_991
    reservation_order.save(update_fields=["personal_booking_reservation_id_snapshot", "updated_at"])
    drop_in_order = _manual_review_order(club=club, owner_user=owner_user)
    drop_in_order.personal_drop_in_booking_id_snapshot = 999_992
    drop_in_order.save(update_fields=["personal_drop_in_booking_id_snapshot", "updated_at"])

    finance = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "live_finance",
    )

    assert "bank_order_personal_reservation_origin_missing" in finance["blocker_codes"]
    assert "bank_order_personal_drop_in_origin_missing" in finance["blocker_codes"]


@pytest.mark.django_db
def test_readiness_blocks_online_drop_in_link_without_bank_order(club, owner_user):
    _online_drop_in_family(club=club, owner_user=owner_user, suffix="missing-order", with_order=False)

    finance = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "live_finance",
    )

    assert "online_personal_drop_in_link_missing_bank_order" in finance["blocker_codes"]


@pytest.mark.django_db
def test_readiness_blocks_mismatched_drop_in_order_snapshot(club, owner_user):
    first_booking, _, first_order = _online_drop_in_family(
        club=club,
        owner_user=owner_user,
        suffix="first",
    )
    second_booking, _, second_order = _online_drop_in_family(
        club=club,
        owner_user=owner_user,
        suffix="second",
    )
    assert first_order is not None and second_order is not None
    second_order.status = BankPaymentOrder.Status.CANCELLED
    second_order.save(update_fields=["status", "updated_at"])
    first_order.personal_drop_in_booking_id_snapshot = second_booking.id
    first_order.save(update_fields=["personal_drop_in_booking_id_snapshot", "updated_at"])

    finance = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "live_finance",
    )

    assert first_booking.id != second_booking.id
    assert "personal_drop_in_bank_order_link_mismatch" in finance["blocker_codes"]
    assert "bank_order_personal_drop_in_origin_mismatch" in finance["blocker_codes"]


@pytest.mark.django_db
def test_readiness_reports_personal_default_price_shadow_mismatch(club):
    LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price="1000.00",
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price="1200.00",
        trainings_limit=1,
        duration_days=30,
        scope="club",
        location=None,
        trainer_payout_policy="on_checkin",
        is_personal_booking_default=True,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        name="One personal session",
        credits_total=1,
        scope="club",
        location=None,
        trainer_payout_policy="on_checkin",
        paid_amount_basis=tariff.price,
    )

    dimension = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "personal_default_tariff",
    )

    assert dimension["legacy_price_mismatch_count"] == 1
    assert dimension["blocker_codes"] == ["legacy_drop_in_price_mismatch"]


@pytest.mark.django_db
def test_readiness_accepts_generic_and_distinct_trainer_personal_defaults(club):
    location = LocationFactory(club=club)
    first_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    second_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    _personal_booking_default(club=club, location=location, training_type=first_type)
    _personal_booking_default(club=club, location=location, training_type=second_type)
    _personal_booking_default(
        club=club,
        location=location,
        training_type=first_type,
        personal_booking_trainer=TrainerFactory(club=club),
        price=Decimal("1200.00"),
    )
    _personal_booking_default(
        club=club,
        location=location,
        training_type=first_type,
        personal_booking_trainer=TrainerFactory(club=club),
        price=Decimal("1300.00"),
    )
    _personal_booking_default(
        club=club,
        location=location,
        training_type=second_type,
        personal_booking_trainer=TrainerFactory(club=club),
        price=Decimal("1400.00"),
    )

    report = audit_unified_client_journey_readiness(club_ids=(club.id,))
    dimension = _dimension(report, "personal_default_tariff")

    assert report["is_ready"] is True
    assert dimension["blocker_codes"] == []


@pytest.mark.django_db
def test_readiness_blocks_duplicate_generic_personal_default_candidates(club):
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    generic = _personal_booking_default(club=club, location=location, training_type=training_type)
    duplicate = _personal_booking_default(
        club=club,
        location=location,
        training_type=training_type,
        is_personal_booking_default=False,
    )

    with patch(
        "apps.students.journey_readiness.personal_booking_default_candidates",
        return_value=[generic, duplicate],
    ):
        dimension = _dimension(
            audit_unified_client_journey_readiness(club_ids=(club.id,)),
            "personal_default_tariff",
        )

    assert dimension["ambiguous_default_count"] == 1
    assert dimension["blocker_codes"] == ["personal_booking_tariff_ambiguous"]


@pytest.mark.django_db
def test_readiness_blocks_duplicate_same_trainer_personal_default_candidates(club):
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    generic = _personal_booking_default(club=club, location=location, training_type=training_type)
    trainer = TrainerFactory(club=club)
    trainer_default = _personal_booking_default(
        club=club,
        location=location,
        training_type=training_type,
        personal_booking_trainer=trainer,
    )
    duplicate = _personal_booking_default(
        club=club,
        location=location,
        training_type=training_type,
        personal_booking_trainer=trainer,
        is_personal_booking_default=False,
    )

    def candidates(*, trainer_id, **_kwargs):
        return [generic] if trainer_id is None else [trainer_default, duplicate]

    with patch(
        "apps.students.journey_readiness.personal_booking_default_candidates",
        side_effect=candidates,
    ):
        dimension = _dimension(
            audit_unified_client_journey_readiness(club_ids=(club.id,)),
            "personal_default_tariff",
        )

    assert dimension["ambiguous_default_count"] == 1
    assert dimension["blocker_codes"] == ["personal_booking_tariff_ambiguous"]


@pytest.mark.django_db
def test_readiness_blocks_invalid_trainer_personal_default_contract(club):
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=None,
    )
    _personal_booking_default(club=club, location=location, training_type=training_type)
    _personal_booking_default(
        club=club,
        location=location,
        training_type=training_type,
        personal_booking_trainer=TrainerFactory(club=club),
        trainings_limit=2,
    )

    dimension = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "personal_default_tariff",
    )

    assert dimension["invalid_default_count"] == 1
    assert dimension["blocker_codes"] == ["personal_booking_tariff_invalid"]


@pytest.mark.django_db
def test_readiness_accepts_confirmed_terminal_legacy_personal_payment_but_blocks_live_legacy_evidence(
    club,
    owner_user,
):
    _linked_legacy_personal_payment(
        club=club,
        owner_user=owner_user,
        suffix="terminal",
        booking_state=PersonalDropInBooking.State.NO_SHOW,
    )
    _linked_legacy_personal_payment(
        club=club,
        owner_user=owner_user,
        suffix="live",
        booking_state=PersonalDropInBooking.State.SCHEDULED,
    )

    from apps.billing.management.commands.audit_operational_admissions import _audit_club

    audit = _audit_club(club=club)
    report = audit_unified_client_journey_readiness(club_ids=(club.id,))
    operational = _dimension(report, "operational_admission")
    personal_terms = _dimension(report, "personal_terms")

    assert audit["invalid_state_counts"]["target_schedule_missing"] == 1
    assert "target_schedule_missing" in operational["blocker_codes"]
    assert personal_terms["blocker_codes"] == ["legacy_partial_personal_terms"]

@pytest.mark.django_db
def test_readiness_operational_admission_ignores_unrelated_pending_payment_families(club, owner_user):
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    for _ in range(2):
        tariff = TariffFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )

    operational = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "operational_admission",
    )

    assert operational["status"] == "ready"
    assert operational["operational_admission_payment_count"] == 0
    assert operational["blocker_codes"] == []


@pytest.mark.django_db
def test_readiness_reuses_authoritative_operational_admission_invariants(club, owner_user):
    other_club = ClubFactory()
    foreign_schedule = ScheduleFactory(club=other_club)
    PaymentFactory(
        club=club,
        student=StudentFactory(club=club),
        tariff=TariffFactory(club=club),
        target_schedule=foreign_schedule,
        target_start_date=timezone.localdate(),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        recorded_by=owner_user,
    )

    operational = _dimension(
        audit_unified_client_journey_readiness(club_ids=(club.id,)),
        "operational_admission",
    )

    assert "target_schedule_tenancy_mismatch" in operational["blocker_codes"]
    assert "missing_subscription" in operational["blocker_codes"]
    assert "missing_enrollment" in operational["blocker_codes"]


@pytest.mark.django_db
def test_readiness_blocks_pending_manual_reconciliation_candidates(club, owner_user):
    _reconciliation_candidate(club=club, owner_user=owner_user)

    report = audit_unified_client_journey_readiness(club_ids=(club.id,))
    operational = _dimension(report, "operational_admission")

    assert report["is_ready"] is False
    assert operational["status"] == "blocked"
    assert operational["legacy_pending_reconcile_count"] == 1
    assert operational["blocker_codes"] == [
        "legacy_pending_manual_reconciliation_required"
    ]


@pytest.mark.django_db
def test_readiness_reports_finance_queue_age_and_keeps_json_pii_free(club, owner_user):
    manual = PaymentFactory(
        club=club,
        student=StudentFactory(
            club=club,
            first_name="Sensitive Name",
            phone="+79170000001",
            email="sensitive@example.test",
        ),
        recorded_by=owner_user,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
    )
    order = _manual_review_order(club=club, owner_user=owner_user)
    now = timezone.now()
    Payment.objects.for_club(club).filter(id=manual.id).update(created_at=now - timedelta(days=8))
    BankPaymentOrder.objects.for_club(club).filter(id=order.id).update(
        created_at=now - timedelta(days=2),
        provider_payment_url="https://private.example.test/payment",
        last_error_message="sensitive-provider-error",
    )

    report = audit_unified_client_journey_readiness(club_ids=(club.id,))
    finance = _dimension(report, "live_finance")
    serialized = json.dumps(report)

    assert finance["payment_status_counts"] == {"pending": 2}
    assert finance["bank_order_status_counts"] == {"manual_review": 1}
    assert finance["manual_review_queues"]["manual_payment"] == {
        "count": 1,
        "age_bucket_counts": {"lt_1h": 0, "1h_to_24h": 0, "1d_to_7d": 0, "gte_7d": 1},
        "max_age_seconds": pytest.approx(8 * 24 * 60 * 60, abs=5),
    }
    assert finance["manual_review_queues"]["online_order"] == {
        "count": 1,
        "age_bucket_counts": {"lt_1h": 0, "1h_to_24h": 0, "1d_to_7d": 1, "gte_7d": 0},
        "max_age_seconds": pytest.approx(2 * 24 * 60 * 60, abs=5),
    }
    assert "Sensitive Name" not in serialized
    assert "+79170000001" not in serialized
    assert "sensitive@example.test" not in serialized
    assert "provider_payment_url" not in serialized
    assert "private.example.test" not in serialized
    assert "sensitive-provider-error" not in serialized


@pytest.mark.django_db
def test_readiness_aggregates_each_tenant_without_leaking_other_club_state(club):
    other_club = ClubFactory()
    StudentFactory(club=other_club, status=Student.Status.ACTIVE)

    report = audit_unified_client_journey_readiness()

    reports = {club_report["club_id"]: club_report for club_report in report["clubs"]}
    assert report["audited_club_count"] == 2
    assert report["invalid_club_count"] == 1
    assert reports[club.id]["is_ready"] is True
    assert reports[other_club.id]["is_ready"] is False


@pytest.mark.django_db
def test_readiness_command_logs_one_safe_aggregate_event_and_fail_on_invalid(club):
    stdout = io.StringIO()
    with patch("apps.students.management.commands.audit_unified_client_journey.logger.info") as log_info:
        call_command("audit_unified_client_journey", "--club-id", club.id, stdout=stdout)

    report = json.loads(stdout.getvalue())
    assert report["is_ready"] is True
    log_info.assert_called_once_with(
        "unified_client_journey_readiness_audited",
        extra={
            "audited_club_count": 1,
            "invalid_club_count": 0,
            "is_ready": True,
        },
    )
    StudentFactory(club=club, status=Student.Status.ACTIVE)
    with pytest.raises(CommandError, match="unified_client_journey_readiness_invalid"):
        call_command(
            "audit_unified_client_journey",
            "--club-id",
            club.id,
            "--fail-on-invalid",
            stdout=io.StringIO(),
        )
    with pytest.raises(CommandError, match="one of the arguments"):
        call_command("audit_unified_client_journey", stdout=io.StringIO())
