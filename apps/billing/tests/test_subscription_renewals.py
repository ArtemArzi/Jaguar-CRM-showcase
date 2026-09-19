from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.attendance.services.staff_intents import get_personal_commercial_context
from apps.attendance.tests.factories import TrainingGroupFactory
from apps.billing.models import (
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionRenewalEvent,
    TrainingType,
)
from apps.billing.service_modules.payment_creation import create_payment
from apps.billing.service_modules.renewals import (
    build_subscription_command_fingerprint,
    create_manual_subscription_renewal,
    finalize_subscription_renewal,
    is_exact_renewal_source_actionable,
)
from apps.billing.services import verify_payment
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory


def _renewal_family(*, club, source_expires_at, source_trainings_left=8):
    tariff = TariffFactory(
        club=club,
        training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
        duration_days=30,
        trainings_limit=8,
    )
    student = StudentFactory(club=club)
    chain_id = uuid4()
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=source_expires_at,
        trainings_left=source_trainings_left,
        renewal_chain_id=chain_id,
    )
    renewed = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=timezone.now() + timedelta(days=30),
        renewed_from=source,
        trainings_left=8,
        renewal_chain_id=chain_id,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=renewed,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.CONFIRMED,
    )
    return source, renewed, payment


@pytest.mark.django_db
def test_renewal_finalizer_expires_exhausted_active_source_without_carry(club):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=12),
        source_trainings_left=0,
    )

    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    source.refresh_from_db()
    renewed.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED
    assert event.carry_snapshot["outcome"] == "exhausted"
    assert event.carry_snapshot["carried_days"] == 0
    assert renewed.expires_at < source.expires_at + timedelta(days=30)


@pytest.mark.django_db
def test_renewal_finalizer_accepts_naturally_expired_source_without_carry(club):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at - timedelta(seconds=1),
    )

    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    source.refresh_from_db()
    assert source.status == Subscription.Status.EXPIRED
    assert event.carry_snapshot["outcome"] == "expired"
    assert event.carry_snapshot["carried_days"] == 0


@pytest.mark.django_db
def test_renewal_finalizer_persists_same_day_expiry_carry(club):
    finalized_at = timezone.now()
    source_expiry = finalized_at + timedelta(hours=2)
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=source_expiry,
    )

    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    renewed.refresh_from_db()
    assert event.carry_snapshot["carried_days"] == 0
    assert renewed.expires_at == source_expiry + timedelta(days=renewed.tariff.duration_days)


@pytest.mark.django_db
def test_renewal_finalizer_carries_duplicate_component_shapes_by_tariff_component_id(club):
    finalized_at = timezone.now()
    tariff = TariffFactory(club=club, duration_days=30, trainings_limit=None)
    student = StudentFactory(club=club)
    chain_id = uuid4()
    first = TariffComponentFactory(tariff=tariff, club=club, credits_total=8)
    second = TariffComponentFactory(tariff=tariff, club=club, credits_total=8)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=finalized_at + timedelta(days=5),
        trainings_left=None,
        renewal_chain_id=chain_id,
    )
    renewed = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=finalized_at + timedelta(days=30),
        renewed_from=source,
        trainings_left=None,
        renewal_chain_id=chain_id,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=first,
        credits_total=8,
        credits_left=2,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=second,
        credits_total=8,
        credits_left=5,
    )
    first_new = SubscriptionComponentFactory(
        club=club,
        subscription=renewed,
        tariff_component=first,
        credits_total=8,
        credits_left=7,
    )
    second_new = SubscriptionComponentFactory(
        club=club,
        subscription=renewed,
        tariff_component=second,
        credits_total=8,
        credits_left=11,
    )
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=renewed,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.CONFIRMED,
    )

    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    first_new.refresh_from_db()
    second_new.refresh_from_db()
    assert first_new.credits_left == 9
    assert second_new.credits_left == 16
    carried_components = {
        (item["from_component_id"], item["to_component_id"], item["credits"])
        for item in event.carry_snapshot["components"]
    }
    assert carried_components == {
        (component.id, target.id, credits)
        for component, target, credits in [
            (SubscriptionComponent.objects.get(subscription=source, tariff_component=first), first_new, 2),
            (SubscriptionComponent.objects.get(subscription=source, tariff_component=second), second_new, 5),
        ]
    }


@pytest.mark.django_db
def test_renewal_event_is_append_only_through_scoped_and_unscoped_managers(club):
    finalized_at = timezone.now()
    _, _, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    event.carry_snapshot = {"tampered": True}
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        SubscriptionRenewalEvent.objects.for_club(club).filter(id=event.id).update(finalized_at=finalized_at)
    with pytest.raises(ValidationError):
        SubscriptionRenewalEvent.objects.unscoped().filter(id=event.id).delete()
    with pytest.raises(ValidationError):
        event.delete()


@pytest.mark.django_db
def test_renewal_finalizer_rejects_early_closed_source_without_event(club):
    finalized_at = timezone.now()
    source, _, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    source.status = Subscription.Status.CANCELLED
    source.save(update_fields=["status", "updated_at"])

    with pytest.raises(BusinessLogicError, match="закрыт"):
        finalize_subscription_renewal(
            club_id=club.id,
            payment_id=payment.id,
            finalized_at=finalized_at,
        )

    assert not SubscriptionRenewalEvent.objects.for_club(club).filter(payment_id=payment.id).exists()


@pytest.mark.django_db
def test_manual_renewal_early_closed_source_rolls_back_pending_family(club):
    finalized_at = timezone.now()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        duration_days=30,
        trainings_limit=8,
    )
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        expires_at=finalized_at + timedelta(days=3),
        trainings_left=3,
    )
    reviewer = UserFactory()
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=student.id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=reviewer.id,
            command_idempotency_key="manual-early-closed-source",
        )
        source.status = Subscription.Status.CANCELLED
        source.save(update_fields=["status", "updated_at"])

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=reviewer.id,
                action="confirm",
            )

    payment.refresh_from_db()
    payment.subscription.refresh_from_db()
    assert exc_info.value.code == "renewal_source_closed"
    assert payment.status == Payment.Status.PENDING
    assert payment.subscription.status == Subscription.Status.PENDING
    assert not SubscriptionRenewalEvent.objects.for_club(club).filter(payment=payment).exists()


@pytest.mark.django_db
def test_renewal_rejects_stale_finalized_source_and_allows_latest_chain_leaf(club):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )
    reviewer = UserFactory()

    with patch("django_q.tasks.async_task"):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_manual_subscription_renewal(
                club_id=club.id,
                student_id=source.student_id,
                renewed_from_subscription_id=source.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=reviewer.id,
                command_idempotency_key="stale-source-k2",
            )

        latest_payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=renewed.student_id,
            renewed_from_subscription_id=renewed.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=reviewer.id,
            command_idempotency_key="latest-chain-leaf-k2",
        )
    assert exc_info.value.code == "renewal_source_finalized_successor"
    assert latest_payment.subscription.renewed_from_id == renewed.id


@pytest.mark.django_db
def test_historical_confirmed_successor_blocks_stale_source_even_after_successor_expires(club):
    """Pre-event families still make their former source non-leaf permanently."""

    finalized_at = timezone.now()
    source, successor, confirmed_payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    assert confirmed_payment.status == Payment.Status.CONFIRMED
    successor.status = Subscription.Status.EXPIRED
    successor.trainings_left = 0
    successor.expires_at = finalized_at + timedelta(days=30)
    successor.save(update_fields=["status", "trainings_left", "expires_at", "updated_at"])

    assert not SubscriptionRenewalEvent.objects.for_club(club).filter(renewed_from=source).exists()
    assert is_exact_renewal_source_actionable(source=source) is False
    assert is_exact_renewal_source_actionable(source=successor) is True

    with patch("django_q.tasks.async_task"):
        with pytest.raises(BusinessLogicError) as stale_error:
            create_manual_subscription_renewal(
                club_id=club.id,
                student_id=source.student_id,
                renewed_from_subscription_id=source.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=UserFactory().id,
                command_idempotency_key="historical-stale-source-k2",
            )
        latest = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=successor.student_id,
            renewed_from_subscription_id=successor.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=UserFactory().id,
            command_idempotency_key="historical-latest-source-k2",
        )

    assert stale_error.value.code == "renewal_source_finalized_successor"
    assert latest.subscription.renewed_from_id == successor.id


@pytest.mark.django_db
def test_terminal_unconfirmed_successor_does_not_block_exact_source_leaf(club):
    finalized_at = timezone.now()
    tariff = TariffFactory(
        club=club,
        training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
    )
    source = SubscriptionFactory(
        club=club,
        student=StudentFactory(club=club),
        tariff=tariff,
        status=Subscription.Status.EXPIRED,
        trainings_left=0,
        expires_at=finalized_at + timedelta(days=3),
    )
    terminal = SubscriptionFactory(
        club=club,
        student=source.student,
        tariff=tariff,
        status=Subscription.Status.CANCELLED,
        renewed_from=source,
        renewal_chain_id=uuid4(),
    )
    PaymentFactory(
        club=club,
        student=source.student,
        tariff=tariff,
        subscription=terminal,
        status=Payment.Status.REJECTED,
    )

    assert is_exact_renewal_source_actionable(source=source) is True


@pytest.mark.django_db
def test_null_expiry_exhausted_source_is_actionable_and_finalizes_without_carry(club):
    """A legacy source with no timer is naturally exhausted when credits are gone."""

    tariff = TariffFactory(
        club=club,
        training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
        trainings_limit=8,
    )
    source = SubscriptionFactory(
        club=club,
        student=StudentFactory(club=club),
        tariff=tariff,
        status=Subscription.Status.EXPIRED,
        trainings_left=0,
        expires_at=None,
    )
    reviewer = UserFactory()
    assert is_exact_renewal_source_actionable(source=source) is True

    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=source.student_id,
            renewed_from_subscription_id=source.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=reviewer.id,
            command_idempotency_key="null-expiry-exhausted-k1",
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=reviewer.id,
            action="confirm",
        )

    event = SubscriptionRenewalEvent.objects.for_club(club).get(payment=payment)
    payment.subscription.refresh_from_db()
    assert event.carry_snapshot["outcome"] == "exhausted"
    assert event.carry_snapshot["carried_days"] == 0
    assert payment.subscription.trainings_left == tariff.trainings_limit


@pytest.mark.django_db
@pytest.mark.parametrize("tamper", ["tariff", "chain"])
def test_renewal_finalizer_rejects_tampered_exact_source_before_mutation(club, tamper):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    if tamper == "tariff":
        renewed.tariff = TariffFactory(club=club)
        renewed.save(update_fields=["tariff", "updated_at"])
    else:
        renewed.renewal_chain_id = uuid4()
        renewed.save(update_fields=["renewal_chain_id", "updated_at"])

    with pytest.raises(BusinessLogicError) as exc_info:
        finalize_subscription_renewal(
            club_id=club.id,
            payment_id=payment.id,
            finalized_at=finalized_at,
        )

    source.refresh_from_db()
    assert exc_info.value.code == "renewal_source_mismatch"
    assert source.status == Subscription.Status.ACTIVE
    assert not SubscriptionRenewalEvent.objects.for_club(club).filter(payment_id=payment.id).exists()


@pytest.mark.django_db
def test_renewal_finalizer_grandfathers_unambiguous_pre_slice6_pending_chain(club):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    chain_id = renewed.renewal_chain_id
    source.renewal_chain_id = None
    source.save(update_fields=["renewal_chain_id", "updated_at"])

    event = finalize_subscription_renewal(
        club_id=club.id,
        payment_id=payment.id,
        finalized_at=finalized_at,
    )

    source.refresh_from_db()
    assert event.id is not None
    assert source.renewal_chain_id == chain_id


@pytest.mark.django_db
def test_renewal_finalizer_rejects_ambiguous_legacy_chain_without_backfill(club):
    finalized_at = timezone.now()
    source, renewed, payment = _renewal_family(
        club=club,
        source_expires_at=finalized_at + timedelta(days=3),
    )
    source.renewal_chain_id = None
    source.save(update_fields=["renewal_chain_id", "updated_at"])
    SubscriptionFactory(
        club=club,
        student=source.student,
        tariff=source.tariff,
        status=Subscription.Status.CANCELLED,
        renewed_from=source,
        renewal_chain_id=uuid4(),
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        finalize_subscription_renewal(
            club_id=club.id,
            payment_id=payment.id,
            finalized_at=finalized_at,
        )

    source.refresh_from_db()
    assert exc_info.value.code == "renewal_source_mismatch"
    assert source.renewal_chain_id is None


@pytest.mark.django_db
def test_payment_renewal_source_tariff_snapshot_is_immutable(club):
    _, _, payment = _renewal_family(
        club=club,
        source_expires_at=timezone.now() + timedelta(days=3),
    )
    payment.renewal_source_tariff_name_snapshot = "Original source tariff"
    payment.save(update_fields=["renewal_source_tariff_name_snapshot", "updated_at"])
    payment.renewal_source_tariff_name_snapshot = "Tampered source tariff"

    with pytest.raises(ValidationError):
        payment.save(update_fields=["renewal_source_tariff_name_snapshot", "updated_at"])
    with pytest.raises(ValidationError):
        Payment.objects.for_club(club).filter(id=payment.id).update(
            renewal_source_tariff_name_snapshot="Bulk tamper"
        )


@pytest.mark.django_db
def test_commercial_context_uses_frozen_group_and_renewal_names_after_rename(club):
    group = TrainingGroupFactory(club=club, name="Frozen Group")
    tariff = TariffFactory(club=club)
    student = StudentFactory(club=club)
    group_payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.REJECTED,
        target_training_group=group,
        target_start_date=timezone.localdate() + timedelta(days=1),
        target_group_name_snapshot="Frozen Group",
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.EXPIRED,
    )
    renewed = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        renewed_from=source,
        renewal_chain_id=uuid4(),
    )
    renewal_payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=renewed,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.CONFIRMED,
        renewal_source_tariff_name_snapshot="Frozen Renewal Tariff",
    )
    group.name = "Mutated Group"
    group.save(update_fields=["name", "updated_at"])
    tariff.name = "Mutated Tariff"
    tariff.save(update_fields=["name", "updated_at"])

    context = get_personal_commercial_context(club_id=club.id, student_id=student.id)
    by_payment_id = {item["payment_id"]: item for item in context}
    assert by_payment_id[group_payment.id]["group_name"] == "Frozen Group"
    assert by_payment_id[renewal_payment.id]["tariff_name"] == "Frozen Renewal Tariff"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection command-arbitration semantics",
)
def test_postgresql_cross_student_same_key_has_one_payment_family_and_no_orphan(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type)
    first_student = StudentFactory(club=club)
    second_student = StudentFactory(club=club)
    user = UserFactory()
    key = "pg-cross-student-command-key"
    gate = Barrier(2)
    transaction.commit()

    def submit(student):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return (
                "payment",
                create_payment(
                    club_id=club.id,
                    student_id=student.id,
                    tariff_id=tariff.id,
                    payment_method=Payment.Method.CASH,
                    recorded_by_id=user.id,
                    command_idempotency_key=key,
                    command_fingerprint=build_subscription_command_fingerprint(
                        student_id=student.id,
                        tariff_id=tariff.id,
                        payment_method=Payment.Method.CASH,
                    ),
                ).id,
            )
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, (first_student, second_student)))

    assert sum(kind == "payment" for kind, _value in outcomes) == 1, outcomes
    assert any(value == "idempotency_conflict" for kind, value in outcomes if kind == "business_error")
    assert Payment.objects.for_club(club).filter(command_idempotency_key=key).count() == 1
    assert Subscription.objects.for_club(club).filter(payment__command_idempotency_key=key).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL duplicate manual-confirmation lock semantics",
)
@patch("django_q.tasks.async_task")
def test_postgresql_duplicate_manual_renewal_confirmation_has_one_event_and_leaf(
    _mock_async,
    club,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type, duration_days=30, trainings_limit=8)
    student = StudentFactory(club=club)
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=3,
        expires_at=timezone.now() + timedelta(days=7),
    )
    reviewer = UserFactory()
    payment = create_manual_subscription_renewal(
        club_id=club.id,
        student_id=student.id,
        renewed_from_subscription_id=source.id,
        payment_method=Payment.Method.CASH,
        recorded_by_id=reviewer.id,
        command_idempotency_key="pg-duplicate-manual-renewal",
    )
    gate = Barrier(2)
    transaction.commit()

    def confirm():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=reviewer.id,
                action="confirm",
            )
            return "payment", result.status
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: confirm(), range(2)))

    payment.refresh_from_db()
    source.refresh_from_db()
    assert not {
        code
        for kind, code in outcomes
        if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }, outcomes
    assert payment.status == Payment.Status.CONFIRMED
    assert source.status == Subscription.Status.EXPIRED
    assert SubscriptionRenewalEvent.objects.for_club(club).filter(payment=payment).count() == 1
    assert Subscription.objects.for_club(club).filter(
        renewed_from=source,
        status=Subscription.Status.ACTIVE,
        deleted_at__isnull=True,
    ).count() == 1
