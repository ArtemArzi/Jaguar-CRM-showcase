"""T1 — Group salary 'paid once on sale' tests.

Covers:
- verify_payment(confirm) creates sale earning when GROUP + seller present
- amount uses payment.amount (post-discount), not original_amount
- skipped + warning when GROUP + no seller
- idempotent (re-confirm doesn't double-create)
- rejected payment never creates earning
- per-checkin path (calculate_salary) skips GROUP entirely
- personal/mini still pay per-checkin
- reverse_salary protects sale earnings via earning_source filter
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import transaction
from django.utils import timezone

from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import calculate_salary, reverse_salary
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Payment, TrainingType
from apps.billing.services import create_payment, create_subscription, verify_payment
from apps.billing.tests.factories import (
    DiscountFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


@pytest.fixture(autouse=True)
def _disable_payment_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


def _make_group_setup(club, *, price=Decimal("4000")):
    location = LocationFactory(club=club)
    tt = TrainingTypeFactory(
        club=club, slug="group", kind=TrainingType.Kind.GROUP
    )
    tariff = TariffFactory(
        club=club,
        training_type=tt,
        price=price,
        scope="location",
        location=location,
    )
    trainer = TrainerFactory(club=club)
    TrainerLocationFactory(
        club=club,
        trainer=trainer,
        location=location,
        rate_group=Decimal("20.00"),
    )
    student = StudentFactory(club=club)
    user = UserFactory()
    return location, tt, tariff, trainer, student, user


@pytest.mark.django_db
class TestGroupSaleEarning:
    def test_group_subscription_pays_seller_once_on_confirm(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
            )

        # The async_task is mocked; invoke the underlying task synchronously
        from apps.billing.tasks import create_sale_earning
        create_sale_earning(payment.id, club.id)

        earnings = TrainerEarning.objects.filter(payment=payment)
        assert earnings.count() == 1
        e = earnings.get()
        assert e.trainer_id == trainer.id
        assert e.earning_source == TrainerEarning.Source.SALE
        assert e.checkin_id is None
        # 4000 × 20% = 800
        assert e.amount == Decimal("800.00")

    def test_close_period_rejects_pending_group_sale_earning_until_materialized(self, club):
        from apps.billing.tasks import create_sale_earning
        from apps.trainers.services import close_trainer_payroll_period

        loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        target_dt = timezone.now()
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("4000.00"),
            original_amount=Decimal("4000.00"),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.CONFIRMED,
            recorded_by=user,
            verified_by=user,
            verified_at=target_dt,
            seller_trainer=trainer,
            sale_earning_snapshot_recorded=True,
            sale_trainer_id_snapshot=trainer.id,
            sale_training_type_id_snapshot=tt.id,
            sale_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_rate_percent_snapshot=Decimal("20.00"),
            sale_amount_basis_snapshot=Decimal("4000.00"),
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.CONFIRM_TIME,
        )
        target_date = timezone.localtime(target_dt).date()

        with pytest.raises(BusinessLogicError) as exc_info:
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=target_date.replace(day=1),
                period_end=target_date,
                reason="Closed",
                actor_user_id=user.id,
            )

        assert exc_info.value.code == "payroll_close_pending_salary"

        create_sale_earning(payment.id, club.id)
        close = close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=user.id,
        )

        assert close.salary_total_snapshot == Decimal("800.00")

    def test_create_sale_earning_rejects_closed_payroll_period(self, club):
        from apps.billing.tasks import create_sale_earning
        from apps.trainers.services import close_trainer_payroll_period

        loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        target_dt = timezone.now()
        target_date = timezone.localtime(target_dt).date()
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=user.id,
        )
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("4000.00"),
            original_amount=Decimal("4000.00"),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.CONFIRMED,
            recorded_by=user,
            verified_by=user,
            verified_at=target_dt,
            seller_trainer=trainer,
            sale_earning_snapshot_recorded=True,
            sale_trainer_id_snapshot=trainer.id,
            sale_training_type_id_snapshot=tt.id,
            sale_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_rate_percent_snapshot=Decimal("20.00"),
            sale_amount_basis_snapshot=Decimal("4000.00"),
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.CONFIRM_TIME,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_sale_earning(payment.id, club.id)

        assert exc_info.value.code == "payroll_period_closed"
        assert not TrainerEarning.objects.filter(payment=payment).exists()

    def test_verify_payment_uses_club_local_day_for_closed_payroll_period(self, club):
        from apps.trainers.services import close_trainer_payroll_period

        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        closed_local_date = date(2026, 7, 16)
        verified_at = datetime(2026, 7, 15, 20, 30, tzinfo=UTC)
        _loc, _tt, tariff, trainer, student, user = _make_group_setup(club)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_local_date,
            period_end=closed_local_date,
            reason="Closed",
            actor_user_id=user.id,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
                verified_at=verified_at,
            )

        payment.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert payment.status == Payment.Status.PENDING
        assert payment.verified_at is None

    def test_verify_payment_allows_nearby_timestamp_outside_closed_club_day(self, club):
        from apps.trainers.services import close_trainer_payroll_period

        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        closed_local_date = date(2026, 7, 16)
        open_verified_at = datetime(2026, 7, 15, 18, 30, tzinfo=UTC)
        _loc, _tt, tariff, trainer, student, user = _make_group_setup(club)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_local_date,
            period_end=closed_local_date,
            reason="Closed",
            actor_user_id=user.id,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )

        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=user.id,
            action="confirm",
            verified_at=open_verified_at,
        )

        assert result.status == Payment.Status.CONFIRMED
        assert result.verified_at == open_verified_at

    def test_create_sale_earning_uses_club_local_day_for_closed_period(self, club):
        from apps.billing.tasks import create_sale_earning
        from apps.trainers.services import close_trainer_payroll_period

        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        closed_local_date = date(2026, 7, 16)
        verified_at = datetime(2026, 7, 15, 20, 30, tzinfo=UTC)
        _loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_local_date,
            period_end=closed_local_date,
            reason="Closed",
            actor_user_id=user.id,
        )
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("4000.00"),
            original_amount=Decimal("4000.00"),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.CONFIRMED,
            recorded_by=user,
            verified_by=user,
            verified_at=verified_at,
            seller_trainer=trainer,
            sale_earning_snapshot_recorded=True,
            sale_trainer_id_snapshot=trainer.id,
            sale_training_type_id_snapshot=tt.id,
            sale_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_rate_percent_snapshot=Decimal("20.00"),
            sale_amount_basis_snapshot=Decimal("4000.00"),
            sale_snapshot_provenance=Payment.SaleSnapshotProvenance.CONFIRM_TIME,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_sale_earning(payment.id, club.id)

        assert exc_info.value.code == "payroll_period_closed"
        assert not TrainerEarning.objects.filter(payment=payment).exists()

    def test_group_uses_discounted_amount(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(
            club, price=Decimal("5000")
        )
        discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("20"))

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
            discount_ids=[discount.id],
        )
        # 5000 - 20% = 4000
        assert payment.amount == Decimal("4000.00")

        from apps.billing.tasks import create_sale_earning
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
            )
        create_sale_earning(payment.id, club.id)

        e = TrainerEarning.objects.get(payment=payment)
        # 4000 × 20% = 800
        assert e.amount == Decimal("800.00")
        assert e.subscription_price == Decimal("4000.00")

    def test_group_without_seller_no_earning(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=None,
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
            )
        # task wouldn't have been scheduled; calling directly should also skip
        from apps.billing.tasks import create_sale_earning
        create_sale_earning(payment.id, club.id)

        assert not TrainerEarning.objects.filter(payment=payment).exists()

    def test_create_sale_earning_idempotent(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
            )
        from apps.billing.tasks import create_sale_earning
        create_sale_earning(payment.id, club.id)
        create_sale_earning(payment.id, club.id)
        create_sale_earning(payment.id, club.id)

        assert TrainerEarning.objects.filter(payment=payment).count() == 1

    def test_rejected_payment_no_earning(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="reject",
                rejection_reason="receipt mismatch",
            )
        from apps.billing.tasks import create_sale_earning
        create_sale_earning(payment.id, club.id)

        assert not TrainerEarning.objects.filter(payment=payment).exists()

    def test_seller_trainer_other_club_rejected(self, club, other_club):
        loc, tt, tariff, _, student, user = _make_group_setup(club)
        other_trainer = TrainerFactory(club=other_club)

        with pytest.raises(BusinessLogicError):
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="cash",
                recorded_by_id=user.id,
                seller_trainer_id=other_trainer.id,
            )

    @pytest.mark.django_db(transaction=True)
    def test_verify_payment_defers_group_sale_earning_until_outer_commit(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )

        with patch("django_q.tasks.async_task") as mock_async:
            with transaction.atomic():
                verify_payment(
                    payment_id=payment.id,
                    club_id=club.id,
                    verified_by_id=user.id,
                    action="confirm",
                )
                assert mock_async.call_count == 0

            mock_async.assert_called_once_with(
                "apps.billing.tasks.create_sale_earning",
                payment.id,
                club_id=club.id,
            )

    @pytest.mark.django_db(transaction=True)
    def test_create_subscription_defers_group_sale_earning_until_outer_commit(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)

        with patch("django_q.tasks.async_task") as mock_async:
            with transaction.atomic():
                subscription = create_subscription(
                    club_id=club.id,
                    student_id=student.id,
                    tariff_id=tariff.id,
                    recorded_by_id=user.id,
                    payment_method=Payment.Method.CASH,
                    seller_trainer_id=trainer.id,
                )
                payment = Payment.objects.get(subscription=subscription)
                assert mock_async.call_count == 0

            mock_async.assert_called_once_with(
                "apps.billing.tasks.create_sale_earning",
                payment.id,
                club_id=club.id,
            )


@pytest.mark.django_db
class TestCalculateSalaryGroupSkip:
    def test_group_checkin_does_not_create_earning(self, club):
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, location=loc)

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=tt,
            subscription=sub,
            is_debt=False,
        )

        calculate_salary(checkin.id, club.id)

        assert not TrainerEarning.objects.filter(checkin=checkin).exists()

    def test_personal_checkin_still_creates_earning(self, club):
        location = LocationFactory(club=club)
        tt = TrainingTypeFactory(
            club=club, slug="personal", kind=TrainingType.Kind.PERSONAL
        )
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("3000"))
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, location=location)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=location,
            rate_personal=Decimal("50.00"),
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=tt,
            subscription=sub,
            location=location,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        e = TrainerEarning.objects.get(checkin=checkin)
        assert e.earning_source == TrainerEarning.Source.CHECKIN
        assert e.amount == Decimal("1500.00")  # 3000 × 50%

    def test_mini_group_checkin_still_creates_earning(self, club):
        location = LocationFactory(club=club)
        tt = TrainingTypeFactory(
            club=club, slug="mini_group", kind=TrainingType.Kind.MINI_GROUP
        )
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("3000"))
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, location=location)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=location,
            rate_mini_group=Decimal("40.00"),
        )

        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=tt,
            subscription=sub,
            location=location,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        calculate_salary(checkin.id, club.id)
        e = TrainerEarning.objects.get(checkin=checkin)
        assert e.amount == Decimal("1200.00")  # 3000 × 40%


@pytest.mark.django_db
class TestReverseSalaryProtectsSale:
    def test_reverse_salary_filters_by_earning_source(self, club):
        """A reverse_salary call must NEVER cancel a sale earning."""
        loc, tt, tariff, trainer, student, user = _make_group_setup(club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr("django_q.tasks.async_task", lambda *a, **kw: None)
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=user.id,
                action="confirm",
            )
        from apps.billing.tasks import create_sale_earning
        create_sale_earning(payment.id, club.id)

        sale_earning = TrainerEarning.objects.get(payment=payment)
        assert sale_earning.cancelled is False

        # Pretend a checkin gets cancelled (no checkin id matches but call
        # should still be a no-op against sale earning).
        reverse_salary(checkin_id=999999, club_id=club.id)
        sale_earning.refresh_from_db()
        assert sale_earning.cancelled is False
