from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
)
from apps.billing.models import DebtLifecycleEvent, DebtSettlementEvent, DebtWriteOffEvent, Payment, TrainingType
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.grades.tests.factories import GradeSystemFactory
from apps.students.tests.factories import StudentFactory


def _tariff_for_club(club):
    training_type = TrainingTypeFactory(club=club)
    return TariffFactory(club=club, training_type=training_type)


def _subscription_for_club(club):
    tariff = _tariff_for_club(club)
    student = StudentFactory(club=club)
    return SubscriptionFactory(club=club, student=student, tariff=tariff)


def _payment_for_club(club, *, subscription=None):
    if subscription is None:
        subscription = _subscription_for_club(club)
    return PaymentFactory(
        club=club,
        student=subscription.student,
        tariff=subscription.tariff,
        subscription=subscription,
    )


def _debt_for_club(club):
    student = StudentFactory(club=club)
    checkin = CheckinFactory(club=club, student=student)
    return DebtFactory(
        club=club,
        student=student,
        checkin=checkin,
        tariff_price=Decimal("1000.00"),
    )


def _debt_snapshot(debt):
    return {
        "amount_snapshot": debt.tariff_price,
        "debt_id_snapshot": debt.id,
        "student_id_snapshot": debt.student_id,
        "student_name_snapshot": str(debt.student),
        "checkin_id_snapshot": debt.checkin_id,
        "debt_reason_snapshot": debt.reason,
    }


def _lifecycle_event_for(club, *, debt, payment=None, subscription=None, actor=None):
    return DebtLifecycleEvent(
        club=club,
        debt=debt,
        payment=payment,
        subscription=subscription,
        actor=actor,
        event_type=DebtLifecycleEvent.EventType.RESERVED,
        previous_state="open",
        new_state="reserved",
        **_debt_snapshot(debt),
    )


def _writeoff_event_for(club, *, debt, actor):
    return DebtWriteOffEvent(
        club=club,
        debt=debt,
        written_off_by=actor,
        reason="manual correction",
        decided_at=timezone.now(),
        **_debt_snapshot(debt),
    )


def _payment_model_for(club, *, recorded_by, target_schedule=None):
    subscription = _subscription_for_club(club)
    return Payment(
        club=club,
        student=subscription.student,
        tariff=subscription.tariff,
        subscription=subscription,
        amount=Decimal("1000.00"),
        original_amount=Decimal("1000.00"),
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        recorded_by=recorded_by,
        target_schedule=target_schedule,
    )


@pytest.mark.django_db
class TestTrainingTypeClean:
    def test_rejects_foreign_grade_system(self, club, other_club):
        training_type = TrainingType(
            club=club,
            name="Kids Boxing",
            slug="kids-boxing",
            kind=TrainingType.Kind.GROUP,
            grade_system=GradeSystemFactory(club=other_club),
            drop_in_price=Decimal("1000.00"),
        )

        with pytest.raises(ValidationError) as exc_info:
            training_type.full_clean()

        assert "grade_system" in exc_info.value.message_dict

    def test_accepts_same_club_grade_system(self, club):
        training_type = TrainingType(
            club=club,
            name="Kids Boxing",
            slug="kids-boxing",
            kind=TrainingType.Kind.GROUP,
            grade_system=GradeSystemFactory(club=club),
            drop_in_price=Decimal("1000.00"),
        )

        training_type.full_clean()


@pytest.mark.django_db
class TestTrainingGroupPaymentOwnership:
    def test_group_payment_requires_matching_group_anchor_and_membership(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        group = TrainingGroupFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
        )
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=group.location,
            training_group=group,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            location=group.location,
            scope="location",
        )
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = PaymentFactory.build(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=membership.starts_on,
            target_training_group=group,
            target_group_membership=membership,
            conversion_group_membership=membership,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
        )

        payment.full_clean()
        payment.save()

        foreign_group = TrainingGroupFactory(club=club, training_type=training_type, location=group.location)
        payment.target_training_group = foreign_group
        with pytest.raises(ValidationError) as exc_info:
            payment.full_clean()

        assert "target_schedule" in exc_info.value.message_dict

    def test_renewal_cannot_adopt_group_membership_and_ownership_is_unique(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        group = TrainingGroupFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        membership = TrainingGroupMembershipFactory(
            club=club,
            student=student,
            training_group=group,
        )
        tariff = TariffFactory(club=club, training_type=training_type, location=group.location, scope="location")
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = PaymentFactory.build(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            recorded_by=owner_user,
            target_training_group=group,
            target_group_membership=membership,
            conversion_group_membership=membership,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.RENEWAL,
        )

        with pytest.raises(ValidationError) as exc_info:
            payment.full_clean()

        assert "conversion_group_membership" in exc_info.value.message_dict
        owner_payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=SubscriptionFactory(club=club, student=student, tariff=tariff),
            target_training_group=group,
            target_group_membership=membership,
            conversion_group_membership=membership,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
            target_start_date=membership.starts_on,
        )
        with transaction.atomic():
            with pytest.raises(IntegrityError):
                PaymentFactory(
                    club=club,
                    student=student,
                    tariff=tariff,
                    subscription=SubscriptionFactory(club=club, student=student, tariff=tariff),
                    target_training_group=group,
                    target_group_membership=membership,
                    conversion_group_membership=membership,
                    group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
                    target_start_date=membership.starts_on,
                )

        owner_payment.group_membership_action_snapshot = Payment.GroupMembershipActionSnapshot.RENEWAL
        with pytest.raises(ValidationError):
            owner_payment.save()

    def test_provider_event_supports_deferred_normalized_replay_snapshot(self, club):
        subscription = _subscription_for_club(club)
        payment = _payment_for_club(club, subscription=subscription)
        from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent

        order = BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=subscription.student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.OWNER,
            amount_snapshot=payment.amount,
            purpose_snapshot="Deferred replay test",
            expires_at=timezone.now() + timedelta(days=1),
            created_by=payment.recorded_by,
        )
        event = BankPaymentProviderEvent.objects.create(
            club=club,
            order=order,
            provider=order.provider,
            event_type="payment_status",
            provider_event_id="deferred-model-event",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
            normalized_status_snapshot="APPROVED",
            provider_paid_at_snapshot=timezone.now(),
        )

        event.full_clean()
        assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.DEFERRED


@pytest.mark.django_db
class TestDebtSettlementEventClean:
    def test_rejects_foreign_debt(self, club, other_club):
        event = DebtSettlementEvent(
            club=club,
            debt=_debt_for_club(other_club),
            payment=_payment_for_club(club),
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "debt" in exc_info.value.message_dict

    def test_rejects_foreign_payment(self, club, other_club):
        event = DebtSettlementEvent(
            club=club,
            debt=_debt_for_club(club),
            payment=_payment_for_club(other_club),
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "payment" in exc_info.value.message_dict

    def test_accepts_same_club_links(self, club):
        event = DebtSettlementEvent(
            club=club,
            debt=_debt_for_club(club),
            payment=_payment_for_club(club),
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        event.full_clean()


@pytest.mark.django_db
class TestDebtLifecycleEventClean:
    def test_rejects_foreign_debt(self, club, other_club, owner_user):
        event = _lifecycle_event_for(
            club,
            debt=_debt_for_club(other_club),
            payment=_payment_for_club(club),
            subscription=_subscription_for_club(club),
            actor=owner_user,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "debt" in exc_info.value.message_dict

    def test_rejects_foreign_payment(self, club, other_club, owner_user):
        event = _lifecycle_event_for(
            club,
            debt=_debt_for_club(club),
            payment=_payment_for_club(other_club),
            subscription=_subscription_for_club(club),
            actor=owner_user,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "payment" in exc_info.value.message_dict

    def test_rejects_foreign_subscription(self, club, other_club, owner_user):
        event = _lifecycle_event_for(
            club,
            debt=_debt_for_club(club),
            payment=_payment_for_club(club),
            subscription=_subscription_for_club(other_club),
            actor=owner_user,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "subscription" in exc_info.value.message_dict

    def test_accepts_same_club_links(self, club, owner_user):
        event = _lifecycle_event_for(
            club,
            debt=_debt_for_club(club),
            payment=_payment_for_club(club),
            subscription=_subscription_for_club(club),
            actor=owner_user,
        )

        event.full_clean()


@pytest.mark.django_db
class TestDebtWriteOffEventClean:
    def test_rejects_foreign_debt(self, club, other_club, owner_user):
        event = _writeoff_event_for(
            club,
            debt=_debt_for_club(other_club),
            actor=owner_user,
        )

        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()

        assert "debt" in exc_info.value.message_dict

    def test_accepts_same_club_debt(self, club, owner_user):
        event = _writeoff_event_for(
            club,
            debt=_debt_for_club(club),
            actor=owner_user,
        )

        event.full_clean()


@pytest.mark.django_db
class TestPaymentClean:
    def test_rejects_foreign_target_schedule(self, club, other_club, owner_user):
        payment = _payment_model_for(
            club,
            recorded_by=owner_user,
            target_schedule=ScheduleFactory(club=other_club),
        )

        with pytest.raises(ValidationError) as exc_info:
            payment.full_clean()

        assert "target_schedule" in exc_info.value.message_dict

    def test_accepts_same_club_target_schedule(self, club, owner_user):
        payment = _payment_model_for(
            club,
            recorded_by=owner_user,
            target_schedule=ScheduleFactory(club=club),
        )

        payment.full_clean()

    def test_accepts_no_target_schedule(self, club, owner_user):
        payment = _payment_model_for(club, recorded_by=owner_user)

        payment.full_clean()
