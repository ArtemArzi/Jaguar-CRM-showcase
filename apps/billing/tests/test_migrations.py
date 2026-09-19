import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.clubs.models import Club


@pytest.fixture
def migration_executor_with_restore():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    yield executor
    MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_payment_command_identity_0043_adds_fields_and_live_origin_constraints(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0042_tariff_personal_booking_default")]
    after = [("billing", "0043_payment_command_identity_and_live_personal_origins")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    before_payment = before_apps.get_model("billing", "Payment")
    with pytest.raises(FieldDoesNotExist):
        before_payment._meta.get_field("command_idempotency_key")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    after_payment = after_apps.get_model("billing", "Payment")
    after_order = after_apps.get_model("billing", "BankPaymentOrder")

    assert after_payment._meta.get_field("command_idempotency_key").null is True
    assert after_payment._meta.get_field("command_fingerprint").max_length == 64
    payment_constraints = {constraint.name: constraint for constraint in after_payment._meta.constraints}
    assert payment_constraints["uniq_payment_command_idempotency"].condition is not None
    order_constraints = {constraint.name: constraint for constraint in after_order._meta.constraints}
    assert order_constraints["uniq_live_bank_order_personal_reservation"].condition is not None
    assert order_constraints["uniq_live_bank_order_personal_dropin"].condition is not None


@pytest.mark.django_db(transaction=True)
def test_subscription_renewal_0044_adds_event_and_single_live_pending_child(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0043_payment_command_identity_and_live_personal_origins")]
    after = [("billing", "0045_payment_renewal_source_tariff_name_snapshot")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        before_apps.get_model("billing", "SubscriptionRenewalEvent")
    before_subscription = before_apps.get_model("billing", "Subscription")
    assert "uniq_live_pending_subscription_renewal" not in {
        constraint.name for constraint in before_subscription._meta.constraints
    }

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    event = after_apps.get_model("billing", "SubscriptionRenewalEvent")
    subscription = after_apps.get_model("billing", "Subscription")
    payment = after_apps.get_model("billing", "Payment")
    assert event._meta.get_field("carry_snapshot").get_internal_type() == "JSONField"
    assert payment._meta.get_field("renewal_source_tariff_name_snapshot").max_length == 200
    constraints = {constraint.name: constraint for constraint in subscription._meta.constraints}
    assert constraints["uniq_live_pending_subscription_renewal"].condition is not None

    Club = after_apps.get_model("clubs", "Club")
    Student = after_apps.get_model("students", "Student")
    TrainingType = after_apps.get_model("billing", "TrainingType")
    Tariff = after_apps.get_model("billing", "Tariff")
    club = Club.objects.create(name="Renewal migration", city="Perm", disciplines=["boxing"])
    student = Student.objects.create(
        club_id=club.id,
        first_name="Renewal",
        last_name="Source",
        phone="+79000000041",
        status="active",
    )
    training_type = TrainingType.objects.create(
        club_id=club.id,
        name="Renewal type",
        slug="renewal-migration-type",
        kind="group",
    )
    tariff = Tariff.objects.create(
        club_id=club.id,
        training_type_id=training_type.id,
        name="Renewal tariff",
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
        scope="club",
        description="Migration live child constraint",
    )
    source = subscription.objects.create(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        status="active",
        trainings_left=8,
        activated_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=30),
        scope="club",
        paid_amount=Decimal("5000.00"),
    )
    subscription.objects.create(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        status="pending",
        trainings_left=8,
        activated_at=timezone.now(),
        scope="club",
        paid_amount=Decimal("5000.00"),
        renewed_from_id=source.id,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        subscription.objects.create(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            status="pending",
            trainings_left=8,
            activated_at=timezone.now(),
            scope="club",
            paid_amount=Decimal("5000.00"),
            renewed_from_id=source.id,
        )


@pytest.mark.django_db(transaction=True)
def test_subscription_renewal_0044_blocks_preexisting_duplicate_pending_children(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [
        ("billing", "0043_payment_command_identity_and_live_personal_origins"),
        ("students", "0010_student_provenance_and_intake_commands"),
    ]
    after = [
        ("billing", "0044_subscription_renewal_events"),
        ("students", "0010_student_provenance_and_intake_commands"),
    ]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    Club = before_apps.get_model("clubs", "Club")
    Student = before_apps.get_model("students", "Student")
    TrainingType = before_apps.get_model("billing", "TrainingType")
    Tariff = before_apps.get_model("billing", "Tariff")
    Subscription = before_apps.get_model("billing", "Subscription")
    club = Club.objects.create(name="Duplicate renewal migration", city="Perm", disciplines=["boxing"])
    student = Student.objects.create(
        club_id=club.id,
        first_name="Duplicate",
        last_name="Renewal",
        phone="+79000000042",
        status="active",
    )
    training_type = TrainingType.objects.create(
        club_id=club.id,
        name="Duplicate renewal type",
        slug="duplicate-renewal-migration-type",
        kind="group",
    )
    tariff = Tariff.objects.create(
        club_id=club.id,
        training_type_id=training_type.id,
        name="Duplicate renewal tariff",
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
        scope="club",
        description="Duplicate pending migration guard",
    )
    source = Subscription.objects.create(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        status="active",
        trainings_left=8,
        activated_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=30),
        scope="club",
        paid_amount=Decimal("5000.00"),
    )
    children = [
        Subscription.objects.create(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            status="pending",
            trainings_left=8,
            activated_at=timezone.now(),
            scope="club",
            paid_amount=Decimal("5000.00"),
            renewed_from_id=source.id,
        )
        for _ in range(2)
    ]

    with pytest.raises(RuntimeError, match="duplicate live pending subscription renewals"):
        MigrationExecutor(connection).migrate(after)

    # The migration never selects a financial winner.  Once an operator has
    # resolved one exact pending family, the same graph can install the guard.
    Subscription.objects.filter(id=children[-1].id).delete()
    MigrationExecutor(connection).migrate(after)


@pytest.mark.django_db(transaction=True)
def test_personal_staff_command_0024_adds_cross_family_claim_constraint(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("attendance", "0023_personal_service_terms_snapshot")]
    after = [("attendance", "0024_personal_staff_intent_command")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        before_apps.get_model("attendance", "PersonalStaffIntentCommand")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    command = after_apps.get_model("attendance", "PersonalStaffIntentCommand")
    assert command._meta.get_field("command_key").max_length == 120
    assert command._meta.get_field("command_fingerprint").max_length == 64
    constraints = {constraint.name: constraint for constraint in command._meta.constraints}
    assert constraints["uniq_personal_staff_intent_command_key"].fields == ("club", "command_key")


@pytest.mark.django_db(transaction=True)
def test_personal_staff_command_0025_adds_attempt_snapshot_fields(migration_executor_with_restore):
    executor = migration_executor_with_restore
    before = [("attendance", "0024_personal_staff_intent_command")]
    after = [("attendance", "0025_personal_staff_command_attempt_snapshot")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    command_before = before_apps.get_model("attendance", "PersonalStaffIntentCommand")
    with pytest.raises(FieldDoesNotExist):
        command_before._meta.get_field("payment_link_id_snapshot")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    command_after = after_apps.get_model("attendance", "PersonalStaffIntentCommand")
    assert command_after._meta.get_field("payment_link_id_snapshot").null is True
    assert command_after._meta.get_field("result_bound_at").null is True


@pytest.mark.django_db
def test_refund_0028_backfills_only_unambiguous_conversion_enrollment():
    from apps.attendance.models import ScheduleEnrollment
    from apps.attendance.tests.factories import ScheduleFactory
    from apps.billing.models import Payment
    from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory

    migration = importlib.import_module(
        "apps.billing.migrations.0028_backfill_refund_cases_and_conversion_enrollments"
    )
    schedule = ScheduleFactory()
    subscription = SubscriptionFactory(
        club=schedule.club,
        student__club=schedule.club,
        tariff__club=schedule.club,
        tariff__training_type=schedule.training_type,
    )
    payment = PaymentFactory(
        club=schedule.club,
        student=subscription.student,
        tariff=subscription.tariff,
        subscription=subscription,
        status=Payment.Status.CONFIRMED,
        target_schedule=schedule,
        target_start_date=timezone.localdate(),
    )
    exact = ScheduleEnrollment.objects.create(
        club=schedule.club,
        student=subscription.student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=payment.target_start_date,
        created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    )

    ambiguous_schedule = ScheduleFactory(club=schedule.club)
    ambiguous_subscription = SubscriptionFactory(
        club=schedule.club,
        student__club=schedule.club,
        tariff__club=schedule.club,
        tariff__training_type=ambiguous_schedule.training_type,
    )
    ambiguous_payment = PaymentFactory(
        club=schedule.club,
        student=ambiguous_subscription.student,
        tariff=ambiguous_subscription.tariff,
        subscription=ambiguous_subscription,
        status=Payment.Status.CONFIRMED,
        target_schedule=ambiguous_schedule,
        target_start_date=timezone.localdate(),
    )
    for status in [ScheduleEnrollment.Status.ACTIVE, ScheduleEnrollment.Status.CANCELLED]:
        ScheduleEnrollment.objects.create(
            club=schedule.club,
            student=ambiguous_subscription.student,
            schedule=ambiguous_schedule,
            status=status,
            starts_on=ambiguous_payment.target_start_date,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )

    migration.backfill_conversion_enrollments(django_apps, None)
    migration.backfill_conversion_enrollments(django_apps, None)

    payment.refresh_from_db()
    ambiguous_payment.refresh_from_db()
    assert payment.conversion_enrollment_id == exact.id
    assert ambiguous_payment.conversion_enrollment_id is None


@pytest.mark.django_db
def test_refund_0028_merges_legacy_provider_and_review_evidence_into_one_open_case():
    from apps.billing.models import (
        BankPaymentOrder,
        BankPaymentOrderReviewEvent,
        BankPaymentProviderEvent,
        Payment,
        PaymentRefund,
        PaymentRefundCase,
    )
    from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory

    migration = importlib.import_module(
        "apps.billing.migrations.0028_backfill_refund_cases_and_conversion_enrollments"
    )
    subscription = SubscriptionFactory()
    payment = PaymentFactory(
        club=subscription.club,
        student=subscription.student,
        tariff=subscription.tariff,
        subscription=subscription,
        status=Payment.Status.CONFIRMED,
    )
    order = BankPaymentOrder.objects.create(
        club=subscription.club,
        payment=payment,
        subscription=subscription,
        student=subscription.student,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.OWNER,
        status=BankPaymentOrder.Status.REFUNDED,
        amount_snapshot=payment.amount,
        currency="RUB",
        purpose_snapshot="Legacy refund",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=payment.recorded_by,
    )
    provider_event = BankPaymentProviderEvent.objects.create(
        club=subscription.club,
        order=order,
        provider=order.provider,
        event_type="acquiringInternetPayment",
        provider_event_id="legacy-refund-provider-event",
        provider_status="REFUNDED",
        amount_snapshot=None,
        received_at=timezone.now(),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    review_event = BankPaymentOrderReviewEvent.objects.create(
        club=subscription.club,
        order=order,
        actor=payment.recorded_by,
        resolution=BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
        previous_status=BankPaymentOrder.Status.MANUAL_REVIEW,
        new_status=BankPaymentOrder.Status.REFUNDED,
        previous_payment_status=Payment.Status.CONFIRMED,
        new_payment_status=Payment.Status.CONFIRMED,
        previous_subscription_status=subscription.status,
        new_subscription_status=subscription.status,
        reason="Legacy provider-only resolution",
        evidence_metadata={"accounting_effect": "provider_order_only"},
    )

    migration.backfill_refund_cases(django_apps, None)
    migration.backfill_refund_cases(django_apps, None)

    refund_case = PaymentRefundCase.objects.get(order=order)
    assert refund_case.provider_event_id == provider_event.id
    assert refund_case.legacy_review_event_id == review_event.id
    assert refund_case.status == PaymentRefundCase.Status.DETECTED
    assert refund_case.detected_amount == order.amount_snapshot
    assert not PaymentRefund.objects.filter(refund_case=refund_case).exists()


@pytest.mark.django_db
def test_training_group_0022_backfills_one_off_rollout_state_per_existing_club():
    from apps.attendance.models import TrainingGroupRolloutState

    migration = importlib.import_module(
        "apps.attendance.migrations.0022_alter_scheduleenrollment_created_from_traininggroup_and_more"
    )
    first_club = Club.objects.create(name="Legacy Group Club", city="Moscow", disciplines=["boxing"])
    second_club = Club.objects.create(name="Legacy Group Club Two", city="Kazan", disciplines=["mma"])

    migration.create_off_rollout_states(django_apps, None)
    migration.create_off_rollout_states(django_apps, None)

    states = list(
        TrainingGroupRolloutState.objects.order_by("club_id").values_list("club_id", "mode")
    )
    assert (first_club.id, "off") in states
    assert (second_club.id, "off") in states
    assert TrainingGroupRolloutState.objects.filter(club_id__in=[first_club.id, second_club.id]).count() == 2


@pytest.mark.django_db(transaction=True)
def test_tariff_0042_executor_backfills_only_exact_unambiguous_defaults(
    migration_executor_with_restore,
):
    """Exercise 0042 against its historical apps, including bad legacy rows."""

    before = [
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0041_repair_legacy_manual_reconciliation_attempts"),
    ]
    after = [
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0042_tariff_personal_booking_default"),
    ]
    executor = migration_executor_with_restore
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps
    Club = apps.get_model("clubs", "Club")
    Location = apps.get_model("clubs", "Location")
    TrainingType = apps.get_model("billing", "TrainingType")
    Tariff = apps.get_model("billing", "Tariff")
    TariffComponent = apps.get_model("billing", "TariffComponent")

    club = Club.objects.create(
        name="Personal default migration club",
        city="Moscow",
        disciplines=["boxing"],
    )
    location = Location.objects.create(club_id=club.id, name="Legacy malformed location")

    def create_type(slug):
        return TrainingType.objects.create(
            club_id=club.id,
            name=slug,
            slug=slug,
            kind="personal",
            drop_in_price=Decimal("1000.00"),
            trial_free=True,
        )

    def create_complete_tariff(
        *,
        training_type,
        name,
        price=Decimal("1000.00"),
        location_id=None,
        component_name=None,
    ):
        tariff = Tariff.objects.create(
            club_id=club.id,
            training_type_id=training_type.id,
            name=name,
            price=price,
            trainings_limit=1,
            duration_days=30,
            scope="club",
            location_id=location_id,
            is_active=True,
            trainer_payout_policy="on_checkin",
        )
        TariffComponent.objects.create(
            club_id=club.id,
            tariff_id=tariff.id,
            training_type_id=training_type.id,
            name=f"{name} component" if component_name is None else component_name,
            entitlement_kind="finite_credits",
            credits_total=1,
            scope="club",
            trainer_payout_policy="on_checkin",
            paid_amount_basis=price,
            is_active=True,
        )
        return tariff

    exact = create_complete_tariff(training_type=create_type("exact"), name="Exact")
    ambiguous_type = create_type("ambiguous")
    first_ambiguous = create_complete_tariff(training_type=ambiguous_type, name="First ambiguous")
    second_ambiguous = create_complete_tariff(training_type=ambiguous_type, name="Second ambiguous")
    no_match = create_complete_tariff(
        training_type=create_type("no-match"),
        name="No price match",
        price=Decimal("900.00"),
    )
    malformed = create_complete_tariff(
        training_type=create_type("malformed"),
        name="Malformed legacy scope",
        location_id=location.id,
    )
    unnamed_component = create_complete_tariff(
        training_type=create_type("unnamed-component"),
        name="Unnamed component",
        component_name="",
    )
    unnamed_tariff = create_complete_tariff(
        training_type=create_type("unnamed-tariff"),
        name="",
    )
    mixed_ambiguous_type = create_type("mixed-ambiguous-location")
    mixed_club_fallback = create_complete_tariff(
        training_type=mixed_ambiguous_type,
        name="Mixed club fallback",
    )
    mixed_location_first = create_complete_tariff(
        training_type=mixed_ambiguous_type,
        name="Mixed location first",
    )
    mixed_location_second = create_complete_tariff(
        training_type=mixed_ambiguous_type,
        name="Mixed location second",
    )
    for tariff in (mixed_location_first, mixed_location_second):
        tariff.scope = "location"
        tariff.location_id = location.id
        tariff.save(update_fields=["scope", "location_id"])
        TariffComponent.objects.filter(tariff_id=tariff.id).update(
            scope="location",
            location_id=location.id,
        )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    Tariff = after_apps.get_model("billing", "Tariff")
    defaults = {
        tariff.id: tariff.is_personal_booking_default
        for tariff in Tariff.objects.filter(
            id__in=[
                exact.id,
                first_ambiguous.id,
                second_ambiguous.id,
                no_match.id,
                malformed.id,
                unnamed_component.id,
                unnamed_tariff.id,
                mixed_club_fallback.id,
                mixed_location_first.id,
                mixed_location_second.id,
            ]
        )
    }
    assert defaults == {
        exact.id: True,
        first_ambiguous.id: False,
        second_ambiguous.id: False,
        no_match.id: False,
        malformed.id: False,
        unnamed_component.id: False,
        unnamed_tariff.id: False,
        mixed_club_fallback.id: False,
        mixed_location_first.id: False,
        mixed_location_second.id: False,
    }


@pytest.mark.django_db(transaction=True)
def test_personal_terms_0023_executor_keeps_mutated_catalog_intent_legacy_partial(
    migration_executor_with_restore,
):
    """Existing rows lack all immutable D7 fields, even with a matching tariff."""

    before = [
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0042_tariff_personal_booking_default"),
    ]
    after = [
        ("attendance", "0023_personal_service_terms_snapshot"),
        ("billing", "0042_tariff_personal_booking_default"),
    ]
    executor = migration_executor_with_restore
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps
    Club = apps.get_model("clubs", "Club")
    Location = apps.get_model("clubs", "Location")
    TrainingType = apps.get_model("billing", "TrainingType")
    Tariff = apps.get_model("billing", "Tariff")
    Trainer = apps.get_model("trainers", "Trainer")
    User = apps.get_model("auth", "User")
    reservation_model = apps.get_model("attendance", "PersonalBookingPaymentReservation")

    club = Club.objects.create(name="Terms migration club", city="Kazan", disciplines=["mma"])
    location = Location.objects.create(club_id=club.id, name="Terms location")
    training_type = TrainingType.objects.create(
        club_id=club.id,
        name="Historical personal",
        slug="historical-personal",
        kind="personal",
        drop_in_price=Decimal("1000.00"),
        trial_free=True,
    )
    tariff = Tariff.objects.create(
        club_id=club.id,
        training_type_id=training_type.id,
        name="Mutable historical tariff",
        price=Decimal("1000.00"),
        trainings_limit=1,
        duration_days=30,
        scope="location",
        location_id=location.id,
        is_active=True,
        is_personal_booking_default=True,
        trainer_payout_policy="on_checkin",
    )
    user = User.objects.create(username="terms-migration-user", password="!")
    # Student's own later migration remains applied while this fixture moves
    # only billing/attendance.  Seed it through the current model so its
    # mandatory provenance columns are supplied by their model default.
    from apps.students.models import Student as CurrentStudent

    student = CurrentStudent.objects.create(
        club_id=club.id,
        first_name="Legacy",
        last_name="Student",
        phone="+79990001122",
        status="active",
    )
    trainer = Trainer.objects.create(
        club_id=club.id,
        first_name="Legacy",
        last_name="Trainer",
        phone="",
    )
    reservation = reservation_model.objects.create(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
        tariff_id=tariff.id,
        starts_at=timezone.now() + timedelta(days=2),
        ends_at=timezone.now() + timedelta(days=2, hours=1),
        expires_at=timezone.now() + timedelta(hours=1),
        created_by_id=user.id,
    )
    # This would have been wrongly copied into a complete snapshot by the
    # rejected mutable-catalog backfill design.
    tariff.duration_days = 999
    tariff.save(update_fields=["duration_days", "updated_at"])

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    terms_model = after_apps.get_model("attendance", "PersonalServiceTermsSnapshot")
    terms = terms_model.objects.get(reservation_id=reservation.id)
    assert terms.terms_version == "legacy_partial"
    assert terms.duration_days is None


@pytest.mark.django_db(transaction=True)
def test_training_group_0022_postgresql_migration_rehearsal_backfills_existing_clubs(
    migration_executor_with_restore,
):
    """Rehearse the real 0022 schema/data migration from its immediate predecessor state."""

    executor = migration_executor_with_restore
    before = [
        ("attendance", "0021_personal_drop_in_booking"),
        ("billing", "0029_debt_required_tariff"),
    ]
    after = [
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0030_bankpaymentproviderevent_normalized_status_snapshot_and_more"),
    ]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    Club = before_apps.get_model("clubs", "Club")
    first_club = Club.objects.create(
        name="Training Group Migration Rehearsal One",
        city="Moscow",
        disciplines=["boxing"],
    )
    second_club = Club.objects.create(
        name="Training Group Migration Rehearsal Two",
        city="Kazan",
        disciplines=["mma"],
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    TrainingGroupRolloutState = after_apps.get_model("attendance", "TrainingGroupRolloutState")

    assert list(
        TrainingGroupRolloutState.objects.filter(club_id__in=[first_club.id, second_club.id])
        .order_by("club_id")
        .values_list("club_id", "mode")
    ) == [
        (first_club.id, "off"),
        (second_club.id, "off"),
    ]


@pytest.mark.django_db(transaction=True)
def test_subscription_freeze_0025_rejects_duplicate_pending_before_unique_constraint(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [
        ("billing", "0024_bankpaymentorderreviewevent"),
        ("students", "0009_student_guardian_phone"),
    ]
    after = [
        ("billing", "0025_subscriptionfreeze_pending_unique"),
        ("students", "0009_student_guardian_phone"),
    ]

    executor.migrate(before)
    apps = executor.loader.project_state(before).apps
    TrainingType = apps.get_model("billing", "TrainingType")
    Tariff = apps.get_model("billing", "Tariff")
    Subscription = apps.get_model("billing", "Subscription")
    SubscriptionFreeze = apps.get_model("billing", "SubscriptionFreeze")
    historic_student = apps.get_model("students", "Student")

    user_model = get_user_model()
    user = user_model.objects.create_user(username="freeze-migration-owner")
    club = Club.objects.create(name="Migration Club", city="Moscow", disciplines=["boxing"])
    student = historic_student.objects.create(
        club_id=club.id,
        first_name="Freeze",
        last_name="Legacy",
        phone="+79001112233",
        guardian_phone="",
        email="",
        status="active",
        source="other",
    )
    training_type = TrainingType.objects.create(
        club_id=club.id,
        name="Group",
        slug="group",
        kind="group",
    )
    tariff = Tariff.objects.create(
        club_id=club.id,
        training_type=training_type,
        name="Base",
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
        scope="club",
    )
    subscription = Subscription.objects.create(
        club_id=club.id,
        student_id=student.id,
        tariff=tariff,
        status="active",
        paid_amount=Decimal("5000.00"),
        trainings_left=8,
        expires_at=timezone.now() + timedelta(days=30),
        scope="club",
    )
    now = timezone.now()
    keep = SubscriptionFreeze.objects.create(
        club_id=club.id,
        subscription=subscription,
        days=3,
        reason="vacation",
        status="pending",
        frozen_by_id=user.id,
        starts_at=now,
    )
    reject = SubscriptionFreeze.objects.create(
        club_id=club.id,
        subscription=subscription,
        days=4,
        reason="injury",
        status="pending",
        frozen_by_id=user.id,
        starts_at=now + timedelta(seconds=1),
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    apps = executor.loader.project_state(after).apps
    SubscriptionFreeze = apps.get_model("billing", "SubscriptionFreeze")

    assert list(
        SubscriptionFreeze.objects.filter(subscription_id=subscription.id, status="pending").values_list(
            "id",
            flat=True,
        )
    ) == [keep.id]
    reject = SubscriptionFreeze.objects.get(id=reject.id)
    assert reject.status == "rejected"
    assert reject.decision_reason == "Закрыта автоматически перед ограничением дублей pending-заявок"


@pytest.mark.django_db(transaction=True)
def test_provider_webhook_delivery_0031_migrates_forward_and_backward(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0030_bankpaymentproviderevent_normalized_status_snapshot_and_more")]
    after = [("billing", "0031_providerwebhookdelivery")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        before_apps.get_model("billing", "ProviderWebhookDelivery")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    ProviderWebhookDelivery = after_apps.get_model("billing", "ProviderWebhookDelivery")
    delivery = ProviderWebhookDelivery.objects.create(
        provider="tochka",
        payload_hash="a" * 64,
        provider_event_id_hash="b" * 64,
        event_type="acquiringInternetPayment",
        provider_status="APPROVED",
        payment_type="sbp",
        amount_snapshot=Decimal("1.00"),
        received_at=timezone.now(),
        request_id="migration-rehearsal",
        outcome="verified_non_actionable",
    )
    assert delivery.id is not None

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    reverted_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        reverted_apps.get_model("billing", "ProviderWebhookDelivery")


@pytest.mark.django_db(transaction=True)
def test_bank_payment_order_recovery_claim_0036_migrates_forward_and_backward(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0035_bankpaymentorder_creation_absence")]
    after = [("billing", "0036_bankpaymentorder_creation_recovery_claim")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    before_order = before_apps.get_model("billing", "BankPaymentOrder")
    with pytest.raises(FieldDoesNotExist):
        before_order._meta.get_field("creation_recovery_claim_token")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    after_order = after_apps.get_model("billing", "BankPaymentOrder")
    assert after_order._meta.get_field("creation_recovery_claim_token").max_length == 64
    assert after_order._meta.get_field("creation_recovery_claimed_at").null is True

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    reverted_apps = executor.loader.project_state(before).apps
    reverted_order = reverted_apps.get_model("billing", "BankPaymentOrder")
    with pytest.raises(FieldDoesNotExist):
        reverted_order._meta.get_field("creation_recovery_claimed_at")


@pytest.mark.django_db(transaction=True)
def test_bank_payment_reconciliation_0032_backfills_legacy_tochka_evidence(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0031_providerwebhookdelivery")]
    after = [("billing", "0032_bankpaymentreconciliationattempt_and_intent")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    User = before_apps.get_model("auth", "User")
    Club = before_apps.get_model("clubs", "Club")
    Student = before_apps.get_model("students", "Student")
    TrainingType = before_apps.get_model("billing", "TrainingType")
    Tariff = before_apps.get_model("billing", "Tariff")
    Subscription = before_apps.get_model("billing", "Subscription")
    Payment = before_apps.get_model("billing", "Payment")
    BankPaymentOrder = before_apps.get_model("billing", "BankPaymentOrder")

    user = User.objects.create(
        username="migration-0032-owner",
        first_name="Migration",
        last_name="Owner",
        email="migration-0032@example.test",
        password="!",
    )
    club = Club.objects.create(name="Migration 0032 Club", city="Perm", disciplines=["boxing"])
    student = Student.objects.create(
        club_id=club.id,
        first_name="Legacy",
        last_name="Student",
        phone="+79000000000",
        status="active",
    )
    training_type = TrainingType.objects.create(
        club_id=club.id,
        name="Legacy Group",
        slug="legacy-group",
        kind="group",
    )
    tariff = Tariff.objects.create(
        club_id=club.id,
        training_type_id=training_type.id,
        name="Legacy Tariff",
        price=Decimal("5000.00"),
        trainings_limit=8,
        duration_days=30,
        scope="club",
        description="Migration rehearsal",
    )

    def create_order(*, suffix: str, status: str, operation_id: str):
        subscription = Subscription.objects.create(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            status="pending",
            trainings_left=8,
            activated_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
            scope="club",
            paid_amount=Decimal("5000.00"),
        )
        payment = Payment.objects.create(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            subscription_id=subscription.id,
            recorded_by_id=user.id,
            amount=Decimal("5000.00"),
            original_amount=Decimal("5000.00"),
            payment_method="online",
            status="pending",
            rejection_reason="",
        )
        return BankPaymentOrder.objects.create(
            club_id=club.id,
            payment_id=payment.id,
            subscription_id=subscription.id,
            student_id=student.id,
            provider="tochka",
            source="owner",
            status=status,
            amount_snapshot=Decimal("5000.00"),
            purpose_snapshot=f"Legacy {suffix}",
            provider_operation_id=operation_id,
            provider_payment_link_id=f"legacy-link-{suffix}",
            provider_payment_url=f"https://bank.example.test/{suffix}",
            provider_payment_modes=["sbp"],
            expires_at=timezone.now() + timedelta(days=1),
            created_by_id=user.id,
        )

    known = create_order(suffix="known", status="pending", operation_id="operation-known")
    ambiguous = create_order(suffix="ambiguous", status="pending", operation_id="")
    manual = create_order(suffix="manual", status="manual_review", operation_id="")
    manual_known = create_order(
        suffix="manual-known",
        status="manual_review",
        operation_id="operation-manual-known",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    migrated_order_model = after_apps.get_model("billing", "BankPaymentOrder")
    attempt_model = after_apps.get_model("billing", "BankPaymentReconciliationAttempt")

    for order_id in [known.id, ambiguous.id, manual.id, manual_known.id]:
        migrated = migrated_order_model.objects.get(id=order_id)
        assert migrated.link_creation_state == "dispatched"
        assert migrated.link_creation_claimed_at == migrated.created_at
        assert migrated.link_creation_dispatched_at == migrated.created_at

    known_attempt = attempt_model.objects.get(order_id=known.id)
    ambiguous_attempt = attempt_model.objects.get(order_id=ambiguous.id)
    manual_attempt = attempt_model.objects.get(order_id=manual.id)
    manual_known_attempt = attempt_model.objects.get(order_id=manual_known.id)
    assert known_attempt.status == "pending"
    assert known_attempt.retry_at is not None
    assert known_attempt.last_error_code == ""
    assert ambiguous_attempt.status == "retry"
    assert ambiguous_attempt.last_error_code == "tochka_operation_id_missing"
    assert ambiguous_attempt.retry_at is not None
    assert manual_attempt.status == "manual_review"
    assert manual_attempt.last_error_code == "tochka_operation_id_missing"
    assert manual_attempt.retry_at is None
    assert manual_known_attempt.status == "manual_review"
    assert manual_known_attempt.last_error_code == "tochka_legacy_manual_review"
    assert manual_known_attempt.retry_at is None


@pytest.mark.django_db
def test_repair_0041_marks_only_retryable_legacy_manual_attempts():
    from apps.billing.models import (
        BankPaymentOrder,
        BankPaymentReconciliationAttempt,
    )
    from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory

    migration = importlib.import_module(
        "apps.billing.migrations.0041_repair_legacy_manual_reconciliation_attempts"
    )
    def create_attempt(*, operation_id: str, error_code: str):
        subscription = SubscriptionFactory()
        payment = PaymentFactory(
            club=subscription.club,
            student=subscription.student,
            tariff=subscription.tariff,
            subscription=subscription,
            amount=subscription.tariff.price,
            original_amount=subscription.tariff.price,
            payment_method="online",
            status="pending",
        )
        order = BankPaymentOrder.objects.create(
            club=subscription.club,
            payment=payment,
            subscription=subscription,
            student=subscription.student,
            provider="tochka",
            source="owner",
            status="manual_review",
            amount_snapshot=payment.amount,
            purpose_snapshot="Legacy manual review",
            provider_operation_id=operation_id,
            provider_payment_modes=["sbp"],
            expires_at=timezone.now() + timedelta(days=1),
            created_by=payment.recorded_by,
        )
        return BankPaymentReconciliationAttempt.objects.create(
            club=subscription.club,
            order=order,
            status="manual_review",
            last_error_code=error_code,
        )

    retryable = create_attempt(operation_id="operation-legacy", error_code="")
    missing_operation = create_attempt(operation_id="", error_code="")
    explained = create_attempt(
        operation_id="operation-explained",
        error_code="bank_payment_amount_mismatch",
    )

    migration.mark_retryable_legacy_manual_attempts(django_apps, None)

    retryable.refresh_from_db()
    missing_operation.refresh_from_db()
    explained.refresh_from_db()
    assert retryable.last_error_code == "tochka_legacy_manual_review"
    assert missing_operation.last_error_code == ""
    assert explained.last_error_code == "bank_payment_amount_mismatch"


@pytest.mark.django_db(transaction=True)
def test_payment_provider_readiness_snapshot_0037_migrates_forward_and_backward(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("billing", "0036_bankpaymentorder_creation_recovery_claim")]
    after = [("billing", "0037_paymentproviderreadinesssnapshot")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        before_apps.get_model("billing", "PaymentProviderReadinessSnapshot")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    snapshot_model = after_apps.get_model("billing", "PaymentProviderReadinessSnapshot")
    checked_at = timezone.now()
    snapshot = snapshot_model.objects.create(
        provider="tochka",
        customer_code_hash="c" * 64,
        merchant_id_hash="m" * 64,
        retailer_status="REG",
        is_active=True,
        payment_modes=["sbp"],
        cashbox_ready=False,
        checked_at=checked_at,
        expires_at=checked_at + timedelta(hours=1),
    )
    assert snapshot.id is not None

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    reverted_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        reverted_apps.get_model("billing", "PaymentProviderReadinessSnapshot")
