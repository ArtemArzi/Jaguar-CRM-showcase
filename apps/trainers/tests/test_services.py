from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import calculate_salary
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import Payment, TrainingType
from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerRate
from apps.trainers.selectors import get_trainers
from apps.trainers.services import (
    assert_trainer_payroll_date_open,
    close_trainer_payroll_period,
    correct_trainer_earning,
    create_trainer,
    update_trainer_locations,
    update_trainer_rates,
)
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


@pytest.mark.django_db
class TestCreateTrainer:
    def test_create_trainer(self, club):
        trainer = create_trainer(
            club_id=club.id,
            first_name="Sergey",
            last_name="Ivanov",
            phone="+79001111111",
        )
        assert trainer.first_name == "Sergey"
        assert trainer.last_name == "Ivanov"
        assert trainer.phone == "+79001111111"
        assert trainer.is_active is True
        assert trainer.club_id == club.id

    def test_create_trainer_with_locations(self, club):
        from apps.billing.models import TrainingType
        from apps.billing.tests.factories import TrainingTypeFactory
        from apps.trainers.models import TrainerRate

        loc1 = LocationFactory(club=club)
        loc2 = LocationFactory(club=club)
        tt_group = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tt_personal = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        trainer = create_trainer(
            club_id=club.id,
            first_name="Sergey",
            last_name="Ivanov",
            locations=[
                {
                    "location_id": loc1.id,
                    "rate_group": 25,
                    "rate_personal": 50,
                    "rate_mini_group": 40,
                },
                {
                    "location_id": loc2.id,
                },
            ],
        )
        tl = trainer.trainer_locations.all()
        assert tl.count() == 2
        rates = TrainerRate.objects.filter(trainer=trainer, location=loc1)
        assert rates.get(training_type=tt_group).percent == Decimal("25")
        assert rates.get(training_type=tt_personal).percent == Decimal("50")

    def test_create_trainer_location_wrong_club(self, club, other_club):
        other_loc = LocationFactory(club=other_club)
        with pytest.raises(BusinessLogicError) as exc_info:
            create_trainer(
                club_id=club.id,
                first_name="Sergey",
                last_name="Ivanov",
                locations=[{"location_id": other_loc.id}],
            )
        assert exc_info.value.code == "location_club_mismatch"


@pytest.mark.django_db
class TestUpdateTrainerLocations:
    def test_update_trainer_locations(self, club):
        from apps.billing.models import TrainingType
        from apps.billing.tests.factories import TrainingTypeFactory
        from apps.trainers.models import TrainerRate

        loc1 = LocationFactory(club=club)
        loc2 = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        tt_group = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(trainer=trainer, location=loc1)

        result = update_trainer_locations(
            trainer_id=trainer.id,
            club_id=club.id,
            locations=[
                {"location_id": loc2.id, "rate_group": 30, "rate_personal": 60, "rate_mini_group": 45},
            ],
        )
        assert len(result) == 1
        assert result[0].location_id == loc2.id
        rate = TrainerRate.objects.get(
            trainer=trainer, location=loc2, training_type=tt_group,
        )
        assert rate.percent == Decimal("30")
        # Old location should be removed
        assert trainer.trainer_locations.count() == 1


@pytest.mark.django_db
class TestUpdateTrainerRates:
    def test_upsert_and_cross_tenant(self, club, other_club):
        from apps.billing.tests.factories import TrainingTypeFactory

        loc1 = LocationFactory(club=club)
        loc2 = LocationFactory(club=club)
        tt1 = TrainingTypeFactory(club=club)
        tt2 = TrainingTypeFactory(club=club)
        trainer = TrainerFactory(club=club)

        rates = [
            {"location_id": loc1.id, "training_type_id": tt1.id, "percent": Decimal("10")},
            {"location_id": loc1.id, "training_type_id": tt2.id, "percent": Decimal("20")},
            {"location_id": loc2.id, "training_type_id": tt1.id, "percent": Decimal("30")},
            {"location_id": loc2.id, "training_type_id": tt2.id, "percent": Decimal("40")},
        ]
        n = update_trainer_rates(club_id=club.id, trainer_id=trainer.id, rates=rates)
        assert n == 4
        assert TrainerRate.objects.for_club(club).filter(trainer=trainer).count() == 4

        # Upsert — same keys, new percents, should NOT create new rows
        updated = [
            {"location_id": loc1.id, "training_type_id": tt1.id, "percent": Decimal("11")},
            {"location_id": loc1.id, "training_type_id": tt2.id, "percent": Decimal("22")},
            {"location_id": loc2.id, "training_type_id": tt1.id, "percent": Decimal("33")},
            {"location_id": loc2.id, "training_type_id": tt2.id, "percent": Decimal("44")},
        ]
        update_trainer_rates(club_id=club.id, trainer_id=trainer.id, rates=updated)
        assert TrainerRate.objects.for_club(club).filter(trainer=trainer).count() == 4
        tr = TrainerRate.objects.for_club(club).get(
            trainer=trainer, location=loc1, training_type=tt1,
        )
        assert tr.percent == Decimal("11")

        # Cross-tenant: other_club trainer → raises
        other_trainer = TrainerFactory(club=other_club)
        with pytest.raises(BusinessLogicError):
            update_trainer_rates(
                club_id=club.id,
                trainer_id=other_trainer.id,
                rates=[{"location_id": loc1.id, "training_type_id": tt1.id, "percent": Decimal("5")}],
            )


@pytest.mark.django_db
class TestTrainerTenantIsolation:
    def test_get_trainers_tenant_isolation(self, club, other_club):
        TrainerFactory(club=club)
        TrainerFactory(club=other_club)
        trainers = get_trainers(club=club)
        assert trainers.count() == 1
        assert trainers.first().club_id == club.id


@pytest.mark.django_db
class TestTrainerLocationUnique:
    def test_trainer_location_unique(self, club):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(trainer=trainer, location=loc)
        with pytest.raises(IntegrityError):
            TrainerLocationFactory(trainer=trainer, location=loc)


@pytest.mark.django_db
class TestTrainerPayrollPeriodClose:
    def _create_earning(self, *, club, target_date):
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
        from apps.billing.models import TrainingType
        from apps.billing.tests.factories import TrainingTypeFactory
        from apps.trainers.models import TrainerEarning

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer, training_type=training_type)
        checkin = CheckinFactory(
            club=club,
            schedule=schedule,
            trainer=trainer,
            training_type=training_type,
            date=target_date,
        )
        earning = TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            checkin=checkin,
            earning_type=TrainingType.Kind.PERSONAL,
            amount=Decimal("3000.00"),
            rate_percent=Decimal("50.00"),
            subscription_price=Decimal("6000.00"),
        )
        return earning

    def test_close_period_persists_reason_actor_and_snapshot(self, club, owner_user):
        from apps.trainers.models import TrainerPayrollPeriodClose

        earning = self._create_earning(club=club, target_date=timezone.localdate())

        close = close_trainer_payroll_period(
            club_id=club.id,
            period_start=earning.checkin.date.replace(day=1),
            period_end=earning.checkin.date,
            reason="June payroll approved",
            actor_user_id=owner_user.id,
        )

        assert close.period_start == earning.checkin.date.replace(day=1)
        assert close.period_end == earning.checkin.date
        assert close.reason == "June payroll approved"
        assert close.closed_by_id == owner_user.id
        assert close.salary_total_snapshot == Decimal("3000.00")
        assert str(earning.trainer_id) in close.trainer_totals_snapshot
        assert TrainerPayrollPeriodClose.objects.for_club(club).count() == 1

    def test_close_period_rejects_overlap_same_club(self, club, owner_user):
        today = timezone.localdate()
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=today.replace(day=1),
            period_end=today,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=today,
                period_end=today + timedelta(days=3),
                reason="Overlap",
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "trainer_payroll_period_overlap"

    def test_close_period_rejects_pending_checkin_salary_until_materialized(self, club, owner_user):
        target_date = timezone.localdate()
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000.00"))
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        TrainerLocationFactory(
            club=club,
            trainer=schedule.trainer,
            location=schedule.location,
            rate_personal=Decimal("20.00"),
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            subscription=sub,
            date=target_date,
            is_debt=False,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)

        with pytest.raises(BusinessLogicError) as exc_info:
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=target_date.replace(day=1),
                period_end=target_date,
                reason="Closed",
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "payroll_close_pending_salary"

        calculate_salary(checkin.id, club.id)
        close = close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        assert close.salary_total_snapshot == Decimal("1000.00")
        assert close.trainer_totals_snapshot[str(schedule.trainer_id)]["total"] == "1000.00"

    def test_close_period_pending_sale_salary_uses_club_local_day_boundary(self, club, owner_user):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        local_sale_day = datetime(2026, 6, 28, 20, 30, tzinfo=UTC).date() + timedelta(days=1)
        trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("4000.00"))
        student = StudentFactory(club=club)
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Payment.Status.CONFIRMED,
            verified_at=datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
            sale_earning_snapshot_recorded=True,
            sale_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            sale_trainer_id_snapshot=trainer.id,
            sale_rate_percent_snapshot=Decimal("20.00"),
            sale_amount_basis_snapshot=Decimal("4000.00"),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            close_trainer_payroll_period(
                club_id=club.id,
                period_start=local_sale_day,
                period_end=local_sale_day,
                reason="Closed",
                actor_user_id=owner_user.id,
            )

        assert exc_info.value.code == "payroll_close_pending_salary"

    def test_assert_date_open_blocks_closed_period(self, club, owner_user):
        today = timezone.localdate()
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=today.replace(day=1),
            period_end=today,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            assert_trainer_payroll_date_open(club_id=club.id, target_date=today)

        assert exc_info.value.code == "payroll_period_closed"

    def test_manual_correction_after_close_is_blocked(self, club, owner_user):
        today = timezone.localdate()
        earning = self._create_earning(club=club, target_date=today)
        target_trainer = TrainerFactory(club=club)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=today.replace(day=1),
            period_end=today,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            correct_trainer_earning(
                club_id=club.id,
                earning_id=earning.id,
                target_trainer_id=target_trainer.id,
                reason="Move payout",
                actor_user_id=owner_user.id,
                idempotency_key="closed-correction",
            )

        assert exc_info.value.code == "payroll_period_closed"

    def test_payment_earning_correction_uses_club_local_closed_day(self, club, owner_user):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        closed_local_date = date(2026, 7, 16)
        verified_at = datetime(2026, 7, 15, 20, 30, tzinfo=UTC)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_local_date,
            period_end=closed_local_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        subscription = SubscriptionFactory(club=club, tariff=tariff)
        source_trainer = TrainerFactory(club=club)
        target_trainer = TrainerFactory(club=club)
        payment = PaymentFactory(
            club=club,
            student=subscription.student,
            tariff=tariff,
            subscription=subscription,
            status=Payment.Status.CONFIRMED,
            verified_at=verified_at,
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

        with pytest.raises(BusinessLogicError) as exc_info:
            correct_trainer_earning(
                club_id=club.id,
                earning_id=earning.id,
                target_trainer_id=target_trainer.id,
                reason="Move payout",
                actor_user_id=owner_user.id,
                idempotency_key="club-local-closed-correction",
            )

        assert exc_info.value.code == "payroll_period_closed"

    def test_idempotent_existing_correction_still_returns_after_close(self, club, owner_user):
        today = timezone.localdate()
        earning = self._create_earning(club=club, target_date=today)
        target_trainer = TrainerFactory(club=club)
        first = correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=target_trainer.id,
            reason="Move payout",
            actor_user_id=owner_user.id,
            idempotency_key="existing-before-close",
        )
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=today.replace(day=1),
            period_end=today,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        second = correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=target_trainer.id,
            reason="Move payout",
            actor_user_id=owner_user.id,
            idempotency_key="existing-before-close",
        )

        assert second.created is False
        assert second.debit.id == first.debit.id
        assert second.credit.id == first.credit.id



@pytest.mark.django_db
class TestTrainingGroupResponsibleTrainerGuard:
    def test_reassign_then_deactivate_requires_no_active_group_owner(self, club):
        from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
        from apps.attendance.services.training_groups import reassign_training_group_responsibility
        from apps.trainers.services import update_trainer

        rollout_state = TrainingGroupRolloutState.objects.for_club(club).get()
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.filter(id=rollout_state.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )
        responsible_trainer = TrainerFactory(club=club)
        replacement_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        group = TrainingGroup.objects.create(
            club=club,
            name="Owned group",
            training_type=training_type,
            location=location,
            responsible_trainer=responsible_trainer,
            status=TrainingGroup.Status.ACTIVE,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            update_trainer(
                trainer_id=responsible_trainer.id,
                club_id=club.id,
                is_active=False,
            )

        assert exc_info.value.code == "trainer_responsible_training_group_active"
        reassign_training_group_responsibility(
            club_id=club.id,
            training_group_id=group.id,
            responsible_trainer_id=replacement_trainer.id,
        )
        deactivated = update_trainer(
            trainer_id=responsible_trainer.id,
            club_id=club.id,
            is_active=False,
        )
        group.refresh_from_db()

        assert deactivated.is_active is False
        assert group.responsible_trainer_id == replacement_trainer.id

    @pytest.mark.django_db(transaction=True)
    def test_postgresql_group_create_race_with_trainer_deactivation_preserves_invariant(
        self,
        club,
        settings,
    ):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import close_old_connections, connection

        from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
        from apps.attendance.services.training_groups import create_training_group
        from apps.trainers.services import update_trainer

        if connection.vendor != "postgresql":
            pytest.skip("requires PostgreSQL row-lock semantics; run in the PostgreSQL CI suite")

        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        rollout_state = TrainingGroupRolloutState.objects.for_club(club).get()
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.filter(id=rollout_state.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )
        trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        gate = Barrier(2)

        def create_group() -> str:
            close_old_connections()
            try:
                gate.wait(timeout=10)
                create_training_group(
                    club_id=club.id,
                    name="Concurrent trainer group",
                    training_type_id=training_type.id,
                    location_id=location.id,
                    responsible_trainer_id=trainer.id,
                    actor_user_id=None,
                )
                return "created"
            except BusinessLogicError as exc:
                return exc.code
            finally:
                close_old_connections()

        def deactivate_trainer() -> str:
            close_old_connections()
            try:
                gate.wait(timeout=10)
                update_trainer(
                    trainer_id=trainer.id,
                    club_id=club.id,
                    is_active=False,
                )
                return "deactivated"
            except BusinessLogicError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            created = executor.submit(create_group)
            deactivated = executor.submit(deactivate_trainer)
            outcomes = {"group": created.result(), "trainer": deactivated.result()}

        trainer.refresh_from_db()
        has_active_group = TrainingGroup.objects.for_club(club).filter(
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        ).exists()
        assert not (has_active_group and not trainer.is_active)
        if outcomes["group"] == "created":
            assert outcomes["trainer"] == "trainer_responsible_training_group_active"
            assert trainer.is_active is True
        else:
            assert outcomes == {
                "group": "training_group_responsible_trainer_invalid",
                "trainer": "deactivated",
            }
            assert trainer.is_active is False
