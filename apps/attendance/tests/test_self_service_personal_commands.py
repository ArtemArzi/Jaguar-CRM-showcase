from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import (
    Checkin,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalSelfServiceCommand,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.services.checkin import create_checkin
from apps.attendance.services.enrollment import (
    _assert_self_service_future_entitlement_capacity,
    book_personal_availability_slot,
    book_personal_session,
    cancel_personal_booking,
    cancel_schedule_enrollment,
    close_personal_booking_payment_reservation_for_order,
    confirm_personal_booking_payment_reservation_for_order,
    create_personal_booking_payment_reservation,
    freeze_schedule_enrollment,
    transfer_schedule_enrollment,
)
from apps.attendance.services.self_service import _command_fingerprint, execute_self_service_personal_command
from apps.attendance.tests.factories import CheckinFactory
from apps.billing.models import (
    BankPaymentOrder,
    Payment,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.tests.factories import (
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as auth_params
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _enable(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={"unified_client_journey_enabled": True},
    )


@pytest.fixture
def migration_executor_with_restore():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    yield executor
    MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_personal_self_service_command_0026_migration_adds_durable_binding_model(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    before = [("attendance", "0025_personal_staff_command_attempt_snapshot")]
    after = [("attendance", "0026_personal_self_service_command")]

    executor.migrate(before)
    before_apps = executor.loader.project_state(before).apps
    with pytest.raises(LookupError):
        before_apps.get_model("attendance", "PersonalSelfServiceCommand")

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    after_apps = executor.loader.project_state(after).apps
    command = after_apps.get_model("attendance", "PersonalSelfServiceCommand")
    assert command._meta.get_field("command_key").max_length == 120
    assert command._meta.get_field("command_fingerprint").max_length == 64
    with pytest.raises(FieldDoesNotExist):
        command._meta.get_field("payment_method")
    constraints = {constraint.name: constraint for constraint in command._meta.constraints}
    assert constraints["uniq_personal_self_service_command_key"].fields == ("club", "command_key")
    assert constraints["personal_self_service_command_binding"].condition is not None


def _default_tariff(*, club, training_type, location):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("2000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=tariff.price,
    )
    update_tariff(tariff_id=tariff.id, club_id=club.id, is_personal_booking_default=True)
    return tariff


def _context(*, club, days=7):
    trainer = TrainerFactory(club=club, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    starts_at = (timezone.now() + timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    return trainer, location, training_type, slot


def _coverage(*, club, student, tariff, credits=1):
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=credits,
        scope=tariff.scope,
        location=tariff.location,
    )
    tariff_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=tariff.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=credits,
        scope=tariff.scope,
        location=tariff.location,
        paid_amount_basis=tariff.price,
    )
    component = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=tariff_component,
        training_type=tariff.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=credits,
        credits_left=credits,
        scope=tariff.scope,
        location=tariff.location,
    )
    return subscription, component


def _weekly_coverage(*, club, student, tariff, weekly_limit=1):
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=None,
        scope=tariff.scope,
        location=tariff.location,
    )
    tariff_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=tariff.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT,
        credits_total=None,
        weekly_limit=weekly_limit,
        scope=tariff.scope,
        location=tariff.location,
        paid_amount_basis=tariff.price,
    )
    component = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=tariff_component,
        training_type=tariff.training_type,
        entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT,
        credits_total=None,
        credits_left=None,
        weekly_limit=weekly_limit,
        scope=tariff.scope,
        location=tariff.location,
    )
    return subscription, component


def _legacy_componentless_personal_booking(*, club, student, subscription, slot):
    """Create pre-component append-only evidence without rewriting history."""

    target_date = slot.starts_at.date()
    schedule = Schedule.objects.create(
        club=club,
        day_of_week=target_date.weekday(),
        start_time=slot.starts_at.time(),
        end_time=slot.ends_at.time(),
        group_name="Legacy personal entitlement booking",
        trainer=slot.trainer,
        location=slot.location,
        training_type=slot.training_type,
        one_time_date=target_date,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    )
    ScheduleBookingEvent.objects.create(
        club=club,
        enrollment=enrollment,
        schedule=schedule,
        student=student,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        effective_date=target_date,
        metadata={"subscription_id": subscription.id},
    )
    return enrollment


@pytest.mark.django_db
def test_capability_on_options_and_command_derive_student_entitlement_without_paid_default(
    settings, club, student_user
):
    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff)

    options = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    )
    assert options.status_code == 200
    assert options.json()[0]["capability"] == "can_book"
    assert options.json()[0]["offer_digest"] == ""

    result = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "self-book-1"},
        **auth_params(student_user, club, role="student"),
    )
    assert result.status_code == 201
    assert result.json()["capability"] == "can_book"
    assert result.json()["allowed_actions"] == ["view_booking"]
    assert PersonalSelfServiceCommand.objects.for_club(club).get().entitlement_subscription_id is not None


@pytest.mark.django_db
def test_legacy_componentless_active_booking_reserves_flag_on_capacity_until_dedicated_cancellation(
    settings, club, student_user
):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=1)
    legacy_enrollment = _legacy_componentless_personal_booking(
        club=club,
        student=student,
        subscription=subscription,
        slot=first_slot,
    )
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )

    with pytest.raises(BusinessLogicError) as capacity_exc:
        _assert_self_service_future_entitlement_capacity(
            subscription=subscription,
            component=component,
            starts_at=second_slot.starts_at,
        )
    assert capacity_exc.value.code == "subscription_component_future_capacity_exhausted"
    with pytest.raises(BusinessLogicError) as command_exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=second_slot.id,
            idempotency_key="legacy-componentless-blocked",
            offer_digest=None,
        )
    assert command_exc.value.code == "personal_availability_slot_unavailable"

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=legacy_enrollment.id,
        actor_user_id=student_user.id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        reason="legacy capacity release",
    )
    assert cancelled.event_created is True
    assert execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key="legacy-componentless-after-cancel",
        offer_digest=None,
    ).created is True


@pytest.mark.django_db
def test_legacy_frozen_personal_booking_still_reserves_flag_on_capacity_until_dedicated_cancellation(
    settings, club, student_user
):
    """Historical frozen personal bookings still own their booked visit."""

    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=1)
    legacy_enrollment = _legacy_componentless_personal_booking(
        club=club,
        student=student,
        subscription=subscription,
        slot=first_slot,
    )
    legacy_enrollment.status = ScheduleEnrollment.Status.FROZEN
    legacy_enrollment.save(update_fields=["status"])
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )

    with pytest.raises(BusinessLogicError) as capacity_exc:
        _assert_self_service_future_entitlement_capacity(
            subscription=subscription,
            component=component,
            starts_at=second_slot.starts_at,
        )
    assert capacity_exc.value.code == "subscription_component_future_capacity_exhausted"
    with pytest.raises(BusinessLogicError) as command_exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=second_slot.id,
            idempotency_key="legacy-frozen-componentless-blocked",
            offer_digest=None,
        )
    assert command_exc.value.code == "personal_availability_slot_unavailable"

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=legacy_enrollment.id,
        actor_user_id=student_user.id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        reason="legacy frozen capacity release",
    )
    assert cancelled.event_created is True
    assert execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key="legacy-frozen-componentless-after-cancel",
        offer_digest=None,
    ).created is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    "entitlement_kind",
    [
        TariffComponent.EntitlementKind.FINITE_CREDITS,
        TariffComponent.EntitlementKind.WEEKLY_LIMIT,
    ],
)
def test_component_selection_skips_reserved_preferred_component_for_free_matching_component(
    settings, club, student_user, entitlement_kind
):
    """Location precedence is deterministic, but capacity selects the next usable component."""

    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=1 if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    tariff_component_fields = {
        "entitlement_kind": entitlement_kind,
        "credits_total": 1 if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
        "weekly_limit": 1 if entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT else None,
        "paid_amount_basis": tariff.price,
    }
    subscription_component_fields = {
        "entitlement_kind": entitlement_kind,
        "credits_total": tariff_component_fields["credits_total"],
        "weekly_limit": tariff_component_fields["weekly_limit"],
        "credits_left": 1 if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
    }
    # The free club component deliberately receives the lower id.  The first
    # visit must still use the later location component; the second must skip
    # that now-reserved component and bind the earlier-id club component.
    fallback_tariff_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        scope=Tariff.Scope.CLUB,
        location=None,
        **tariff_component_fields,
    )
    fallback_component = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=fallback_tariff_component,
        training_type=training_type,
        scope=Tariff.Scope.CLUB,
        location=None,
        **subscription_component_fields,
    )
    preferred_tariff_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        scope=Tariff.Scope.LOCATION,
        location=location,
        **tariff_component_fields,
    )
    preferred_component = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=preferred_tariff_component,
        training_type=training_type,
        scope=Tariff.Scope.LOCATION,
        location=location,
        **subscription_component_fields,
    )
    assert fallback_component.id < preferred_component.id
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.ends_at + timedelta(minutes=15),
        ends_at=first_slot.ends_at + timedelta(hours=1, minutes=15),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )

    first = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key=f"component-preferred-{entitlement_kind}",
        offer_digest=None,
    )
    assert first.command.entitlement_component_id == preferred_component.id
    options = client.get(
        f"/personal-availability/self-service/options/?date={second_slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    )
    assert options.status_code == 200
    option = next(item for item in options.json() if item["slot_id"] == second_slot.id)
    assert option["capability"] == "can_book"
    second = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key=f"component-fallback-{entitlement_kind}",
        offer_digest=None,
    )
    assert second.command.entitlement_component_id == fallback_component.id
    assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_pending_matching_subscription_hides_paid_option_and_creates_no_new_command_artifacts(
    settings, club, student_user
):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    tariff = _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )

    options = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    )
    assert options.status_code == 200
    assert options.json() == []
    with pytest.raises(BusinessLogicError) as exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            idempotency_key="pending-subscription-no-new-command",
            offer_digest=None,
        )
    assert exc.value.code == "personal_availability_slot_unavailable"
    assert not PersonalSelfServiceCommand.objects.for_club(club).exists()
    assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
    assert not BankPaymentOrder.objects.for_club(club).exists()


@pytest.mark.django_db
def test_ambiguous_legacy_componentless_booking_fails_closed_for_every_candidate(settings, club, student_user):
    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=1)
    alternate = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=training_type,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            scope=Tariff.Scope.LOCATION,
            location=location,
        ),
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        credits_left=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    legacy_enrollment = _legacy_componentless_personal_booking(
        club=club,
        student=student,
        subscription=subscription,
        slot=slot,
    )

    for candidate in (component, alternate):
        with pytest.raises(BusinessLogicError) as exc:
            _assert_self_service_future_entitlement_capacity(
                subscription=subscription,
                component=candidate,
                starts_at=slot.starts_at + timedelta(days=1),
            )
        assert exc.value.code == "subscription_component_future_capacity_exhausted"
    with pytest.raises(BusinessLogicError) as checkin_exc:
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=legacy_enrollment.schedule_id,
            training_type_id=training_type.id,
            source=Checkin.Source.MANUAL,
            checkin_date=slot.starts_at.date(),
        )
    assert checkin_exc.value.code == "personal_booking_component_evidence_ambiguous"


@pytest.mark.django_db
def test_flag_off_direct_personal_booking_persists_component_evidence_for_flag_on_drain(
    settings, club, student_user
):
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=1)

    direct = book_personal_session(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=first_slot.starts_at,
        ends_at=first_slot.ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        subscription_id=subscription.id,
        actor_user_id=student_user.id,
        idempotency_key="flag-off-direct-evidence",
        availability_slot_id=first_slot.id,
    )
    event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=direct.enrollment)
    assert event.metadata["subscription_id"] == subscription.id
    assert event.metadata["subscription_component_id"] == component.id

    _enable(settings, club)
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    with pytest.raises(BusinessLogicError) as exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=second_slot.id,
            idempotency_key="flag-on-after-direct",
            offer_digest=None,
        )
    assert exc.value.code == "personal_availability_slot_unavailable"


@pytest.mark.django_db
def test_generic_enrollment_actions_cannot_release_exact_personal_booking_capacity(
    settings, club, student_user, owner_user
):
    _enable(settings, club)
    trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    booked = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key="generic-lifecycle-guard",
        offer_digest=None,
    )
    enrollment_id = booked.command.enrollment_id_snapshot
    target_schedule = Schedule.objects.create(
        club=club,
        day_of_week=slot.starts_at.weekday(),
        start_time=time(15, 0),
        end_time=time(16, 0),
        group_name="Forbidden exact-personal transfer target",
        trainer=trainer,
        location=location,
        training_type=training_type,
        one_time_date=slot.starts_at.date(),
    )
    guarded_actions = (
        lambda: cancel_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment_id,
            ends_on=slot.starts_at.date(),
        ),
        lambda: freeze_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment_id,
        ),
        lambda: transfer_schedule_enrollment(
            club_id=club.id,
            enrollment_id=enrollment_id,
            target_schedule_id=target_schedule.id,
            ends_on=slot.starts_at.date(),
        ),
    )
    for action in guarded_actions:
        with pytest.raises(BusinessLogicError) as exc:
            action()
        assert exc.value.code == "personal_booking_use_booking_action"

    for endpoint, payload in (
        (f"/schedules/enrollments/{enrollment_id}/freeze/", None),
        (
            f"/schedules/enrollments/{enrollment_id}/cancel/",
            {"ends_on": slot.starts_at.date().isoformat()},
        ),
    ):
        response = client.post(
            endpoint,
            **({"json": payload} if payload is not None else {}),
            **auth_params(owner_user, club, role="owner"),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "personal_booking_use_booking_action"

    slot.refresh_from_db()
    enrollment = ScheduleEnrollment.objects.for_club(club).get(id=enrollment_id)
    assert enrollment.status == ScheduleEnrollment.Status.ACTIVE
    assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
    assert cancel_personal_booking(
        club_id=club.id,
        enrollment_id=enrollment_id,
        actor_user_id=student_user.id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        reason="dedicated cancellation remains authoritative",
    ).event_created is True
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED


@pytest.mark.django_db
def test_capability_on_no_entitlement_exposes_sbp_only_and_replays_live_order(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)

    options = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    )
    option = options.json()[0]
    assert option["capability"] == "can_pay"
    assert set(option) == {
        "slot_id", "date", "starts_at", "ends_at", "trainer_id", "trainer_name", "location_id",
        "location_name", "training_type_id", "training_type_name", "capability", "offer_tariff_name",
        "offer_price", "offer_digest",
    }

    payload = {"idempotency_key": "self-pay-1", "offer_digest": option["offer_digest"]}
    first = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json=payload,
        **auth_params(student_user, club, role="student"),
    )
    second = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json=payload,
        **auth_params(student_user, club, role="student"),
    )
    assert [first.status_code, second.status_code] == [201, 200]
    assert first.json()["bank_payment_order_id"] == second.json()["bank_payment_order_id"]
    assert first.json()["allowed_actions"] == ["open_bank_payment_order", "cancel_bank_payment_order"]


@pytest.mark.django_db
def test_payment_readiness_hides_can_pay_and_creates_no_command_or_order(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    params = auth_params(student_user, club, role="student")
    offered = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **params,
    ).json()[0]

    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = False
    hidden = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **params,
    )
    assert hidden.status_code == 200
    assert hidden.json() == []
    with pytest.raises(BusinessLogicError) as exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            idempotency_key="readiness-off",
            offer_digest=offered["offer_digest"],
        )
    assert exc.value.code == "personal_availability_slot_unavailable"
    assert not PersonalSelfServiceCommand.objects.for_club(club).exists()
    assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
    assert not BankPaymentOrder.objects.for_club(club).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("entitlement_kind", "weekly_limit"),
    (
        (TariffComponent.EntitlementKind.FINITE_CREDITS, None),
        (TariffComponent.EntitlementKind.WEEKLY_LIMIT, 1),
    ),
)
def test_reserved_entitlement_falls_back_to_actionable_sbp_without_owner_rejection(
    settings, club, student_user, entitlement_kind, weekly_limit
):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    trainer, location, training_type, first_slot = _context(club=club)
    paid_tariff = _default_tariff(club=club, training_type=training_type, location=location)
    entitlement_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1 if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    if weekly_limit is None:
        _coverage(club=club, student=student, tariff=entitlement_tariff, credits=1)
    else:
        _weekly_coverage(club=club, student=student, tariff=entitlement_tariff, weekly_limit=weekly_limit)
        week_start = first_slot.starts_at - timedelta(days=first_slot.starts_at.weekday())
        first_slot.starts_at = week_start
        first_slot.ends_at = week_start + timedelta(hours=1)
        first_slot.save(update_fields=["starts_at", "ends_at", "updated_at"])
    first = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key=f"reserved-{entitlement_kind}-first",
        offer_digest=None,
    )
    assert first.created is True
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    option = client.get(
        f"/personal-availability/self-service/options/?date={second_slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    assert option["capability"] == "can_pay"
    assert option["offer_tariff_name"] == paid_tariff.name
    paid = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key=f"reserved-{entitlement_kind}-sbp",
        offer_digest=option["offer_digest"],
    )
    assert paid.created is True
    assert paid.command.action == PersonalSelfServiceCommand.Action.PAY


@pytest.mark.django_db
def test_self_service_reservation_reads_are_source_scoped_and_conceal_cross_source_details(
    settings, club, student_user, parent_user
):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    trainer, location, training_type, student_slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    child = StudentFactory(
        club=club,
        user=student_user,
        parent_user=parent_user,
        is_child=True,
        status=Student.Status.ACTIVE,
    )
    parent_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=student_slot.starts_at + timedelta(days=1),
        ends_at=student_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    student_option = client.get(
        f"/personal-availability/self-service/options/?date={student_slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    parent_option = client.get(
        f"/personal-availability/self-service/options/?date={parent_slot.starts_at.date().isoformat()}&child_student_id={child.id}",
        **auth_params(parent_user, club, role="parent"),
    ).json()[0]
    student_command = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source=PersonalSelfServiceCommand.Source.STUDENT,
        student_id=child.id,
        slot_id=student_slot.id,
        idempotency_key="student-source-private-reservation",
        offer_digest=student_option["offer_digest"],
    )
    student_reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(
        id=student_command.command.reservation_id_snapshot,
    )
    close_personal_booking_payment_reservation_for_order(
        club_id=club.id,
        order_id=student_reservation.bank_payment_order_id,
        status=PersonalBookingPaymentReservation.Status.CANCELLED,
        reason="prepare independent parent source record",
    )
    student_reservation.subscription.deleted_at = timezone.now()
    student_reservation.subscription.save(update_fields=["deleted_at", "updated_at"])
    parent_command = execute_self_service_personal_command(
        club=club,
        actor_user_id=parent_user.id,
        source=PersonalSelfServiceCommand.Source.PARENT,
        student_id=child.id,
        slot_id=parent_slot.id,
        idempotency_key="parent-source-private-reservation",
        offer_digest=parent_option["offer_digest"],
    )
    student_reservation_id = student_command.command.reservation_id_snapshot
    parent_reservation_id = parent_command.command.reservation_id_snapshot

    student_list = client.get(
        "/personal-availability/payment-reservations/",
        **auth_params(student_user, club, role="student"),
    )
    parent_list = client.get(
        f"/personal-availability/payment-reservations/?child_student_id={child.id}",
        **auth_params(parent_user, club, role="parent"),
    )
    assert [item["id"] for item in student_list.json()] == [student_reservation_id]
    assert [item["id"] for item in parent_list.json()] == [parent_reservation_id]
    student_foreign = client.get(
        f"/personal-availability/payment-reservations/{parent_reservation_id}/",
        **auth_params(student_user, club, role="student"),
    )
    parent_foreign = client.get(
        f"/personal-availability/payment-reservations/{student_reservation_id}/?child_student_id={child.id}",
        **auth_params(parent_user, club, role="parent"),
    )
    assert student_foreign.status_code == 404
    assert parent_foreign.status_code == 404


@pytest.mark.django_db
def test_stale_post_claim_payment_owner_rejection_leaves_no_live_command_shell(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    params = auth_params(student_user, club, role="student")
    offer = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **params,
    ).json()[0]
    from apps.attendance.services import self_service as self_service_service

    original_claim = self_service_service._claim_self_service_command

    def claim_then_withdraw_slot(**kwargs):
        command, created = original_claim(**kwargs)
        PersonalAvailabilitySlot.objects.for_club(club).filter(id=slot.id).update(
            status=PersonalAvailabilitySlot.Status.BLOCKED
        )
        return command, created

    with patch.object(self_service_service, "_claim_self_service_command", side_effect=claim_then_withdraw_slot):
        with pytest.raises(BusinessLogicError):
            execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=slot.id,
                idempotency_key="withdraw-after-claim",
                offer_digest=offer["offer_digest"],
            )
    assert not PersonalSelfServiceCommand.objects.for_club(club).exists()
    assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
    cards = client.get("/personal-availability/self-service/commands/", **params)
    assert cards.status_code == 200
    assert cards.json()["live"] == []


@pytest.mark.django_db
def test_parent_scope_is_exact_live_child_and_actor_scoped(settings, club, parent_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    second_parent = UserFactory()
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    child = StudentFactory(
        club=club,
        parent_user=parent_user,
        is_child=True,
        status=Student.Status.ACTIVE,
    )
    other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}&child_student_id={child.id}",
        **auth_params(parent_user, club, role="parent"),
    ).json()[0]
    created = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={
            "child_student_id": child.id,
            "idempotency_key": "parent-command",
            "offer_digest": option["offer_digest"],
        },
        **auth_params(parent_user, club, role="parent"),
    )
    assert created.status_code == 201
    command_id = created.json()["command_id"]
    hidden = client.get(
        f"/personal-availability/self-service/commands/{command_id}/?child_student_id={child.id}",
        **auth_params(second_parent, club, role="parent"),
    )
    foreign_child = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}&child_student_id={other_child.id}",
        **auth_params(parent_user, club, role="parent"),
    )
    assert hidden.status_code == 404
    assert foreign_child.status_code == 404


@pytest.mark.django_db
def test_parent_child_scope_conceals_deleted_and_other_club_children(settings, club, other_club, parent_user):
    _enable(settings, club)
    _trainer, _location, _training_type, slot = _context(club=club)
    deleted_child = StudentFactory(
        club=club,
        parent_user=parent_user,
        is_child=True,
        deleted_at=timezone.now(),
    )
    foreign_child = StudentFactory(
        club=other_club,
        parent_user=parent_user,
        is_child=True,
    )
    params = auth_params(parent_user, club, role="parent")
    deleted = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}&child_student_id={deleted_child.id}",
        **params,
    )
    other_club_result = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}&child_student_id={foreign_child.id}",
        **params,
    )
    assert deleted.status_code == 404
    assert other_club_result.status_code == 404


@pytest.mark.django_db
def test_terminal_sbp_retry_is_new_command_and_source_scoped_history(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    first = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "retry-1", "offer_digest": option["offer_digest"]},
        **auth_params(student_user, club, role="student"),
    )
    command_id = first.json()["command_id"]
    cancelled = client.post(
        f"/personal-availability/self-service/commands/{command_id}/cancel/",
        json={},
        **auth_params(student_user, club, role="student"),
    )
    assert cancelled.status_code == 200
    assert "retry_bank_payment" in cancelled.json()["allowed_actions"]
    retry_option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    retry = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "retry-2", "offer_digest": retry_option["offer_digest"]},
        **auth_params(student_user, club, role="student"),
    )
    assert retry.status_code == 201, retry.json()
    cards = client.get(
        "/personal-availability/self-service/commands/",
        **auth_params(student_user, club, role="student"),
    ).json()
    assert len(cards["live"]) == 1
    assert len(cards["latest_terminal"]) == 1
    assert PersonalBookingPaymentReservation.objects.for_club(club).filter(student=student).count() == 2


@pytest.mark.django_db
def test_same_key_conflicts_across_student_and_slot(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    other_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=slot.trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(hours=2),
        ends_at=slot.ends_at + timedelta(hours=2),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    first_student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    second_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    first = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=first_student.id,
        slot_id=slot.id,
        idempotency_key="collision",
        offer_digest=option["offer_digest"],
    )
    assert first.created is True
    with pytest.raises(BusinessLogicError) as exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=second_student.id,
            slot_id=other_slot.id,
            idempotency_key="collision",
            offer_digest=option["offer_digest"],
        )
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_same_key_conflicts_when_server_selected_action_changes(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    tariff = _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key="cross-method",
        offer_digest=option["offer_digest"],
    )
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    with pytest.raises(BusinessLogicError) as exc:
        execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            idempotency_key="cross-method",
            offer_digest=None,
        )
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_flag_off_rejects_new_command_but_existing_card_drains(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    created = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "drain", "offer_digest": option["offer_digest"]},
        **auth_params(student_user, club, role="student"),
    )
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    replay = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "drain", "offer_digest": option["offer_digest"]},
        **auth_params(student_user, club, role="student"),
    )
    cards = client.get(
        "/personal-availability/self-service/commands/",
        **auth_params(student_user, club, role="student"),
    )
    cancelled = client.post(
        f"/personal-availability/self-service/commands/{created.json()['command_id']}/cancel/",
        json={},
        **auth_params(student_user, club, role="student"),
    )
    terminal_cards = client.get(
        "/personal-availability/self-service/commands/",
        **auth_params(student_user, club, role="student"),
    )
    assert replay.status_code == 200
    assert cards.status_code == 200
    assert cards.json()["live"][0]["command_id"] == created.json()["command_id"]
    assert cancelled.status_code == 200
    assert terminal_cards.status_code == 200
    assert terminal_cards.json()["live"] == []
    terminal = terminal_cards.json()["latest_terminal"][0]
    assert terminal["command_id"] == created.json()["command_id"]
    assert terminal["status"] == "cancelled"
    assert "retry_bank_payment" not in terminal["allowed_actions"]


@pytest.mark.django_db
def test_capability_on_gates_legacy_personal_book_and_payment_mutations_but_flag_off_preserves_legacy(
    settings, club, student_user
):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    tariff = _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, _component = _coverage(club=club, student=student, tariff=tariff)
    params = auth_params(student_user, club, role="student")

    blocked_book = client.post(
        f"/personal-availability/{slot.id}/book/",
        json={"subscription_id": subscription.id, "idempotency_key": "legacy-book-blocked"},
        **params,
    )
    blocked_payment = client.post(
        f"/personal-availability/{slot.id}/payment-reservations/",
        json={"tariff_id": tariff.id, "idempotency_key": "legacy-payment-blocked"},
        **params,
    )
    assert blocked_book.status_code == 400
    assert blocked_book.json()["code"] == "unified_personal_command_required"
    assert blocked_payment.status_code == 400
    assert blocked_payment.json()["code"] == "unified_personal_command_required"

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    legacy_book = client.post(
        f"/personal-availability/{slot.id}/book/",
        json={"subscription_id": subscription.id, "idempotency_key": "legacy-book-allowed"},
        **params,
    )
    assert legacy_book.status_code == 201, legacy_book.json()

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    drained_book = client.post(
        f"/personal-availability/{slot.id}/book/",
        json={"subscription_id": subscription.id, "idempotency_key": "legacy-book-allowed"},
        **params,
    )
    assert drained_book.status_code == 200, drained_book.json()

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False

    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=slot.trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(days=1),
        ends_at=slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    payment_user = UserFactory()
    StudentFactory(club=club, user=payment_user, status=Student.Status.ACTIVE)
    legacy_payment = client.post(
        f"/personal-availability/{second_slot.id}/payment-reservations/",
        json={"tariff_id": tariff.id, "idempotency_key": "legacy-payment-allowed"},
        **auth_params(payment_user, club, role="student"),
    )
    assert legacy_payment.status_code == 201, legacy_payment.json()


@pytest.mark.django_db
def test_sbp_command_recovers_after_provider_dispatch_before_durable_bind(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    kwargs = {
        "club": club,
        "actor_user_id": student_user.id,
        "source": "student",
        "student_id": student.id,
        "slot_id": slot.id,
        "idempotency_key": "lost-bind-response",
        "offer_digest": option["offer_digest"],
    }

    with patch(
        "apps.attendance.services.self_service._bind_self_service_command",
        side_effect=RuntimeError("lost response after provider dispatch"),
    ), pytest.raises(RuntimeError, match="lost response"):
        execute_self_service_personal_command(**kwargs)

    command = PersonalSelfServiceCommand.objects.for_club(club).get(command_key="lost-bind-response")
    assert command.result_bound_at is None
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(idempotency_key="lost-bind-response")
    assert reservation.bank_payment_order_id is not None
    assert BankPaymentOrder.objects.for_club(club).filter(personal_payment_reservation=reservation).count() == 1

    before_replay = client.get(
        f"/personal-availability/self-service/commands/{command.id}/",
        **auth_params(student_user, club, role="student"),
    )
    assert before_replay.status_code == 200
    assert before_replay.json()["reservation_id"] == reservation.id
    assert before_replay.json()["provider_payment_url"]
    assert before_replay.json()["allowed_actions"] == ["open_bank_payment_order", "cancel_bank_payment_order"]
    command.refresh_from_db()
    assert command.result_bound_at is not None

    recovered = execute_self_service_personal_command(**kwargs)
    assert recovered.created is False
    command.refresh_from_db()
    assert command.reservation_id_snapshot == reservation.id
    assert BankPaymentOrder.objects.for_club(club).filter(personal_payment_reservation=reservation).count() == 1


@pytest.mark.django_db
def test_confirmed_sbp_booking_cancellation_is_terminal_without_refund_or_retry(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    params = auth_params(student_user, club, role="student")
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **params,
    ).json()[0]
    created = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "confirmed-cancel", "offer_digest": option["offer_digest"]},
        **params,
    ).json()
    command = PersonalSelfServiceCommand.objects.for_club(club).get(id=created["command_id"])
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=command.reservation_id_snapshot)
    reservation.payment.status = Payment.Status.CONFIRMED
    reservation.payment.save(update_fields=["status", "updated_at"])
    reservation.subscription.status = Subscription.Status.ACTIVE
    reservation.subscription.expires_at = timezone.now() + timedelta(days=30)
    reservation.subscription.save(update_fields=["status", "expires_at", "updated_at"])
    selected_component = SubscriptionComponent.objects.for_club(club).get(
        subscription_id=reservation.subscription_id,
    )
    reservation.bank_payment_order.status = BankPaymentOrder.Status.APPROVED
    reservation.bank_payment_order.save(update_fields=["status", "updated_at"])
    confirmed = confirm_personal_booking_payment_reservation_for_order(
        club_id=club.id,
        order_id=reservation.bank_payment_order_id,
        actor_user_id=student_user.id,
    )
    assert confirmed is not None
    booking_event = ScheduleBookingEvent.objects.for_club(club).get(
        enrollment_id=confirmed.enrollment_id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
    )
    assert booking_event.metadata["subscription_component_id"] == selected_component.id

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=confirmed.enrollment_id,
        actor_user_id=student_user.id,
        origin="student_self_booking",
        reason="plans changed",
    )
    assert cancelled.event_created is True
    reservation.refresh_from_db()
    confirmed.enrollment.refresh_from_db()
    slot.refresh_from_db()
    assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
    assert confirmed.enrollment.status == ScheduleEnrollment.Status.CANCELLED
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
    assert reservation.subscription.status == Subscription.Status.ACTIVE
    assert PaymentRefundCase.objects.for_club(club).count() == 0

    card = client.get(
        f"/personal-availability/self-service/commands/{command.id}/",
        **params,
    )
    assert card.status_code == 200
    assert card.json()["status"] == PersonalBookingPaymentReservation.Status.CANCELLED
    assert card.json()["slot_id"] is None
    assert card.json()["booking_id"] is None
    assert card.json()["order_status"] == ""
    assert "view_booking" not in card.json()["allowed_actions"]
    assert "retry_bank_payment" not in card.json()["allowed_actions"]


@pytest.mark.django_db
def test_confirmed_sbp_future_booking_reserves_the_selected_entitlement(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    params = auth_params(student_user, club, role="student")
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **params,
    ).json()[0]
    created = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "confirmed-capacity", "offer_digest": option["offer_digest"]},
        **params,
    ).json()
    command = PersonalSelfServiceCommand.objects.for_club(club).get(id=created["command_id"])
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=command.reservation_id_snapshot)
    reservation.payment.status = Payment.Status.CONFIRMED
    reservation.payment.save(update_fields=["status", "updated_at"])
    reservation.subscription.status = Subscription.Status.ACTIVE
    reservation.subscription.expires_at = timezone.now() + timedelta(days=30)
    reservation.subscription.save(update_fields=["status", "expires_at", "updated_at"])
    selected_component = SubscriptionComponent.objects.for_club(club).get(
        subscription_id=reservation.subscription_id,
    )
    reservation.bank_payment_order.status = BankPaymentOrder.Status.APPROVED
    reservation.bank_payment_order.save(update_fields=["status", "updated_at"])
    confirmed = confirm_personal_booking_payment_reservation_for_order(
        club_id=club.id,
        order_id=reservation.bank_payment_order_id,
        actor_user_id=student_user.id,
    )
    assert confirmed is not None
    booking_event = ScheduleBookingEvent.objects.for_club(club).get(
        enrollment_id=confirmed.enrollment_id,
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
    )
    assert booking_event.metadata["subscription_component_id"] == selected_component.id
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(days=1),
        ends_at=slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )

    with transaction.atomic(), pytest.raises(BusinessLogicError) as exc:
        locked_subscription = (
            Subscription.objects.for_club(club)
            .select_for_update()
            .get(id=reservation.subscription_id)
        )
        locked_component = (
            SubscriptionComponent.objects.for_club(club)
            .select_for_update()
            .get(id=selected_component.id)
        )
        _assert_self_service_future_entitlement_capacity(
            subscription=locked_subscription,
            component=locked_component,
            starts_at=second_slot.starts_at,
        )
    assert exc.value.code == "subscription_component_future_capacity_exhausted"


@pytest.mark.django_db
def test_manual_review_command_has_no_retry_or_provider_link(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    created = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "manual-review", "offer_digest": option["offer_digest"]},
        **auth_params(student_user, club, role="student"),
    ).json()
    command = PersonalSelfServiceCommand.objects.for_club(club).get(id=created["command_id"])
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=command.reservation_id_snapshot)
    reservation.status = PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
    reservation.save(update_fields=["status", "updated_at"])
    BankPaymentOrder.objects.for_club(club).filter(id=reservation.bank_payment_order_id).update(
        status=BankPaymentOrder.Status.MANUAL_REVIEW
    )
    card = client.get(
        f"/personal-availability/self-service/commands/{command.id}/",
        **auth_params(student_user, club, role="student"),
    )
    assert card.status_code == 200
    assert card.json()["status"] == "manual_review"
    assert card.json()["provider_payment_url"] == ""
    assert card.json()["allowed_actions"] == []


@pytest.mark.django_db
def test_unbound_entitlement_command_recovers_exact_booking_evidence(settings, club, student_user):
    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff)
    key = "recover-booking-bind"
    booked = book_personal_availability_slot(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        actor_user_id=student_user.id,
        origin="student_self_booking",
        idempotency_key=key,
        reserve_self_service_entitlement=True,
    )
    command = PersonalSelfServiceCommand.objects.for_club(club).create(
        club=club,
        command_key=key,
        command_fingerprint=_command_fingerprint(
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            action="book",
            offer_digest="",
        ),
        actor_id=student_user.id,
        source="student",
        student_id=student.id,
        availability_slot_id=slot.id,
        action="book",
    )

    recovered = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key=key,
        offer_digest=None,
    )
    command.refresh_from_db()
    assert recovered.created is True
    assert command.enrollment_id == booked.enrollment.id
    assert command.entitlement_subscription_id is not None
    assert command.entitlement_component_id is not None


@pytest.mark.django_db
def test_command_detail_recovers_unbound_entitlement_booking_before_replay(settings, club, student_user):
    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff)
    key = "read-recover-booking"
    booked = book_personal_availability_slot(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        actor_user_id=student_user.id,
        origin="student_self_booking",
        idempotency_key=key,
        reserve_self_service_entitlement=True,
    )
    command = PersonalSelfServiceCommand.objects.for_club(club).create(
        club=club,
        command_key=key,
        command_fingerprint=_command_fingerprint(
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            action="book",
            offer_digest="",
        ),
        actor_id=student_user.id,
        source="student",
        student_id=student.id,
        availability_slot_id=slot.id,
        action="book",
    )

    detail = client.get(
        f"/personal-availability/self-service/commands/{command.id}/",
        **auth_params(student_user, club, role="student"),
    )

    assert detail.status_code == 200
    assert detail.json()["booking_id"] == booked.enrollment.id
    assert detail.json()["allowed_actions"] == ["view_booking"]
    command.refresh_from_db()
    assert command.result_bound_at is not None


@pytest.mark.django_db
def test_unbound_sbp_command_recovers_exact_live_reservation(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    key = "recover-payment-bind"
    reservation = create_personal_booking_payment_reservation(
        club_id=club.id,
        student_id=student.id,
        trainer_id=slot.trainer_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        location_id=slot.location_id,
        training_type_id=slot.training_type_id,
        tariff_id=None,
        availability_slot_id=slot.id,
        offer_digest=option["offer_digest"],
        created_by_id=student_user.id,
        source="student",
        idempotency_key=key,
        command_idempotency_key=key,
    )
    command = PersonalSelfServiceCommand.objects.for_club(club).create(
        club=club,
        command_key=key,
        command_fingerprint=_command_fingerprint(
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            action="pay",
            offer_digest=option["offer_digest"],
        ),
        actor_id=student_user.id,
        source="student",
        student_id=student.id,
        availability_slot_id=slot.id,
        action="pay",
        offer_digest=option["offer_digest"],
    )

    recovered = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key=key,
        offer_digest=option["offer_digest"],
    )
    command.refresh_from_db()
    assert recovered.created is True
    assert command.reservation_id_snapshot == reservation.id
    assert command.result_bound_at is not None


@pytest.mark.django_db
def test_same_key_sbp_attachment_handoff_retries_the_existing_owner_once(settings, club, student_user):
    """Model the PostgreSQL loser after the winner committed only reservation evidence."""

    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    key = "attachment-handoff"
    reservation = PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=student,
        trainer=slot.trainer,
        location=location,
        training_type=training_type,
        tariff=tariff,
        availability_slot=slot,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        expires_at=slot.starts_at,
        idempotency_key=key,
        created_by=student_user,
    )
    command = PersonalSelfServiceCommand.objects.for_club(club).create(
        club=club,
        command_key=key,
        command_fingerprint=_command_fingerprint(
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            action="pay",
            offer_digest=option["offer_digest"],
        ),
        actor_id=student_user.id,
        source="student",
        student_id=student.id,
        availability_slot_id=slot.id,
        action="pay",
        offer_digest=option["offer_digest"],
    )
    handoff = BusinessLogicError(
        "Personal payment reservation attachment is in progress; retry the same request",
        code="personal_payment_reservation_attachment_retry",
    )
    with patch(
        "apps.attendance.services.self_service.create_personal_booking_payment_reservation",
        side_effect=[handoff, reservation],
    ) as owner:
        result = execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            idempotency_key=key,
            offer_digest=option["offer_digest"],
        )

    command.refresh_from_db()
    assert owner.call_count == 2
    assert result.created is True
    assert command.reservation_id_snapshot == reservation.id
    assert command.result_bound_at is not None


@pytest.mark.django_db
def test_mini_group_is_not_exposed_or_accepted(settings, club, student_user):
    _enable(settings, club)
    trainer = TrainerFactory(club=club, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=(timezone.now() + timedelta(days=7)).replace(hour=11, minute=0, second=0, microsecond=0),
        ends_at=(timezone.now() + timedelta(days=7)).replace(hour=12, minute=0, second=0, microsecond=0),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    options = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    )
    rejected = client.post(
        f"/personal-availability/self-service/slots/{slot.id}/command/",
        json={"idempotency_key": "mini"},
        **auth_params(student_user, club, role="student"),
    )
    assert options.json() == []
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "personal_self_service_mini_group_forbidden"


@pytest.mark.django_db
def test_weekly_future_capacity_combines_used_and_reserved_and_cancellation_frees_it(
    settings, club, student_user
):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=None,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _weekly_coverage(club=club, student=student, tariff=tariff, weekly_limit=2)
    week_start = first_slot.starts_at - timedelta(days=first_slot.starts_at.weekday())
    first_slot.starts_at = week_start
    first_slot.ends_at = week_start + timedelta(hours=1)
    first_slot.save(update_fields=["starts_at", "ends_at", "updated_at"])
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=week_start + timedelta(days=1),
        ends_at=week_start + timedelta(days=1, hours=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    CheckinFactory(
        club=club,
        student=student,
        training_type=training_type,
        subscription=subscription,
        subscription_component=component,
        date=week_start.date(),
    )
    key = "weekly-unbound-claim"
    command = PersonalSelfServiceCommand.objects.for_club(club).create(
        club=club,
        command_key=key,
        command_fingerprint=_command_fingerprint(
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=first_slot.id,
            action="book",
            offer_digest="",
        ),
        actor_id=student_user.id,
        source="student",
        student_id=student.id,
        availability_slot_id=first_slot.id,
        action="book",
    )

    # A durable claim that never reached an owner has no component/event
    # evidence, so it cannot reserve the remaining weekly place forever.
    booked = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key=key,
        offer_digest=None,
    )
    replay = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key=key,
        offer_digest=None,
    )
    assert booked.created is True
    assert replay.created is False
    assert replay.command.id == command.id

    with transaction.atomic(), pytest.raises(BusinessLogicError) as exc:
        locked_subscription = Subscription.objects.for_club(club).select_for_update().get(id=subscription.id)
        locked_component = SubscriptionComponent.objects.for_club(club).select_for_update().get(id=component.id)
        _assert_self_service_future_entitlement_capacity(
            subscription=locked_subscription,
            component=locked_component,
            starts_at=second_slot.starts_at,
        )
    assert exc.value.code == "subscription_component_future_capacity_exhausted"

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=booked.command.enrollment_id_snapshot,
        actor_user_id=student_user.id,
        origin="student_self_booking",
        reason="weekly capacity release",
    )
    assert cancelled.event_created is True
    with transaction.atomic():
        locked_subscription = Subscription.objects.for_club(club).select_for_update().get(id=subscription.id)
        locked_component = SubscriptionComponent.objects.for_club(club).select_for_update().get(id=component.id)
        _assert_self_service_future_entitlement_capacity(
            subscription=locked_subscription,
            component=locked_component,
            starts_at=second_slot.starts_at,
        )


@pytest.mark.django_db
def test_started_active_booking_keeps_capacity_reserved_until_cancelled(settings, club, student_user):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=2,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=2)
    first = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key="started-active-first",
        offer_digest=None,
    )
    first_enrollment = ScheduleEnrollment.objects.for_club(club).select_related("schedule").get(
        id=first.command.enrollment_id_snapshot
    )
    first_enrollment.schedule.one_time_date = timezone.localdate() - timedelta(days=1)
    first_enrollment.schedule.save(update_fields=["one_time_date", "updated_at"])
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    second = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key="started-active-second",
        offer_digest=None,
    )
    assert second.created is True

    with transaction.atomic(), pytest.raises(BusinessLogicError) as exc:
        locked_subscription = Subscription.objects.for_club(club).select_for_update().get(id=subscription.id)
        locked_component = SubscriptionComponent.objects.for_club(club).select_for_update().get(id=component.id)
        _assert_self_service_future_entitlement_capacity(
            subscription=locked_subscription,
            component=locked_component,
            starts_at=second_slot.starts_at + timedelta(days=1),
        )
    assert exc.value.code == "subscription_component_future_capacity_exhausted"

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=second.command.enrollment_id_snapshot,
        actor_user_id=student_user.id,
        origin="student_self_booking",
        reason="release active started capacity",
    )
    assert cancelled.event_created is True
    with transaction.atomic():
        locked_subscription = Subscription.objects.for_club(club).select_for_update().get(id=subscription.id)
        locked_component = SubscriptionComponent.objects.for_club(club).select_for_update().get(id=component.id)
        _assert_self_service_future_entitlement_capacity(
            subscription=locked_subscription,
            component=locked_component,
            starts_at=second_slot.starts_at + timedelta(days=1),
        )


@pytest.mark.django_db
def test_exact_backdated_checkin_consumes_booking_without_double_reservation(settings, club, student_user):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=2,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    subscription, component = _coverage(club=club, student=student, tariff=tariff, credits=2)
    first = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=first_slot.id,
        idempotency_key="backdated-checkin-first",
        offer_digest=None,
    )
    first_enrollment = ScheduleEnrollment.objects.for_club(club).select_related("schedule").get(
        id=first.command.enrollment_id_snapshot
    )
    first_enrollment.schedule.one_time_date = timezone.localdate() - timedelta(days=1)
    first_enrollment.schedule.save(update_fields=["one_time_date", "updated_at"])
    CheckinFactory(
        club=club,
        student=student,
        schedule=first_enrollment.schedule,
        training_type=training_type,
        subscription=subscription,
        subscription_component=component,
        date=first_enrollment.schedule.one_time_date,
    )
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )

    second = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=second_slot.id,
        idempotency_key="backdated-checkin-second",
        offer_digest=None,
    )

    assert second.created is True


@pytest.mark.django_db
def test_generic_checkin_cannot_consume_credit_reserved_by_active_self_service_booking(
    settings, club, student_user
):
    _enable(settings, club)
    trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    booked = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key="generic-checkin-reservation",
        offer_digest=None,
    )
    generic_schedule = Schedule.objects.create(
        club=club,
        day_of_week=slot.starts_at.weekday(),
        start_time=time(15, 0),
        end_time=time(16, 0),
        group_name="Generic personal capacity check",
        trainer=trainer,
        location=location,
        training_type=training_type,
        one_time_date=slot.starts_at.date(),
    )

    with pytest.raises(BusinessLogicError) as exc:
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=generic_schedule.id,
            training_type_id=training_type.id,
            source="manual",
            checkin_date=slot.starts_at.date(),
        )
    assert exc.value.code == "subscription_component_credits_exhausted"

    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=booked.command.enrollment_id_snapshot,
        actor_user_id=student_user.id,
        origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
        reason="release generic check-in capacity",
    )
    assert cancelled.event_created is True
    cancelled.enrollment.refresh_from_db()
    assert cancelled.enrollment.status == ScheduleEnrollment.Status.CANCELLED
    with patch("apps.attendance.services.async_task"):
        created = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=generic_schedule.id,
            training_type_id=training_type.id,
            source="manual",
            checkin_date=slot.starts_at.date(),
        )
    assert created["created"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL two-connection locks")
def test_postgresql_same_command_key_replays_one_durable_result(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    gate = Barrier(2)

    def submit():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=slot.id,
                idempotency_key="pg-same-command-key",
                offer_digest=option["offer_digest"],
            )
            return "result", result.created, result.command.id
        except BusinessLogicError as exc:
            return "business_error", exc.code, None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: submit(), range(2)))

    assert {outcome[0] for outcome in outcomes} == {"result"}, outcomes
    assert sum(outcome[1] for outcome in outcomes) == 1, outcomes
    assert len({outcome[2] for outcome in outcomes}) == 1
    assert PersonalSelfServiceCommand.objects.for_club(club).count() == 1
    assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL owner-commit visibility")
def test_postgresql_owner_commit_binds_capacity_before_coordinator_returns(settings, club, student_user):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    owner_committed = Event()
    release_coordinator = Event()
    original_owner = book_personal_availability_slot

    def pause_after_owner_commit(**kwargs):
        booking = original_owner(**kwargs)
        if kwargs["idempotency_key"] == "owner-bound-first":
            owner_committed.set()
            assert release_coordinator.wait(timeout=10)
        return booking

    def submit_first():
        close_old_connections()
        try:
            return execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=first_slot.id,
                idempotency_key="owner-bound-first",
                offer_digest=None,
            )
        finally:
            close_old_connections()

    with patch(
        "apps.attendance.services.self_service.book_personal_availability_slot",
        side_effect=pause_after_owner_commit,
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_result = executor.submit(submit_first)
            assert owner_committed.wait(timeout=10)
            command = PersonalSelfServiceCommand.objects.for_club(club).get(command_key="owner-bound-first")
            assert command.result_bound_at is not None
            try:
                with pytest.raises(BusinessLogicError):
                    execute_self_service_personal_command(
                        club=club,
                        actor_user_id=student_user.id,
                        source="student",
                        student_id=student.id,
                        slot_id=second_slot.id,
                        idempotency_key="owner-bound-second",
                        offer_digest=None,
                    )
            finally:
                release_coordinator.set()
            assert first_result.result(timeout=10).created is True

    assert ScheduleEnrollment.objects.for_club(club).filter(status=ScheduleEnrollment.Status.ACTIVE).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL Student/component locks")
def test_postgresql_two_future_slots_do_not_over_reserve_one_component_credit(settings, club, student_user):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    gate = Barrier(2)

    def submit(slot, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=slot.id,
                idempotency_key=key,
                offer_digest=None,
            )
            return "result", result.command.id
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                ((first_slot, "pg-component-one"), (second_slot, "pg-component-two")),
            )
        )

    assert sum(outcome[0] == "result" for outcome in outcomes) == 1, outcomes
    assert {outcome[1] for outcome in outcomes if outcome[0] == "business_error"} == {
        "subscription_component_future_capacity_exhausted"
    }
    assert PersonalSelfServiceCommand.objects.for_club(club).filter(
        action=PersonalSelfServiceCommand.Action.BOOK,
        enrollment__status=ScheduleEnrollment.Status.ACTIVE,
    ).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL Student/component locks")
def test_postgresql_two_future_slots_do_not_over_reserve_one_weekly_component(settings, club, student_user):
    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=None,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _weekly_coverage(club=club, student=student, tariff=tariff, weekly_limit=1)
    week_start = first_slot.starts_at - timedelta(days=first_slot.starts_at.weekday())
    first_slot.starts_at = week_start
    first_slot.ends_at = week_start + timedelta(hours=1)
    first_slot.save(update_fields=["starts_at", "ends_at", "updated_at"])
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=week_start + timedelta(days=1),
        ends_at=week_start + timedelta(days=1, hours=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    gate = Barrier(2)

    def submit(slot, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=slot.id,
                idempotency_key=key,
                offer_digest=None,
            )
            return "result", result.command.id
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                ((first_slot, "pg-week-one"), (second_slot, "pg-week-two")),
            )
        )

    assert sum(outcome[0] == "result" for outcome in outcomes) == 1, outcomes
    assert {outcome[1] for outcome in outcomes if outcome[0] == "business_error"} == {
        "subscription_component_future_capacity_exhausted"
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL shared staff/self entitlement locks")
@pytest.mark.parametrize(
    ("entitlement_kind", "first_owner"),
    (
        (TariffComponent.EntitlementKind.FINITE_CREDITS, "staff"),
        (TariffComponent.EntitlementKind.FINITE_CREDITS, "self"),
        (TariffComponent.EntitlementKind.WEEKLY_LIMIT, "staff"),
        (TariffComponent.EntitlementKind.WEEKLY_LIMIT, "self"),
    ),
)
def test_postgresql_staff_and_self_share_exact_future_entitlement_capacity_in_both_orders(
    settings, club, student_user, entitlement_kind, first_owner
):
    """Staff-first/self-first prove one ledger; cancellation releases it."""

    from apps.attendance.services.staff_intents import submit_staff_personal_intent

    _enable(settings, club)
    trainer, location, training_type, first_slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1 if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS else None,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    if entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
        subscription, _component = _coverage(club=club, student=student, tariff=tariff, credits=1)
    else:
        subscription, _component = _weekly_coverage(club=club, student=student, tariff=tariff, weekly_limit=1)
        week_start = first_slot.starts_at - timedelta(days=first_slot.starts_at.weekday())
        first_slot.starts_at = week_start
        first_slot.ends_at = week_start + timedelta(hours=1)
        first_slot.save(update_fields=["starts_at", "ends_at", "updated_at"])
    second_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=first_slot.starts_at + timedelta(days=1),
        ends_at=first_slot.ends_at + timedelta(days=1),
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    )
    owner = UserFactory()

    def staff_book(slot, key):
        return submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="entitlement",
            subscription_id=subscription.id,
            offer_digest=None,
            idempotency_key=key,
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        ).receipt["enrollment_id"]

    def self_book(slot, key):
        return execute_self_service_personal_command(
            club=club,
            actor_user_id=student_user.id,
            source="student",
            student_id=student.id,
            slot_id=slot.id,
            idempotency_key=key,
            offer_digest=None,
        ).command.enrollment_id_snapshot

    first = staff_book if first_owner == "staff" else self_book
    second = self_book if first_owner == "staff" else staff_book
    first_origin = (
        ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION
        if first_owner == "staff"
        else ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
    )
    first_enrollment_id = first(first_slot, f"pg-{entitlement_kind}-{first_owner}-first")
    second_key = f"pg-{entitlement_kind}-{first_owner}-second"
    second_slot.refresh_from_db()
    assert second_slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
    assert second_slot.booked_enrollment_id is None
    assert not ScheduleBookingEvent.objects.for_club(club).filter(
        metadata__availability_slot_id=second_slot.id,
    ).exists()
    assert not PersonalSelfServiceCommand.objects.for_club(club).filter(
        availability_slot_id=second_slot.id,
    ).exists()
    with pytest.raises(BusinessLogicError) as exc:
        second(second_slot, second_key)
    if first_owner == "staff":
        # The self-service selector deliberately hides a slot with exhausted
        # entitlement when there is no ready paid alternative.  The slot has
        # not changed; assert the authoritative owner ledger separately.
        assert exc.value.code == "personal_availability_slot_unavailable"
        with pytest.raises(BusinessLogicError) as capacity_exc:
            _assert_self_service_future_entitlement_capacity(
                subscription=subscription,
                component=_component,
                starts_at=second_slot.starts_at,
            )
        assert capacity_exc.value.code == "subscription_component_future_capacity_exhausted"
        assert not PersonalSelfServiceCommand.objects.for_club(club).filter(
            command_key=second_key,
        ).exists()
    else:
        assert exc.value.code == "subscription_component_future_capacity_exhausted"
    cancelled = cancel_personal_booking(
        club_id=club.id,
        enrollment_id=first_enrollment_id,
        actor_user_id=owner.id if first_owner == "staff" else student_user.id,
        origin=first_origin,
        reason="shared capacity release",
    )
    assert cancelled.event_created is True
    assert second(second_slot, f"{second_key}-after-cancel")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL D12 overlap locks")
def test_postgresql_self_service_booking_and_generic_entitlement_checkin_do_not_deadlock(settings, club, student_user):
    """Force both consumers onto one Student/component and require one result."""

    _enable(settings, club)
    trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    generic_schedule = Schedule.objects.create(
        club=club,
        day_of_week=slot.starts_at.weekday(),
        start_time=time(15, 0),
        end_time=time(16, 0),
        group_name="PG generic entitlement overlap",
        trainer=trainer,
        location=location,
        training_type=training_type,
        one_time_date=slot.starts_at.date(),
    )
    gate = Barrier(2)

    def submit_book():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return "book", execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=student.id,
                slot_id=slot.id,
                idempotency_key="pg-book-checkin-overlap",
                offer_digest=None,
            ).created
        except BusinessLogicError as exc:
            return "book_error", exc.code
        finally:
            close_old_connections()

    def submit_checkin():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return "checkin", create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=generic_schedule.id,
                training_type_id=training_type.id,
                source="manual",
                checkin_date=slot.starts_at.date(),
            )["created"]
        except BusinessLogicError as exc:
            return "checkin_error", exc.code
        finally:
            close_old_connections()

    with patch("apps.attendance.services.async_task"):
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(submit_book), executor.submit(submit_checkin)]
            outcomes = [outcome.result(timeout=15) for outcome in outcomes]

    assert sum(outcome[0] in {"book", "checkin"} for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] in {"book_error", "checkin_error"} for outcome in outcomes) == 1, outcomes
    assert (
        ScheduleEnrollment.objects.for_club(club)
        .filter(status=ScheduleEnrollment.Status.ACTIVE, created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING)
        .count()
        + Checkin.objects.for_club(club).filter(student=student, schedule=generic_schedule).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL entitlement cancel/check-in locks")
def test_postgresql_entitlement_cancel_and_exact_checkin_share_personal_identity_lock_order(
    settings, club, student_user
):
    _enable(settings, club)
    _trainer, location, training_type, slot = _context(club=club)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=student, tariff=tariff, credits=1)
    booked = execute_self_service_personal_command(
        club=club,
        actor_user_id=student_user.id,
        source="student",
        student_id=student.id,
        slot_id=slot.id,
        idempotency_key="pg-cancel-checkin-booking",
        offer_digest=None,
    )
    enrollment = ScheduleEnrollment.objects.for_club(club).select_related("schedule").get(
        id=booked.command.enrollment_id_snapshot,
    )
    gate = Barrier(2)

    def submit_cancel():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return "cancel", cancel_personal_booking(
                club_id=club.id,
                enrollment_id=enrollment.id,
                actor_user_id=student_user.id,
                origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
                reason="PG exact-checkin overlap",
            ).event_created
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    def submit_checkin():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            return "checkin", create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=enrollment.schedule_id,
                training_type_id=training_type.id,
                source="manual",
                checkin_date=slot.starts_at.date(),
            )["created"]
        except BusinessLogicError as exc:
            return "checkin_error", exc.code
        finally:
            close_old_connections()

    with patch("apps.attendance.services.async_task"):
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(submit_cancel), executor.submit(submit_checkin)]
            outcomes = [outcome.result(timeout=15) for outcome in outcomes]

    assert sum(outcome[0] in {"cancel", "checkin"} for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] in {"cancel_error", "checkin_error"} for outcome in outcomes) == 1, outcomes
    enrollment.refresh_from_db()
    exact_checkins = Checkin.objects.for_club(club).filter(student=student, schedule=enrollment.schedule).count()
    assert (enrollment.status == ScheduleEnrollment.Status.CANCELLED) != bool(exact_checkins)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL provider cleanup lock overlap")
def test_postgresql_provider_failure_cleanup_and_entitlement_booking_share_slot_order(
    settings, club, student_user
):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    entitled_student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    entitlement_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
    )
    subscription, _component = _coverage(
        club=club,
        student=entitled_student,
        tariff=entitlement_tariff,
        credits=1,
    )
    paying_user = UserFactory()
    paying_student = StudentFactory(club=club, user=paying_user, status=Student.Status.ACTIVE)
    option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(paying_user, club, role="student"),
    ).json()[0]
    payment_command = execute_self_service_personal_command(
        club=club,
        actor_user_id=paying_user.id,
        source="student",
        student_id=paying_student.id,
        slot_id=slot.id,
        idempotency_key="pg-provider-cleanup-held-slot",
        offer_digest=option["offer_digest"],
    )
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(
        id=payment_command.command.reservation_id_snapshot,
    )
    gate = Barrier(2)

    def cleanup():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            close_personal_booking_payment_reservation_for_order(
                club_id=club.id,
                order_id=reservation.bank_payment_order_id,
                status=PersonalBookingPaymentReservation.Status.CANCELLED,
                reason="provider failed",
                code="provider_failed",
            )
            return "cleanup"
        finally:
            close_old_connections()

    def entitlement_book():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = book_personal_availability_slot(
                club_id=club.id,
                slot_id=slot.id,
                student_id=entitled_student.id,
                actor_user_id=student_user.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                subscription_id=subscription.id,
                idempotency_key="pg-provider-cleanup-entitlement",
            )
            return "book", result.created
        except BusinessLogicError as exc:
            return "book_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [executor.submit(cleanup), executor.submit(entitlement_book)]
        outcomes = [outcome.result(timeout=15) for outcome in outcomes]

    assert outcomes[0] == "cleanup"
    if outcomes[1][0] == "book_error":
        assert book_personal_availability_slot(
            club_id=club.id,
            slot_id=slot.id,
            student_id=entitled_student.id,
            actor_user_id=student_user.id,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            subscription_id=subscription.id,
            idempotency_key="pg-provider-cleanup-entitlement-retry",
        ).created is True
    else:
        assert outcomes[1] == ("book", True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL personal-slot locks")
def test_postgresql_entitlement_and_sbp_cannot_claim_the_same_personal_slot(settings, club, student_user):
    _enable(settings, club)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    _trainer, location, training_type, slot = _context(club=club)
    _default_tariff(club=club, training_type=training_type, location=location)
    entitlement_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("3000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.LOCATION,
        location=location,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    entitled_student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    _coverage(club=club, student=entitled_student, tariff=entitlement_tariff, credits=1)
    paying_user = UserFactory()
    paying_student = StudentFactory(club=club, user=paying_user, status=Student.Status.ACTIVE)
    offer = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(paying_user, club, role="student"),
    ).json()[0]
    entitlement_option = client.get(
        f"/personal-availability/self-service/options/?date={slot.starts_at.date().isoformat()}",
        **auth_params(student_user, club, role="student"),
    ).json()[0]
    assert offer["capability"] == "can_pay"
    assert entitlement_option["capability"] == "can_book"
    gate = Barrier(2)

    def book_from_entitlement():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = execute_self_service_personal_command(
                club=club,
                actor_user_id=student_user.id,
                source="student",
                student_id=entitled_student.id,
                slot_id=slot.id,
                idempotency_key="pg-entitlement-claim",
                offer_digest=None,
            )
            return "booked", result.command.id
        except BusinessLogicError as exc:
            return "book_error", exc.code
        finally:
            close_old_connections()

    def create_sbp():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            reservation = create_personal_booking_payment_reservation(
                club_id=club.id,
                student_id=paying_student.id,
                trainer_id=slot.trainer_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                location_id=slot.location_id,
                training_type_id=slot.training_type_id,
                tariff_id=None,
                availability_slot_id=slot.id,
                offer_digest=offer["offer_digest"],
                created_by_id=paying_user.id,
                source="student",
                idempotency_key="pg-sbp-claim",
                command_idempotency_key="pg-sbp-claim",
            )
            return "sbp", reservation.id
        except BusinessLogicError as exc:
            return "sbp_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in [
            executor.submit(book_from_entitlement),
            executor.submit(create_sbp),
        ]]

    assert sum(kind in {"booked", "sbp"} for kind, _value in outcomes) == 1, outcomes
    assert not {
        code for kind, code in outcomes if kind.endswith("error") and code in {"deadlock_detected", "database_locked"}
    }
    slot.refresh_from_db()
    assert slot.status in {PersonalAvailabilitySlot.Status.BOOKED, PersonalAvailabilitySlot.Status.HELD}
