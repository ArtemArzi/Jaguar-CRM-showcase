from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.attendance.tests.test_student_corrections import recorded_visit as recorded_visit
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import TrainerSettlementEntry, TrainerSettlementReconciliation, TrainerSettlementResolution
from apps.trainers.settlement_selectors import get_trainer_settlement_summary
from apps.trainers.settlement_services import (
    record_historical_settlement_change,
    record_trainer_settlement,
    resolve_trainer_settlement,
)
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def settlement_case(club, owner_user, settings):
    settings.TRAINER_SETTLEMENTS_ENABLED = True
    return club, owner_user, TrainerFactory(club=club), club_localdate(club) - timedelta(days=10)


def record(case, kind="opening", **kwargs):
    club, owner, trainer, start = case
    defaults = dict(
        club_id=club.id,
        actor_user_id=owner.id,
        trainer_id=trainer.id,
        kind=kind,
        effective_on=start,
        reason="Сверено",
        source_namespace="test",
        source_key=str(uuid4()),
    )
    if kind == "opening":
        defaults["balance_delta"] = Decimal("1000")
    elif kind == "payout":
        defaults.update(amount=Decimal("300"), payment_method="cash")
    defaults.update(kwargs)
    return record_trainer_settlement(**defaults)


def summary(case, day=None):
    club, _, trainer, start = case
    return get_trainer_settlement_summary(club=club, trainer_id=trainer.id, date_from=start, date_to=day or start)


def test_unknown_zero_debt_and_advance_are_distinct(settlement_case):
    case = settlement_case
    assert summary(case)["status"] == "unconfirmed" and summary(case)["balance"] is None
    opening = record(case, balance_delta=Decimal("0"))
    assert summary(case)["balance"] == 0
    record(case, "payout", amount=Decimal("100"), confirm_advance=True)
    assert summary(case)["balance"] == -100
    before = get_trainer_settlement_summary(
        club=case[0],
        trainer_id=case[2].id,
        date_from=opening.effective_on - timedelta(days=1),
        date_to=opening.effective_on - timedelta(days=1),
    )
    assert before["balance"] is None and before["status"] == "before_opening"


def test_payout_replay_reversal_asof_and_feature_off_drain(settlement_case, settings):
    case = settlement_case
    record(case)
    payout = record(case, "payout", source_key="paid")
    assert record(case, "payout", source_key="paid").id == payout.id
    assert summary(case)["balance"] == 700
    settings.TRAINER_SETTLEMENTS_ENABLED = False
    assert record(case, "payout", source_key="paid").id == payout.id
    reversal_day = case[3] + timedelta(days=2)
    reverse = record(case, "payout_reversal", reversal_of_id=payout.id, effective_on=reversal_day, source_key="undo")
    assert reverse.balance_delta == 300
    assert summary(case)["balance"] == 700
    assert summary(case, reversal_day)["balance"] == 1000
    assert summary(case, reversal_day)["paid"] == 300 and summary(case, reversal_day)["reversed"] == 300
    with pytest.raises(BusinessLogicError, match="отключены"):
        record(case, "payout")
    with pytest.raises(BusinessLogicError) as error:
        record(case, "payout", source_key="paid", amount=Decimal("301"))
    assert error.value.code == "idempotency_conflict"


def test_advance_requires_confirmation_and_future_or_early_reversal_rejected(settlement_case):
    case = settlement_case
    record(case)
    with pytest.raises(BusinessLogicError) as error:
        record(case, "payout", amount=Decimal("1001"))
    assert error.value.code == "settlement_advance_confirmation"
    payout = record(case, "payout", amount=Decimal("1001"), confirm_advance=True)
    assert summary(case)["balance"] == -1
    with pytest.raises(BusinessLogicError):
        record(case, "payout_reversal", reversal_of_id=payout.id, effective_on=case[3] - timedelta(days=1))
    with pytest.raises(BusinessLogicError):
        record(case, "payout", effective_on=club_localdate(case[0]) + timedelta(days=1))


def test_historical_change_blocks_payout_until_linked_resolution(settlement_case, settings):
    case = settlement_case
    opening = record(case)
    event = dict(
        club_id=case[0].id,
        trainer_id=case[2].id,
        effective_on=case[3] - timedelta(days=1),
        event_key="source:1",
        suggested_delta=Decimal("500"),
        evidence={"id": 1},
    )
    pending = record_historical_settlement_change(**event)
    assert record_historical_settlement_change(**event).id == pending.id
    assert summary(case)["balance"] is None
    with pytest.raises(BusinessLogicError) as error:
        record(case, "payout")
    assert error.value.code == "settlement_reconciliation_required"
    settings.TRAINER_SETTLEMENTS_ENABLED = False
    resolution = resolve_trainer_settlement(
        club_id=case[0].id,
        actor_user_id=case[1].id,
        case_id=pending.id,
        action="adjust_opening",
        reason="Не был учтён",
        source_namespace="test",
        source_key="resolve",
        balance_delta=Decimal("500"),
        effective_on=case[3] + timedelta(days=1),
    )
    assert resolution.correction.opening_id == opening.id
    assert summary(case)["balance"] == 1000
    assert summary(case, case[3] + timedelta(days=1))["balance"] == 1500
    assert TrainerSettlementResolution.objects.count() == 1


def test_baseline_included_resolution_and_late_payout(settlement_case):
    case = settlement_case
    record(case)
    payout = record(case, "payout", effective_on=case[3] - timedelta(days=2), confirm_advance=True)
    pending = TrainerSettlementReconciliation.objects.get(trainer=case[2])
    assert pending.evidence["entry_id"] == payout.id
    args = dict(
        club_id=case[0].id,
        actor_user_id=case[1].id,
        case_id=pending.id,
        action="already_included",
        reason="В начальной сверке уже учтено",
        source_namespace="test",
        source_key="included",
    )
    first = resolve_trainer_settlement(**args)
    assert resolve_trainer_settlement(**args).id == first.id
    assert summary(case)["balance"] == 1000


def test_settlement_evidence_cannot_be_changed_or_deleted(settlement_case):
    entry = record(settlement_case)
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        TrainerSettlementEntry.objects.filter(id=entry.id).update(balance_delta=0)
    with pytest.raises(ValidationError):
        TrainerSettlementEntry.objects.filter(id=entry.id).delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        TrainerSettlementEntry.objects.create(
            club=settlement_case[0],
            trainer=settlement_case[2],
            actor=settlement_case[1],
            kind="payout",
            effective_on=entry.effective_on,
            amount=1,
            balance_delta=1,
            opening=entry,
            source_namespace="test",
            source_key="invalid",
            reason="invalid",
        )


def test_foreign_trainer_and_revoked_actor_replay_are_denied(settlement_case, other_club):
    case = settlement_case
    record(case, source_key="own")
    foreign = TrainerFactory(club=other_club)
    with pytest.raises(BusinessLogicError):
        record(case, trainer_id=foreign.id)
    from apps.clubs.models import ClubMembership

    ClubMembership.objects.filter(club=case[0], user=case[1]).update(is_active=False)
    with pytest.raises(BusinessLogicError) as error:
        record(case, source_key="own")
    assert error.value.code == "actor_not_authorized"


def test_stale_payout_does_not_record_another_fact(settlement_case):
    case = settlement_case
    record(case)
    fingerprint = summary(case)["fingerprint"]
    record(case, "payout")
    with pytest.raises(BusinessLogicError) as error:
        record(case, "payout", expected_fingerprint=fingerprint)
    assert error.value.code == "settlement_stale_preview"
    assert TrainerSettlementEntry.objects.filter(kind="payout").count() == 1


def test_payout_does_not_change_earned_or_pnl_and_close_does_not_block_payout(settlement_case):
    from apps.attendance.tests.factories import CheckinFactory
    from apps.dashboard.services import get_pnl_report
    from apps.trainers.models import TrainerPayrollPeriodClose
    from apps.trainers.services import close_trainer_payroll_period
    from apps.trainers.tests.factories import TrainerEarningFactory

    case = settlement_case
    visit = CheckinFactory(club=case[0], trainer=case[2], date=case[3])
    TrainerEarningFactory(club=case[0], trainer=case[2], checkin=visit, amount=Decimal("500"))
    record(case, balance_delta=0)
    assert summary(case)["earned"] == summary(case)["balance"] == 500
    before = get_pnl_report(club=case[0], date_from=case[3], date_to=case[3])
    close_trainer_payroll_period(
        club_id=case[0].id, period_start=case[3], period_end=case[3], reason="Проверено", actor_user_id=case[1].id
    )
    record(case, "payout", amount=Decimal("500"))
    assert summary(case)["balance"] == 0 and summary(case)["earned"] == 500
    assert get_pnl_report(club=case[0], date_from=case[3], date_to=case[3]) == before
    assert TrainerPayrollPeriodClose.objects.get().salary_total_snapshot == 500


def test_recipient_correction_requires_separate_resolution_for_both_cutoffs(settlement_case):
    from apps.attendance.tests.factories import CheckinFactory
    from apps.trainers.services import correct_trainer_earning
    from apps.trainers.tests.factories import TrainerEarningFactory

    case = settlement_case
    second = TrainerFactory(club=case[0])
    visit = CheckinFactory(club=case[0], trainer=case[2], date=case[3] - timedelta(days=2))
    earning = TrainerEarningFactory(club=case[0], trainer=case[2], checkin=visit, amount=Decimal("500"))
    record(case)
    record(case, trainer_id=second.id, effective_on=case[3] - timedelta(days=1))
    result = correct_trainer_earning(
        club_id=case[0].id,
        earning_id=earning.id,
        target_trainer_id=second.id,
        actor_user_id=case[1].id,
        reason="Получатель исправлен",
        idempotency_key="recipient",
    )
    pending = TrainerSettlementReconciliation.objects.order_by("trainer_id")
    assert pending.count() == 2
    assert {c.suggested_delta for c in pending} == {Decimal("500"), Decimal("-500")}
    first_case = pending.get(trainer=case[2])
    resolve_trainer_settlement(
        club_id=case[0].id,
        actor_user_id=case[1].id,
        case_id=first_case.id,
        action="already_included",
        reason="Учтено",
        source_namespace="test",
        source_key="a",
    )
    assert summary(case)["status"] == "known"
    assert (
        get_trainer_settlement_summary(club=case[0], trainer_id=second.id, date_from=case[3], date_to=case[3])["status"]
        == "needs_reconciliation"
    )
    assert result.debit.payable_amount_delta == -500
    correct_trainer_earning(
        club_id=case[0].id,
        earning_id=earning.id,
        target_trainer_id=second.id,
        actor_user_id=case[1].id,
        reason="Получатель исправлен",
        idempotency_key="recipient",
    )
    assert pending.count() == 2


@pytest.mark.parametrize("basis,rate,expected", [("1000", "50", "500"), ("333.33", "12.34", "41.13")])
def test_real_salary_creation_and_cancellation_create_durable_cases(settlement_case, basis, rate, expected):
    from apps.attendance.models import CheckinCascadeEvent
    from apps.attendance.tasks import calculate_salary, reverse_salary
    from apps.attendance.tests.factories import CheckinFactory
    from apps.billing.tests.factories import SubscriptionFactory
    from apps.trainers.models import TrainerEarning

    case = settlement_case
    record(case)
    visit = CheckinFactory(
        club=case[0], trainer=case[2], date=case[3] - timedelta(days=1), training_type__kind="personal"
    )
    visit.subscription = SubscriptionFactory(club=case[0], student=visit.student, tariff__club=case[0])
    visit.save(update_fields=["subscription"])
    CheckinCascadeEvent.objects.create(
        club=case[0],
        checkin=visit,
        effect="salary",
        expected=True,
        payload={
            "calculation_basis": "checkin_salary_snapshot",
            "snapshot_provenance": "checkin_queue",
            "trainer_id_snapshot": case[2].id,
            "training_type_kind_snapshot": "personal",
            "payout_policy_snapshot": "on_checkin",
            "rate_percent_snapshot": rate,
            "subscription_price_snapshot": basis,
        },
    )
    calculate_salary(visit.id, case[0].id)
    earning = TrainerEarning.objects.get(checkin=visit)
    assert earning.amount == Decimal(expected)
    assert TrainerSettlementReconciliation.objects.get().suggested_delta == Decimal(expected)
    reverse_salary(visit.id, case[0].id)
    assert set(TrainerSettlementReconciliation.objects.values_list("suggested_delta", flat=True)) == {
        Decimal(expected),
        -Decimal(expected),
    }
    reverse_salary(visit.id, case[0].id)
    assert TrainerSettlementReconciliation.objects.count() == 2


def test_pending_salary_blocks_baseline_until_source_processed(settlement_case):
    from apps.attendance.models import CheckinCascadeEvent
    from apps.attendance.tests.factories import CheckinFactory

    case = settlement_case
    visit = CheckinFactory(club=case[0], trainer=case[2], date=case[3] - timedelta(days=1))
    CheckinCascadeEvent.objects.create(club=case[0], checkin=visit, effect="salary", expected=True)
    with pytest.raises(BusinessLogicError) as error:
        record(case)
    assert error.value.code == "settlement_pending_salary"
    assert not TrainerSettlementEntry.objects.exists()


def _observed_club_race(club_id, first_call, second_call):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from django.db import close_old_connections, connection

    from apps.clubs.models import Club

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL row-lock evidence")
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
                    Club.objects.select_for_update(no_key=True).get(id=club_id)
                    locked.set()
                    assert release.wait(15)
                else:
                    waiting.set()
                try:
                    return (first_call if first else second_call)()
                except BusinessLogicError as error:
                    return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, True)
        assert locked.wait(10)
        second = executor.submit(run, False)
        assert waiting.wait(10)
        observed = False
        try:
            deadline = monotonic() + 10
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [pids[True], pids[False]])
                    observed = cursor.fetchone()[0]
                if observed:
                    break
                sleep(0.02)
            assert observed, "Actual losing command did not wait on Club fence"
        finally:
            release.set()
        return first.result(timeout=20), second.result(timeout=20)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mode", ["same_key", "overpayment", "reversal"])
def test_postgres_settlement_commands_serialize_and_revalidate(settlement_case, mode):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL")
    case = settlement_case
    record(case)
    payout = record(case, "payout") if mode == "reversal" else None

    def first():
        if payout:
            return record(case, "payout_reversal", reversal_of_id=payout.id, source_key="reverse-one").id
        return record(case, "payout", amount=Decimal("600"), source_key="first").id

    def second():
        if payout:
            return record(case, "payout_reversal", reversal_of_id=payout.id, source_key="reverse-two").id
        return record(case, "payout", amount=Decimal("600"), source_key="first" if mode == "same_key" else "second").id

    a, b = _observed_club_race(case[0].id, first, second)
    if mode == "same_key":
        assert a == b and summary(case)["balance"] == 400
    elif mode == "overpayment":
        assert b == "settlement_advance_confirmation" and summary(case)["balance"] == 400
    else:
        assert b == "settlement_already_reversed" and summary(case)["balance"] == 1000


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("winner", ["baseline", "correction"])
def test_postgres_baseline_vs_actual_recipient_correction(settlement_case, winner):
    from django.db import connection

    from apps.attendance.tests.factories import CheckinFactory
    from apps.trainers.services import correct_trainer_earning
    from apps.trainers.tests.factories import TrainerEarningFactory

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL")
    case = settlement_case
    second = TrainerFactory(club=case[0])
    visit = CheckinFactory(club=case[0], trainer=case[2], date=case[3] - timedelta(days=2))
    earning = TrainerEarningFactory(club=case[0], trainer=case[2], checkin=visit, amount=Decimal("500"))
    record(case)

    def baseline():
        return record(case, trainer_id=second.id, source_key="second-opening").id

    def correction():
        return correct_trainer_earning(
            club_id=case[0].id,
            actor_user_id=case[1].id,
            earning_id=earning.id,
            target_trainer_id=second.id,
            reason="Сверено",
            idempotency_key="race-correction",
        ).credit.id

    calls = (baseline, correction) if winner == "baseline" else (correction, baseline)
    a, b = _observed_club_race(case[0].id, *calls)
    assert isinstance(a, int) and isinstance(b, int)
    assert TrainerSettlementReconciliation.objects.filter(trainer=case[2]).count() == 1
    assert TrainerSettlementReconciliation.objects.filter(trainer=second).count() == (1 if winner == "baseline" else 0)


@pytest.mark.django_db(transaction=True)
def test_postgres_salary_waiting_on_cancellation_does_not_create_false_earning(recorded_visit, monkeypatch):
    from django.db import connection

    from apps.attendance.models import CheckinCascadeEvent
    from apps.attendance.services.checkin import cancel_checkin
    from apps.attendance.tasks import calculate_salary
    from apps.trainers.models import TrainerEarning

    if connection.vendor != "postgresql":
        pytest.skip("Requires disposable PostgreSQL")
    actor, sub, component, visit = recorded_visit
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    component.trainer_payout_policy_snapshot = "on_checkin"
    component.save()
    CheckinCascadeEvent.objects.create(
        club_id=sub.club_id,
        checkin=visit,
        effect="salary",
        expected=True,
        payload={
            "calculation_basis": "checkin_salary_snapshot",
            "snapshot_provenance": "checkin_queue",
            "trainer_id_snapshot": visit.trainer_id,
            "training_type_kind_snapshot": "personal",
            "payout_policy_snapshot": "on_checkin",
            "rate_percent_snapshot": "50",
            "subscription_price_snapshot": "1000",
        },
    )

    def cancel_first():
        cancel_checkin(club_id=sub.club_id, checkin_id=visit.id, cancelled_by_user_id=actor.id, user_role="owner")
        return "cancelled"

    def salary_second():
        calculate_salary(visit.id, sub.club_id)
        return "processed"

    assert _observed_club_race(sub.club_id, cancel_first, salary_second) == ("cancelled", "processed")
    assert not TrainerEarning.objects.filter(checkin=visit).exists()
    assert not TrainerSettlementReconciliation.objects.filter(club_id=sub.club_id).exists()
