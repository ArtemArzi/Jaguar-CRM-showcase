from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing.models import OpeningEntitlementSnapshot, Payment, Subscription, Tariff
from apps.billing.service_modules.opening_subscriptions import issue_opening_entitlement, preview_opening_entitlement
from apps.billing.service_modules.opening_terms import OpeningEntitlementTerms
from apps.billing.tests.factories import TariffFactory
from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarning, TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def command(club, settings):
    from datetime import datetime, time

    from apps.attendance.tests.factories import ScheduleFactory
    from apps.clubs.timezones import club_localdate, club_zoneinfo

    settings.STUDENT_OPENING_IMPORT_ENABLED = True
    actor = UserFactory()
    ClubMembership.objects.create(club=club, user=actor, role="owner")
    owner = TrainerFactory(club=club)
    tariff = TariffFactory(club=club, training_type__club=club, training_type__kind="personal")
    now = timezone.now()
    cutover_day = club_localdate(club) + timedelta(days=1)
    cutover_schedule = ScheduleFactory(
        club=club, training_type=tariff.training_type, one_time_date=cutover_day,
        day_of_week=cutover_day.weekday(), start_time=time(18), end_time=time(19),
    )
    terms = OpeningEntitlementTerms(
        source_namespace="synthetic", student_source_key="pupil", entitlement_source_key="package",
        payment_source_key="payment", first_name="Synthetic", last_name="Opening", is_child=False,
        phone="+79990000111", guardian_phone="", date_of_birth=None, tariff_id=tariff.id,
        started_on=(now - timedelta(days=7)).date(), expires_on=(now + timedelta(days=20)).date(),
        effective_on=(now - timedelta(days=7)).date(), covered_through=now - timedelta(days=1),
        operational_cutover=timezone.make_aware(datetime.combine(cutover_day, time(18)), club_zoneinfo(club)),
        original_total=12, original_used=5, original_left=7,
        paid_amount=Decimal("12000.00"), payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        payment_method=Payment.Method.UNKNOWN, external_gap_confirmed=True, past_training_confirmed=True,
        package_owner_trainer_id=owner.id,
        cutover_schedule_id=cutover_schedule.id,
    )
    return actor, terms


def apply(club, actor, terms):
    preview = preview_opening_entitlement(club_id=club.id, actor_user_id=actor.id, terms=terms)
    return issue_opening_entitlement(
        club_id=club.id, actor_user_id=actor.id, terms=terms, preview_fingerprint=preview.fingerprint, channel="test",
    )


@pytest.mark.parametrize("payout_policy", [Tariff.PayoutPolicy.ON_CHECKIN, Tariff.PayoutPolicy.NONE])
def test_atomic_personal_source_original_basis_and_safe_replay(club, command, settings, payout_policy):
    actor, terms = command
    assigned = TrainerFactory(club=club)
    terms = replace(terms, assigned_trainer_id=assigned.id, payout_policy=payout_policy)
    with patch("apps.billing.tasks.create_sale_earning") as sale:
        receipt = apply(club, actor, terms)
        sale.assert_not_called()
    student = receipt.subscription.student
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at and student.crm_entry_kind == Student.CrmEntryKind.EXISTING_STUDENT
    assert student.user_id is None and student.parent_user_id is None
    assert student.assigned_trainer_id == assigned.id
    allocation = TrainerPackageAllocation.objects.for_club(club).get(subscription=receipt.subscription)
    assert allocation.owner_trainer_id == terms.package_owner_trainer_id
    assert allocation.source == "opening"
    assert (allocation.sessions_total_snapshot, allocation.sessions_remaining_snapshot) == (12, 7)
    assert allocation.amount_snapshot == Decimal("12000")
    assert receipt.component.unit_amount_basis_snapshot == Decimal("1000")
    assert not TrainerEarning.objects.for_club(club).exists()
    settings.STUDENT_OPENING_IMPORT_ENABLED = False
    replay = issue_opening_entitlement(
        club_id=club.id, actor_user_id=actor.id, terms=terms, preview_fingerprint="lost-response", channel="cli",
    )
    assert replay.id == receipt.id
    assert Payment.objects.for_club(club).count() == Subscription.objects.for_club(club).count() == 1
    with pytest.raises(BusinessLogicError) as error:
        issue_opening_entitlement(
            club_id=club.id, actor_user_id=actor.id, terms=replace(terms, original_used=4, original_left=8),
            preview_fingerprint="lost-response", channel="cli",
        )
    assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize("status", ["lead", "trial", "active", "at_risk", "churned", "lost"])
@pytest.mark.parametrize("history_only", [False, True])
def test_student_transition_table_preserves_origin_and_assignment(club, command, status, history_only):
    actor, terms = command
    student = StudentFactory(
        club=club, phone=terms.phone, first_name=terms.first_name, last_name=terms.last_name,
        date_of_birth=terms.date_of_birth, status=status, crm_entry_kind="lead_intake",
    )
    original_assigned = student.assigned_trainer_id
    terms = replace(terms, student_id=student.id, confirm_student_transition=True)
    if history_only:
        terms = replace(terms, original_left=0, original_used=12)
    receipt = apply(club, actor, terms)
    student.refresh_from_db()
    expected = status if history_only or status in {"active", "at_risk"} else "active"
    assert student.status == expected
    assert student.crm_entry_kind == "lead_intake" and student.assigned_trainer_id == original_assigned
    assert student.lead_status is None and student.became_student_at
    from apps.students.selectors import get_student_workspace

    assert get_student_workspace(club=club).filter(id=student.id).exists()
    assert receipt.history_only == history_only
    assert receipt.subscription.status == ("expired" if history_only else "active")


def test_failed_allocation_rolls_back_identity_payment_and_receipt(club, command):
    actor, terms = command
    with patch("apps.trainers.services.create_package_allocation_for_subscription", side_effect=RuntimeError("test")):
        with pytest.raises(RuntimeError):
            apply(club, actor, terms)
    assert not Student.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()
    assert not OpeningEntitlementSnapshot.objects.for_club(club).exists()


def test_stale_preview_and_revoked_actor_rejected(club, command):
    actor, terms = command
    preview = preview_opening_entitlement(club_id=club.id, actor_user_id=actor.id, terms=terms)
    tariff = Tariff.objects.for_club(club).get(id=terms.tariff_id)
    tariff.name = "Changed"
    tariff.save()
    with pytest.raises(BusinessLogicError) as error:
        issue_opening_entitlement(club_id=club.id, actor_user_id=actor.id, terms=terms,
                                  preview_fingerprint=preview.fingerprint, channel="test")
    assert error.value.code == "preview_stale"
    receipt = apply(club, actor, terms)
    ClubMembership.objects.filter(club=club, user=actor).update(is_active=False)
    with pytest.raises(BusinessLogicError) as error:
        issue_opening_entitlement(club_id=club.id, actor_user_id=actor.id, terms=terms,
                                  preview_fingerprint=preview.fingerprint, channel="test")
    assert error.value.code == "actor_not_authorized"
    assert OpeningEntitlementSnapshot.objects.for_club(club).count() == 1 and receipt.id


def test_duplicate_financial_candidate_requires_distinct_reference(club, command):
    actor, terms = command
    receipt = apply(club, actor, terms)
    terms = replace(terms, entitlement_source_key="another", payment_source_key="another")
    with pytest.raises(BusinessLogicError) as error:
        apply(club, actor, terms)
    assert error.value.code == "payment_source_needs_review"
    next_receipt = apply(club, actor, replace(terms, distinct_payment_reference="Source receipt 2"))
    assert next_receipt.subscription.student_id == receipt.subscription.student_id
    assert Student.objects.for_club(club).count() == 1


@pytest.mark.parametrize("changes", [
    {"external_gap_confirmed": False}, {"past_training_confirmed": False},
    {"original_left": 8}, {"paid_amount": Decimal("12.001")}, {"payment_method": "online"},
])
def test_source_facts_fail_closed_without_artifacts(club, command, changes):
    actor, terms = command
    with pytest.raises(BusinessLogicError):
        apply(club, actor, replace(terms, **changes))
    assert not Student.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()


def test_lead_without_either_workspace_evidence_remains_invalid(club):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        StudentFactory(club=club, status="lead", lead_status=None, became_student_at=None)


@pytest.fixture
def group_command(club, command, settings):
    from datetime import datetime, time

    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.tests.factories import ScheduleFactory, TrainingGroupFactory
    from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
    from apps.clubs.timezones import club_localdate, club_zoneinfo

    actor, terms = command
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club), mode="active",
    )
    group = TrainingGroupFactory(club=club)
    day = club_localdate(club) + timedelta(days=3)
    schedule = ScheduleFactory(
        club=club, training_group=group, training_type=group.training_type, location=group.location,
        trainer=group.responsible_trainer, day_of_week=day.weekday(), start_time=time(18), end_time=time(19),
    )
    seller = TrainerFactory(club=club)
    tariff = TariffFactory(club=club, training_type=group.training_type)
    terms = replace(
        terms, tariff_id=tariff.id, package_owner_trainer_id=None, payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
        paid_amount=Decimal("6500"), sale_trainer_id=seller.id, sale_rate_percent=Decimal("20"),
        schedule_id=schedule.id, training_group_id=group.id,
        cutover_schedule_id=None,
        operational_cutover=timezone.make_aware(datetime.combine(day, time(18)), club_zoneinfo(club)),
    )
    return actor, terms, group, seller, day


@pytest.mark.parametrize("independent", [False, True])
def test_group_ownership_and_historical_commission(club, group_command, independent):
    from apps.attendance.models import TrainingGroupMembership
    from apps.attendance.tests.factories import TrainingGroupMembershipFactory

    actor, terms, group, seller, day = group_command
    existing = None
    if independent:
        student = StudentFactory(
            club=club, phone=terms.phone, first_name=terms.first_name, last_name=terms.last_name,
            date_of_birth=terms.date_of_birth, status="active",
        )
        existing = TrainingGroupMembershipFactory(
            club=club, student=student, training_group=group, starts_on=day - timedelta(days=14),
        )
        terms = replace(terms, student_id=student.id)
    receipt = apply(club, actor, terms)
    payment = receipt.payment
    membership = TrainingGroupMembership.objects.for_club(club).get(student=receipt.subscription.student)
    if independent:
        assert membership.id == existing.id
        assert membership.authority == "independent" and payment.conversion_group_membership_id is None
    else:
        assert membership.source == "import" and membership.authority == "payment_owned"
        assert payment.conversion_group_membership_id == membership.id
        assert payment.conversion_enrollment_id
    assert payment.target_group_membership_id == membership.id
    assert membership.starts_on == (existing.starts_on if independent else day)
    earning = TrainerEarning.objects.for_club(club).get(payment=payment)
    assert earning.trainer_id == seller.id and earning.amount == Decimal("1300")
    assert payment.sale_trainer_id_snapshot == seller.id
    if independent:
        from apps.attendance.services import create_checkin

        with pytest.raises(BusinessLogicError) as error:
            create_checkin(
                club_id=club.id, student_id=receipt.subscription.student_id,
                schedule_id=terms.schedule_id, training_type_id=group.training_type_id,
                source="kiosk", checkin_date=day - timedelta(days=7),
            )
        assert error.value.code == "opening_attendance_already_covered"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("winner", ["import", "close", "duplicate"])
def test_postgres_import_serializes_with_close_and_replay(club, group_command, winner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from django.db import close_old_connections, connection, transaction

    from apps.clubs.models import Club
    from apps.trainers.models import TrainerPayrollPeriodClose
    from apps.trainers.services import close_trainer_payroll_period

    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections and observed blocking")
    actor, terms, _, _, _ = group_command
    preview = preview_opening_entitlement(club_id=club.id, actor_user_id=actor.id, terms=terms)
    locked, release, waiting = Event(), Event(), Event()
    pids = {}

    def operation(kind):
        if kind == "import":
            return issue_opening_entitlement(
                club_id=club.id, actor_user_id=actor.id, terms=terms,
                preview_fingerprint=preview.fingerprint, channel="test",
            ).id
        return close_trainer_payroll_period(
            club_id=club.id, period_start=terms.effective_on, period_end=terms.effective_on,
            reason="Synthetic concurrent close", actor_user_id=actor.id,
        ).id

    def run(first, kind):
        close_old_connections()
        try:
            with transaction.atomic():
                # PostgreSQL diagnostics are parameterized, with no business SQL.
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids[first] = cursor.fetchone()[0]
                if first:
                    Club.objects.select_for_update().get(id=club.id)
                    locked.set()
                    assert release.wait(15), "Test did not release the leading Club lock"
                else:
                    waiting.set()
                try:
                    return operation(kind)
                except BusinessLogicError as error:
                    return error.code
        finally:
            close_old_connections()

    first_kind = "close" if winner == "close" else "import"
    second_kind = "close" if winner == "import" else "import"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, True, first_kind)
        assert locked.wait(10)
        second = executor.submit(run, False, second_kind)
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
            assert observed, "The losing command did not wait on the winning Club fence"
        finally:
            release.set()
        first_result, second_result = first.result(timeout=15), second.result(timeout=15)
    if winner == "close":
        assert second_result == "payroll_period_closed"
        assert not OpeningEntitlementSnapshot.objects.for_club(club).exists()
        assert not Payment.objects.for_club(club).exists()
        assert TrainerPayrollPeriodClose.objects.for_club(club).get().salary_total_snapshot == 0
    elif winner == "import":
        assert isinstance(first_result, int) and isinstance(second_result, int)
        assert OpeningEntitlementSnapshot.objects.for_club(club).count() == 1
        assert TrainerPayrollPeriodClose.objects.for_club(club).get().salary_total_snapshot == Decimal("1300")
    else:
        assert first_result == second_result
        assert OpeningEntitlementSnapshot.objects.for_club(club).count() == 1
        assert Payment.objects.for_club(club).count() == TrainerEarning.objects.for_club(club).count() == 1


@pytest.mark.parametrize("invalid", ["future_coverage", "future_payment", "future_source_start", "past_cutover"])
def test_personal_source_dates_require_current_review(club, command, invalid):
    actor, terms = command
    now = timezone.now()
    if invalid == "future_coverage":
        terms = replace(terms, covered_through=now + timedelta(hours=1))
    elif invalid == "future_payment":
        terms = replace(terms, effective_on=(now + timedelta(days=3)).date())
    elif invalid == "future_source_start":
        terms = replace(terms, started_on=(now + timedelta(days=1)).date())
    else:
        terms = replace(terms, operational_cutover=now - timedelta(hours=1))
    with pytest.raises(BusinessLogicError):
        apply(club, actor, terms)
    assert not OpeningEntitlementSnapshot.objects.for_club(club).exists()


def test_explicit_unrelated_student_is_not_identity_agreement(club, command):
    actor, terms = command
    other = StudentFactory(club=club, status="active")
    with pytest.raises(BusinessLogicError) as error:
        apply(club, actor, replace(terms, student_id=other.id))
    assert error.value.code == "identity_needs_review"
    assert not Payment.objects.for_club(club).exists()


def test_personal_cutover_requires_exact_occurrence_and_non_crossing_coverage(club, command):
    from datetime import datetime, time

    from apps.attendance.tests.factories import ScheduleFactory
    from apps.clubs.timezones import club_zoneinfo

    actor, terms = command
    with pytest.raises(BusinessLogicError) as error:
        apply(club, actor, replace(terms, operational_cutover=terms.operational_cutover + timedelta(minutes=5)))
    assert error.value.code == "opening_cutover_needs_review"
    cutoff_day = timezone.localtime(terms.covered_through, club_zoneinfo(club)).date()
    # A recurrent anchor has a real past occurrence containing the source cutoff.
    schedule = ScheduleFactory(
        club=club, training_type_id=Tariff.objects.for_club(club).get(id=terms.tariff_id).training_type_id,
        day_of_week=cutoff_day.weekday(), start_time=time(18), end_time=time(19),
    )
    cutoff = timezone.make_aware(datetime.combine(cutoff_day, time(18, 30)), club_zoneinfo(club))
    cutover = timezone.make_aware(datetime.combine(cutoff_day + timedelta(days=7), time(18)), club_zoneinfo(club))
    with pytest.raises(BusinessLogicError) as error:
        apply(club, actor, replace(terms, covered_through=cutoff, operational_cutover=cutover,
                                  cutover_schedule_id=schedule.id))
    assert error.value.code == "opening_cutover_needs_review"
    assert not Payment.objects.for_club(club).exists()


def test_personal_next_visit_pays_actual_trainer_from_original_total(club, command):
    from apps.attendance.models import Schedule
    from apps.attendance.services import create_checkin
    from apps.attendance.tasks import calculate_salary
    from apps.clubs.timezones import club_zoneinfo
    from apps.trainers.tests.factories import TrainerRateFactory

    actor, terms = command
    assigned = TrainerFactory(club=club)
    terms = replace(terms, assigned_trainer_id=assigned.id)
    schedule = Schedule.objects.for_club(club).get(id=terms.cutover_schedule_id)
    assert len({assigned.id, terms.package_owner_trainer_id, schedule.trainer_id}) == 3
    TrainerRateFactory(club=club, trainer=schedule.trainer, location=schedule.location,
                       training_type=schedule.training_type, percent=Decimal("40"))
    receipt = apply(club, actor, terms)
    # The current catalog is deliberately different from the reviewed source.
    Tariff.objects.for_club(club).filter(id=terms.tariff_id).update(price=Decimal("9999"), trainings_limit=3)
    with patch("apps.attendance.services.checkin.async_task"):
        visit = create_checkin(
            club_id=club.id, student_id=receipt.subscription.student_id, schedule_id=schedule.id,
            training_type_id=schedule.training_type_id, source="manual",
            checkin_date=timezone.localtime(terms.operational_cutover, club_zoneinfo(club)).date(),
        )
    calculate_salary(visit["checkin_id"], club.id)
    calculate_salary(visit["checkin_id"], club.id)
    earning = TrainerEarning.objects.for_club(club).get(checkin_id=visit["checkin_id"])
    assert earning.trainer_id == schedule.trainer_id and earning.amount == Decimal("400")
    receipt.component.refresh_from_db()
    assert (receipt.component.credits_used, receipt.component.credits_left) == (6, 6)


@pytest.mark.parametrize("catalog_component_exists", [False, True])
def test_opening_source_renews_through_exact_existing_owner(club, command, catalog_component_exists):
    from apps.billing.service_modules.payment_review import verify_payment
    from apps.billing.service_modules.renewals import create_manual_subscription_renewal

    actor, terms = command
    if catalog_component_exists:
        from apps.billing.tests.factories import TariffComponentFactory

        TariffComponentFactory(tariff_id=terms.tariff_id)
    receipt = apply(club, actor, terms)
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id, student_id=receipt.subscription.student_id,
            renewed_from_subscription_id=receipt.subscription_id,
            payment_method="cash", recorded_by_id=actor.id, command_idempotency_key="opening-renewal",
        )
        verify_payment(club_id=club.id, payment_id=payment.id, verified_by_id=actor.id, action="confirm")
        replay = create_manual_subscription_renewal(
            club_id=club.id, student_id=receipt.subscription.student_id,
            renewed_from_subscription_id=receipt.subscription_id,
            payment_method="cash", recorded_by_id=actor.id, command_idempotency_key="opening-renewal",
        )
        assert replay.id == payment.id
    payment.refresh_from_db()
    receipt.subscription.refresh_from_db()
    assert payment.origin == Payment.Origin.ORDINARY
    assert payment.package_owner_trainer_id == terms.package_owner_trainer_id
    assert payment.subscription.renewed_from_id == receipt.subscription_id
    assert receipt.subscription.status == Subscription.Status.EXPIRED
    # Purchased count stays immutable; both remaining projections include carry.
    child_component = payment.subscription.components.get()
    assert child_component.credits_total == 8 and child_component.credits_left == 15
    assert payment.subscription.trainings_left == 15
    carry = payment.subscription.renewal_finalization_event.carry_snapshot
    assert carry["components"][0]["credits"] == 7
    from datetime import time

    from apps.attendance.services import create_checkin
    from apps.attendance.tests.factories import ScheduleFactory

    historical_day = terms.started_on + timedelta(days=1)
    historical_schedule = ScheduleFactory(
        club=club, training_type=child_component.training_type,
        day_of_week=historical_day.weekday(), start_time=time(10), end_time=time(11),
    )
    with pytest.raises(BusinessLogicError) as error:
        create_checkin(
            club_id=club.id, student_id=receipt.subscription.student_id, schedule_id=historical_schedule.id,
            training_type_id=child_component.training_type_id, source="manual", checkin_date=historical_day,
        )
    assert error.value.code == "opening_attendance_already_covered"
    receipt.refresh_from_db()
    assert receipt.original_left == 7 and receipt.original_used == 5


def test_revised_opening_source_keeps_audited_total_and_payout_snapshot(club, command):
    from apps.billing.service_modules.payment_review import verify_payment
    from apps.billing.service_modules.renewals import create_manual_subscription_renewal
    from apps.billing.service_modules.tariff_revisions import revise_tariff_price
    from apps.billing.tests.factories import TariffComponentFactory

    actor, terms = command
    terms = replace(
        terms,
        original_total=20,
        original_used=6,
        original_left=14,
        payout_policy=Tariff.PayoutPolicy.NONE,
    )
    receipt = apply(club, actor, terms)
    source = receipt.subscription
    source_expiry = source.expires_at
    source_tariff = Tariff.objects.get(id=terms.tariff_id)
    TariffComponentFactory(
        tariff=source_tariff,
        training_type=source_tariff.training_type,
        credits_total=8,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )

    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=terms.tariff_id,
        new_price=Decimal("8500"),
        new_name="Opening successor",
        actor_user_id=actor.id,
        idempotency_key="opening-cross-version-revision",
    )
    with patch("django_q.tasks.async_task"):
        payment = create_manual_subscription_renewal(
            club_id=club.id,
            student_id=source.student_id,
            renewed_from_subscription_id=source.id,
            payment_method="cash",
            recorded_by_id=actor.id,
            command_idempotency_key="opening-cross-version-renewal",
            expected_target_tariff_id=revision.target_tariff_id,
            expected_target_price=revision.target_tariff.price,
        )
        verify_payment(
            club_id=club.id,
            payment_id=payment.id,
            verified_by_id=actor.id,
            action="confirm",
        )

    source.refresh_from_db()
    payment.refresh_from_db()
    child = payment.subscription
    child.refresh_from_db()
    child_component = child.components.get()
    source_component_snapshot = source.components.get()
    assert source_component_snapshot.tariff_component_id is None
    assert source_component_snapshot.credits_total == 20
    assert source_component_snapshot.credits_left == 14
    assert source_component_snapshot.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.NONE
    assert source_component_snapshot.paid_amount_basis_snapshot == terms.paid_amount
    assert payment.amount == Decimal("8500.00")
    assert child_component.credits_total == 8
    assert child_component.credits_left == 22
    assert child.trainings_left == 22
    assert child.expires_at == source_expiry + timedelta(days=30)
    assert source.status == Subscription.Status.EXPIRED


def test_paid_provider_renewal_with_ambiguous_opening_carry_enters_review(club, command, settings):
    import json

    from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent, SubscriptionComponent
    from apps.billing.service_modules.bank_orders import create_bank_payment_order
    from apps.billing.service_modules.provider_events import process_bank_payment_webhook

    actor, terms = command
    settings.DEBUG = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    receipt = apply(club, actor, terms)
    with patch("django_q.tasks.async_task"):
        order = create_bank_payment_order(
            club_id=club.id, student_id=receipt.subscription.student_id, tariff_id=terms.tariff_id,
            source=BankPaymentOrder.Source.OWNER, created_by_id=actor.id,
            package_owner_trainer_id=terms.package_owner_trainer_id,
            renewed_from_subscription_id=receipt.subscription_id,
        )
        # Two otherwise compatible targets must never silently choose one.
        component = order.subscription.components.get()
        SubscriptionComponent.objects.create(
            club=club, subscription=order.subscription, training_type_id=component.training_type_id,
            entitlement_kind=component.entitlement_kind, credits_total=8, credits_left=8,
            paid_amount_basis_snapshot=Decimal("5000"), scope=component.scope,
            location_id=component.location_id, trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps({
                "webhookType": "acquiringInternetPayment", "event_id": "synthetic-opening-carry",
                "status": "APPROVED", "paymentLinkId": order.provider_payment_link_id,
                "operationId": "synthetic-operation", "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            }).encode(),
            headers={}, request_id="synthetic-opening-review",
        )
    order.refresh_from_db()
    event.refresh_from_db()
    receipt.subscription.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert event.processing_status == BankPaymentProviderEvent.ProcessingStatus.FAILED
    assert event.failure_code == "bank_payment_opening_renewal_component_needs_review"
    assert order.payment.status == Payment.Status.PENDING
    assert order.subscription.status == Subscription.Status.PENDING
    assert receipt.subscription.status == Subscription.Status.ACTIVE
    assert not receipt.subscription.renewal_finalization_events.exists()
