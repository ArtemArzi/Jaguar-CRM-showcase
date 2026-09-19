"""Tests for resolve_trainer_rate selector — the new flexible rate lookup."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.billing.models import TrainingType
from apps.billing.tests.factories import TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.trainers.selectors import resolve_trainer_rate
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory, TrainerRateFactory


@pytest.mark.django_db
class TestResolveTrainerRate:
    def test_exact_match_wins(self, club):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)

        TrainerRateFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
            percent=Decimal("42.50"),
        )

        rate = resolve_trainer_rate(
            club_id=club.id, trainer_id=trainer.id,
            location_id=loc.id, training_type_id=tt.id,
        )
        assert rate == Decimal("42.50")

    def test_missing_rate_returns_none_without_fallback(self, club):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        # No TrainerLocation, no TrainerRate → None
        rate = resolve_trainer_rate(
            club_id=club.id, trainer_id=trainer.id,
            location_id=loc.id, training_type_id=tt.id,
        )
        assert rate is None

    def test_fallback_any_location_finds_other_loc(self, club):
        loc_a = LocationFactory(club=club)
        loc_b = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)

        TrainerRateFactory(
            club=club, trainer=trainer, location=loc_a, training_type=tt,
            percent=Decimal("30"),
        )
        # Query against loc_b with fallback — should find loc_a rate
        rate = resolve_trainer_rate(
            club_id=club.id, trainer_id=trainer.id,
            location_id=loc_b.id, training_type_id=tt.id,
            fallback_any_location=True,
        )
        assert rate == Decimal("30")

    def test_fallback_without_rate_returns_none(self, club):
        trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        assert resolve_trainer_rate(
            club_id=club.id, trainer_id=trainer.id,
            location_id=None, training_type_id=tt.id,
            fallback_any_location=True,
        ) is None

    def test_tenant_isolation(self, club, other_club):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)

        TrainerRateFactory(
            club=club, trainer=trainer, location=loc, training_type=tt,
            percent=Decimal("99"),
        )
        # Query from other_club: cross-tenant shouldn't see it
        rate = resolve_trainer_rate(
            club_id=other_club.id, trainer_id=trainer.id,
            location_id=loc.id, training_type_id=tt.id,
        )
        assert rate is None

    def test_factory_legacy_kwargs_create_trainer_rate(self, club):
        """TrainerLocationFactory(rate_personal=55) upserts a TrainerRate row."""
        trainer = TrainerFactory(club=club)
        loc = LocationFactory(club=club)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)

        TrainerLocationFactory(
            club=club, trainer=trainer, location=loc,
            rate_personal=Decimal("55.00"),
        )
        rate = resolve_trainer_rate(
            club_id=club.id, trainer_id=trainer.id,
            location_id=loc.id, training_type_id=tt.id,
        )
        assert rate == Decimal("55.00")
