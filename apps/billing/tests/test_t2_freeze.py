"""T2 — Freeze rules: min_trainings_to_freeze + auto_unfreeze cron."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing.models import Subscription, SubscriptionFreeze
from apps.billing.services import (
    freeze_subscription,
    get_or_create_club_settings,
    update_club_settings,
)
from apps.billing.tasks import auto_unfreeze_expired
from apps.billing.tests.factories import (
    SubscriptionFactory,
    SubscriptionFreezeFactory,
    TariffFactory,
)
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory


@pytest.fixture
def setup_freeze_env(db, club):
    user = UserFactory()
    student = StudentFactory(club=club)
    tariff = TariffFactory(club=club, trainings_limit=8)
    return {"user": user, "student": student, "tariff": tariff}


@pytest.mark.django_db
class TestMinTrainingsToFreeze:
    def test_default_min_is_two(self, club):
        s = get_or_create_club_settings(club.id)
        assert s.min_trainings_to_freeze == 2

    def test_freeze_blocked_when_below_min(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=1,
            expires_at=timezone.now() + timedelta(days=30),
        )
        with pytest.raises(BusinessLogicError) as ei:
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=7,
                reason="vacation",
                frozen_by_id=env["user"].id,
            )
        assert ei.value.code == "freeze_below_min_trainings"

    def test_freeze_allowed_at_min(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
            expires_at=timezone.now() + timedelta(days=30),
        )
        freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=5,
            reason="vacation",
            frozen_by_id=env["user"].id,
        )
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.FROZEN

    def test_freeze_allowed_for_unlimited(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=None,
            expires_at=timezone.now() + timedelta(days=30),
        )
        freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=5,
            reason="vacation",
            frozen_by_id=env["user"].id,
        )
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.FROZEN

    def test_min_trainings_in_settings_whitelist(self, club):
        update_club_settings(club_id=club.id, min_trainings_to_freeze=5)
        s = get_or_create_club_settings(club.id)
        assert s.min_trainings_to_freeze == 5

    def test_custom_min_blocks_freeze(self, club, setup_freeze_env):
        env = setup_freeze_env
        update_club_settings(club_id=club.id, min_trainings_to_freeze=4)
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=3,
            expires_at=timezone.now() + timedelta(days=30),
        )
        with pytest.raises(BusinessLogicError):
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=7,
                reason="vacation",
                frozen_by_id=env["user"].id,
            )


@pytest.mark.django_db
class TestAutoUnfreeze:
    def test_unfreezes_when_period_ended(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=timezone.now() + timedelta(days=30),
        )
        SubscriptionFreezeFactory(
            club=club,
            subscription=sub,
            days=7,
            frozen_by=env["user"],
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=None,
        )
        result = auto_unfreeze_expired()
        assert result["unfrozen"] == 1
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.ACTIVE

    def test_unfreeze_task_keeps_date_expired_subscription_expired(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=timezone.now() - timedelta(days=1),
        )
        SubscriptionFreezeFactory(
            club=club,
            subscription=sub,
            days=7,
            frozen_by=env["user"],
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=None,
        )

        result = auto_unfreeze_expired()

        assert result["unfrozen"] == 1
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.EXPIRED

    def test_skips_still_active_freeze(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=timezone.now() + timedelta(days=30),
        )
        SubscriptionFreezeFactory(
            club=club,
            subscription=sub,
            days=14,
            frozen_by=env["user"],
            starts_at=timezone.now() - timedelta(days=2),
            ends_at=None,
        )
        result = auto_unfreeze_expired()
        assert result["unfrozen"] == 0
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.FROZEN

    def test_skips_pending_freeze_requests(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.ACTIVE,
            trainings_left=4,
            expires_at=timezone.now() + timedelta(days=30),
        )
        freeze = SubscriptionFreezeFactory(
            club=club,
            subscription=sub,
            days=3,
            frozen_by=env["user"],
            status=SubscriptionFreeze.FreezeStatus.PENDING,
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=None,
        )

        result = auto_unfreeze_expired()

        assert result == {"unfrozen": 0, "skipped": 0}
        freeze.refresh_from_db()
        sub.refresh_from_db()
        assert freeze.ends_at is None
        assert sub.status == Subscription.Status.ACTIVE

    def test_idempotent(self, club, setup_freeze_env):
        env = setup_freeze_env
        sub = SubscriptionFactory(
            club=club,
            student=env["student"],
            tariff=env["tariff"],
            status=Subscription.Status.FROZEN,
            trainings_left=4,
            expires_at=timezone.now() + timedelta(days=30),
        )
        SubscriptionFreezeFactory(
            club=club,
            subscription=sub,
            days=3,
            frozen_by=env["user"],
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=None,
        )
        r1 = auto_unfreeze_expired()
        r2 = auto_unfreeze_expired()
        assert r1["unfrozen"] == 1
        assert r2["unfrozen"] == 0
