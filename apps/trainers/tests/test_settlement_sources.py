from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing.tests.test_opening_issuer import apply, command, group_command
from apps.billing.tests.test_refunds import _refund_case, _sale_earning
from apps.clubs.timezones import club_localdate
from apps.trainers.models import TrainerSettlementReconciliation
from apps.trainers.settlement_services import record_trainer_settlement
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db
# Explicit pytest fixture registration for real opening service integration.
command = command
group_command = group_command


def test_real_opening_commission_after_baseline_requires_review_once(club, group_command, settings):
    actor, terms, _, seller, _ = group_command
    settings.TRAINER_SETTLEMENTS_ENABLED = True
    record_trainer_settlement(
        club_id=club.id,
        actor_user_id=actor.id,
        trainer_id=seller.id,
        kind="opening",
        effective_on=club_localdate(club),
        balance_delta=Decimal("1000"),
        reason="Сверено до переноса",
        source_namespace="test",
        source_key="baseline",
    )
    receipt = apply(club, actor, terms)
    case = TrainerSettlementReconciliation.objects.get(trainer=seller)
    assert case.suggested_delta == Decimal("1300") and case.evidence["payment_id"] == receipt.payment_id
    from apps.billing.service_modules.opening_subscriptions import issue_opening_entitlement

    assert (
        issue_opening_entitlement(
            club_id=club.id, actor_user_id=actor.id, terms=terms, preview_fingerprint="replay", channel="test"
        ).id
        == receipt.id
    )
    assert TrainerSettlementReconciliation.objects.count() == 1


def test_real_refund_before_baseline_creates_resolution_case(club, owner_user, settings):
    from apps.billing.refund_services import approve_payment_refund_case

    settings.TRAINER_SETTLEMENTS_ENABLED = True
    now = timezone.now()
    order, refund_case = _refund_case(
        club=club, owner_user=owner_user, kind="partial", received_at=now - timedelta(days=3)
    )
    trainer = TrainerFactory(club=club)
    _sale_earning(club=club, payment=order.payment, trainer=trainer, amount=Decimal("1000"))
    record_trainer_settlement(
        club_id=club.id,
        actor_user_id=owner_user.id,
        trainer_id=trainer.id,
        kind="opening",
        effective_on=club_localdate(club),
        balance_delta=Decimal("1000"),
        reason="Сверено",
        source_namespace="test",
        source_key="baseline",
    )
    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=refund_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="refund",
        amount=Decimal("1000"),
        refund_kind="partial",
        reason="Подтверждён возврат",
    )
    case = TrainerSettlementReconciliation.objects.get(trainer=trainer)
    assert case.suggested_delta == Decimal("-200")
    assert case.effective_on == refund.accounting_date
