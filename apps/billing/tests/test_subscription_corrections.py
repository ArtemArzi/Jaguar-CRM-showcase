from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.billing.models import Subscription, SubscriptionCorrection, SubscriptionRenewalEvent
from apps.billing.service_modules.entitlements import refresh_subscription_counters
from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings
from apps.billing.service_modules.subscription_corrections import correct_subscription, preview_subscription_correction
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    SubscriptionFreezeFactory,
)
from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning

pytestmark = pytest.mark.django_db


@pytest.fixture
def case(club, settings):
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    actor = UserFactory()
    ClubMembership.objects.create(club=club, user=actor, role="owner")
    sub = SubscriptionFactory(
        club=club, student=StudentFactory(club=club), tariff__club=club, tariff__training_type__club=club,
        trainings_left=7, trainings_used=5,
    )
    component = SubscriptionComponentFactory(
        club=club,
        subscription=sub,
        credits_total=12,
        credits_left=7,
        credits_used=5,
        unit_amount_basis_snapshot=Decimal("1000"),
    )
    return actor, sub, component


def command(case, **extra):
    actor, sub, component = case
    return dict(club_id=sub.club_id, actor_user_id=actor.id, subscription_id=sub.id, component_id=component.id, **extra)


def apply(case, key="one", **extra):
    args = command(case, **extra)
    preview = preview_subscription_correction(**args)
    receipt = correct_subscription(**args, command_key=key, reason="Сверка", expected_fingerprint=preview.fingerprint)
    return receipt


def test_positive_delta_preserves_purchase_and_visits_and_safe_replay(case, settings):
    _, sub, component = case
    receipt = apply(case, desired_remaining=15)
    component.refresh_from_db()
    sub.refresh_from_db()
    assert (component.credits_total, component.credits_used, component.credits_left) == (12, 5, 15)
    assert component.unit_amount_basis_snapshot == Decimal("1000")
    assert (sub.trainings_left, sub.trainings_used) == (15, 5)
    assert receipt.balance_delta == 8
    assert subscription_balance_findings(subscription=sub) == []
    assert TrainerEarning.objects.for_club(sub.club_id).count() == 0
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    replay = correct_subscription(
        **command(case, desired_remaining=15), command_key="one", reason="Сверка", expected_fingerprint="lost-response"
    )
    assert replay.id == receipt.id
    with pytest.raises(BusinessLogicError) as error:
        correct_subscription(
            **command(case, desired_remaining=16),
            command_key="one",
            reason="Сверка",
            expected_fingerprint="lost-response",
        )
    assert error.value.code == "idempotency_conflict"


def test_compensation_after_visit_reverses_delta_not_previous_absolute(case):
    _, sub, component = case
    receipt = apply(case, desired_remaining=9)
    component.credits_left = 8
    component.credits_used = 6
    component.save()
    refresh_subscription_counters(subscription=sub)
    compensation = apply(case, key="undo", reverses_id=receipt.id)
    component.refresh_from_db()
    sub.refresh_from_db()
    assert compensation.balance_delta == -2
    assert (component.credits_total, component.credits_used, component.credits_left) == (12, 6, 6)
    assert subscription_balance_findings(subscription=sub) == []


def test_carry_is_read_from_exact_receipt_not_mutable_predecessor(case):
    _, sub, component = case
    source = SubscriptionFactory(club=sub.club, student=sub.student, tariff=sub.tariff, status="expired")
    old = SubscriptionComponentFactory(club=sub.club, subscription=source, credits_left=3)
    sub.renewed_from = source
    sub.trainings_left = 10
    sub.save()
    component.credits_left = 10
    component.save()
    payment = PaymentFactory(
        club=sub.club, student=sub.student, tariff=sub.tariff, subscription=sub, status="confirmed"
    )
    SubscriptionRenewalEvent.objects.create(
        club=sub.club,
        renewed_from=source,
        renewed_to=sub,
        payment=payment,
        finalized_at=timezone.now(),
        carry_snapshot={
            "components": [
                {"kind": "finite_credits", "from_component_id": old.id, "to_component_id": component.id, "credits": 3}
            ]
        },
    )
    old.credits_left = 0
    old.save()
    receipt = apply(case, desired_remaining=11)
    sub.refresh_from_db()
    assert receipt.balance_delta == 1
    assert subscription_balance_findings(subscription=sub) == []


@pytest.mark.parametrize("remaining", [-1, 1.5, True])
def test_invalid_remaining_rejected(case, remaining):
    with pytest.raises(BusinessLogicError):
        apply(case, desired_remaining=remaining)
    assert not SubscriptionCorrection.objects.exists()


def test_unproven_balance_is_not_rebased(case):
    _, _, component = case
    component.credits_left = 8
    component.save()
    with pytest.raises(BusinessLogicError) as error:
        apply(case, desired_remaining=9)
    assert error.value.code == "correction_balance_needs_review"


def test_stale_preview_does_not_overwrite_consumption(case):
    args = command(case, desired_remaining=8)
    preview = preview_subscription_correction(**args)
    _, sub, component = case
    component.credits_left -= 1
    component.credits_used += 1
    component.save()
    refresh_subscription_counters(subscription=sub)
    with pytest.raises(BusinessLogicError) as error:
        correct_subscription(**args, command_key="stale", reason="Сверка", expected_fingerprint=preview.fingerprint)
    assert error.value.code == "correction_stale_preview"
    component.refresh_from_db()
    assert component.credits_left == 6
    assert not SubscriptionCorrection.objects.exists()


@pytest.mark.parametrize("dependency", ["freeze", "pending_renewal", "cancelled", "pending", "unconfirmed_payment"])
def test_lifecycle_dependencies_block_correction(case, dependency):
    actor, sub, _ = case
    if dependency == "freeze":
        SubscriptionFreezeFactory(subscription=sub, club=sub.club, frozen_by=actor)
    elif dependency == "pending_renewal":
        SubscriptionFactory(club=sub.club, student=sub.student, tariff=sub.tariff, renewed_from=sub, status="pending")
    elif dependency == "unconfirmed_payment":
        PaymentFactory(club=sub.club, student=sub.student, tariff=sub.tariff, subscription=sub)
    else:
        sub.status = dependency
        sub.save()
    with pytest.raises(BusinessLogicError):
        apply(case, desired_remaining=8)
    assert not SubscriptionCorrection.objects.exists()


def test_expiry_date_inclusive_and_reversal_rejects_intervening_change(case):
    _, sub, _ = case
    sub.club.timezone = "Asia/Yekaterinburg"
    sub.club.save()
    desired = timezone.now().date() + timedelta(days=45)
    receipt = apply(case, desired_expires_on=desired)
    sub.refresh_from_db()
    assert timezone.localtime(sub.expires_at, timezone.get_fixed_timezone(300)).date() == desired + timedelta(days=1)
    apply(case, key="expiry2", desired_expires_on=desired + timedelta(days=2))
    with pytest.raises(BusinessLogicError) as error:
        apply(case, key="undo", reverses_id=receipt.id)
    assert error.value.code == "correction_expiry_dependency"


def test_naturally_expired_balance_does_not_revive_until_expiry_extended(case):
    _, sub, _ = case
    sub.status = "expired"
    sub.expires_at = timezone.now() - timedelta(days=1)
    sub.save()
    apply(case, desired_remaining=8)
    sub.refresh_from_db()
    assert sub.status == Subscription.Status.EXPIRED
    apply(case, key="expiry", desired_expires_on=timezone.now().date() + timedelta(days=1))
    sub.refresh_from_db()
    assert sub.status == Subscription.Status.ACTIVE


def test_journal_immutable_including_queryset_and_actor_gate_on_replay(case):
    actor, sub, _ = case
    receipt = apply(case, desired_remaining=8)
    for mutation in [
        lambda: receipt.save(),
        lambda: receipt.delete(),
        lambda: SubscriptionCorrection.objects.filter(id=receipt.id).update(reason="replace"),
        lambda: SubscriptionCorrection.objects.filter(id=receipt.id).delete(),
    ]:
        with pytest.raises(ValidationError):
            mutation()
    ClubMembership.objects.filter(club=sub.club, user=actor).update(is_active=False)
    with pytest.raises(BusinessLogicError) as error:
        correct_subscription(
            **command(case, desired_remaining=8), command_key="one", reason="Сверка", expected_fingerprint="replay"
        )
    assert error.value.code == "actor_not_authorized"


def test_foreign_subscription_never_accessible(case):
    actor, _, _ = case
    other = ClubFactory()
    ClubMembership.objects.create(club=other, user=actor, role="admin")
    args = command(case, desired_remaining=8)
    args["club_id"] = other.id
    with pytest.raises(BusinessLogicError) as error:
        preview_subscription_correction(**args)
    assert error.value.code == "target_not_available"


def test_mixed_projection_uses_non_numeric_capacity_and_all_real_usage(case):
    _, sub, component = case
    SubscriptionComponentFactory(
        club=sub.club,
        subscription=sub,
        entitlement_kind="unlimited",
        credits_total=None,
        credits_left=None,
        credits_used=2,
    )
    apply(case, desired_remaining=8)
    sub.refresh_from_db()
    assert sub.trainings_left is None and sub.trainings_used == 7
    assert subscription_balance_findings(subscription=sub) == []
    component.entitlement_kind = "unlimited"
    component.save()
    with pytest.raises(BusinessLogicError) as error:
        apply(case, key="nonfinite", desired_remaining=9)
    assert error.value.code == "correction_non_finite"


def test_personal_capacity_includes_delta_without_changing_money_basis(case):
    from apps.attendance.services.enrollment import assert_personal_entitlement_booking_capacity

    _, sub, component = case
    apply(case, desired_remaining=15)
    sub.refresh_from_db()
    component.refresh_from_db()
    # Thirteen credits have since been consumed, including above the purchased
    # count. Real counters, not count(Checkin), include the imported baseline.
    component.credits_left = 2
    component.credits_used = 18
    component.save()
    refresh_subscription_counters(subscription=sub)
    assert_personal_entitlement_booking_capacity(subscription=sub, component=component, starts_at=timezone.now())
    assert component.unit_amount_basis_snapshot == Decimal("1000")
    component.credits_left = 0
    component.credits_used = 20
    component.save()
    with pytest.raises(BusinessLogicError):
        assert_personal_entitlement_booking_capacity(subscription=sub, component=component, starts_at=timezone.now())


def test_expiry_compensation_keeps_later_balance_delta(case):
    _, sub, component = case
    old_expiry = sub.expires_at
    first = apply(case, desired_expires_on=timezone.now().date() + timedelta(days=40))
    apply(case, key="more", desired_remaining=9)
    apply(case, key="undo", reverses_id=first.id)
    sub.refresh_from_db()
    component.refresh_from_db()
    assert sub.expires_at == old_expiry
    assert component.credits_left == 9


def test_audit_outputs_ids_and_counters_without_modifying_rows(case):
    import json
    from io import StringIO

    from django.core.management import call_command

    _, sub, component = case
    sub.trainings_left = 99
    sub.save()
    before = (sub.updated_at, component.updated_at)
    output = StringIO()
    call_command("audit_subscription_balances", club_id=sub.club_id, stdout=output)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[-1]["needs_review"] == 1
    assert rows[0]["findings"][0]["code"] == "aggregate_balance_mismatch"
    sub.refresh_from_db()
    component.refresh_from_db()
    assert (sub.updated_at, component.updated_at) == before
    assert sub.trainings_left == 99


def test_default_off_blocks_new_corrections(case, settings):
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    with pytest.raises(BusinessLogicError) as error:
        apply(case, desired_remaining=8)
    assert error.value.code == "student_corrections_disabled"


def _schedule_for(case):
    from datetime import time

    from apps.attendance.tests.factories import ScheduleFactory
    from apps.clubs.timezones import club_localdate

    _, sub, component = case
    day = club_localdate(sub.club)
    return ScheduleFactory(
        club=sub.club,
        training_type=component.training_type,
        one_time_date=day,
        day_of_week=day.weekday(),
        start_time=time(0, 1),
        end_time=time(23, 59),
    )


def _consume(case, schedule):
    from apps.attendance.services.checkin import create_checkin

    _, sub, component = case
    return create_checkin(
        club_id=sub.club_id,
        student_id=sub.student_id,
        schedule_id=schedule.id,
        training_type_id=component.training_type_id,
        source="manual",
        checkin_date=schedule.one_time_date,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("writer", ["consume", "cancel", "freeze", "replay", "renewal", "refund"])
def test_postgres_correction_serializes_with_actual_lifecycle_owner(case, monkeypatch, writer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from django.db import close_old_connections, connection, transaction

    from apps.attendance.services.checkin import cancel_checkin
    from apps.billing.service_modules.freezes import freeze_subscription
    from apps.clubs.models import Club, ClubSettings

    if connection.vendor != "postgresql":
        pytest.skip("Observed row-lock race requires disposable PostgreSQL")
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)
    actor, sub, component = case
    refund_payment = None
    if writer == "refund":
        component.trainer_payout_policy_snapshot = "none"
        component.save()
        refund_payment = PaymentFactory(
            club=sub.club,
            student=sub.student,
            tariff=sub.tariff,
            subscription=sub,
            status="confirmed",
            payment_method="online",
            verified_at=timezone.now(),
        )
    if writer == "renewal":
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        TrainerPackageAllocation.objects.create(
            club=sub.club,
            subscription=sub,
            student=sub.student,
            tariff=sub.tariff,
            training_type=component.training_type,
            owner_trainer=TrainerFactory(club=sub.club),
            amount_snapshot=Decimal("5000"),
            sessions_total_snapshot=12,
            sessions_remaining_snapshot=7,
        )
    schedule = _schedule_for(case)
    checkin_id = _consume(case, schedule)["checkin_id"] if writer == "cancel" else None
    if writer == "freeze":
        ClubSettings.objects.update_or_create(club=sub.club, defaults={"freeze_enabled": True})
    args = command(case, desired_remaining=9)
    preview = preview_subscription_correction(**args)
    kwargs = dict(**args, command_key="race", reason="Сверка", expected_fingerprint=preview.fingerprint)
    locked, release, waiting = Event(), Event(), Event()
    pids = {}

    def run(first):
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[first] = cursor.fetchone()[0]
                if first:
                    if writer == "freeze":
                        Subscription.objects.for_club(sub.club_id).select_for_update().get(id=sub.id)
                    else:
                        Club.objects.select_for_update().get(id=sub.club_id)
                    locked.set()
                    assert release.wait(15)
                else:
                    waiting.set()
                try:
                    if not first or writer == "replay":
                        return correct_subscription(**kwargs).id
                    if writer == "consume":
                        return _consume(case, schedule)["checkin_id"]
                    if writer == "cancel":
                        cancel_checkin(checkin_id=checkin_id, club_id=sub.club_id, cancelled_by_user_id=actor.id)
                        return "cancelled"
                    if writer == "renewal":
                        from apps.billing.service_modules.payment_review import verify_payment
                        from apps.billing.service_modules.renewals import create_manual_subscription_renewal

                        payment = create_manual_subscription_renewal(
                            club_id=sub.club_id,
                            student_id=sub.student_id,
                            renewed_from_subscription_id=sub.id,
                            payment_method="cash",
                            recorded_by_id=actor.id,
                            command_idempotency_key="renew-race",
                        )
                        verify_payment(
                            club_id=sub.club_id, payment_id=payment.id, verified_by_id=actor.id, action="confirm"
                        )
                        return payment.id
                    if writer == "refund":
                        from apps.billing.refund_services import approve_payment_refund_case
                        from apps.billing.tests.test_refunds import _refund_case_for_existing_payment

                        _, refund_case = _refund_case_for_existing_payment(
                            club=sub.club,
                            owner_user=actor,
                            payment=refund_payment,
                            provider_event_id="correction-race",
                        )
                        return approve_payment_refund_case(
                            club_id=sub.club_id,
                            case_id=refund_case.id,
                            actor_user_id=actor.id,
                            idempotency_key="refund-race",
                            amount=refund_payment.amount,
                            refund_kind="full",
                            reason="Synthetic race",
                            entitlement_action="revoke_remaining",
                        ).id
                    return freeze_subscription(
                        club_id=sub.club_id, subscription_id=sub.id, days=1, reason="vacation", frozen_by_id=actor.id
                    ).id
                except BusinessLogicError as error:
                    return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, True)
        assert locked.wait(10)
        second = executor.submit(run, False)
        assert waiting.wait(10)
        try:
            observed = False
            deadline = monotonic() + 10
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [pids[True], pids[False]])
                    observed = cursor.fetchone()[0]
                if observed:
                    break
                sleep(0.02)
            assert observed, "Correction did not wait for the actual competing lifecycle lock"
        finally:
            release.set()
        winner, loser = first.result(timeout=15), second.result(timeout=15)
    if writer == "replay":
        assert isinstance(winner, int) and winner == loser
        assert SubscriptionCorrection.objects.for_club(sub.club_id).count() == 1
    else:
        assert winner == "cancelled" or isinstance(winner, int), winner
        expected = {
            "freeze": "correction_subscription_unavailable",
            "refund": "correction_subscription_unavailable",
            "renewal": "correction_non_leaf",
        }.get(writer, "correction_stale_preview")
        assert loser == expected
        assert not SubscriptionCorrection.objects.for_club(sub.club_id).exists()


def test_legacy_scalar_carry_expiry_receipt_matches_saved_projection(case):
    _, sub, component = case
    source = SubscriptionFactory(club=sub.club, student=sub.student, tariff=sub.tariff, status="expired")
    sub.renewed_from = source
    sub.trainings_left = 10
    sub.save()
    payment = PaymentFactory(
        club=sub.club, student=sub.student, tariff=sub.tariff, subscription=sub, status="confirmed"
    )
    SubscriptionRenewalEvent.objects.create(
        club=sub.club,
        renewed_from=source,
        renewed_to=sub,
        payment=payment,
        finalized_at=timezone.now(),
        carry_snapshot={"legacy_finite_credits": 3, "components": []},
    )
    receipt = apply(case, desired_expires_on=timezone.now().date() + timedelta(days=40))
    sub.refresh_from_db()
    component.refresh_from_db()
    assert sub.trainings_left == receipt.after["trainings_left"] == 10
    assert component.credits_left == 7
    assert any(f["code"] == "correction_carry_needs_review" for f in subscription_balance_findings(subscription=sub))
    with pytest.raises(BusinessLogicError) as error:
        apply(case, key="balance", desired_remaining=8)
    assert error.value.code == "correction_balance_needs_review"


def test_exact_personal_reservation_blocks_correction_until_released(case):
    from apps.attendance.models import ScheduleBookingEvent, ScheduleEnrollment

    actor, sub, _ = case
    schedule = _schedule_for(case)
    enrollment = ScheduleEnrollment.objects.create(
        club=sub.club,
        student=sub.student,
        schedule=schedule,
        status="active",
        created_from="manual",
        starts_on=schedule.one_time_date,
    )
    ScheduleBookingEvent.objects.create(
        club=sub.club,
        enrollment=enrollment,
        student=sub.student,
        schedule=schedule,
        actor=actor,
        event_type="personal_session_booked",
        origin="planned_session_action",
        effective_date=schedule.one_time_date,
        metadata={"subscription_id": sub.id},
    )
    with pytest.raises(BusinessLogicError) as error:
        apply(case, desired_remaining=8)
    assert error.value.code == "correction_reservation_conflict"


def test_technical_admin_cannot_bypass_correction_journal(case):
    from django.contrib import admin
    from django.test import RequestFactory

    actor, sub, _ = case
    actor.is_staff = actor.is_superuser = True
    request = RequestFactory().get("/admin/")
    request.user = actor
    for model in [Subscription, SubscriptionCorrection]:
        model_admin = admin.site._registry[model]
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)
        assert model_admin.has_view_permission(request)


@pytest.mark.parametrize("kind", ["balance", "expiry"])
def test_feature_off_allows_exact_inverse_but_not_new_corrections(case, settings, kind):
    from apps.clubs.timezones import club_localdate

    _, sub, component = case
    original_expiry = sub.expires_at
    changes = {"desired_remaining": 15} if kind == "balance" else {
        "desired_expires_on": club_localdate(sub.club) + timedelta(days=50)
    }
    original = apply(case, **changes)
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    reversed_receipt = apply(case, key="feature-off-inverse", reverses_id=original.id)
    sub.refresh_from_db()
    component.refresh_from_db()
    assert reversed_receipt.reverses_id == original.id
    assert (component.credits_total, component.credits_used, component.credits_left) == (12, 5, 7)
    assert sub.expires_at == original_expiry
    with pytest.raises(BusinessLogicError) as error:
        apply(case, key="feature-off-new", desired_remaining=8)
    assert error.value.code == "student_corrections_disabled"
    with pytest.raises(BusinessLogicError) as error:
        apply(case, key="feature-off-mixed", reverses_id=original.id, desired_remaining=99)
    assert error.value.code == "invalid_correction"
