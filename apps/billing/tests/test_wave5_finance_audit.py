from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.attendance.models import Checkin, CheckinCascadeEvent
from apps.attendance.services import cancel_checkin, create_checkin
from apps.attendance.tasks import calculate_salary
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Payment, Subscription, Tariff, TrainingType
from apps.billing.services import create_payment, create_subscription, verify_payment, write_off_debt
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerRate
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


@pytest.mark.django_db
class TestSalarySnapshotIntegrity:
    @pytest.mark.parametrize(
        ("kind", "rate_field", "initial_rate", "expected_amount"),
        [
            (TrainingType.Kind.PERSONAL, "rate_personal", Decimal("20.00"), Decimal("600.00")),
            (TrainingType.Kind.MINI_GROUP, "rate_mini_group", Decimal("30.00"), Decimal("900.00")),
        ],
    )
    @patch("apps.attendance.services.async_task")
    def test_checkin_salary_uses_queued_snapshot_after_tariff_and_rate_mutation(
        self,
        mock_async,
        club,
        kind,
        rate_field,
        initial_rate,
        expected_amount,
    ):
        training_type = TrainingTypeFactory(club=club, kind=kind)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("3000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            **{rate_field: initial_rate},
        )

        result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=training_type.id,
            source=Checkin.Source.MANUAL,
        )

        assert result["subscription_id"] == subscription.id
        mock_async.assert_any_call(
            "apps.attendance.tasks.calculate_salary",
            result["checkin_id"],
            club_id=club.id,
        )

        tariff.price = Decimal("9000.00")
        tariff.save(update_fields=["price"])
        TrainerRate.objects.for_club(club.id).filter(
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=training_type,
        ).update(percent=Decimal("80.00"))

        calculate_salary(result["checkin_id"], club.id)

        earning = TrainerEarning.objects.get(checkin_id=result["checkin_id"])
        assert earning.amount == expected_amount
        assert earning.subscription_price == Decimal("3000.00")
        assert earning.rate_percent == initial_rate
        assert earning.trainer_id == schedule.trainer_id
        assert earning.earning_type == kind

    def test_eligible_salary_checkin_without_event_fails_loud_instead_of_live_recompute(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("3000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=subscription,
        )
        TrainerLocationFactory(
            club=club,
            trainer=checkin.trainer,
            location=checkin.location,
            rate_personal=Decimal("20.00"),
        )
        tariff.price = Decimal("9000.00")
        tariff.save(update_fields=["price"])

        with pytest.raises(BusinessLogicError) as exc:
            calculate_salary(checkin.id, club.id)

        assert exc.value.code == "salary_snapshot_missing"
        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    @pytest.mark.django_db(transaction=True)
    @patch("django_q.tasks.async_task")
    def test_group_sale_earning_uses_confirmed_seller_rate_snapshot_after_rate_mutation(
        self,
        mock_async,
        club,
        owner_user,
    ):
        location = schedule_location = ScheduleFactory(club=club).location
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            scope="location",
            location=location,
        )
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(
            club=club,
            trainer=trainer,
            location=schedule_location,
            rate_group=Decimal("25.00"),
        )
        student = StudentFactory(club=club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            seller_trainer_id=trainer.id,
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        payment.refresh_from_db()
        assert payment.sale_snapshot_provenance == Payment.SaleSnapshotProvenance.CONFIRM_TIME
        mock_async.assert_any_call(
            "apps.billing.tasks.create_sale_earning",
            payment.id,
            club_id=club.id,
        )

        TrainerRate.objects.for_club(club.id).filter(
            trainer=trainer,
            location=location,
            training_type=training_type,
        ).update(percent=Decimal("80.00"))

        create_sale_earning(payment.id, club.id)

        earning = TrainerEarning.objects.get(payment=payment)
        assert earning.trainer_id == trainer.id
        assert earning.amount == Decimal("1000.00")
        assert earning.subscription_price == Decimal("4000.00")
        assert earning.rate_percent == Decimal("25.00")

    def test_legacy_salary_cascade_without_snapshot_fails_loud(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("3000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=subscription,
        )
        TrainerLocationFactory(
            club=club,
            trainer=checkin.trainer,
            location=checkin.location,
            rate_personal=Decimal("20.00"),
        )
        CheckinCascadeEvent.objects.create(
            club=club,
            checkin=checkin,
            effect=CheckinCascadeEvent.Effect.SALARY,
            expected=True,
            task_name="apps.attendance.tasks.calculate_salary",
            payload={"checkin_id": checkin.id, "club_id": club.id},
        )

        with pytest.raises(BusinessLogicError) as exc:
            calculate_salary(checkin.id, club.id)

        assert exc.value.code == "salary_snapshot_missing"
        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    def test_legacy_current_state_salary_snapshot_requires_manual_reconciliation(self, club):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("3000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=subscription,
        )
        CheckinCascadeEvent.objects.create(
            club=club,
            checkin=checkin,
            effect=CheckinCascadeEvent.Effect.SALARY,
            expected=True,
            task_name="apps.attendance.tasks.calculate_salary",
            payload={
                "checkin_id": checkin.id,
                "club_id": club.id,
                "trainer_id_snapshot": checkin.trainer_id,
                "training_type_id_snapshot": training_type.id,
                "training_type_kind_snapshot": training_type.kind,
                "subscription_price_snapshot": "3000.00",
                "rate_percent_snapshot": "20.00",
                "calculation_basis": "checkin_salary_snapshot",
                "snapshot_provenance": "legacy_backfill_current_state",
            },
        )

        with pytest.raises(BusinessLogicError) as exc:
            calculate_salary(checkin.id, club.id)

        assert exc.value.code == "salary_snapshot_legacy_reconciliation_required"
        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    def test_legacy_confirmed_group_sale_without_snapshot_fails_loud(self, club, owner_user):
        location = ScheduleFactory(club=club).location
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            scope="location",
            location=location,
        )
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(
            club=club,
            trainer=trainer,
            location=location,
            rate_group=Decimal("25.00"),
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="active",
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
            verified_by=owner_user,
            seller_trainer=trainer,
            sale_earning_snapshot_recorded=False,
        )

        with pytest.raises(BusinessLogicError) as exc:
            create_sale_earning(payment.id, club.id)

        assert exc.value.code == "sale_earning_snapshot_missing"
        assert not TrainerEarning.objects.filter(payment=payment).exists()

    def test_legacy_current_state_group_sale_snapshot_requires_manual_reconciliation(
        self,
        club,
        owner_user,
    ):
        location = ScheduleFactory(club=club).location
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            scope="location",
            location=location,
        )
        trainer = TrainerFactory(club=club)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="active",
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("4000.00"),
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
            verified_by=owner_user,
            seller_trainer=trainer,
            sale_earning_snapshot_recorded=True,
            sale_trainer_id_snapshot=trainer.id,
            sale_training_type_id_snapshot=training_type.id,
            sale_training_type_kind_snapshot=training_type.kind,
            sale_rate_percent_snapshot=Decimal("25.00"),
            sale_amount_basis_snapshot=Decimal("4000.00"),
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.LEGACY_BACKFILL_CURRENT_STATE,
        )

        with pytest.raises(BusinessLogicError) as exc:
            create_sale_earning(payment.id, club.id)

        assert exc.value.code == "sale_earning_legacy_reconciliation_required"
        assert not TrainerEarning.objects.filter(payment=payment).exists()


@pytest.mark.django_db
class TestDebtLifecycleAuditEvents:
    def _event_model(self):
        from apps.billing.models import DebtLifecycleEvent

        return DebtLifecycleEvent

    def test_direct_subscription_debt_attachment_records_lifecycle_snapshot(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=Decimal("1500.00"),
        )

        with patch("django_q.tasks.async_task"):
            subscription = create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
                debt_ids=[debt.id],
            )

        event = self._event_model().objects.for_club(club.id).get(debt=debt)
        assert event.event_type == event.EventType.ATTACHED
        assert event.actor_id == owner_user.id
        assert event.reason == "subscription"
        assert event.previous_state == "open"
        assert event.new_state == "resolved:subscription"
        assert event.debt_id_snapshot == debt.id
        assert event.student_id_snapshot == student.id
        assert event.student_name_snapshot == str(student)
        assert event.amount_snapshot == Decimal("1500.00")
        assert event.checkin_id_snapshot == checkin.id
        assert event.payment_id == subscription.payment.id
        assert event.subscription_id == subscription.id
        assert event.created_at is not None

    @patch("django_q.tasks.async_task")
    def test_pending_payment_confirm_does_not_auto_attach_unselected_debt(self, mock_async, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="pending",
            trainings_left=8,
            trainings_used=0,
            expires_at=None,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        debt.refresh_from_db()
        subscription.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert debt.settlement_payment_id is None
        assert subscription.status == Subscription.Status.ACTIVE
        assert not self._event_model().objects.for_club(club.id).filter(debt=debt).exists()
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_selected_debt_reserved_and_confirmed_records_lifecycle_snapshots(
        self,
        mock_async,
        club,
        owner_user,
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        events = list(
            self._event_model().objects.for_club(club.id).filter(debt=debt).order_by("created_at", "id")
        )
        assert [event.event_type for event in events] == [
            events[0].EventType.RESERVED,
            events[0].EventType.CONFIRMED,
        ]
        assert [event.previous_state for event in events] == ["open", "reserved"]
        assert [event.new_state for event in events] == ["reserved", "resolved:payment"]
        assert all(event.actor_id == owner_user.id for event in events)
        assert all(event.payment_id == payment.id for event in events)

    @patch("django_q.tasks.async_task")
    def test_selected_debt_rejected_records_lifecycle_snapshot(self, mock_async, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="Receipt mismatch",
        )

        event = (
            self._event_model().objects.for_club(club.id).filter(debt=debt).order_by("created_at", "id").last()
        )
        assert event.event_type == event.EventType.REJECTED
        assert event.actor_id == owner_user.id
        assert event.reason == "Receipt mismatch"
        assert event.previous_state == "reserved"
        assert event.new_state == "open"
        assert event.payment_id == payment.id

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_cancel_checkin_records_debt_lifecycle_snapshot(
        self,
        mock_payment_async,
        mock_cancel_async,
        club,
        owner_user,
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="pending",
            trainings_left=8,
            trainings_used=0,
            expires_at=None,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        cancel_checkin(
            checkin_id=checkin.id,
            club_id=club.id,
            cancelled_by_user_id=owner_user.id,
            user_role="owner",
        )

        event = (
            self._event_model().objects.for_club(club.id).filter(debt=debt).order_by("created_at", "id").last()
        )
        assert event.event_type == event.EventType.CANCELLED
        assert event.actor_id == owner_user.id
        assert event.reason == "checkin_cancelled"
        assert event.previous_state == "open"
        assert event.new_state == "resolved:cancelled"
        assert event.payment_id is None
        assert event.subscription_id is None

    def test_write_off_records_lifecycle_snapshot(self, club, owner_user):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=Decimal("1200.00"),
        )

        write_off_debt(
            debt_id=debt.id,
            club_id=club.id,
            written_off_by_id=owner_user.id,
            reason="Manual correction",
        )

        event = self._event_model().objects.for_club(club.id).get(debt=debt)
        assert event.event_type == event.EventType.WRITTEN_OFF
        assert event.actor_id == owner_user.id
        assert event.reason == "Manual correction"
        assert event.previous_state == "open"
        assert event.new_state == "resolved:writeoff"
        assert event.amount_snapshot == Decimal("1200.00")
