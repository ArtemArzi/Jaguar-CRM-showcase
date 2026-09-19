from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.attendance.schemas import PersonalCommercialReceiptOut
from apps.attendance.services.staff_intents import get_personal_commercial_context
from apps.billing.models import Payment, Subscription, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.renewals import create_manual_subscription_renewal
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.students.tests.factories import StudentFactory


def _renewal_case(*, club, source_price=Decimal("8000")):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="Source package",
        price=source_price,
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=7,
        trainings_used=1,
        expires_at=timezone.now() + timedelta(days=7),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=component,
        credits_left=7,
    )
    return student, tariff, component, source


def _receipt_for(*, club, student, payment_id):
    return next(
        receipt
        for receipt in get_personal_commercial_context(
            club_id=club.id,
            student_id=student.id,
        )
        if receipt["payment_id"] == payment_id
    )


@pytest.mark.django_db
def test_rejected_revision_renewal_projects_current_target_offer(club, owner_user):
    student, source_tariff, _source_component, source = _renewal_case(club=club)
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Current package",
        actor_user_id=owner_user.id,
        idempotency_key="receipt-source-a-to-b",
    )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key="receipt-rejected-renewal",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=revision.target_tariff.price,
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="Client changed plans",
        )

    receipt = _receipt_for(club=club, student=student, payment_id=payment.id)
    PersonalCommercialReceiptOut(**receipt)
    assert receipt["tariff_name"] == source_tariff.name
    assert receipt["amount"] == "8500.00"
    assert receipt["renewal_target_tariff_id"] == revision.target_tariff_id
    assert receipt["renewal_target_tariff_name"] == revision.target_tariff.name
    assert receipt["renewal_target_price"] == revision.target_tariff.price
    assert receipt["allowed_actions"] == ["create_renewal"]


@pytest.mark.django_db
def test_accepted_intermediate_receipt_keeps_original_financial_snapshots_after_next_revision(
    club,
    owner_user,
):
    student, source_tariff, _source_component, source = _renewal_case(club=club)
    first = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Intermediate package",
        actor_user_id=owner_user.id,
        idempotency_key="receipt-a-to-b",
    )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key="receipt-accepted-b",
            expected_target_tariff_id=first.target_tariff_id,
            expected_target_price=first.target_tariff.price,
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
    second = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=first.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Latest package",
        actor_user_id=owner_user.id,
        idempotency_key="receipt-b-to-c",
    )

    receipt = _receipt_for(club=club, student=student, payment_id=payment.id)
    assert second.target_tariff_id != payment.tariff_id
    assert receipt["status"] == Payment.Status.CONFIRMED
    assert receipt["tariff_id"] == first.target_tariff_id
    assert receipt["tariff_name"] == source_tariff.name
    assert receipt["amount"] == "8500.00"
    assert receipt["renewal_target_tariff_id"] is None
    assert receipt["renewal_target_price"] is None


@pytest.mark.django_db
def test_unavailable_terminal_renewal_receipt_has_controlled_empty_offer(club, owner_user):
    student, source_tariff, _source_component, source = _renewal_case(club=club)
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key="receipt-unavailable-source",
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="Offer expired",
        )
    Tariff.objects.filter(id=source_tariff.id).update(is_active=False)

    receipt = _receipt_for(club=club, student=student, payment_id=payment.id)
    assert receipt["renewal_target_tariff_id"] is None
    assert receipt["renewal_target_tariff_name"] == ""
    assert receipt["renewal_target_price"] is None
