from datetime import timedelta
from decimal import Decimal

import pytest

from apps.clubs.timezones import club_localdate
from apps.trainers.models import TrainerSettlementEntry
from apps.trainers.settlement_selectors import trainer_period_presets
from apps.trainers.settlement_services import record_trainer_settlement
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer_card(club, owner_user, client, settings):
    settings.TRAINER_SETTLEMENTS_ENABLED = True
    trainer = TrainerFactory(club=club)
    client.force_login(owner_user)
    return trainer


def test_card_explicit_zero_opening_payout_advance_and_reversal(client, trainer_card):
    trainer = trainer_card
    url = f"/dashboard/trainers/{trainer.id}/settlements/"
    page = client.get(f"/dashboard/trainers/{trainer.id}/")
    assert "Расчёты до даты перехода не сверены" in page.content.decode()
    form = client.get(url, {"kind": "opening"})
    data = dict(
        kind="opening",
        source_key=form.context["source_key"],
        effective_on=str(club_localdate(trainer.club)),
        value="0",
        reason="Сверено",
        expected_fingerprint=form.context["summary"]["fingerprint"],
    )
    assert "записана" in client.post(url, data).content.decode()
    assert "записана" in client.post(url, data).content.decode()
    form = client.get(url)
    data = dict(
        kind="payout",
        source_key=form.context["source_key"],
        effective_on=str(club_localdate(trainer.club)),
        value="300",
        reason="Выдан аванс",
        payment_method="cash",
        expected_fingerprint=form.context["summary"]["fingerprint"],
    )
    rejected = client.post(url, data)
    assert "Подтвердите" in rejected.context["error"] and rejected.context["value"] == "300"
    data["confirm_advance"] = "1"
    assert "записана" in client.post(url, data).content.decode()
    payout = TrainerSettlementEntry.objects.get(trainer=trainer, kind="payout")
    form = client.get(url, {"kind": "payout_reversal", "reversal_of_id": payout.id})
    result = client.post(
        url,
        dict(
            kind="payout_reversal",
            reversal_of_id=payout.id,
            source_key=form.context["source_key"],
            effective_on=str(club_localdate(trainer.club)),
            reason="Запись ошибочна",
            expected_fingerprint=form.context["summary"]["fingerprint"],
        ),
    )
    assert "записана" in result.content.decode()
    page = client.get(f"/dashboard/trainers/{trainer.id}/")
    assert page.context["settlement"]["balance"] == 0
    assert page.context["settlement"]["earned"] == 0


def test_presets_are_explicit_monday_sunday_and_shared_earned(client, trainer_card):
    trainer = trainer_card
    presets = trainer_period_presets(club=trainer.club)
    for preset in presets:
        page = client.get(f"/dashboard/trainers/{trainer.id}/", {"period": preset["key"]})
        assert page.status_code == 200
        assert (page.context["date_from"], page.context["date_to"]) == (preset["start"], preset["end"])
        assert page.context["summary"]["total_amount"] == page.context["settlement"]["earned"]
    two = next(p for p in presets if p["key"] == "two_weeks")
    assert two["start"].weekday() == 0 and two["end"].weekday() == 6
    assert (two["end"] - two["start"]).days == 13


def test_trainer_write_and_foreign_card_denied(client, trainer_card, trainer_user, other_club):
    foreign = TrainerFactory(club=other_club)
    assert client.get(f"/dashboard/trainers/{foreign.id}/settlements/").status_code == 404
    client.force_login(trainer_user)
    assert client.get(f"/dashboard/trainers/{trainer_card.id}/settlements/").status_code == 403


def test_api_settlement_read_is_self_scoped(trainer_card, trainer_user, owner_user, bypass_jwt_auth):
    from ninja.testing import TestClient

    from apps.common.tests.helpers import make_auth_params
    from config.api import api

    trainer = trainer_card
    trainer.user = trainer_user
    trainer.save()
    client = TestClient(api)
    own = client.get(
        f"/trainers/{trainer.id}/settlements/summary/", **make_auth_params(trainer_user, trainer.club, role="trainer")
    )
    assert own.status_code == 200 and own.json()["balance"] is None
    other = TrainerFactory(club=trainer.club)
    denied = client.get(
        f"/trainers/{other.id}/settlements/summary/", **make_auth_params(trainer_user, trainer.club, role="trainer")
    )
    assert denied.status_code == 403


def test_stale_payout_preserves_input_and_requires_resubmission(client, trainer_card, owner_user):
    trainer = trainer_card
    day = club_localdate(trainer.club)
    record_trainer_settlement(
        club_id=trainer.club_id,
        actor_user_id=owner_user.id,
        trainer_id=trainer.id,
        kind="opening",
        effective_on=day - timedelta(days=1),
        balance_delta=Decimal("1000"),
        reason="Сверено",
        source_namespace="test",
        source_key="opening",
    )
    url = f"/dashboard/trainers/{trainer.id}/settlements/"
    form = client.get(url)
    command = dict(
        club_id=trainer.club_id,
        actor_user_id=owner_user.id,
        trainer_id=trainer.id,
        kind="payout",
        effective_on=day,
        amount=Decimal("100"),
        payment_method="cash",
        reason="Выдано",
        source_namespace="test",
        source_key="another",
    )
    record_trainer_settlement(**command)
    data = dict(
        kind="payout",
        effective_on=str(day),
        value="200",
        payment_method="cash",
        reason="По ведомости",
        source_key=form.context["source_key"],
        expected_fingerprint=form.context["summary"]["fingerprint"],
    )
    result = client.post(url, data)
    assert "Данные изменились" in result.context["error"] and result.context["value"] == "200"
    data["expected_fingerprint"] = result.context["summary"]["fingerprint"]
    assert "записана" in client.post(url, data).content.decode()


def test_settlement_browser_fixture_has_expected_sources(settings):
    from apps.common.management.commands.prepare_trainer_settlements_e2e import Command
    from apps.trainers.models import TrainerEarning

    settings.TRAINER_SETTLEMENTS_ENABLED = True
    fixture = Command().create_fixture()
    assert TrainerEarning.objects.filter(trainer_id=fixture["trainer_id"]).count() == 2
    assert TrainerSettlementEntry.objects.filter(trainer_id=fixture["trainer_id"]).count() == 0
    assert TrainerSettlementEntry.objects.filter(trainer_id=fixture["second_trainer_id"]).get().balance_delta == 0
