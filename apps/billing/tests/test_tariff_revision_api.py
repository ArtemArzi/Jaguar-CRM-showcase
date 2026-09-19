from decimal import Decimal
from unittest.mock import patch

import pytest
from ninja.testing import TestClient

from apps.billing.models import Subscription, TariffComponent, TrainingType
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, ClubSettingsFactory, UserFactory
from apps.common.tests.helpers import make_auth_params
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
def test_revised_manual_replay_rechecks_live_role_and_tenant_scope(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    source_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="Source package",
        price=Decimal("8000"),
        trainings_limit=8,
    )
    source_component = TariffComponentFactory(
        club=club,
        tariff=source_tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_tariff.price,
    )
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=source_tariff,
        status=Subscription.Status.ACTIVE,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Current package",
        actor_user_id=owner_user.id,
        idempotency_key="api-replay-a-to-b",
    )
    payload = {
        "student_id": student.id,
        "renewed_from_subscription_id": source.id,
        "payment_method": "cash",
        "idempotency_key": "api-replay-renewal",
        "expected_target_tariff_id": revision.target_tariff_id,
        "expected_target_price": "8500.00",
    }
    auth = make_auth_params(owner_user, club)
    with patch("django_q.tasks.async_task"):
        created = client.post("/billing/payments/renewals/", json=payload, **auth)
    assert created.status_code == 201, created.json()

    ClubMembership.objects.filter(user=owner_user, club=club).update(is_active=False)
    revoked = client.post("/billing/payments/renewals/", json=payload, **auth)
    assert revoked.status_code == 403, revoked.json()

    other_club = ClubFactory(name="Other renewal tenant")
    other_owner = UserFactory()
    ClubMembershipFactory(user=other_owner, club=other_club, role=ClubMembership.Role.OWNER)
    ClubSettingsFactory(club=other_club, unified_client_journey_enabled=True)
    foreign = client.post(
        "/billing/payments/renewals/",
        json=payload,
        **make_auth_params(other_owner, other_club),
    )
    assert foreign.status_code == 400, foreign.json()
    assert foreign.json()["code"] == "renewal_source_not_found"


@pytest.mark.django_db
def test_generic_payment_api_resolves_exact_source_before_current_tariff_lookup(
    club,
    owner_user,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    source_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="Source package",
        price=Decimal("8000"),
        trainings_limit=8,
    )
    source_component = TariffComponentFactory(
        club=club,
        tariff=source_tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_tariff.price,
    )
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=source_tariff,
        status=Subscription.Status.ACTIVE,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Current package",
        actor_user_id=owner_user.id,
        idempotency_key="api-generic-a-b",
    )
    auth = make_auth_params(owner_user, club)

    missing_offer = client.post(
        "/billing/payments/",
        json={
            "student_id": student.id,
            "tariff_id": source_tariff.id,
            "renewed_from_subscription_id": source.id,
            "payment_method": "cash",
            "idempotency_key": "api-generic-missing-offer",
        },
        **auth,
    )
    assert missing_offer.status_code == 400, missing_offer.json()
    assert missing_offer.json()["code"] == "renewal_offer_required"

    with patch("django_q.tasks.async_task"):
        created = client.post(
            "/billing/payments/",
            json={
                "student_id": student.id,
                "tariff_id": revision.target_tariff_id,
                "renewed_from_subscription_id": source.id,
                "payment_method": "cash",
                "expected_target_tariff_id": revision.target_tariff_id,
                "expected_target_price": "8500.00",
                "idempotency_key": "api-generic-target-offer",
            },
            **auth,
        )
    assert created.status_code == 201, created.json()
    assert created.json()["tariff"]["id"] == revision.target_tariff_id
    assert created.json()["amount"] == "8500.00"

    ClubMembership.objects.filter(user=owner_user, club=club).update(is_active=False)
    replay_after_revocation = client.post(
        "/billing/payments/",
        json={
            "student_id": student.id,
            "tariff_id": revision.target_tariff_id,
            "renewed_from_subscription_id": source.id,
            "payment_method": "cash",
            "expected_target_tariff_id": revision.target_tariff_id,
            "expected_target_price": "8500.00",
            "idempotency_key": "api-generic-target-offer",
        },
        **auth,
    )
    assert replay_after_revocation.status_code == 403, replay_after_revocation.json()
