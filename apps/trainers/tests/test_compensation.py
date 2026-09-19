import importlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Sum

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Payment, TrainingType
from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import (
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerPackageAllocation,
    TrainerPayrollPeriodClose,
)
from apps.trainers.services import (
    close_trainer_payroll_period,
    correct_trainer_earning,
    record_checkin_package_transfer,
    reverse_checkin_manual_corrections,
    reverse_checkin_package_transfer,
)
from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory


def _adjustment_subscription_for_club(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    student = StudentFactory(club=club)
    return SubscriptionFactory(club=club, student=student, tariff=tariff)


def _adjustment_payment_for_club(club, *, subscription=None):
    if subscription is None:
        subscription = _adjustment_subscription_for_club(club)
    return PaymentFactory(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=subscription.tariff,
    )


def _adjustment_kwargs(club, trainer):
    return {
        "club": club,
        "trainer": trainer,
        "amount_basis_snapshot": Decimal("1000.00"),
        "payable_amount_delta": Decimal("0.00"),
        "affects_payroll": False,
        "direction": TrainerEarningAdjustment.Direction.INFO,
        "kind": TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
        "effective_date": date(2026, 6, 24),
    }


def _foreign_adjustment_related_object(field_name, other_club):
    if field_name in {"trainer", "counterparty_trainer"}:
        return TrainerFactory(club=other_club)
    if field_name == "source_checkin":
        return CheckinFactory(club=other_club)
    if field_name == "source_subscription":
        return _adjustment_subscription_for_club(other_club)
    if field_name == "source_payment":
        return _adjustment_payment_for_club(other_club)
    if field_name == "reversal_of":
        foreign_trainer = TrainerFactory(club=other_club)
        return TrainerEarningAdjustment.objects.create(**_adjustment_kwargs(other_club, foreign_trainer))
    raise AssertionError(f"Unsupported related field: {field_name}")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field_name",
    [
        "trainer",
        "source_checkin",
        "source_subscription",
        "source_payment",
        "counterparty_trainer",
        "reversal_of",
    ],
)
def test_trainer_earning_adjustment_clean_rejects_cross_club_related_object(club, other_club, field_name):
    trainer = TrainerFactory(club=club)
    adjustment_kwargs = _adjustment_kwargs(club, trainer)
    adjustment_kwargs[field_name] = _foreign_adjustment_related_object(field_name, other_club)
    adjustment = TrainerEarningAdjustment(**adjustment_kwargs)

    with pytest.raises(ValidationError) as exc_info:
        adjustment.full_clean()

    assert field_name in exc_info.value.message_dict


@pytest.mark.django_db
def test_trainer_earning_adjustment_clean_allows_same_club_related_objects(club):
    trainer = TrainerFactory(club=club)
    counterparty_trainer = TrainerFactory(club=club)
    subscription = _adjustment_subscription_for_club(club)
    payment = _adjustment_payment_for_club(club, subscription=subscription)
    checkin = CheckinFactory(club=club, student=subscription.student)
    reversal = TrainerEarningAdjustment.objects.create(**_adjustment_kwargs(club, trainer))
    adjustment = TrainerEarningAdjustment(
        **_adjustment_kwargs(club, trainer),
        source_checkin=checkin,
        source_subscription=subscription,
        source_payment=payment,
        counterparty_trainer=counterparty_trainer,
        reversal_of=reversal,
    )

    adjustment.full_clean()


@pytest.mark.django_db
def test_trainer_payroll_period_close_clean_rejects_end_before_start(club, owner_user):
    close = TrainerPayrollPeriodClose(
        club=club,
        period_start=date(2026, 6, 30),
        period_end=date(2026, 6, 29),
        closed_by=owner_user,
        reason="Manual close",
    )

    with pytest.raises(ValidationError) as exc_info:
        close.full_clean()

    assert "period_end" in exc_info.value.message_dict


@pytest.mark.django_db
def test_trainer_payroll_period_close_clean_accepts_same_day_period(club, owner_user):
    close = TrainerPayrollPeriodClose(
        club=club,
        period_start=date(2026, 6, 30),
        period_end=date(2026, 6, 30),
        closed_by=owner_user,
        reason="Manual close",
    )

    close.full_clean()


@pytest.mark.django_db
def test_package_transfer_records_informational_adjustment_without_double_counting(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(
        training_type=training_type,
        price=Decimal("6000.00"),
        trainings_limit=6,
    )
    owner_trainer = TrainerFactory(club=club, first_name="Owner")
    actual_trainer = TrainerFactory(club=club, first_name="Actual")
    location = LocationFactory(club=club)
    TrainerRateFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
        percent=Decimal("50.00"),
    )
    schedule = ScheduleFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=actual_trainer,
        location=location,
        subscription=subscription,
        date=date(2026, 6, 16),
    )
    payment = PaymentFactory(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        payment=payment,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=6,
        sessions_remaining_snapshot=5,
        amount_snapshot=Decimal("6000.00"),
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("3000.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("6000.00"),
    )

    transfer = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert transfer is not None
    assert transfer.trainer == actual_trainer
    assert transfer.counterparty_trainer == owner_trainer
    assert transfer.source_checkin == checkin
    assert transfer.source_subscription == subscription
    assert transfer.source_payment == payment
    assert transfer.kind == TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER
    assert transfer.direction == TrainerEarningAdjustment.Direction.INFO
    assert transfer.amount_basis_snapshot == earning.amount
    assert transfer.affects_payroll is False
    assert transfer.payable_amount_delta == Decimal("0.00")
    payable_total = (
        TrainerEarning.objects.for_club(club).filter(cancelled=False).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    adjustment_total = sum(
        a.payable_amount_delta
        for a in TrainerEarningAdjustment.objects.for_club(club).filter(affects_payroll=True)
    )
    assert payable_total + adjustment_total == Decimal("3000.00")


@pytest.mark.django_db
def test_correct_trainer_earning_creates_paired_manual_adjustments(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    source_trainer = TrainerFactory(club=club, first_name="Actual")
    target_trainer = TrainerFactory(club=club, first_name="Owner")
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=source_trainer,
        location=location,
        training_type=training_type,
    )
    checkin = CheckinFactory(
        club=club,
        schedule=schedule,
        trainer=source_trainer,
        training_type=training_type,
        location=location,
        date=date(2026, 6, 20),
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("3000.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("6000.00"),
    )

    result = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="Package owner should receive this payout",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-1",
    )

    assert result.created is True
    assert result.debit.trainer == source_trainer
    assert result.debit.counterparty_trainer == target_trainer
    assert result.debit.direction == TrainerEarningAdjustment.Direction.DEBIT
    assert result.debit.kind == TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT
    assert result.debit.affects_payroll is True
    assert result.debit.payable_amount_delta == Decimal("-3000.00")
    assert result.credit.trainer == target_trainer
    assert result.credit.counterparty_trainer == source_trainer
    assert result.credit.direction == TrainerEarningAdjustment.Direction.CREDIT
    assert result.credit.payable_amount_delta == Decimal("3000.00")
    assert result.credit.correction_group_id == result.debit.correction_group_id
    assert result.credit.idempotency_key == "manual-correction-1"
    assert result.credit.source_checkin == checkin
    assert result.debit.source_earning == earning
    assert result.credit.source_earning == earning
    assert result.credit.created_by == owner_user
    earning.refresh_from_db()
    assert earning.trainer == source_trainer
    assert earning.amount == Decimal("3000.00")


@pytest.mark.django_db
def test_reverse_checkin_manual_corrections_disables_payroll_deltas(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    source_trainer = TrainerFactory(club=club, first_name="Actual")
    target_trainer = TrainerFactory(club=club, first_name="Owner")
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=source_trainer,
        location=location,
        training_type=training_type,
    )
    checkin = CheckinFactory(
        club=club,
        schedule=schedule,
        trainer=source_trainer,
        training_type=training_type,
        location=location,
        date=date(2026, 6, 20),
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("3000.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("6000.00"),
    )
    result = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="Package owner should receive this payout",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-cancelled-checkin",
    )

    first = reverse_checkin_manual_corrections(checkin_id=checkin.id, club_id=club.id)
    second = reverse_checkin_manual_corrections(checkin_id=checkin.id, club_id=club.id)

    assert first == 2
    assert second == 0
    result.debit.refresh_from_db()
    result.credit.refresh_from_db()
    assert result.debit.affects_payroll is False
    assert result.credit.affects_payroll is False


@pytest.mark.django_db
def test_reverse_checkin_manual_corrections_rejects_closed_payroll_period(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    source_trainer = TrainerFactory(club=club, first_name="Actual")
    target_trainer = TrainerFactory(club=club, first_name="Owner")
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=source_trainer,
        location=location,
        training_type=training_type,
    )
    checkin = CheckinFactory(
        club=club,
        schedule=schedule,
        trainer=source_trainer,
        training_type=training_type,
        location=location,
        date=date(2026, 6, 20),
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("3000.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("6000.00"),
    )
    result = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="Package owner should receive this payout",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-closed-period",
    )
    close_trainer_payroll_period(
        club_id=club.id,
        period_start=checkin.date,
        period_end=checkin.date,
        reason="Closed payout period",
        actor_user_id=owner_user.id,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        reverse_checkin_manual_corrections(checkin_id=checkin.id, club_id=club.id)

    assert exc_info.value.code == "payroll_period_closed"
    result.debit.refresh_from_db()
    result.credit.refresh_from_db()
    assert result.debit.affects_payroll is True
    assert result.credit.affects_payroll is True


@pytest.mark.django_db
def test_correct_trainer_earning_is_idempotent_for_same_key(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    checkin = CheckinFactory(club=club, trainer=source_trainer, training_type=training_type)
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("5000.00"),
    )

    first = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="First click",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-repeat",
    )
    second = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="First click",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-repeat",
    )

    assert second.created is False
    assert second.debit.id == first.debit.id
    assert second.credit.id == first.credit.id
    assert TrainerEarningAdjustment.objects.for_club(club).filter(
        kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
        idempotency_key="manual-correction-repeat",
    ).count() == 2


@pytest.mark.django_db
def test_correct_trainer_earning_rejects_second_correction_with_different_key(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    source_trainer = TrainerFactory(club=club)
    first_target = TrainerFactory(club=club)
    second_target = TrainerFactory(club=club)
    checkin = CheckinFactory(club=club, trainer=source_trainer, training_type=training_type)
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("5000.00"),
    )

    correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=first_target.id,
        reason="First correction",
        actor_user_id=owner_user.id,
        idempotency_key="manual-correction-first",
    )

    with pytest.raises(BusinessLogicError) as exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=second_target.id,
            reason="Second correction",
            actor_user_id=owner_user.id,
            idempotency_key="manual-correction-second",
        )

    assert exc.value.code == "trainer_earning_already_corrected"
    assert TrainerEarningAdjustment.objects.for_club(club).filter(
        kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
    ).count() == 2


@pytest.mark.django_db
def test_correct_trainer_earning_rejects_tampered_idempotency_key(club, owner_user):
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    checkin = CheckinFactory(club=club, trainer=source_trainer)
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("5000.00"),
    )

    with pytest.raises(BusinessLogicError) as exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=target_trainer.id,
            reason="Tampered hidden key",
            actor_user_id=owner_user.id,
            idempotency_key="x" * 121,
        )

    assert exc.value.code == "manual_correction_idempotency_invalid"


@pytest.mark.django_db
def test_correct_sale_earning_uses_local_payment_date(club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(training_type=training_type)
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    payment = PaymentFactory(
        club=club,
        student=subscription.student,
        tariff=tariff,
        subscription=subscription,
        status=Payment.Status.CONFIRMED,
        verified_at=datetime(2026, 6, 30, 22, 30, tzinfo=UTC),
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        payment=payment,
        earning_source=TrainerEarning.Source.SALE,
        earning_type=TrainingType.Kind.GROUP,
        amount=Decimal("1000.00"),
        rate_percent=Decimal("20.00"),
        subscription_price=Decimal("5000.00"),
    )

    result = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="Sale attribution correction",
        actor_user_id=owner_user.id,
        idempotency_key="manual-sale-correction",
    )

    assert result.debit.effective_date == date(2026, 7, 1)
    assert result.credit.effective_date == date(2026, 7, 1)


@pytest.mark.django_db
def test_correct_trainer_earning_rejects_invalid_manual_correction(club, other_club, owner_user):
    source_trainer = TrainerFactory(club=club)
    other_trainer = TrainerFactory(club=other_club)
    checkin = CheckinFactory(club=club, trainer=source_trainer)
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=Decimal("5000.00"),
    )

    with pytest.raises(BusinessLogicError) as reason_exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=source_trainer.id,
            reason="",
            actor_user_id=owner_user.id,
            idempotency_key="manual-correction-empty-reason",
        )
    assert reason_exc.value.code == "correction_reason_required"
    with pytest.raises(BusinessLogicError) as same_exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=source_trainer.id,
            reason="Same trainer",
            actor_user_id=owner_user.id,
            idempotency_key="manual-correction-same-trainer",
        )
    assert same_exc.value.code == "target_trainer_same_as_source"
    with pytest.raises(BusinessLogicError) as target_exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=other_trainer.id,
            reason="Cross club",
            actor_user_id=owner_user.id,
            idempotency_key="manual-correction-cross-club",
        )
    assert target_exc.value.code == "target_trainer_not_found"
    assert not TrainerEarningAdjustment.objects.for_club(club).filter(
        kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
    ).exists()


@pytest.mark.django_db
def test_record_checkin_package_transfer_repairs_missing_transfer_when_earning_already_exists(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    owner_trainer = TrainerFactory(club=club)
    actual_trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=actual_trainer,
        location=location,
        subscription=subscription,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
    )
    TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=tariff.price,
    )

    first = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)
    second = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert first is not None
    assert second.id == first.id
    assert TrainerEarningAdjustment.objects.for_club(club).filter(
        source_checkin=checkin,
        kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
    ).count() == 1


@pytest.mark.django_db
def test_record_checkin_package_transfer_returns_existing_row_after_race(club, monkeypatch):
    import apps.trainers.services as trainer_services

    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    owner_trainer = TrainerFactory(club=club)
    actual_trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=actual_trainer,
        location=location,
        subscription=subscription,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
    )
    TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=tariff.price,
    )
    original_save = TrainerEarningAdjustment.save
    refetch_calls = 0

    def save_raises_integrity_error(self, *args, **kwargs):
        raise IntegrityError("simulated package transfer race")

    def refetch_after_simulated_race(*, club_id, checkin_id, trainer_id):
        nonlocal refetch_calls
        refetch_calls += 1
        if refetch_calls == 1:
            return None
        conflicting = TrainerEarningAdjustment(
            club_id=club_id,
            trainer_id=trainer_id,
            amount_basis_snapshot=Decimal("2500.00"),
            payable_amount_delta=Decimal("0.00"),
            affects_payroll=False,
            direction=TrainerEarningAdjustment.Direction.INFO,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
            effective_date=checkin.date,
            source_checkin_id=checkin_id,
            source_subscription_id=subscription.id,
            counterparty_trainer_id=owner_trainer.id,
            reason="package_owner_differs_from_actual_trainer",
        )
        original_save(conflicting)
        return conflicting

    monkeypatch.setattr(trainer_services, "_refetch_package_transfer_adjustment", refetch_after_simulated_race)
    monkeypatch.setattr(TrainerEarningAdjustment, "save", save_raises_integrity_error)

    transfer = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert transfer is not None
    assert transfer.trainer_id == actual_trainer.id
    assert TrainerEarningAdjustment.objects.for_club(club).filter(
        source_checkin=checkin,
        kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
    ).count() == 1


@pytest.mark.django_db
def test_record_checkin_package_transfer_uses_earning_trainer_snapshot(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    package_owner = TrainerFactory(club=club)
    actual_trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=package_owner,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=package_owner,
        location=location,
        subscription=subscription,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=package_owner,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
    )
    TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=tariff.price,
    )

    transfer = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert transfer is not None
    assert transfer.trainer == actual_trainer
    assert transfer.counterparty_trainer == package_owner


@pytest.mark.django_db
def test_migration_backfills_active_personal_package_allocations_from_seller(club, owner_user):
    migration = importlib.import_module("apps.trainers.migrations.0009_trainer_package_compensation")
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type, trainings_limit=8, price=Decimal("8000"))
    student = StudentFactory(club=club)
    owner_trainer = TrainerFactory(club=club)
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status="active",
        trainings_left=5,
        paid_amount=Decimal("8000.00"),
    )
    payment = PaymentFactory(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        seller_trainer=owner_trainer,
        recorded_by=owner_user,
        status="confirmed",
    )

    migration.backfill_package_allocations(apps, None)

    allocation = TrainerPackageAllocation.objects.get(subscription=subscription)
    assert allocation.owner_trainer_id == owner_trainer.id
    assert allocation.payment_id == payment.id
    assert allocation.sessions_total_snapshot == 8
    assert allocation.sessions_remaining_snapshot == 5
    assert allocation.amount_snapshot == Decimal("8000.00")
    assert allocation.source == TrainerPackageAllocation.Source.PAYMENT


@pytest.mark.django_db
def test_reverse_checkin_package_transfer_creates_idempotent_reversal(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    owner_trainer = TrainerFactory(club=club)
    actual_trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=actual_trainer,
        location=location,
        subscription=subscription,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
    )
    TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=tariff.price,
    )
    transfer = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    first = reverse_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)
    second = reverse_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert first is not None
    assert second.id == first.id
    assert first.reversal_of == transfer
    assert first.kind == TrainerEarningAdjustment.Kind.REVERSAL
    assert first.affects_payroll is False
    assert first.payable_amount_delta == Decimal("0.00")


@pytest.mark.django_db
def test_reverse_checkin_package_transfer_returns_existing_reversal_after_race(club, monkeypatch):
    import apps.trainers.services as trainer_services

    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    owner_trainer = TrainerFactory(club=club)
    actual_trainer = TrainerFactory(club=club)
    location = LocationFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=actual_trainer,
        location=location,
        training_type=training_type,
    )
    subscription = SubscriptionFactory(club=club, tariff=tariff)
    checkin = CheckinFactory(
        club=club,
        student=subscription.student,
        schedule=schedule,
        training_type=training_type,
        trainer=actual_trainer,
        location=location,
        subscription=subscription,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
    )
    TrainerEarning.objects.create(
        club=club,
        trainer=actual_trainer,
        checkin=checkin,
        earning_type=TrainingType.Kind.PERSONAL,
        amount=Decimal("2500.00"),
        rate_percent=Decimal("50.00"),
        subscription_price=tariff.price,
    )
    transfer = record_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)
    original_save = TrainerEarningAdjustment.save
    refetch_calls = 0

    def save_raises_integrity_error(self, *args, **kwargs):
        raise IntegrityError("simulated package transfer reversal race")

    def refetch_after_simulated_race(*, club_id, transfer):
        nonlocal refetch_calls
        refetch_calls += 1
        if refetch_calls == 1:
            return None
        conflicting = TrainerEarningAdjustment(
            club_id=club_id,
            trainer_id=transfer.trainer_id,
            amount_basis_snapshot=transfer.amount_basis_snapshot,
            payable_amount_delta=Decimal("0.00"),
            affects_payroll=False,
            direction=TrainerEarningAdjustment.Direction.INFO,
            kind=TrainerEarningAdjustment.Kind.REVERSAL,
            effective_date=transfer.effective_date,
            source_checkin_id=transfer.source_checkin_id,
            source_subscription_id=transfer.source_subscription_id,
            source_payment_id=transfer.source_payment_id,
            counterparty_trainer_id=transfer.counterparty_trainer_id,
            reason="checkin_cancelled",
            reversal_of_id=transfer.id,
        )
        original_save(conflicting)
        return conflicting

    monkeypatch.setattr(trainer_services, "_refetch_package_transfer_reversal", refetch_after_simulated_race)
    monkeypatch.setattr(TrainerEarningAdjustment, "save", save_raises_integrity_error)

    reversal = reverse_checkin_package_transfer(checkin_id=checkin.id, club_id=club.id)

    assert reversal is not None
    assert reversal.reversal_of_id == transfer.id
    assert TrainerEarningAdjustment.objects.for_club(club).filter(
        reversal_of=transfer,
        kind=TrainerEarningAdjustment.Kind.REVERSAL,
    ).count() == 1


@pytest.mark.django_db
def test_package_allocation_rejects_cross_club_subscription(club, other_club):
    training_type = TrainingTypeFactory(club=club)
    tariff = TariffFactory(training_type=training_type)
    foreign_subscription = SubscriptionFactory(club=other_club)
    owner_trainer = TrainerFactory(club=club)

    allocation = TrainerPackageAllocation(
        club=club,
        subscription=foreign_subscription,
        student=foreign_subscription.student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=0,
        amount_snapshot=tariff.price,
    )

    with pytest.raises(ValidationError):
        allocation.full_clean()


@pytest.mark.django_db
def test_package_allocation_rejects_inconsistent_subscription_snapshot(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type)
    other_training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    other_tariff = TariffFactory(training_type=other_training_type)
    subscription = SubscriptionFactory(club=club, student=StudentFactory(club=club), tariff=tariff)
    other_subscription = SubscriptionFactory(
        club=club,
        student=StudentFactory(club=club),
        tariff=other_tariff,
    )
    payment = PaymentFactory(
        club=club,
        subscription=other_subscription,
        student=other_subscription.student,
        tariff=other_tariff,
    )
    owner_trainer = TrainerFactory(club=club)
    allocation = TrainerPackageAllocation(
        club=club,
        subscription=subscription,
        payment=payment,
        student=other_subscription.student,
        tariff=other_tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=0,
        amount_snapshot=tariff.price,
    )

    with pytest.raises(ValidationError) as exc_info:
        allocation.full_clean()

    assert set(exc_info.value.message_dict) >= {"student", "tariff", "training_type", "payment"}


@pytest.mark.django_db
def test_migration_0013_links_unambiguous_manual_correction_to_source_earning(
    club,
    owner_user,
):
    migration = importlib.import_module(
        "apps.trainers.migrations.0013_trainerearningadjustment_source_earning_and_more"
    )
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    subscription = SubscriptionFactory(club=club)
    payment = PaymentFactory(
        club=club,
        student=subscription.student,
        tariff=subscription.tariff,
        subscription=subscription,
        status=Payment.Status.CONFIRMED,
    )
    earning = TrainerEarning.objects.create(
        club=club,
        trainer=source_trainer,
        payment=payment,
        earning_source=TrainerEarning.Source.SALE,
        earning_type=TrainingType.Kind.GROUP,
        amount=Decimal("1000.00"),
        rate_percent=Decimal("20.00"),
        subscription_price=payment.amount,
    )
    correction = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        reason="Legacy sale correction",
        actor_user_id=owner_user.id,
        idempotency_key="legacy-sale-correction-backfill",
    )
    TrainerEarningAdjustment.objects.filter(
        correction_group_id=correction.debit.correction_group_id,
    ).update(source_earning=None)

    migration.backfill_manual_correction_source_earning(apps, None)

    correction.debit.refresh_from_db()
    correction.credit.refresh_from_db()
    assert correction.debit.source_earning_id == earning.id
    assert correction.credit.source_earning_id == earning.id
