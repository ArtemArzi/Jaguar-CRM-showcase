from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import BankPaymentOrder, Payment, PaymentRefund, Subscription
from apps.billing.refund_services import complete_payment_refund_payroll, record_manual_payment_refund
from apps.billing.tests.factories import PaymentFactory, SubscriptionComponentFactory, SubscriptionFactory
from apps.billing.tests.test_opening_issuer import apply, command, group_command
from apps.billing.tests.test_refunds import _refund_case
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarningAdjustment, TrainerPayrollPeriodClose

pytestmark = pytest.mark.django_db
command = command
group_command = group_command


@pytest.fixture
def manual(club, owner_user, settings):
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    sub = SubscriptionFactory(
        club=club, student=StudentFactory(club=club, status="active"), paid_amount=Decimal("1000")
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=sub,
        paid_amount_basis_snapshot=Decimal("1000"),
        trainer_payout_policy_snapshot="on_checkin",
    )
    payment = PaymentFactory(
        club=club,
        student=sub.student,
        subscription=sub,
        amount=Decimal("1000"),
        status="confirmed",
        payment_method="cash",
        verified_at=timezone.now() - timedelta(days=10),
    )
    return payment, dict(
        club_id=club.id,
        payment_id=payment.id,
        subscription_id=sub.id,
        actor_user_id=owner_user.id,
        amount=Decimal("200"),
        accounting_date=club_localdate(club),
        reason="Возврат денег",
        idempotency_key="manual-1",
        entitlement_action="kept_partial",
    )


def test_manual_partial_full_revoke_replay_and_cap(manual, settings):
    payment, args = manual
    component = payment.subscription.components.get()
    component.credits_used = 3
    component.credits_left = 5
    component.save(update_fields=["credits_used", "credits_left"])
    payment.subscription.trainings_used = 3
    payment.subscription.trainings_left = 5
    payment.subscription.save(update_fields=["trainings_used", "trainings_left"])
    purchased = (component.credits_total, component.paid_amount_basis_snapshot, component.unit_amount_basis_snapshot)
    first = record_manual_payment_refund(**args)
    assert first.source == "manual" and first.order_id is None and first.refund_case_id is None
    first.full_clean()
    payment.subscription.refresh_from_db()
    assert payment.subscription.status == "active"
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    assert record_manual_payment_refund(**args).id == first.id
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    with pytest.raises(BusinessLogicError, match="Ключ"):
        record_manual_payment_refund(**(args | {"reason": "Иное"}))
    second = record_manual_payment_refund(
        **(args | {"idempotency_key": "manual-2", "amount": Decimal("800"), "entitlement_action": "revoke_remaining"})
    )
    payment.refresh_from_db()
    payment.subscription.refresh_from_db()
    assert payment.amount == Decimal("1000") and payment.status == "confirmed"
    assert payment.subscription.status == Subscription.Status.CANCELLED
    from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings

    assert subscription_balance_findings(subscription=payment.subscription) == []
    component.refresh_from_db()
    assert (component.credits_used, component.credits_left) == (3, 5)
    assert (
        component.credits_total, component.paid_amount_basis_snapshot, component.unit_amount_basis_snapshot,
    ) == purchased
    assert (payment.subscription.trainings_left, payment.subscription.trainings_used) == (0, 3)
    assert second.personal_booking_disposition == "not_applicable"
    assert not BankPaymentOrder.objects.exists()
    with pytest.raises(BusinessLogicError) as err:
        record_manual_payment_refund(**(args | {"idempotency_key": "manual-3"}))
    assert err.value.code == "payment_refund_amount_exceeds_payment"


def test_manual_rejects_provider_and_exact_wrong_subscription(manual, club, owner_user):
    _, args = manual
    order, _ = _refund_case(club=club, owner_user=owner_user)
    with pytest.raises(BusinessLogicError) as err:
        record_manual_payment_refund(
            **(args | {"payment_id": order.payment_id, "subscription_id": order.subscription_id})
        )
    assert err.value.code == "payment_refund_provider_required"
    with pytest.raises(BusinessLogicError) as err:
        record_manual_payment_refund(**(args | {"subscription_id": order.subscription_id}))
    assert err.value.code == "payment_refund_subscription_mismatch"
    assert not PaymentRefund.objects.exists()


def test_dates_monotone_and_source_constraint(manual, club):
    payment, args = manual
    first = record_manual_payment_refund(**args)
    for day, code in [(11, "payment_refund_accounting_date_invalid"), (1, "payment_refund_date_before_prior")]:
        with pytest.raises(BusinessLogicError) as err:
            record_manual_payment_refund(
                **(
                    args
                    | {
                        "idempotency_key": f"earlier{day}",
                        "accounting_date": club_localdate(club) - timedelta(days=day),
                    }
                )
            )
        assert err.value.code == code
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentRefund.objects.filter(id=first.id).update(source="provider")
    assert Payment.objects.get(id=payment.id).amount == Decimal("1000")


def test_opening_commission_refund_closed_period_and_date_floor(club, group_command, settings):
    actor, terms, _, seller, _ = group_command
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    receipt = apply(club, actor, terms)
    day = club_localdate(club)
    TrainerPayrollPeriodClose.objects.create(
        club=club, period_start=day - timedelta(days=1), period_end=day, closed_by=actor, reason="Закрыто"
    )
    refund = record_manual_payment_refund(
        club_id=club.id,
        payment_id=receipt.payment_id,
        subscription_id=receipt.subscription_id,
        actor_user_id=actor.id,
        amount=Decimal("3250"),
        accounting_date=day,
        reason="Половина оплаты возвращена",
        idempotency_key="opening-refund",
        entitlement_action="kept_partial",
    )
    assert refund.status == PaymentRefund.Status.PAYROLL_ACTION_REQUIRED
    with pytest.raises(BusinessLogicError) as err:
        complete_payment_refund_payroll(
            club_id=club.id, refund_id=refund.id, actor_user_id=actor.id, effective_date=day - timedelta(days=2)
        )
    assert err.value.code == "payment_refund_payroll_date_before_refund"
    complete_payment_refund_payroll(
        club_id=club.id, refund_id=refund.id, actor_user_id=actor.id, effective_date=day + timedelta(days=1)
    )
    debit = TrainerEarningAdjustment.objects.get(source_refund=refund)
    assert debit.trainer_id == seller.id and debit.payable_amount_delta == Decimal("-650")
    assert debit.effective_date == day + timedelta(days=1)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_key", [True, False])
def test_postgres_refund_replay_and_cap_are_serialized(manual, same_key):
    from django.db import connection

    from apps.trainers.tests.test_settlements import _observed_club_race

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL")
    payment, args = manual
    args = args | {"amount": Decimal("600")}
    a, b = _observed_club_race(
        payment.club_id,
        lambda: record_manual_payment_refund(**args).id,
        lambda: (
            record_manual_payment_refund(
                **(args | {"idempotency_key": args["idempotency_key"] if same_key else "other"})
            ).id
        ),
    )
    assert a == b if same_key else b == "payment_refund_amount_exceeds_payment"
    assert PaymentRefund.objects.count() == 1


def test_manual_actor_and_cross_tenant_are_checked_before_replay(manual):
    from apps.clubs.models import ClubMembership
    from apps.clubs.tests.factories import ClubFactory

    payment, args = manual
    record_manual_payment_refund(**args)
    other = ClubFactory()
    with pytest.raises(BusinessLogicError):
        record_manual_payment_refund(**(args | {"club_id": other.id}))
    ClubMembership.objects.filter(club_id=payment.club_id, user_id=args["actor_user_id"]).update(role="trainer")
    with pytest.raises(BusinessLogicError) as err:
        record_manual_payment_refund(**args)
    assert err.value.code == "actor_not_authorized"


def test_personal_opening_refund_closes_allocation_without_cancelling_checkin_salary(club, command, settings):
    from apps.attendance.tests.factories import CheckinFactory
    from apps.trainers.models import TrainerEarning, TrainerPackageAllocation
    from apps.trainers.tests.factories import TrainerEarningFactory

    actor, terms = command
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    receipt = apply(club, actor, terms)
    visit = CheckinFactory(club=club, student=receipt.subscription.student, subscription=receipt.subscription)
    earning = TrainerEarningFactory(club=club, checkin=visit, trainer=visit.trainer, amount=Decimal("500"))
    record_manual_payment_refund(
        club_id=club.id,
        payment_id=receipt.payment_id,
        subscription_id=receipt.subscription_id,
        actor_user_id=actor.id,
        amount=terms.paid_amount,
        accounting_date=club_localdate(club),
        reason="Возврат",
        idempotency_key="personal",
        entitlement_action="revoke_remaining",
    )
    assert not TrainerPackageAllocation.objects.get(subscription=receipt.subscription).is_active
    assert not TrainerEarning.objects.get(id=earning.id).cancelled
    assert not TrainerEarningAdjustment.objects.exists()


@pytest.mark.parametrize("independent", [True, False])
def test_manual_opening_revoke_preserves_independent_group(club, group_command, settings, independent):
    from dataclasses import replace

    from apps.attendance.models import TrainingGroupMembership
    from apps.attendance.tests.factories import TrainingGroupMembershipFactory

    actor, terms, group, _, day = group_command
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    if independent:
        student = StudentFactory(
            club=club,
            phone=terms.phone,
            first_name=terms.first_name,
            last_name=terms.last_name,
            date_of_birth=terms.date_of_birth,
            status="active",
        )
        TrainingGroupMembershipFactory(
            club=club, student=student, training_group=group, starts_on=day - timedelta(days=14)
        )
        terms = replace(terms, student_id=student.id)
    receipt = apply(club, actor, terms)
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
    record_manual_payment_refund(
        club_id=club.id,
        payment_id=receipt.payment_id,
        subscription_id=receipt.subscription_id,
        actor_user_id=actor.id,
        amount=terms.paid_amount,
        accounting_date=club_localdate(club),
        reason="Возврат",
        idempotency_key="group",
        entitlement_action="revoke_remaining",
    )
    membership = TrainingGroupMembership.objects.get(student=receipt.subscription.student)
    assert membership.status == ("active" if independent else "cancelled")
    assert TrainerEarningAdjustment.objects.get(source_payment_id=receipt.payment_id).payable_amount_delta == Decimal(
        "-1300"
    )


@pytest.mark.django_db(transaction=True)
def test_postgres_refund_prevents_waiting_correction_from_reviving_source(manual):
    from django.db import connection

    from apps.billing.models import SubscriptionComponent, SubscriptionCorrection
    from apps.billing.service_modules.subscription_corrections import (
        correct_subscription,
        get_subscription_correction_state,
    )
    from apps.trainers.tests.test_settlements import _observed_club_race

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL")
    payment, args = manual
    component = SubscriptionComponent.objects.get(subscription=payment.subscription)
    preview = get_subscription_correction_state(
        club_id=payment.club_id, actor_user_id=args["actor_user_id"], subscription_id=payment.subscription_id
    )
    a, b = _observed_club_race(
        payment.club_id,
        lambda: (
            record_manual_payment_refund(
                **(args | {"amount": payment.amount, "entitlement_action": "revoke_remaining"})
            ).id
        ),
        lambda: (
            correct_subscription(
                club_id=payment.club_id,
                actor_user_id=args["actor_user_id"],
                subscription_id=payment.subscription_id,
                component_id=component.id,
                desired_remaining=3,
                reason="Сверка",
                command_key="correction",
                expected_fingerprint=preview["fingerprint"],
            ).id
        ),
    )
    assert isinstance(a, int) and isinstance(b, str)
    assert not SubscriptionCorrection.objects.exists()
    payment.subscription.refresh_from_db()
    assert payment.subscription.status == "cancelled"
