import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.billing.models import (
    BankPaymentOrder,
    Payment,
    Subscription,
    SubscriptionComponent,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.bank_orders import create_bank_payment_order
from apps.billing.service_modules.payment_creation import create_payment
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.renewals import create_manual_subscription_renewal
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.services import process_bank_payment_webhook
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory


def _finite_tariff(*, club, name="Package", price=Decimal("8000")):
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.GROUP,
    )
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name=name,
        price=price,
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy="on_payment",
    )
    component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        name=name,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=price,
        trainer_payout_policy="on_payment",
        sort_order=0,
    )
    return tariff, component


def _active_source_subscription(*, club, student, tariff, tariff_component, credits_left=4):
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=credits_left,
        expires_at=timezone.now() + timedelta(days=7),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=tariff_component,
        credits_left=credits_left,
    )
    return source


@pytest.mark.django_db
def test_revised_manual_renewal_uses_target_price_and_carries_source_snapshot(
    club,
    owner_user,
):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    source_expiry = source.expires_at
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Package current",
        actor_user_id=owner_user.id,
        idempotency_key="renewal-revision-a-b",
    )

    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-a-b",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=Decimal("8500"),
        )
        verify_payment(
            club_id=club.id,
            payment_id=payment.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

    payment.refresh_from_db()
    source.refresh_from_db()
    child = payment.subscription
    child.refresh_from_db()
    child_component = child.components.get()
    assert payment.tariff_id == revision.target_tariff_id
    assert payment.amount == Decimal("8500.00")
    assert payment.renewal_source_tariff_name_snapshot == source_tariff.name
    assert child_component.credits_left == 12
    assert child.trainings_left == 12
    assert child.expires_at == source_expiry + timedelta(days=30)
    assert source.status == Subscription.Status.EXPIRED
    with patch("django_q.tasks.async_task"):
        replay = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-a-b",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=Decimal("8500.00"),
        )
    assert replay.id == payment.id


@pytest.mark.django_db
def test_revision_maps_equivalent_historical_inactive_component_for_carry(
    club,
    owner_user,
):
    student = StudentFactory(club=club)
    source_tariff, active_component = _finite_tariff(club=club)
    historical_component = TariffComponentFactory(
        club=club,
        tariff=source_tariff,
        name="Old package label",
        training_type=source_tariff.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_tariff.price,
        trainer_payout_policy="on_payment",
        sort_order=0,
        is_active=False,
    )
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=historical_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Package historical-safe",
        actor_user_id=owner_user.id,
        idempotency_key="renewal-historical-component",
    )
    assert str(historical_component.id) in revision.component_mapping
    assert str(active_component.id) in revision.component_mapping

    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-historical-source",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=revision.target_tariff.price,
        )
        verify_payment(
            club_id=club.id,
            payment_id=payment.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

    payment.subscription.refresh_from_db()
    assert payment.subscription.components.get().credits_left == 12


@pytest.mark.django_db
def test_accepted_intermediate_revision_finishes_after_next_revision(
    club,
    owner_user,
):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    first = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Package B",
        actor_user_id=owner_user.id,
        idempotency_key="renewal-chain-a-b",
    )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-chain-accepted-b",
            expected_target_tariff_id=first.target_tariff_id,
            expected_target_price=first.target_tariff.price,
        )
    second = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=first.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Package C",
        actor_user_id=owner_user.id,
        idempotency_key="renewal-chain-b-c",
    )

    with patch("django_q.tasks.async_task"):
        verify_payment(
            club_id=club.id,
            payment_id=payment.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

    payment.refresh_from_db()
    assert payment.tariff_id == first.target_tariff_id
    assert payment.tariff_id != second.target_tariff_id
    assert payment.subscription.components.get().credits_left == 12


@pytest.mark.django_db
def test_same_id_expected_offer_replay_keeps_legacy_fingerprint(club, owner_user):
    student = StudentFactory(club=club)
    tariff, component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=tariff,
        tariff_component=component,
    )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-same-id",
            expected_target_tariff_id=tariff.id,
            expected_target_price=tariff.price,
        )
        replay = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-same-id",
            expected_target_tariff_id=tariff.id,
            expected_target_price=tariff.price,
        )
    assert replay.id == payment.id


@pytest.mark.django_db
def test_revised_renewal_rejects_stale_expected_price(club, owner_user):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Package stale-check",
        actor_user_id=owner_user.id,
        idempotency_key="renewal-stale-price-revision",
    )

    with pytest.raises(BusinessLogicError) as error:
        create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            command_idempotency_key="renewal-stale-price",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=Decimal("8400"),
        )
    assert error.value.code == "renewal_offer_stale"


@pytest.mark.parametrize(
    "source_status,expires_delta",
    [
        (Subscription.Status.FROZEN, timedelta(days=7)),
        (Subscription.Status.PENDING, timedelta(days=7)),
        (Subscription.Status.CANCELLED, timedelta(days=7)),
        (Subscription.Status.EXPIRED, timedelta(days=7)),
    ],
)
@pytest.mark.django_db
def test_revised_renewal_keeps_source_eligibility_guards(
    club,
    owner_user,
    source_status,
    expires_delta,
):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    source.status = source_status
    source.expires_at = timezone.now() + expires_delta
    source.save(update_fields=["status", "expires_at", "updated_at"])
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Eligibility target",
        actor_user_id=owner_user.id,
        idempotency_key=f"eligibility-{source_status}",
    )

    with pytest.raises(BusinessLogicError) as error:
        create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key=f"eligibility-renewal-{source_status}",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=revision.target_tariff.price,
        )
    assert error.value.code == "renewal_source_closed"


@pytest.mark.django_db
def test_generic_renewal_uses_archived_source_and_returns_typed_offer_errors(club, owner_user):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Package current",
        actor_user_id=owner_user.id,
        idempotency_key="generic-source-archived",
    )

    # The source is archived, but the old generic command still names it. The
    # service must reach the exact offer validator instead of leaking a 404
    # from an active-tariff lookup.
    with pytest.raises(BusinessLogicError) as required:
        create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=source_tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            allow_renewal=True,
            renewed_from_subscription_id=source.id,
            renewal_source_tariff_id=source_tariff.id,
        )
    assert required.value.code == "renewal_offer_required"

    # A client holding the archived source's same-ID offer after the next
    # revision receives a typed stale-offer response, never Tariff.DoesNotExist.
    source_b = _active_source_subscription(
        club=club,
        student=StudentFactory(club=club),
        tariff=revision.target_tariff,
        tariff_component=revision.target_component,
    )
    next_revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=revision.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Package next",
        actor_user_id=owner_user.id,
        idempotency_key="generic-stale-after-next",
    )
    assert next_revision.target_tariff_id != source_b.tariff_id
    with pytest.raises(BusinessLogicError) as stale:
        create_payment(
            club_id=club.id,
            student_id=source_b.student_id,
            tariff_id=source_b.tariff_id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            allow_renewal=True,
            renewed_from_subscription_id=source_b.id,
            renewal_source_tariff_id=source_b.tariff_id,
            expected_target_tariff_id=source_b.tariff_id,
            expected_target_price=source_b.tariff.price,
        )
    assert stale.value.code == "renewal_offer_stale"


@pytest.mark.django_db
def test_unkeyed_manual_family_reuses_only_same_channel_and_context(club, owner_user):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Manual family target",
        actor_user_id=owner_user.id,
        idempotency_key="manual-family-revision",
    )
    request = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": revision.target_tariff_id,
        "payment_method": Payment.Method.CASH,
        "recorded_by_id": owner_user.id,
        "allow_renewal": True,
        "renewed_from_subscription_id": source.id,
        "renewal_source_tariff_id": source_tariff.id,
        "expected_target_tariff_id": revision.target_tariff_id,
        "expected_target_price": revision.target_tariff.price,
    }
    with patch("django_q.tasks.async_task"):
        first = create_payment(**request)
        replay = create_payment(**request)
    assert replay.id == first.id
    assert Payment.objects.for_club(club).filter(subscription__renewed_from=source).count() == 1


@pytest.mark.django_db
def test_manual_then_provider_never_attaches_bank_order_to_manual_payment(settings, club, owner_user):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Manual before provider",
        actor_user_id=owner_user.id,
        idempotency_key="manual-before-provider-revision",
    )
    with patch("django_q.tasks.async_task"):
        manual = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key="manual-before-provider",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=revision.target_tariff.price,
        )
        with pytest.raises(BusinessLogicError) as error:
            create_bank_payment_order(
                club_id=club.id,
                student_id=student.id,
                tariff_id=None,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner_user.id,
                renewed_from_subscription_id=source.id,
                expected_target_tariff_id=revision.target_tariff_id,
                expected_target_price=revision.target_tariff.price,
                command_idempotency_key="provider-after-manual",
            )
    assert error.value.code in {"renewal_pending_exists", "renewal_source_finalized_successor"}
    manual.refresh_from_db()
    assert manual.payment_method == Payment.Method.CASH
    assert manual.status == Payment.Status.PENDING
    assert not BankPaymentOrder.objects.for_club(club).exists()


def _bank_webhook_body(order: BankPaymentOrder, *, status: str) -> bytes:
    return json.dumps(
        {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"tariff-renewal-{order.id}-{status.lower()}",
            "status": status,
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": f"tariff-renewal-op-{order.id}",
            "amount": str(order.amount_snapshot),
            "paid_at": timezone.now().isoformat(),
        }
    ).encode()


@pytest.mark.django_db
def test_provider_reuses_keyed_and_unkeyed_accepted_intermediate_after_next_revision(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    first = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="Provider B",
        actor_user_id=owner_user.id,
        idempotency_key="provider-intermediate-a-b",
    )
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=None,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        renewed_from_subscription_id=source.id,
        expected_target_tariff_id=first.target_tariff_id,
        expected_target_price=first.target_tariff.price,
        command_idempotency_key="provider-intermediate-key",
    )
    paid_at = timezone.now()
    process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=_bank_webhook_body(order, status="APPROVED"),
        headers={},
        request_id="tariff-renewal-provider-approve",
    )
    second = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=first.target_tariff_id,
        new_price=Decimal("9000"),
        new_name="Provider C",
        actor_user_id=owner_user.id,
        idempotency_key="provider-intermediate-b-c",
    )
    assert second.target_tariff_id != order.payment.tariff_id

    # Both replay forms drain the accepted B family before mutable provider
    # readiness/current-leaf checks, even after C has become current.
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
    keyed_replay = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=None,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        renewed_from_subscription_id=source.id,
        expected_target_tariff_id=first.target_tariff_id,
        expected_target_price=first.target_tariff.price,
        command_idempotency_key="provider-intermediate-key",
    )
    unkeyed_replay = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=None,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        renewed_from_subscription_id=source.id,
        expected_target_tariff_id=first.target_tariff_id,
        expected_target_price=first.target_tariff.price,
    )
    assert keyed_replay.id == order.id
    assert unkeyed_replay.id == order.id
    order.payment.refresh_from_db()
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.payment.tariff_id == first.target_tariff_id
    assert paid_at <= timezone.now()


@pytest.mark.parametrize("shape", ["hybrid", "weekly", "unlimited"])
@pytest.mark.django_db
def test_same_id_legacy_shapes_keep_renewal_path(shape, club, owner_user):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("8000"),
        trainings_limit=None,
        duration_days=30,
    )
    components = []
    if shape == "hybrid":
        components.extend(
            [
                TariffComponentFactory(
                    club=club,
                    tariff=tariff,
                    training_type=training_type,
                    entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
                    credits_total=4,
                    weekly_limit=None,
                    paid_amount_basis=Decimal("4000"),
                ),
                TariffComponentFactory(
                    club=club,
                    tariff=tariff,
                    training_type=training_type,
                    entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT,
                    credits_total=None,
                    weekly_limit=2,
                    paid_amount_basis=Decimal("4000"),
                ),
            ]
        )
    else:
        components.append(
            TariffComponentFactory(
                club=club,
                tariff=tariff,
                training_type=training_type,
                entitlement_kind=(
                    TariffComponent.EntitlementKind.WEEKLY_LIMIT
                    if shape == "weekly"
                    else TariffComponent.EntitlementKind.UNLIMITED
                ),
                credits_total=None,
                weekly_limit=2 if shape == "weekly" else None,
                paid_amount_basis=tariff.price,
            )
        )
    student = StudentFactory(club=club)
    source = Subscription.objects.create(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=None,
        expires_at=timezone.now() + timedelta(days=7),
    )
    for component in components:
        SubscriptionComponent.objects.create(
            club=club,
            subscription=source,
            tariff_component=component,
            name_snapshot=component.name,
            training_type=component.training_type,
            entitlement_kind=component.entitlement_kind,
            credits_total=component.credits_total,
            credits_left=component.credits_total,
            weekly_limit=component.weekly_limit,
            scope=component.scope,
            location=component.location,
            trainer_payout_policy_snapshot=component.trainer_payout_policy,
            paid_amount_basis_snapshot=component.paid_amount_basis,
            unit_amount_basis_snapshot=None,
        )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            command_idempotency_key=f"legacy-shape-{shape}",
            expected_target_tariff_id=tariff.id,
            expected_target_price=tariff.price,
        )
    assert payment.tariff_id == tariff.id
    assert payment.subscription.renewed_from_id == source.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row-lock semantics")
def test_postgresql_revision_and_renewal_race_keeps_one_audited_outcome(club, owner_user):
    student = StudentFactory(club=club)
    source_tariff, source_component = _finite_tariff(club=club)
    source = _active_source_subscription(
        club=club,
        student=student,
        tariff=source_tariff,
        tariff_component=source_component,
    )
    barrier = Barrier(2)
    transaction.commit()

    def revise():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            revision = revise_tariff_price(
                club_id=club.id,
                source_tariff_id=source_tariff.id,
                new_price=Decimal("8500"),
                new_name="Raced target",
                actor_user_id=owner_user.id,
                idempotency_key="race-revision",
            )
            return "revision", revision.target_tariff_id
        except BusinessLogicError as error:
            return "error", error.code
        finally:
            close_old_connections()

    def renew():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with patch("django_q.tasks.async_task"):
                payment = create_manual_subscription_renewal(
                    club_id=club.id,
                    student_id=student.id,
                    renewed_from_subscription_id=source.id,
                    payment_method=Payment.Method.CASH,
                    recorded_by_id=owner_user.id,
                    command_idempotency_key="race-renewal",
                )
            return "payment", payment.id
        except BusinessLogicError as error:
            return "error", error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda fn: fn(), (revise, renew)))
    assert sum(kind == "revision" for kind, _value in outcomes) == 1, outcomes
    assert all(
        value in {"renewal_offer_required", "renewal_offer_stale"}
        for kind, value in outcomes
        if kind == "error"
    ), outcomes
    assert Payment.objects.for_club(club).filter(subscription__renewed_from=source).count() <= 1


@pytest.mark.parametrize("with_key", [True, False])
@pytest.mark.django_db
def test_ordinary_bank_order_replay_precedes_legacy_source_inference(
    settings,
    club,
    owner_user,
    with_key,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    tariff, _component = _finite_tariff(club=club)
    student = StudentFactory(club=club)
    command = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
    }
    if with_key:
        command["command_idempotency_key"] = "ordinary-no-edge-replay"

    first = create_bank_payment_order(**command)
    replay = create_bank_payment_order(**command)

    assert replay.id == first.id
    if with_key:
        assert getattr(replay, "_command_replayed", False) is True
    assert replay.renewed_from_subscription_id is None
    assert Payment.objects.for_club(club).filter(student=student).count() == 1


@pytest.mark.django_db
def test_ordinary_accepted_b_replay_survives_b_to_c_after_source_retirement(
    settings,
    club,
    owner_user,
):
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    tariff, _component = _finite_tariff(club=club)
    student = StudentFactory(club=club)
    command = {
        "club_id": club.id,
        "student_id": student.id,
        "tariff_id": tariff.id,
        "source": BankPaymentOrder.Source.OWNER,
        "created_by_id": owner_user.id,
    }
    command["command_idempotency_key"] = "ordinary-accepted-b-replay"

    order = create_bank_payment_order(**command)
    process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=_bank_webhook_body(order, status="APPROVED"),
        headers={},
        request_id=f"ordinary-accepted-{order.id}",
    )
    next_revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=tariff.id,
        new_price=Decimal("8500"),
        new_name="Ordinary current",
        actor_user_id=owner_user.id,
        idempotency_key="ordinary-b-c",
    )

    replay = create_bank_payment_order(**command)

    assert next_revision.target_tariff_id != tariff.id
    assert replay.id == order.id
    assert getattr(replay, "_command_replayed", False) is True
    assert replay.payment.tariff_id == tariff.id
    assert Payment.objects.for_club(club).filter(student=student).count() == 1
