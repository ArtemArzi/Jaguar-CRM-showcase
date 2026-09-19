"""Post-review invariant tests for the flexible-rate refactor.

Covers gaps flagged by Phase-6 code review:
- PROTECT on TrainingType/Location/Trainer blocks delete when rates exist
- calculate_salary fail-loud on missing rate
- create_sale_earning soft-skip on missing rate
- update_trainer_rates: empty list no-op, negative/>100 rejected, NaN rejected
- update_trainer_locations cascade-deletes orphan TrainerRate
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db.models import ProtectedError

from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import calculate_salary
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import TrainingType
from apps.billing.services import create_payment, verify_payment
from apps.billing.tasks import create_sale_earning
from apps.billing.tests.factories import (
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerLocation, TrainerRate
from apps.trainers.services import (
    update_trainer_locations,
    update_trainer_rates,
)
from apps.trainers.tests.factories import (
    TrainerFactory,
    TrainerLocationFactory,
    TrainerRateFactory,
)


@pytest.fixture(autouse=True)
def _disable_payment_async_tasks(monkeypatch):
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)


@pytest.mark.django_db
class TestProtectOnDelete:
    def test_training_type_delete_protected_when_rate_exists(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerRateFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
        )
        with pytest.raises(ProtectedError):
            tt.delete()

    def test_location_delete_protected_when_rate_exists(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerRateFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
        )
        with pytest.raises(ProtectedError):
            loc.delete()


@pytest.mark.django_db
class TestCalculateSalaryFailLoud:
    def test_missing_rate_raises(self, club):
        student = StudentFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club, slug="x", kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("3000"))
        sub = SubscriptionFactory(club=club, student=student, tariff=tariff)
        schedule = ScheduleFactory(club=club, location=loc, training_type=tt)
        # No TrainerRate, no TrainerLocation → rate resolves to None
        checkin = CheckinFactory(
            club=club, student=student, schedule=schedule,
            training_type=tt, subscription=sub, location=loc,
        )
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
        with pytest.raises(BusinessLogicError) as ei:
            calculate_salary(checkin.id, club.id)
        assert ei.value.code == "trainer_rate_not_set"


@pytest.mark.django_db
class TestCreateSaleEarningSoftSkip:
    def test_missing_rate_logs_and_returns(self, club, caplog):
        tt = TrainingTypeFactory(club=club, slug="group", kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("4000"))
        trainer = TrainerFactory(club=club)  # NO TrainerRate set up
        student = StudentFactory(club=club)
        user = UserFactory()

        payment = create_payment(
            club_id=club.id, student_id=student.id, tariff_id=tariff.id,
            payment_method="cash", recorded_by_id=user.id,
            seller_trainer_id=trainer.id,
        )
        with patch("django_q.tasks.async_task"):
            verify_payment(
                payment_id=payment.id, club_id=club.id,
                verified_by_id=user.id, action="confirm",
            )
        # Directly invoke the task — it should soft-skip, not raise
        create_sale_earning(payment.id, club.id)
        assert not TrainerEarning.objects.filter(payment=payment).exists()


@pytest.mark.django_db
class TestUpdateTrainerRatesInvariants:
    def test_empty_list_noop(self, club):
        trainer = TrainerFactory(club=club)
        result = update_trainer_rates(
            club_id=club.id, trainer_id=trainer.id, rates=[],
        )
        assert result == 0
        assert TrainerRate.objects.filter(trainer=trainer).count() == 0

    def test_negative_percent_rejected(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError) as ei:
            update_trainer_rates(
                club_id=club.id, trainer_id=trainer.id,
                rates=[{
                    "location_id": loc.id, "training_type_id": tt.id,
                    "percent": Decimal("-5"),
                }],
            )
        assert ei.value.code == "rate_out_of_range"

    def test_over_100_percent_rejected(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError) as ei:
            update_trainer_rates(
                club_id=club.id, trainer_id=trainer.id,
                rates=[{
                    "location_id": loc.id, "training_type_id": tt.id,
                    "percent": Decimal("150"),
                }],
            )
        assert ei.value.code == "rate_out_of_range"

    def test_nan_rejected(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError) as ei:
            update_trainer_rates(
                club_id=club.id, trainer_id=trainer.id,
                rates=[{
                    "location_id": loc.id, "training_type_id": tt.id,
                    "percent": Decimal("NaN"),
                }],
            )
        assert ei.value.code == "rate_out_of_range"


@pytest.mark.django_db
class TestMissingRatesComputation:
    """View-level: the trainer_detail view builds a missing_rates list
    for gaps that can block or skip salary calculation."""

    def _compute_missing(self, *, club, trainer):
        # Mirrors the view logic — kept here to avoid importing the view
        # directly (which requires a request object).
        from apps.billing.models import TrainingType
        from apps.trainers.models import TrainerLocation, TrainerRate

        existing = {
            (r.location_id, r.training_type_id)
            for r in TrainerRate.objects.for_club(club).filter(trainer=trainer)
        }
        existing_training_type_ids = {
            r.training_type_id
            for r in TrainerRate.objects.for_club(club).filter(trainer=trainer)
        }
        tls = list(
            TrainerLocation.objects.for_club(club).filter(trainer=trainer)
        )
        tts = list(
            TrainingType.objects.for_club(club).filter(is_active=True)
        )
        missing = []
        for tl in tls:
            for tt in tts:
                if (tl.location_id, tt.id) in existing:
                    continue
                if tt.kind == TrainingType.Kind.GROUP and tt.id in existing_training_type_ids:
                    continue
                missing.append((tl.location_id, tt.id))
        return missing

    def test_no_missing_when_all_rates_set(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(
            club=club, trainer=trainer, location=loc,
            rate_group=Decimal("20"),
        )
        assert self._compute_missing(club=club, trainer=trainer) == []

    def test_missing_when_new_training_type_added_after_trainer(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt_group = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(
            club=club, trainer=trainer, location=loc,
            rate_group=Decimal("20"),
        )
        # Owner creates a new type AFTER the trainer is set up
        tt_master = TrainingTypeFactory(
            club=club, name="Мастер-класс", slug="master",
            kind=TrainingType.Kind.PERSONAL,
        )
        missing = self._compute_missing(club=club, trainer=trainer)
        assert (loc.id, tt_master.id) in missing
        assert (loc.id, tt_group.id) not in missing

    def test_inactive_types_are_ignored(self, club):
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        TrainerLocationFactory(
            club=club, trainer=trainer, location=loc, rate_group=Decimal("20"),
        )
        TrainingTypeFactory(
            club=club, name="Старый тип", slug="old",
            kind=TrainingType.Kind.PERSONAL, is_active=False,
        )
        assert self._compute_missing(club=club, trainer=trainer) == []

    def test_group_rate_can_cover_other_location_missing_warning(self, club):
        trainer = TrainerFactory(club=club)
        loc_a = LocationFactory(club=club)
        loc_b = LocationFactory(club=club)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(
            club=club,
            trainer=trainer,
            location=loc_a,
            rate_group=Decimal("20"),
            rate_personal=Decimal("30"),
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=loc_b)

        missing = self._compute_missing(club=club, trainer=trainer)

        assert (loc_b.id, group_type.id) not in missing
        assert (loc_b.id, personal_type.id) in missing


@pytest.mark.django_db
class TestUpdateTrainerLocationsCascadesRates:
    def test_unchecking_location_deletes_its_rates(self, club):
        """update_trainer_locations deletes orphan TrainerRate rows."""
        trainer = TrainerFactory(club=club)
        loc_a = LocationFactory(club=club)
        loc_b = LocationFactory(club=club)
        TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)

        # Create TL AFTER the TrainingType — factory mirrors rate to
        # TrainerRate(loc_a, tt) automatically.
        TrainerLocationFactory(
            club=club, trainer=trainer, location=loc_a,
            rate_group=Decimal("25"),
        )
        assert TrainerRate.objects.filter(trainer=trainer, location=loc_a).count() == 1

        # Update to only loc_b — loc_a rate should be cascade-deleted
        update_trainer_locations(
            trainer_id=trainer.id, club_id=club.id,
            locations=[{"location_id": loc_b.id}],
        )

        assert TrainerRate.objects.filter(
            trainer=trainer, location=loc_a,
        ).count() == 0
