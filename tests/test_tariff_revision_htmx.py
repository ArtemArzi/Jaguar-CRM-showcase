from decimal import Decimal

import pytest

from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.billing.tests.factories import TariffComponentFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubMembership


def _finite_group_tariff(*, club, name="Group 8", price=Decimal("5000")):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
        drop_in_price=None,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name=name,
        price=price,
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        name=name,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    return tariff


@pytest.mark.django_db
def test_price_revision_htmx_hides_source_and_exposes_archive(
    client,
    owner_user,
    club,
):
    source = _finite_group_tariff(club=club, name="Group current")
    client.force_login(owner_user)

    response = client.post(
        f"/dashboard/settings/billing/tariffs/{source.id}/price-revision/",
        {
            "new_name": "Group refreshed",
            "new_price": "5500",
            "idempotency_key": "htmx-revision-1",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 204
    target = Tariff.objects.for_club(club).get(name="Group refreshed")
    assert target.is_active is True
    source.refresh_from_db()
    assert source.is_active is False

    current = client.get("/dashboard/settings/billing/")
    assert current.status_code == 200
    current_content = current.content.decode()
    assert "Group refreshed" in current_content
    assert "Group current" not in current_content

    archive = client.get("/dashboard/settings/billing/tariffs/archive/")
    assert archive.status_code == 200
    archive_content = archive.content.decode()
    assert "Group current" in archive_content
    assert "5000" in archive_content


@pytest.mark.django_db
def test_price_revision_htmx_replay_works_after_source_archived_but_get_stays_hidden(
    client,
    owner_user,
    club,
):
    source = _finite_group_tariff(club=club, name="Replay source")
    client.force_login(owner_user)
    payload = {
        "new_name": "Replay target",
        "new_price": "5500",
        "idempotency_key": "htmx-revision-replay-archived",
    }
    url = f"/dashboard/settings/billing/tariffs/{source.id}/price-revision/"

    first = client.post(url, payload)
    assert first.status_code == 204
    replay = client.post(url, payload)
    assert replay.status_code == 204
    assert Tariff.objects.for_club(club).filter(name="Replay target").count() == 1
    assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_price_revision_htmx_replay_requires_current_management_membership(
    client,
    owner_user,
    club,
):
    source = _finite_group_tariff(club=club, name="Revoked source")
    client.force_login(owner_user)
    payload = {
        "new_name": "Revoked target",
        "new_price": "5500",
        "idempotency_key": "htmx-revision-revoked-replay",
    }
    url = f"/dashboard/settings/billing/tariffs/{source.id}/price-revision/"
    assert client.post(url, payload).status_code == 204

    ClubMembership.objects.filter(club=club, user=owner_user).update(is_active=False)
    denied = client.post(url, payload)
    assert denied.status_code == 302
    assert denied.url.endswith("/dashboard/login/")


@pytest.mark.django_db
def test_price_revision_htmx_renders_bounded_money_error_without_mutation(
    client,
    owner_user,
    club,
):
    source = _finite_group_tariff(club=club, name="Oversize source")
    client.force_login(owner_user)
    response = client.post(
        f"/dashboard/settings/billing/tariffs/{source.id}/price-revision/",
        {
            "new_name": "Oversize target",
            "new_price": "100000000.00",
            "idempotency_key": "htmx-revision-oversize",
        },
    )

    assert response.status_code == 200
    assert "Цена превышает допустимый предел." in response.content.decode()
    assert Tariff.objects.for_club(club).filter(name="Oversize target").count() == 0
    source.refresh_from_db()
    assert source.is_active is True


@pytest.mark.django_db
def test_price_revision_htmx_requires_management_and_tenant_scope(
    client,
    owner_user,
    trainer_user,
    club,
    other_club,
):
    local_source = _finite_group_tariff(club=club, name="Local source")
    foreign_source = _finite_group_tariff(club=other_club, name="Foreign source")

    client.force_login(trainer_user)
    denied = client.get(
        f"/dashboard/settings/billing/tariffs/{local_source.id}/price-revision/",
    )
    assert denied.status_code == 403

    client.force_login(owner_user)
    foreign = client.get(
        f"/dashboard/settings/billing/tariffs/{foreign_source.id}/price-revision/",
    )
    assert foreign.status_code == 404


@pytest.mark.django_db
def test_ordinary_edit_explains_sealed_price_action(
    client,
    owner_user,
    club,
):
    source = _finite_group_tariff(club=club, name="Group source")
    client.force_login(owner_user)
    created = client.post(
        f"/dashboard/settings/billing/tariffs/{source.id}/price-revision/",
        {
            "new_name": "Group target",
            "new_price": "5500",
            "idempotency_key": "htmx-revision-sealed-edit",
        },
    )
    assert created.status_code == 204
    target = Tariff.objects.for_club(club).get(name="Group target")

    edited = client.post(
        f"/dashboard/settings/billing/tariffs/{target.id}/form/",
        {
            "name": target.name,
            "training_type_id": str(target.training_type_id),
            "price": "6000",
            "trainings_limit": str(target.trainings_limit),
            "duration_days": str(target.duration_days),
            "description": target.description,
            "trainer_payout_policy": target.trainer_payout_policy,
        },
    )

    assert edited.status_code == 200
    assert "Изменить цену" in edited.content.decode()
    target.refresh_from_db()
    assert target.price == Decimal("5500.00")
