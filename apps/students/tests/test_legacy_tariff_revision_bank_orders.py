import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.billing.models import (
    BankPaymentOrder,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.services import process_bank_payment_webhook
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import ClubSettingsFactory
from apps.common.tests.helpers import make_auth_params
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _legacy_case(*, club, owner_user, student):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    source_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="Legacy source",
        price=Decimal("8000"),
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    source_component = TariffComponentFactory(
        club=club,
        tariff=source_tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_tariff.price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=source_tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=7,
        trainings_used=1,
        expires_at=timezone.now() + timedelta(days=7),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=source_component,
        credits_left=7,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Legacy target",
        actor_user_id=owner_user.id,
        idempotency_key=f"legacy-a-b-{student.id}",
    )
    return source_tariff, source, revision


def _legacy_payload(*, source_tariff, revision, key=None):
    payload = {
        "tariff_id": source_tariff.id,
        "expected_target_tariff_id": revision.target_tariff_id,
        "expected_target_price": "8500.00",
    }
    if key is not None:
        payload["idempotency_key"] = key
    return payload


def _legacy_endpoint(*, caller, student):
    if caller == "student":
        return "/students/me/bank-payment-orders/"
    return f"/parents/children/{student.id}/bank-payment-orders/"


def _legacy_auth(*, caller, club, student, student_user, parent_user):
    if caller == "student":
        return make_auth_params(student_user, club, role="student")
    return make_auth_params(parent_user, club, role="parent")


@pytest.mark.django_db
@pytest.mark.parametrize("caller", ["student", "parent"])
def test_legacy_self_service_source_tariff_reaches_current_revision(
    settings,
    club,
    owner_user,
    student_user,
    parent_user,
    caller,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettingsFactory(club=club, unified_client_journey_enabled=False)
    student = StudentFactory(
        club=club,
        user=student_user if caller == "student" else None,
        is_child=caller == "parent",
        parent_user=parent_user if caller == "parent" else None,
    )
    source_tariff, source, revision = _legacy_case(
        club=club,
        owner_user=owner_user,
        student=student,
    )

    # The explicit source-subscription contract remains gated while the
    # legacy source-tariff contract continues to work in this capability-off
    # tenant.
    explicit = client.post(
        _legacy_endpoint(caller=caller, student=student),
        json={
            "renewed_from_subscription_id": source.id,
            "idempotency_key": f"legacy-explicit-disabled-{caller}",
        },
        **_legacy_auth(
            caller=caller,
            club=club,
            student=student,
            student_user=student_user,
            parent_user=parent_user,
        ),
    )
    assert explicit.status_code == 404

    with patch("django_q.tasks.async_task"):
        response = client.post(
            _legacy_endpoint(caller=caller, student=student),
            json=_legacy_payload(
                source_tariff=source_tariff,
                revision=revision,
                key=f"legacy-create-{caller}",
            ),
            **_legacy_auth(
                caller=caller,
                club=club,
                student=student,
                student_user=student_user,
                parent_user=parent_user,
            ),
        )

    assert response.status_code == 201, response.json()
    order = BankPaymentOrder.objects.for_club(club).get(id=response.json()["id"])
    order.payment.refresh_from_db()
    assert order.renewed_from_subscription_id == source.id
    assert order.payment.tariff_id == revision.target_tariff_id
    assert order.amount_snapshot == Decimal("8500.00")
    assert order.payment.renewal_source_tariff_name_snapshot == source_tariff.name


@pytest.mark.django_db
@pytest.mark.parametrize("caller", ["student", "parent"])
def test_legacy_self_service_rejects_stale_target_after_next_revision(
    settings,
    club,
    owner_user,
    student_user,
    parent_user,
    caller,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettingsFactory(club=club, unified_client_journey_enabled=False)
    student = StudentFactory(
        club=club,
        user=student_user if caller == "student" else None,
        is_child=caller == "parent",
        parent_user=parent_user if caller == "parent" else None,
    )
    source_tariff, source, revision = _legacy_case(
        club=club,
        owner_user=owner_user,
        student=student,
    )
    revise_tariff_price(
        club_id=club.id,
        source_tariff_id=revision.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Legacy latest",
        actor_user_id=owner_user.id,
        idempotency_key=f"legacy-b-c-{student.id}",
    )

    response = client.post(
        _legacy_endpoint(caller=caller, student=student),
        json=_legacy_payload(source_tariff=source_tariff, revision=revision),
        **_legacy_auth(
            caller=caller,
            club=club,
            student=student,
            student_user=student_user,
            parent_user=parent_user,
        ),
    )

    assert response.status_code == 400, response.json()
    assert response.json()["code"] == "renewal_offer_stale"
    assert not BankPaymentOrder.objects.for_club(club).exists()
    assert source.status == Subscription.Status.ACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize("caller", ["student", "parent"])
def test_legacy_unkeyed_replay_drains_accepted_intermediate_after_next_revision(
    settings,
    club,
    owner_user,
    student_user,
    parent_user,
    caller,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettingsFactory(club=club, unified_client_journey_enabled=False)
    student = StudentFactory(
        club=club,
        user=student_user if caller == "student" else None,
        is_child=caller == "parent",
        parent_user=parent_user if caller == "parent" else None,
    )
    source_tariff, source, revision = _legacy_case(
        club=club,
        owner_user=owner_user,
        student=student,
    )
    payload = _legacy_payload(source_tariff=source_tariff, revision=revision)
    endpoint = _legacy_endpoint(caller=caller, student=student)
    auth = _legacy_auth(
        caller=caller,
        club=club,
        student=student,
        student_user=student_user,
        parent_user=parent_user,
    )

    with patch("django_q.tasks.async_task"):
        first = client.post(endpoint, json=payload, **auth)
    assert first.status_code == 201, first.json()
    order = BankPaymentOrder.objects.for_club(club).get(id=first.json()["id"])
    body = json.dumps(
        {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"legacy-approved-{order.id}",
            "status": "APPROVED",
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": f"legacy-operation-{order.id}",
            "amount": str(order.amount_snapshot),
            "paid_at": timezone.now().isoformat(),
        }
    ).encode()
    with patch("django_q.tasks.async_task"):
        first_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id=f"legacy-approved-{order.id}",
        )
        duplicate_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id=f"legacy-approved-duplicate-{order.id}",
        )
    assert duplicate_event.id == first_event.id

    next_revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=revision.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Legacy latest",
        actor_user_id=owner_user.id,
        idempotency_key=f"legacy-b-c-replay-{student.id}",
    )
    assert next_revision.target_tariff_id != revision.target_tariff_id

    replay = client.post(endpoint, json=payload, **auth)
    assert replay.status_code == 200, replay.json()
    assert replay.json()["id"] == order.id
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    order.payment.refresh_from_db()
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.payment.tariff_id == revision.target_tariff_id
    assert order.payment.renewal_source_tariff_name_snapshot == source_tariff.name
    source.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("caller", ["student", "parent"])
def test_legacy_naturally_expired_source_reaches_revision_with_zero_carry(
    settings,
    club,
    owner_user,
    student_user,
    parent_user,
    caller,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettingsFactory(club=club, unified_client_journey_enabled=False)
    student = StudentFactory(
        club=club,
        user=student_user if caller == "student" else None,
        is_child=caller == "parent",
        parent_user=parent_user if caller == "parent" else None,
    )
    source_tariff, source, revision = _legacy_case(
        club=club,
        owner_user=owner_user,
        student=student,
    )
    source_component = SubscriptionComponent.objects.for_club(club).get(subscription=source)
    source_payment = PaymentFactory(
        club=club,
        student=student,
        tariff=source_tariff,
        subscription=source,
        amount=Decimal("8000.00"),
        status=Payment.Status.CONFIRMED,
    )
    source_paid_amount = source.paid_amount
    source_trainings_used = source.trainings_used
    source_component_remaining = 3
    source.status = Subscription.Status.EXPIRED
    source.expires_at = timezone.now() - timedelta(days=1)
    source.trainings_left = source_component_remaining
    source.save(update_fields=["status", "expires_at", "trainings_left", "updated_at"])
    source_component.credits_left = source_component_remaining
    source_component.save(update_fields=["credits_left", "updated_at"])

    response = client.post(
        _legacy_endpoint(caller=caller, student=student),
        json=_legacy_payload(source_tariff=source_tariff, revision=revision),
        **_legacy_auth(
            caller=caller,
            club=club,
            student=student,
            student_user=student_user,
            parent_user=parent_user,
        ),
    )

    assert response.status_code == 201, response.json()
    order = BankPaymentOrder.objects.for_club(club).get(id=response.json()["id"])
    order.payment.refresh_from_db()
    assert order.renewed_from_subscription_id == source.id
    assert order.payment.tariff_id == revision.target_tariff_id
    assert order.amount_snapshot == Decimal("8500.00")

    paid_at = timezone.now() - timedelta(minutes=1)
    body = json.dumps(
        {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"legacy-expired-approved-{order.id}",
            "status": "APPROVED",
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": f"legacy-expired-operation-{order.id}",
            "amount": str(order.amount_snapshot),
            "paid_at": paid_at.isoformat(),
        }
    ).encode()
    with patch("django_q.tasks.async_task"):
        first_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id=f"legacy-expired-approved-{order.id}",
        )
        duplicate_event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=body,
            headers={},
            request_id=f"legacy-expired-duplicate-{order.id}",
        )
    assert duplicate_event.id == first_event.id

    order.refresh_from_db()
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    target_component = SubscriptionComponent.objects.for_club(club).get(subscription=order.subscription)
    source.refresh_from_db()
    source_component.refresh_from_db()
    source_payment.refresh_from_db()
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.subscription.trainings_left == revision.target_tariff.trainings_limit == 8
    assert order.subscription.expires_at == order.payment.verified_at + timedelta(
        days=revision.target_tariff.duration_days
    )
    assert target_component.credits_left == 8
    assert source.status == Subscription.Status.EXPIRED
    assert source.trainings_left == source_component_remaining
    assert source.trainings_used == source_trainings_used
    assert source_component.credits_left == source_component_remaining
    assert source_payment.amount == Decimal("8000.00")
    assert source_payment.tariff_id == source_tariff.id
    assert source_paid_amount == source.paid_amount
