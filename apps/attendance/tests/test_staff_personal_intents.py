import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from time import monotonic
from unittest.mock import Mock, patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import (
    Checkin,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalPaymentMethodCorrection,
    PersonalServiceTermsSnapshot,
    PersonalStaffIntentCommand,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.personal_offers import personal_offer_payload
from apps.attendance.services import (
    cancel_checkin,
    cancel_personal_booking,
    confirm_personal_booking_payment_reservation_for_order,
    create_checkin,
)
from apps.attendance.services.drop_in import (
    cancel_personal_drop_in_booking,
    create_personal_drop_in_bank_payment_order,
    create_personal_drop_in_payment,
    mark_personal_drop_in_no_show,
)
from apps.attendance.services.enrollment import create_personal_booking_payment_reservation
from apps.attendance.services.personal_locking import lock_complete_personal_scopes
from apps.attendance.services.personal_payment_corrections import replace_personal_payment_method
from apps.attendance.services.staff_intents import (
    get_personal_commercial_context,
    get_staff_direct_personal_offer,
    submit_staff_direct_personal_intent,
    submit_staff_personal_intent,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    Subscription,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.payment_providers.base import ProviderLinkResult
from apps.billing.service_modules.bank_orders import cancel_bank_payment_order, record_provider_creation_manual_review
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.service_modules.provider_events import process_bank_payment_webhook
from apps.billing.services import create_bank_payment_order
from apps.billing.tests.factories import (
    DiscountFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubMembership, ClubSettings
from apps.clubs.tests.factories import ClubMembershipFactory, LocationFactory, UserFactory
from apps.clubs.timezones import club_zoneinfo
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student
from apps.students.scopes import trainer_manual_operational_admission_scope_filter
from apps.students.selectors import get_cabinet_financial_read_model, is_account_access_eligible
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _complete_default(*, club, training_type, location, price, personal_booking_trainer=None):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=price,
        trainings_limit=1,
        duration_days=30,
        scope=Tariff.Scope.LOCATION,
        location=location,
        personal_booking_trainer=personal_booking_trainer,
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
        paid_amount_basis=price,
    )
    update_tariff(tariff_id=tariff.id, club_id=club.id, is_personal_booking_default=True)
    return tariff


def _slot_and_digest(*, club, trainer, location, training_type):
    starts_at = (timezone.now() + timedelta(days=7)).replace(hour=10, minute=0, second=0, microsecond=0)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    return slot, personal_offer_payload(slot=slot, offer=offer)["offer_digest"]


def _additional_slot_and_digest(*, club, trainer, location, training_type, starts_at):
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    return slot, personal_offer_payload(slot=slot, offer=offer)["offer_digest"]


@pytest.mark.django_db
@pytest.mark.parametrize("payment_method", [Payment.Method.CASH, Payment.Method.TRANSFER])
def test_staff_discounted_fixed_slot_manual_intent_freezes_payable_terms_and_exact_replay(
    settings,
    club,
    payment_method,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1800.00"),
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Staff personal discount",
        discount_type="fixed",
        value=Decimal("300.00"),
    )
    student = StudentFactory(club=club, status="lead")
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=timezone.now() + timedelta(days=8),
        ends_at=timezone.now() + timedelta(days=8, hours=1),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
        discount_id=discount.id,
    )
    preview = personal_offer_payload(slot=slot, offer=offer)
    assert preview["offer_trainer_id"] == trainer.id
    assert preview["offer_base_amount"] == "1800.00"
    assert preview["offer_discount_amount"] == "300.00"
    assert preview["offer_payable_amount"] == "1500.00"

    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method=payment_method,
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        discount_id=discount.id,
        idempotency_key=f"discounted-slot-{payment_method}",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=created.receipt["booking_id"])
    terms = PersonalServiceTermsSnapshot.objects.get(booking=booking)
    payment = Payment.objects.get(id=created.receipt["payment_id"])
    component = payment.subscription.components.get()
    event = ScheduleBookingEvent.objects.get(enrollment=booking.enrollment)

    assert booking.price_snapshot == Decimal("1500.00")
    assert terms.terms_version == PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2
    assert terms.discount_id_snapshot == discount.id
    assert terms.base_amount == Decimal("1800.00")
    assert terms.discount_amount == Decimal("300.00")
    assert terms.payable_amount == Decimal("1500.00")
    assert payment.original_amount == Decimal("1800.00")
    assert payment.amount == payment.subscription.paid_amount == Decimal("1500.00")
    assert component.paid_amount_basis_snapshot == Decimal("1500.00")
    assert set(payment.applied_discounts.values_list("id", flat=True)) == {discount.id}
    assert event.metadata["base_amount"] == "1800.00"
    assert event.metadata["discount_id"] == discount.id
    assert event.metadata["payable_amount"] == "1500.00"
    command = PersonalStaffIntentCommand.objects.for_club(club).get(
        command_key=f"discounted-slot-{payment_method}"
    )
    assert command.command_shape["discount_id"] == discount.id
    assert command.command_shape["offer_digest"] == preview["offer_digest"]

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method=payment_method,
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        discount_id=discount.id,
        idempotency_key=f"discounted-slot-{payment_method}",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["payment_id"] == payment.id
    with pytest.raises(BusinessLogicError) as exc_info:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method=payment_method,
            subscription_id=None,
            offer_digest=preview["offer_digest"],
            discount_id=None,
            idempotency_key=f"discounted-slot-{payment_method}",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )
    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_staff_discounted_direct_sbp_freezes_bank_order_amount(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("2000.00"),
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Direct SBP discount",
        discount_type="percent",
        value=Decimal("25.00"),
    )
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=9)).replace(minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    preview = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        discount_id=discount.id,
    )

    created = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        discount_id=discount.id,
        idempotency_key="discounted-direct-sbp",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    reservation = PersonalBookingPaymentReservation.objects.get(id=created.receipt["reservation_id"])
    terms = PersonalServiceTermsSnapshot.objects.get(reservation=reservation)
    payment = reservation.payment
    component = payment.subscription.components.get()

    assert terms.payable_amount == Decimal("1500.00")
    assert payment.original_amount == Decimal("2000.00")
    assert payment.amount == payment.subscription.paid_amount == Decimal("1500.00")
    assert component.paid_amount_basis_snapshot == Decimal("1500.00")
    assert set(payment.applied_discounts.values_list("id", flat=True)) == {discount.id}
    assert reservation.bank_payment_order.payment_id == payment.id
    assert reservation.bank_payment_order.payment.amount == Decimal("1500.00")
    assert reservation.bank_payment_order.amount_snapshot == Decimal("1500.00")


@pytest.mark.django_db
def test_staff_discounted_direct_pay_at_visit_creates_debt_from_frozen_payable(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1700.00"),
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Visit discount",
        discount_type="fixed",
        value=Decimal("200.00"),
    )
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=10)).replace(minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    preview = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        discount_id=discount.id,
    )
    created = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        discount_id=discount.id,
        idempotency_key="discounted-direct-visit",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=created.receipt["booking_id"])
    checkin = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    debt = Debt.objects.get(checkin_id=checkin["checkin_id"])

    assert booking.price_snapshot == Decimal("1500.00")
    assert debt.tariff_price == Decimal("1500.00")
    assert PersonalServiceTermsSnapshot.objects.get(booking=booking).payable_amount == Decimal("1500.00")


@pytest.mark.django_db
def test_staff_direct_cash_intent_is_digest_bound_and_replays_after_catalog_drift(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1450.00"),
    )
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=8)).replace(hour=11, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    preview = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    assert preview["offer_digest"]

    first = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        idempotency_key="direct-cash-1",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert first.created is True
    assert first.receipt["slot_id"] is None
    payment = Payment.objects.get(id=first.receipt["payment_id"])
    assert payment.amount == Decimal("1450.00")
    terms = PersonalServiceTermsSnapshot.objects.get(booking_id=first.receipt["booking_id"])
    assert terms.terms_version == "complete_v2"
    assert PersonalDropInBooking.objects.filter(idempotency_key="direct-cash-1").exists()

    tariff.is_active = False
    tariff.price = Decimal("9999.00")
    tariff.save(update_fields=["is_active", "price", "updated_at"])
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    replay = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        idempotency_key="direct-cash-1",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["payment_id"] == payment.id


@pytest.mark.django_db
def test_staff_direct_entitlement_uses_club_timezone_and_never_needs_paid_default_on_replay(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone", "updated_at"])
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1350.00"),
    )
    tariff.is_personal_booking_default = False
    tariff.save(update_fields=["is_personal_booking_default", "updated_at"])
    student = StudentFactory(club=club, status="active")
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=1,
        status="active",
        scope=Tariff.Scope.LOCATION,
        location=location,
        expires_at=datetime(2100, 1, 1, tzinfo=ZoneInfo("Asia/Yekaterinburg")),
    )
    starts_at = datetime(2099, 5, 5, 10, 0)
    ends_at = datetime(2099, 5, 5, 11, 0)

    first = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="direct-entitlement-yekaterinburg",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert first.created is True
    assert first.receipt["starts_at"].utcoffset() == timedelta(hours=5)
    claim = PersonalStaffIntentCommand.objects.for_club(club).get(
        command_key="direct-entitlement-yekaterinburg"
    )
    assert claim.enrollment_id_snapshot == first.receipt["enrollment_id"]

    tariff.is_active = False
    tariff.save(update_fields=["is_active", "updated_at"])
    training_type.is_active = False
    training_type.save(update_fields=["is_active", "updated_at"])
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    replay = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="direct-entitlement-yekaterinburg",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["enrollment_id"] == first.receipt["enrollment_id"]

    cancel_personal_booking(
        club_id=club.id,
        enrollment_id=first.receipt["enrollment_id"],
        actor_user_id=owner.id,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        reason="terminal replay evidence",
    )
    terminal_replay = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="direct-entitlement-yekaterinburg",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert terminal_replay.created is False
    assert terminal_replay.receipt["status"] == "cancelled"
    assert terminal_replay.receipt["attempted_at"] == ScheduleBookingEvent.objects.get(
        enrollment_id=first.receipt["enrollment_id"],
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
    ).created_at


@pytest.mark.django_db
def test_staff_direct_offer_rejects_mini_group(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    starts_at = timezone.now() + timedelta(days=8)
    with pytest.raises(BusinessLogicError) as exc_info:
        get_staff_direct_personal_offer(
            club_id=club.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            location_id=location.id,
            training_type_id=training_type.id,
        )
    assert exc_info.value.code == "personal_direct_mini_group_forbidden"


@pytest.mark.django_db
def test_staff_fixed_slot_intent_rejects_mini_group_before_commercial_validation(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    student = StudentFactory(club=club, status="lead")
    starts_at = timezone.now() + timedelta(days=8)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="cash",
            subscription_id=None,
            offer_digest="must-not-reach-offer-validation",
            idempotency_key="fixed-slot-mini-group",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )
    assert exc_info.value.code == "personal_staff_intent_mini_group_forbidden"


@pytest.mark.django_db
def test_staff_direct_routes_are_role_scoped_and_receipt_route_is_live(
    settings,
    club,
    other_club,
    owner_user,
    trainer_user,
    student_user,
    parent_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    trainer = TrainerFactory(club=club, user=trainer_user, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1450.00"))
    student = StudentFactory(club=club, status="lead", assigned_trainer=trainer)
    starts_at = (timezone.now() + timedelta(days=8)).replace(hour=11, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    encoded_starts_at = quote(starts_at.isoformat(), safe="")
    encoded_ends_at = quote(ends_at.isoformat(), safe="")
    query = (
        f"?trainer_id={trainer.id}&starts_at={encoded_starts_at}&ends_at={encoded_ends_at}"
        f"&location_id={location.id}&training_type_id={training_type.id}"
    )
    owner_preview = client.get("/personal-availability/direct-offer/" + query, **make_auth_params(owner_user, club))
    assert owner_preview.status_code == 200
    preview_payload = owner_preview.json()
    assert preview_payload["offer_tariff_name"]
    assert preview_payload["offer_price"] == "1450.00"
    assert "name" not in preview_payload
    digest = preview_payload["offer_digest"]
    trainer_preview = client.get(
        "/personal-availability/direct-offer/" + query,
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert trainer_preview.status_code == 200
    for user, role in ((student_user, "student"), (parent_user, "parent")):
        denied = client.get(
            "/personal-availability/direct-offer/" + query,
            **make_auth_params(user, club, role=role),
        )
        assert denied.status_code == 403

    created = client.post(
        "/personal-availability/staff-intents/direct/",
        json={
            "student_id": student.id,
            "trainer_id": trainer.id,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "location_id": location.id,
            "training_type_id": training_type.id,
            "payment_method": "pay_at_visit",
            "offer_digest": digest,
            "idempotency_key": "direct-route-visit",
        },
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert created.status_code == 201
    receipt = created.json()
    read = client.get(
        receipt["resource_route"].removeprefix("/api"),
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert read.status_code == 200
    assert read.json()["booking_id"] == receipt["booking_id"]

    owner_context = client.get(
        f"/students/{student.id}/commercial-context/",
        **make_auth_params(owner_user, club),
    )
    assert owner_context.status_code == 200
    assert owner_context.json()["attempts"][0]["booking_id"] == receipt["booking_id"]
    trainer_context = client.get(
        f"/students/{student.id}/commercial-context/",
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert trainer_context.status_code == 200
    for user, role in ((student_user, "student"), (parent_user, "parent")):
        denied_context = client.get(
            f"/students/{student.id}/commercial-context/",
            **make_auth_params(user, club, role=role),
        )
        assert denied_context.status_code == 403
    foreign_trainer_user = UserFactory()
    TrainerFactory(club=club, user=foreign_trainer_user, is_active=True)
    foreign_trainer_context = client.get(
        f"/students/{student.id}/commercial-context/",
        **make_auth_params(foreign_trainer_user, club, role="trainer"),
    )
    assert foreign_trainer_context.status_code == 403
    tenant_hidden_context = client.get(
        f"/students/{student.id}/commercial-context/",
        **make_auth_params(owner_user, other_club),
    )
    assert tenant_hidden_context.status_code == 404

    foreign_trainer = TrainerFactory(club=other_club, is_active=True)
    foreign = client.get(
        f"/personal-availability/direct-offer/?trainer_id={foreign_trainer.id}&starts_at={encoded_starts_at}"
        f"&ends_at={encoded_ends_at}&location_id={location.id}&training_type_id={training_type.id}",
        **make_auth_params(owner_user, club),
    )
    assert foreign.status_code in {400, 404}


@pytest.mark.django_db
def test_staff_reservation_receipt_route_is_role_and_tenant_scoped(
    settings,
    club,
    other_club,
    owner_user,
    trainer_user,
    student_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    trainer = TrainerFactory(club=club, user=trainer_user, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1460.00"))
    student = StudentFactory(club=club, status="lead", assigned_trainer=trainer)
    starts_at = (timezone.now() + timedelta(days=8)).replace(hour=15, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]
    result = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="reservation-receipt-scope",
        actor_user_id=owner_user.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    route = result.receipt["resource_route"].removeprefix("/api")
    own = client.get(route, **make_auth_params(trainer_user, club, role="trainer"))
    assert own.status_code == 200
    assert own.json()["id"] == result.receipt["reservation_id"]

    foreign_trainer_user = UserFactory()
    TrainerFactory(club=club, user=foreign_trainer_user, is_active=True)
    hidden = client.get(route, **make_auth_params(foreign_trainer_user, club, role="trainer"))
    assert hidden.status_code == 404
    denied = client.get(route, **make_auth_params(student_user, club, role="student"))
    assert denied.status_code == 404
    foreign_club = client.get(route, **make_auth_params(owner_user, other_club))
    assert foreign_club.status_code == 404


@pytest.mark.django_db
def test_staff_cash_intent_freezes_terms_and_replays_or_conflicts(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=Decimal("1450.00"),
    )
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-cash-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )

    payment = Payment.objects.get(id=first.receipt["payment_id"])
    component = payment.subscription.components.get()
    assert first.receipt["amount"] == "1450.00"
    assert payment.amount == Decimal("1450.00")
    assert payment.command_idempotency_key == "staff-cash-1"
    assert payment.command_fingerprint
    assert component.credits_total == component.credits_left == 1
    assert component.tariff_component_id is None
    assert PersonalDropInPaymentLink.objects.get(payment=payment)
    with pytest.raises(ValidationError, match="command identity"):
        Payment.objects.for_club(club).filter(id=payment.id).update(command_fingerprint="0" * 64)
    assert get_personal_commercial_context(club_id=club.id, student_id=student.id) == [
        {
            **first.receipt,
            "slot_id": slot.id,
        }
    ]

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-cash-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    assert replay.created is False
    assert replay.receipt["payment_id"] == payment.id

    other_slot, other_digest = _additional_slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(hours=2),
    )
    with pytest.raises(BusinessLogicError) as wrong_slot:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=other_slot.id,
            student_id=student.id,
            payment_method="cash",
            subscription_id=None,
            offer_digest=other_digest,
            idempotency_key="staff-cash-1",
            actor_user_id=owner.id,
            bank_source="owner",
        )
    assert wrong_slot.value.code == "idempotency_conflict"

    with pytest.raises(BusinessLogicError) as exc_info:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="transfer",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="staff-cash-1",
            actor_user_id=owner.id,
            bank_source="owner",
    )
    assert exc_info.value.code == "idempotency_conflict"
    # Payment and entitlement have different lifecycle artifacts; the durable
    # command claim still makes this an exact cross-family conflict.
    with pytest.raises(BusinessLogicError) as entitlement_conflict:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="entitlement",
            subscription_id=None,
            offer_digest=None,
            idempotency_key="staff-cash-1",
            actor_user_id=owner.id,
            bank_source="owner",
        )
    assert entitlement_conflict.value.code == "idempotency_conflict"
    assert PersonalStaffIntentCommand.objects.for_club(club).filter(command_key="staff-cash-1").count() == 1

    tariff.price = Decimal("9999.00")
    tariff.duration_days = 1
    tariff.is_active = False
    tariff.save(update_fields=["price", "duration_days", "is_active", "updated_at"])
    payment.refresh_from_db()
    assert payment.amount == Decimal("1450.00")
    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="confirm",
    )
    payment.subscription.refresh_from_db()
    assert payment.subscription.expires_at > timezone.now() + timedelta(days=29)
    student.refresh_from_db()
    conversion = LeadLifecycleEvent.objects.for_club(club).filter(
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
    ).latest("id")
    assert student.status == student.Status.ACTIVE
    assert conversion.metadata["source"] == "personal_payment_confirmation"
    assert conversion.metadata["payment_id"] == payment.id


@pytest.mark.django_db
def test_staff_command_replay_keeps_its_rejected_payment_attempt_when_later_attempt_is_live(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1510.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="K1-rejected",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    first_payment = Payment.objects.get(id=first.receipt["payment_id"])
    verify_payment(
        payment_id=first_payment.id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="reject",
        rejection_reason="K1 rejected before retry",
    )
    second_link = create_personal_drop_in_payment(
        club_id=club.id,
        booking_id=first.receipt["booking_id"],
        payment_method=Payment.Method.CASH,
        created_by_id=owner.id,
        idempotency_key="K2-live",
    )
    assert second_link.payment_id != first_payment.id
    assert second_link.payment.status == Payment.Status.PENDING

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="K1-rejected",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["payment_id"] == first_payment.id
    assert replay.receipt["status"] == "rejected"
    command = PersonalStaffIntentCommand.objects.for_club(club).get(command_key="K1-rejected")
    assert command.payment_link_id_snapshot != second_link.id
    with pytest.raises(ValidationError, match="immutable"):
        PersonalStaffIntentCommand.objects.for_club(club).filter(id=command.id).update(
            command_fingerprint="0" * 64
        )


@pytest.mark.django_db
def test_pay_at_visit_command_replay_remains_unlinked_after_later_settlement_attempt(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1520.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="K1-visit",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    later_link = create_personal_drop_in_payment(
        club_id=club.id,
        booking_id=first.receipt["booking_id"],
        payment_method=Payment.Method.CASH,
        created_by_id=owner.id,
        idempotency_key="K2-settlement",
    )
    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="K1-visit",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["payment_id"] is None
    assert replay.receipt["bank_payment_order_id"] is None
    assert replay.receipt["payment_method"] == "pay_at_visit"
    command = PersonalStaffIntentCommand.objects.for_club(club).get(command_key="K1-visit")
    assert command.payment_link_id_snapshot is None
    assert later_link.payment_id is not None


@pytest.mark.django_db
def test_staff_entitlement_replays_after_slot_is_booked_and_wrong_slot_key_conflicts(settings, club):
    """Replay identity is the immutable command, not today's slot availability."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1490.00"))
    student = StudentFactory(club=club, status="active")
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=1,
        status="active",
        scope=Tariff.Scope.LOCATION,
        location=location,
        expires_at=timezone.now() + timedelta(days=30),
    )
    slot, _digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="staff-entitlement-replay",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.BOOKED

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="staff-entitlement-replay",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["booking_id"] == first.receipt["booking_id"]

    cancel_personal_booking(
        club_id=club.id,
        enrollment_id=first.receipt["enrollment_id"],
        actor_user_id=owner.id,
        origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
        reason="terminal slot replay evidence",
    )
    terminal_replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="entitlement",
        subscription_id=subscription.id,
        offer_digest=None,
        idempotency_key="staff-entitlement-replay",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert terminal_replay.created is False
    assert terminal_replay.receipt["status"] == "cancelled"
    assert terminal_replay.receipt["attempted_at"] == ScheduleBookingEvent.objects.get(
        enrollment_id=first.receipt["enrollment_id"],
        event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
    ).created_at
    context = get_personal_commercial_context(club_id=club.id, student_id=student.id)
    assert [receipt["status"] for receipt in context] == ["cancelled"]

    other_slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(days=1),
        ends_at=slot.ends_at + timedelta(days=1),
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=other_slot.id,
            student_id=student.id,
            payment_method="entitlement",
            subscription_id=subscription.id,
            offer_digest=None,
            idempotency_key="staff-entitlement-replay",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )
    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_staff_pay_at_visit_creates_no_finance_before_checkin(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-visit-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )

    assert result.receipt["status"] == "pay_at_visit"
    assert result.receipt["payment_id"] is None
    assert result.receipt["debt_id"] is None
    assert not Payment.objects.for_club(club).exists()

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-visit-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    assert replay.created is False
    assert replay.receipt["booking_id"] == result.receipt["booking_id"]

    with pytest.raises(BusinessLogicError) as changed_method:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="cash",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="staff-visit-1",
            actor_user_id=owner.id,
            bank_source="owner",
        )
    assert changed_method.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_staff_sbp_crash_after_provider_dispatch_keeps_durable_personal_claim(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.TOCHKA
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.TOCHKA_CUSTOMER_CODE = "customer-1"
    settings.TOCHKA_MERCHANT_ID = "merchant-1"
    settings.TOCHKA_RECEIPT_MODE = BankPaymentOrder.ReceiptMode.NONE
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1600.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    provider = Mock()
    provider.create_payment_link.side_effect = KeyboardInterrupt()

    with (
        patch("apps.billing.payment_providers.get_payment_provider", return_value=provider),
        patch("apps.billing.payment_providers.base.online_payments_enabled", return_value=True),
        pytest.raises(KeyboardInterrupt),
    ):
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="sbp",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="staff-sbp-crash-after-dispatch",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )

    reservation = PersonalBookingPaymentReservation.objects.get(idempotency_key="staff-sbp-crash-after-dispatch")
    order = BankPaymentOrder.objects.get(personal_booking_reservation_id_snapshot=reservation.id)
    assert order.link_creation_state == BankPaymentOrder.LinkCreationState.DISPATCHED
    assert reservation.payment_id is None
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.HELD

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-sbp-crash-after-dispatch",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    reservation.refresh_from_db()
    assert reservation.payment_id == order.payment_id
    assert BankPaymentOrder.objects.filter(personal_booking_reservation_id_snapshot=reservation.id).count() == 1


@pytest.mark.django_db
def test_complete_pay_at_visit_debt_is_exactly_reserved_then_confirmed_or_released(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status="lead")
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)

    def attended_booking(*, number: int):
        slot, digest = _slot_and_digest(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
        )
        slot.starts_at += timedelta(days=number)
        slot.ends_at += timedelta(days=number)
        slot.save(update_fields=["starts_at", "ends_at", "updated_at"])
        offer = resolve_personal_booking_offer(
            club_id=club.id,
            training_type_id=training_type.id,
            location_id=location.id,
        )
        digest = personal_offer_payload(slot=slot, offer=offer)["offer_digest"]
        result = submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="pay_at_visit",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key=f"exact-debt-visit-{number}",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )
        booking = PersonalDropInBooking.objects.get(id=result.receipt["booking_id"])
        create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=booking.enrollment.schedule_id,
            training_type_id=training_type.id,
            source=Checkin.Source.KIOSK,
            checkin_date=booking.enrollment.starts_on,
        )
        booking.refresh_from_db()
        return booking

    confirmed_booking = attended_booking(number=1)
    assert confirmed_booking.debt_id is not None
    debt = confirmed_booking.debt
    assert debt.required_tariff_id == confirmed_booking.tariff_id
    assert debt.tariff_price == confirmed_booking.price_snapshot == Decimal("1200.00")
    manual_response = client.post(
        f"/personal-drop-in-bookings/{confirmed_booking.id}/payments/",
        json={
            "payment_method": Payment.Method.CASH,
            "idempotency_key": "exact-debt-confirm",
            "debt_id": debt.id,
        },
        **make_auth_params(owner, club),
    )
    assert manual_response.status_code == 201
    manual_link = PersonalDropInPaymentLink.objects.get(id=manual_response.json()["id"])
    debt.refresh_from_db()
    assert debt.settlement_payment_id == manual_link.payment_id
    verify_payment(
        payment_id=manual_link.payment_id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="confirm",
    )
    debt.refresh_from_db()
    assert debt.resolved_at is not None

    rejected_booking = attended_booking(number=2)
    rejected_debt = rejected_booking.debt
    rejected_link = create_personal_drop_in_payment(
        club_id=club.id,
        booking_id=rejected_booking.id,
        payment_method=Payment.Method.TRANSFER,
        created_by_id=owner.id,
        idempotency_key="exact-debt-reject",
        debt_id=rejected_debt.id,
    )
    with pytest.raises(BusinessLogicError) as exc_info:
        create_personal_drop_in_payment(
            club_id=club.id,
            booking_id=rejected_booking.id,
            payment_method=Payment.Method.TRANSFER,
            created_by_id=owner.id,
            idempotency_key="exact-debt-wrong",
            debt_id=debt.id,
        )
    assert exc_info.value.code == "personal_drop_in_debt_mismatch"
    verify_payment(
        payment_id=rejected_link.payment_id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="reject",
        rejection_reason="client declined",
    )
    rejected_debt.refresh_from_db()
    assert rejected_debt.resolved_at is None
    assert rejected_debt.settlement_payment_id is None

    sbp_booking = attended_booking(number=3)
    sbp_debt = sbp_booking.debt
    sbp_link = create_personal_drop_in_bank_payment_order(
        club_id=club.id,
        booking_id=sbp_booking.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner.id,
        idempotency_key="exact-debt-sbp",
        debt_id=sbp_debt.id,
    )
    sbp_debt.refresh_from_db()
    assert sbp_link.bank_payment_order_id is not None
    assert sbp_debt.settlement_payment_id == sbp_link.payment_id


@pytest.mark.django_db
def test_complete_personal_free_trial_checkin_does_not_convert_trial(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL, trial_free=True)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status="trial")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-trial-no-conversion",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    booking = PersonalDropInBooking.objects.get(id=result.receipt["booking_id"])
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    student.refresh_from_db()
    assert student.status == student.Status.TRIAL
    assert student.became_student_at is None
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
    ).exists()


@pytest.mark.django_db
def test_staff_sbp_intent_creates_terms_payment_order_receipt(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = "mock"
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1600.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-sbp-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )

    payment = Payment.objects.get(id=result.receipt["payment_id"])
    assert result.receipt["bank_payment_order_id"]
    assert result.receipt["allowed_actions"] == [
        "open_bank_payment_order",
        "cancel_if_safe",
        "replace_payment_method",
    ]
    assert payment.amount == Decimal("1600.00")
    assert payment.command_idempotency_key == "staff-sbp-1"
    assert payment.subscription.components.get().tariff_component_id is None

    other_slot, other_digest = _additional_slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=slot.starts_at + timedelta(hours=2),
    )
    with pytest.raises(BusinessLogicError) as wrong_slot:
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=other_slot.id,
            student_id=student.id,
            payment_method="sbp",
            subscription_id=None,
            offer_digest=other_digest,
            idempotency_key="staff-sbp-1",
            actor_user_id=owner.id,
            bank_source="owner",
        )
    assert wrong_slot.value.code == "idempotency_conflict"


@pytest.mark.django_db
def test_staff_command_rolls_back_booking_terms_and_slot_when_manual_family_fails(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1300.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    def fail_manual_family(**kwargs):
        raise BusinessLogicError("payment failed", code="payment_create_failed")

    monkeypatch.setattr(
        "apps.attendance.services.staff_intents.create_personal_drop_in_payment",
        fail_manual_family,
    )
    with pytest.raises(BusinessLogicError, match="payment failed"):
        submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method="cash",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="staff-cash-rollback",
            actor_user_id=owner.id,
            bank_source="owner",
        )

    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
    assert not PersonalDropInBooking.objects.for_club(club).exists()
    assert not PersonalServiceTermsSnapshot.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()


@pytest.mark.django_db
def test_pending_manual_personal_payment_operationally_admits_lead_and_rejection_keeps_student(
    settings,
    club,
    student_user,
    parent_user,
    trainer_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = trainer_user
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True, user=owner)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1500.00"))
    student = StudentFactory(
        club=club,
        user=student_user,
        parent_user=parent_user,
        is_child=True,
        status="lead",
        lead_status="new",
    )
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="transfer",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-transfer-lead-lifecycle",
        actor_user_id=owner.id,
        bank_source="owner",
        v2_manual_admission_command=True,
    )
    payment = Payment.objects.get(id=created.receipt["payment_id"])
    student.refresh_from_db()
    assert payment.status == Payment.Status.PENDING
    assert student.status == student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert is_account_access_eligible(club=club, student=student) is True
    assert (
        Student.objects.for_club(club)
        .filter(id=student.id)
        .filter(
            trainer_manual_operational_admission_scope_filter(
                club=club,
                trainer_id=trainer.id,
                user_id=owner.id,
            )
        )
        .exists()
    )
    from apps.billing.management.commands.audit_operational_admissions import _audit_club

    audit = _audit_club(club=club)
    assert audit["manual_admission_origin_counts"] == {"personal": 1}
    assert audit["invalid_state_counts"] == {}
    assert audit["pending_row_classification_counts"] == {"canonical": 1}
    from django.core.management import call_command

    reconciliation_stdout = io.StringIO()
    call_command(
        "reconcile_manual_operational_admission",
        "--apply",
        club_id=club.id,
        payment_id=payment.id,
        actor_user_id=owner.id,
        stdout=reconciliation_stdout,
    )
    assert json.loads(reconciliation_stdout.getvalue()) == {
        "club_id": club.id,
        "payment_id": payment.id,
        "origin": "personal",
        "outcome": "already_admitted",
    }
    cabinet = get_cabinet_financial_read_model(club=club, student=student)
    assert cabinet["operational_admission"] is None
    assert cabinet["operational_admission_v2"] == {
        "kind": "personal",
        "payment_id": payment.id,
        "recorded_by_id": owner.id,
        "payment_status": "pending",
        "payment_method": "transfer",
        "subscription_status": "pending",
        "start_date": slot.starts_at.date(),
        "checkin_ready": False,
        "account_access_eligible": True,
        "is_qualifying": True,
        "group_label": None,
        "training_group_id": None,
        "group_membership_id": None,
        "enrollment_status": None,
        "booking_id": created.receipt["booking_id"],
        "session_id": created.receipt["schedule_id"],
        "booking_state": "scheduled",
    }
    student_response = client.get(
        "/students/me/financial-state/",
        **make_auth_params(student_user, club, role="student"),
    )
    parent_response = client.get(
        f"/parents/children/{student.id}/",
        **make_auth_params(parent_user, club, role="parent"),
    )
    assert student_response.status_code == parent_response.status_code == 200
    assert student_response.json()["operational_admission"] is None
    assert student_response.json()["operational_admission_v2"]["kind"] == "personal"
    assert parent_response.json()["financial_state"]["operational_admission"] is None
    assert parent_response.json()["financial_state"]["operational_admission_v2"]["kind"] == "personal"
    trainer_response = client.get(
        f"/students/{student.id}/",
        **make_auth_params(owner, club, role="trainer"),
    )
    assert trainer_response.status_code == 200
    assert trainer_response.json()["operational_admission"] is None
    assert trainer_response.json()["operational_admission_v2"]["kind"] == "personal"

    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="reject",
        rejection_reason="receipt not confirmed",
    )

    student.refresh_from_db()
    assert student.status == student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert is_account_access_eligible(club=club, student=student) is False
    assert not (
        Student.objects.for_club(club)
        .filter(id=student.id)
        .filter(
            trainer_manual_operational_admission_scope_filter(
                club=club,
                trainer_id=trainer.id,
                user_id=owner.id,
            )
        )
        .exists()
    )


@pytest.mark.django_db
def test_manual_operational_admission_rejects_mismatched_payment_recorder_before_person_mutation(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    recorder = UserFactory()
    other_actor = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1510.00"))
    student = StudentFactory(club=club, status=Student.Status.LEAD, lead_status=Student.LeadStatus.NEW)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    with monkeypatch.context() as patched:
        patched.setattr(
            "apps.leads.services.admit_lead_for_manual_operational_admission",
            lambda *, evidence: False,
        )
        created = submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method=Payment.Method.CASH,
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="staff-mismatched-manual-admission-actor",
            actor_user_id=recorder.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )

    from apps.leads.services import admit_lead_for_manual_operational_admission
    from apps.students.operational_admission_contracts import ManualOperationalAdmissionEvidence

    payment = Payment.objects.for_club(club).get(id=created.receipt["payment_id"])
    with pytest.raises(BusinessLogicError) as error:
        admit_lead_for_manual_operational_admission(
            evidence=ManualOperationalAdmissionEvidence(
                club_id=club.id,
                student_id=student.id,
                payment_id=payment.id,
                origin="personal",
                actor_user_id=other_actor.id,
            )
        )

    with pytest.raises(BusinessLogicError) as foreign_recorder_error:
        admit_lead_for_manual_operational_admission(
            evidence=ManualOperationalAdmissionEvidence(
                club_id=club.id,
                student_id=student.id,
                payment_id=payment.id,
                origin="personal",
                actor_user_id=recorder.id,
            )
        )

    student.refresh_from_db()
    assert error.value.code == "manual_operational_admission_evidence_invalid"
    assert foreign_recorder_error.value.code == "manual_operational_admission_evidence_invalid"
    assert student.status == Student.Status.LEAD
    assert student.lead_status == Student.LeadStatus.NEW
    assert student.became_student_at is None
    assert not LeadLifecycleEvent.objects.for_club(club).filter(
        student=student,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
    ).exists()


@pytest.mark.django_db
def test_reconciliation_uses_exact_personal_payment_timestamp_and_is_idempotent(
    settings,
    club,
    monkeypatch,
):
    """Only an exact legacy lead row may be reconciled, using durable time."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1520.00"))
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    # Simulate the strictly bounded pre-Slice-2 family: exact financial
    # evidence exists but the old caller never performed person mutation.
    with monkeypatch.context() as patched:
        patched.setattr(
            "apps.leads.services.admit_lead_for_manual_operational_admission",
            lambda *, evidence: False,
        )
        receipt = submit_staff_personal_intent(
            club_id=club.id,
            slot_id=slot.id,
            student_id=student.id,
            payment_method=Payment.Method.CASH,
            subscription_id=None,
            offer_digest=digest,
            idempotency_key="legacy-personal-reconciliation",
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        ).receipt

    payment = Payment.objects.for_club(club).get(id=receipt["payment_id"])
    from django.core.management import call_command

    from apps.billing.management.commands.audit_operational_admissions import _audit_club

    assert _audit_club(club=club)["pending_row_classification_counts"] == {"reconcile": 1}
    first_stdout = io.StringIO()
    call_command(
        "reconcile_manual_operational_admission",
        "--apply",
        club_id=club.id,
        payment_id=payment.id,
        actor_user_id=owner.id,
        stdout=first_stdout,
    )
    assert json.loads(first_stdout.getvalue())["outcome"] == "admitted"
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at == payment.created_at

    second_stdout = io.StringIO()
    call_command(
        "reconcile_manual_operational_admission",
        "--apply",
        club_id=club.id,
        payment_id=payment.id,
        actor_user_id=owner.id,
        stdout=second_stdout,
    )
    assert json.loads(second_stdout.getvalue())["outcome"] == "already_admitted"
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=student,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        metadata__payment_id=payment.id,
    ).count() == 1


@pytest.mark.django_db
def test_rejected_complete_personal_payment_after_checkin_keeps_student_and_reopens_exact_debt(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1525.00"))
    student = StudentFactory(club=club, status="lead", lead_status="new", assigned_trainer=trainer)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-post-checkin-reject",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    booking = PersonalDropInBooking.objects.get(id=created.receipt["booking_id"])
    payment = Payment.objects.get(id=created.receipt["payment_id"])
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    checkin = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    debt = Debt.objects.get(checkin_id=checkin["checkin_id"])
    assert debt.settlement_payment_id == payment.id
    verify_payment(
        payment_id=payment.id,
        club_id=club.id,
        verified_by_id=owner.id,
        action="reject",
        rejection_reason="manual receipt was not accepted",
    )
    debt.refresh_from_db()
    student.refresh_from_db()
    booking.refresh_from_db()
    assert booking.state == PersonalDropInBooking.State.ATTENDED
    assert student.status == student.Status.ACTIVE
    assert debt.resolved_at is None
    assert debt.settlement_payment_id is None


@pytest.mark.django_db
def test_terminal_sbp_same_key_replays_and_new_key_creates_one_new_live_attempt(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = "mock"
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1700.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-sbp-terminal-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    reservation = PersonalBookingPaymentReservation.objects.get(id=first.receipt["reservation_id"])
    order = BankPaymentOrder.objects.get(id=first.receipt["bank_payment_order_id"])
    lock_calls: list[str] = []
    original_personal_lock = lock_complete_personal_scopes
    original_select_for_update = QuerySet.select_for_update

    def record_personal_lock(*args, **kwargs):
        lock_calls.append("complete_personal_scope")
        return original_personal_lock(*args, **kwargs)

    def record_select_for_update(queryset, *args, **kwargs):
        if queryset.model in {BankPaymentOrder, Payment}:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(
        "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
        record_personal_lock,
    )
    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    cancel_bank_payment_order(
        club_id=club.id,
        order_id=order.id,
        actor_user_id=owner.id,
        reason="terminal retry test",
    )
    assert lock_calls.index("complete_personal_scope") < lock_calls.index("BankPaymentOrder")
    assert lock_calls.index("complete_personal_scope") < lock_calls.index("Payment")
    order.refresh_from_db()
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    reservation.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.CANCELLED
    assert order.payment.status == Payment.Status.REJECTED
    assert order.subscription.deleted_at is not None
    assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED

    replay = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-sbp-terminal-1",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    assert replay.created is False
    assert replay.receipt["reservation_id"] == reservation.id
    assert replay.receipt["allowed_actions"] == ["retry_bank_payment"]

    retry_offer = resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    slot.refresh_from_db()
    retry_digest = personal_offer_payload(slot=slot, offer=retry_offer)["offer_digest"]

    retry = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=retry_digest,
        idempotency_key="staff-sbp-terminal-2",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    assert retry.created is True
    assert retry.receipt["reservation_id"] != reservation.id
    assert PersonalBookingPaymentReservation.objects.for_club(club).filter(
        status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
    ).count() == 1


@pytest.mark.django_db
def test_direct_terminal_sbp_receipt_allows_retry_when_exact_time_is_still_free(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1710.00"))
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=9)).replace(hour=13, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]
    first = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="direct-terminal-sbp",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    cancel_bank_payment_order(
        club_id=club.id,
        order_id=first.receipt["bank_payment_order_id"],
        actor_user_id=owner.id,
        reason="direct terminal retry receipt",
    )
    replay = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="direct-terminal-sbp",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    assert replay.created is False
    assert replay.receipt["status"] == BankPaymentOrder.Status.CANCELLED
    assert replay.receipt["allowed_actions"] == ["retry_bank_payment"]
    assert replay.receipt["attempted_at"] is not None


@pytest.mark.django_db
def test_direct_commercial_context_keeps_live_and_only_latest_terminal_per_exact_context(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1720.00"))
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=9)).replace(hour=14, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]

    def submit(key):
        return submit_staff_direct_personal_intent(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            payment_method="sbp",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key=key,
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )

    first = submit("direct-history-1")
    cancel_bank_payment_order(
        club_id=club.id,
        order_id=first.receipt["bank_payment_order_id"],
        actor_user_id=owner.id,
        reason="first terminal",
    )
    second = submit("direct-history-2")
    cancel_bank_payment_order(
        club_id=club.id,
        order_id=second.receipt["bank_payment_order_id"],
        actor_user_id=owner.id,
        reason="second terminal",
    )
    live = submit("direct-history-3")

    receipts = [
        receipt
        for receipt in get_personal_commercial_context(club_id=club.id, student_id=student.id)
        if receipt["reservation_id"] is not None
    ]
    assert {receipt["reservation_id"] for receipt in receipts} == {
        second.receipt["reservation_id"],
        live.receipt["reservation_id"],
    }
    assert {receipt["status"] for receipt in receipts} == {
        BankPaymentOrder.Status.CANCELLED,
        BankPaymentOrder.Status.PENDING,
    }


@pytest.mark.django_db
def test_direct_terminal_retry_uses_club_wall_time_not_utc_schedule_fields(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone", "updated_at"])
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1730.00"))
    student = StudentFactory(club=club, status="lead")

    def terminal_direct(*, key, starts_at):
        ends_at = starts_at + timedelta(hours=1)
        digest = get_staff_direct_personal_offer(
            club_id=club.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
        )["offer_digest"]
        result = submit_staff_direct_personal_intent(
            club_id=club.id,
            student_id=student.id,
            trainer_id=trainer.id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location.id,
            training_type_id=training_type.id,
            payment_method="sbp",
            subscription_id=None,
            offer_digest=digest,
            idempotency_key=key,
            actor_user_id=owner.id,
            bank_source=BankPaymentOrder.Source.OWNER,
        )
        cancel_bank_payment_order(
            club_id=club.id,
            order_id=result.receipt["bank_payment_order_id"],
            actor_user_id=owner.id,
            reason="timezone retry test",
        )
        return result

    starts_at = datetime(2099, 5, 4, 20, 0, tzinfo=ZoneInfo("UTC"))
    utc_decoy = Schedule.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=Schedule.DayOfWeek.MONDAY,
        one_time_date=starts_at.date(),
        start_time=(starts_at + timedelta(minutes=30)).time().replace(tzinfo=None),
        end_time=(starts_at + timedelta(hours=2)).time().replace(tzinfo=None),
        group_name="UTC decoy",
    )
    first = terminal_direct(key="direct-utc-decoy", starts_at=starts_at)
    first_receipt = get_personal_commercial_context(club_id=club.id, student_id=student.id)
    assert next(receipt for receipt in first_receipt if receipt["reservation_id"] == first.receipt["reservation_id"])[
        "allowed_actions"
    ] == ["retry_bank_payment"]

    local_conflict_starts_at = datetime(2099, 5, 5, 20, 0, tzinfo=ZoneInfo("UTC"))
    local_start = timezone.localtime(local_conflict_starts_at, ZoneInfo("Asia/Yekaterinburg"))
    second = terminal_direct(key="direct-local-conflict", starts_at=local_conflict_starts_at)
    Schedule.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=Schedule.DayOfWeek.TUESDAY,
        one_time_date=local_start.date(),
        start_time=(local_start + timedelta(minutes=30)).time().replace(tzinfo=None),
        end_time=(local_start + timedelta(hours=2)).time().replace(tzinfo=None),
        group_name="Local conflict",
    )
    second_receipt = get_personal_commercial_context(club_id=club.id, student_id=student.id)
    assert next(receipt for receipt in second_receipt if receipt["reservation_id"] == second.receipt["reservation_id"])[
        "allowed_actions"
    ] == ["view_booking"]
    assert utc_decoy.id


@pytest.mark.django_db
def test_unattached_live_bank_order_is_bridged_before_checkin_and_its_debt_is_reserved(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1720.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(club=club, trainer=trainer, location=location, training_type=training_type)
    booking_result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="snapshot-checkin-booking",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=booking_result.receipt["booking_id"])
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner.id,
        seller_trainer_id=trainer.id,
        package_owner_trainer_id=trainer.id,
        debt_ids=[],
        allow_reuse=False,
        personal_drop_in_booking_id=booking.id,
        command_idempotency_key="snapshot-checkin-order",
    )
    assert not PersonalDropInPaymentLink.objects.for_club(club).filter(booking=booking).exists()

    checkin = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    link = PersonalDropInPaymentLink.objects.for_club(club).get(bank_payment_order=order)
    debt = Debt.objects.for_club(club).get(checkin_id=checkin["checkin_id"])
    assert link.booking_id == booking.id
    assert debt.settlement_payment_id == order.payment_id


@pytest.mark.django_db
def test_unattached_live_bank_order_is_closed_with_its_booking_cancel(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1730.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(club=club, trainer=trainer, location=location, training_type=training_type)
    booking_result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="snapshot-cancel-booking",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=booking_result.receipt["booking_id"])
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner.id,
        seller_trainer_id=trainer.id,
        package_owner_trainer_id=trainer.id,
        debt_ids=[],
        allow_reuse=False,
        personal_drop_in_booking_id=booking.id,
        command_idempotency_key="snapshot-cancel-order",
    )
    cancel_personal_drop_in_booking(
        club_id=club.id,
        booking_id=booking.id,
        actor_user_id=owner.id,
        reason="snapshot order cancellation",
    )
    order.refresh_from_db()
    booking.refresh_from_db()
    assert order.status == BankPaymentOrder.Status.CANCELLED
    assert order.payment.status == Payment.Status.REJECTED
    assert booking.state == PersonalDropInBooking.State.CANCELLED
    assert PersonalDropInPaymentLink.objects.for_club(club).filter(bank_payment_order=order).exists()


@pytest.mark.django_db
def test_exact_debt_sbp_order_without_bridge_is_visible_on_commercial_reload(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1740.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    booking_result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="exact-debt-reload-booking",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=booking_result.receipt["booking_id"])
    create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    booking.refresh_from_db()
    assert booking.debt_id is not None
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner.id,
        seller_trainer_id=trainer.id,
        package_owner_trainer_id=trainer.id,
        debt_ids=[booking.debt_id],
        allow_reuse=False,
        personal_drop_in_booking_id=booking.id,
        command_idempotency_key="exact-debt-reload-sbp",
    )
    assert not PersonalDropInPaymentLink.objects.for_club(club).filter(
        bank_payment_order_id=order.id
    ).exists()

    receipt = next(
        item
        for item in get_personal_commercial_context(club_id=club.id, student_id=student.id)
        if item["bank_payment_order_id"] == order.id
    )
    assert receipt["payment_id"] == order.payment_id
    assert receipt["payment_method"] == "sbp"
    assert receipt["status"] == order.status
    assert receipt["attempted_at"] == order.created_at


@pytest.mark.django_db
def test_provider_confirmation_enters_complete_personal_scope_before_order_lock(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1650.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-provider-ordered-lock",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    order = BankPaymentOrder.objects.get(id=result.receipt["bank_payment_order_id"])
    lock_calls: list[str] = []
    original_personal_lock = lock_complete_personal_scopes
    original_select_for_update = QuerySet.select_for_update

    def record_personal_lock(*args, **kwargs):
        lock_calls.append("complete_personal_scope")
        return original_personal_lock(*args, **kwargs)

    def record_select_for_update(queryset, *args, **kwargs):
        if queryset.model in {BankPaymentOrder, Payment}:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(
        "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
        record_personal_lock,
    )
    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=json.dumps(
            {
                "webhookType": "acquiringInternetPayment",
                "event_id": f"staff-provider-ordered-lock-{order.id}",
                "status": "APPROVED",
                "paymentLinkId": order.provider_payment_link_id,
                "operationId": f"staff-provider-ordered-lock-op-{order.id}",
                "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            }
        ).encode(),
        headers={},
        request_id="staff-provider-ordered-lock",
    )
    assert event.processing_status == "processed"
    assert lock_calls.index("complete_personal_scope") < lock_calls.index("BankPaymentOrder")
    assert lock_calls.index("complete_personal_scope") < lock_calls.index("Payment")


@pytest.mark.django_db
def test_unified_pending_manual_cancel_and_no_show_close_only_owned_artifacts(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1800.00"))
    student = StudentFactory(club=club, status="lead", lead_status="new", assigned_trainer=trainer)

    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    cancelled = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-cancel-pending",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    payment = Payment.objects.get(id=cancelled.receipt["payment_id"])
    booking = PersonalDropInBooking.objects.get(id=cancelled.receipt["booking_id"])
    cancel_personal_drop_in_booking(
        club_id=club.id,
        booking_id=booking.id,
        actor_user_id=owner.id,
        reason="client cancelled",
    )
    payment.refresh_from_db()
    booking.refresh_from_db()
    slot.refresh_from_db()
    assert payment.status == Payment.Status.REJECTED
    assert payment.subscription.deleted_at is not None
    assert booking.state == PersonalDropInBooking.State.CANCELLED
    assert booking.debt_id is None
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED

    retry_offer = resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    second_digest = personal_offer_payload(slot=slot, offer=retry_offer)["offer_digest"]
    no_show = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="transfer",
        subscription_id=None,
        offer_digest=second_digest,
        idempotency_key="staff-no-show-pending",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    second_payment = Payment.objects.get(id=no_show.receipt["payment_id"])
    second_booking = PersonalDropInBooking.objects.get(id=no_show.receipt["booking_id"])
    monkeypatch.setattr(
        "apps.attendance.services.drop_in.timezone.now",
        lambda: slot.ends_at + timedelta(minutes=1),
    )
    mark_personal_drop_in_no_show(
        club_id=club.id,
        booking_id=second_booking.id,
        actor_user_id=owner.id,
        reason="did not attend",
    )
    second_payment.refresh_from_db()
    second_booking.refresh_from_db()
    assert second_payment.status == Payment.Status.REJECTED
    assert second_payment.subscription.deleted_at is not None
    assert second_booking.state == PersonalDropInBooking.State.NO_SHOW
    assert second_booking.debt_id is None


@pytest.mark.django_db
def test_complete_personal_checkin_orders_cross_family_finance_after_exact_identity(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1750.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    booking_result = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-d12-identity-first",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    booking = PersonalDropInBooking.objects.get(id=booking_result.receipt["booking_id"])
    # This pending generic admission is intentionally created before the
    # complete personal payment.  It forces the check-in finance union to hold
    # a lower unrelated Payment id without allowing it to jump the exact slot.
    generic_subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=booking.tariff,
        status="pending",
    )
    generic_payment = PaymentFactory(
        club=club,
        student=student,
        tariff=booking.tariff,
        subscription=generic_subscription,
        target_schedule=booking.enrollment.schedule,
        conversion_enrollment=booking.enrollment,
        target_start_date=booking.enrollment.starts_on,
        seller_trainer=trainer,
        package_owner_trainer=trainer,
    )
    personal_link = create_personal_drop_in_payment(
        club_id=club.id,
        booking_id=booking.id,
        payment_method="cash",
        created_by_id=owner.id,
        idempotency_key="staff-d12-union-payment",
    )
    personal_payment = personal_link.payment
    assert generic_payment.id < personal_payment.id

    lock_calls: list[str] = []
    original_select_for_update = QuerySet.select_for_update

    def record_select_for_update(queryset, *args, **kwargs):
        if queryset.model.__name__ in {
            "Trainer",
            "Student",
            "PersonalAvailabilitySlot",
            "PersonalDropInBooking",
            "PersonalDropInPaymentLink",
            "ScheduleEnrollment",
            "Payment",
            "Subscription",
            "BankPaymentOrder",
            "Checkin",
        }:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    checkin = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )
    assert checkin["created"] is True
    expected_order = [
        "Trainer",
        "Student",
        "PersonalAvailabilitySlot",
        "PersonalDropInBooking",
        "PersonalDropInPaymentLink",
        "ScheduleEnrollment",
        "Payment",
        "Subscription",
        "Checkin",
    ]
    assert [lock_calls.index(model) for model in expected_order] == sorted(
        lock_calls.index(model) for model in expected_order
    )


@pytest.mark.django_db
def test_complete_personal_cancel_checkin_enters_owner_scope_before_family_rows(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1725.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    intent = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-d12-cancel-checkin",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    booking = PersonalDropInBooking.objects.get(id=intent.receipt["booking_id"])
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    checked_in = create_checkin(
        club_id=club.id,
        student_id=student.id,
        schedule_id=booking.enrollment.schedule_id,
        training_type_id=training_type.id,
        source=Checkin.Source.KIOSK,
        checkin_date=booking.enrollment.starts_on,
    )

    lock_calls: list[str] = []
    original_personal_lock = lock_complete_personal_scopes
    original_select_for_update = QuerySet.select_for_update

    def record_personal_lock(*args, **kwargs):
        lock_calls.append("complete_personal_scope")
        return original_personal_lock(*args, **kwargs)

    def record_select_for_update(queryset, *args, **kwargs):
        if queryset.model.__name__ in {
            "Trainer",
            "Student",
            "PersonalAvailabilitySlot",
            "PersonalDropInBooking",
            "ScheduleEnrollment",
            "Payment",
            "Subscription",
            "BankPaymentOrder",
            "Checkin",
        }:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(
        "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
        record_personal_lock,
    )
    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    with pytest.raises(BusinessLogicError) as exc_info:
        cancel_checkin(
            checkin_id=checked_in["checkin_id"],
            club_id=club.id,
            cancelled_by_user_id=owner.id,
            user_role="owner",
        )
    assert exc_info.value.code == "debt_payment_pending"

    assert lock_calls.index("complete_personal_scope") < lock_calls.index("PersonalDropInBooking")
    assert lock_calls.index("complete_personal_scope") < lock_calls.index("Payment")
    expected_order = [
        "Trainer",
        "Student",
        "PersonalAvailabilitySlot",
        "PersonalDropInBooking",
        "ScheduleEnrollment",
        "Payment",
        "Subscription",
        "Checkin",
    ]
    assert [lock_calls.index(model) for model in expected_order] == sorted(
        lock_calls.index(model) for model in expected_order
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_direct_time_two_students_keeps_one_family(settings, club):
    """Direct trainer/time has the same Trainer-first contention boundary as slots."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1770.00"))
    first_student = StudentFactory(club=club, status="lead")
    second_student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=9)).replace(hour=11, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]
    gate = Barrier(2)

    def submit(student, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_direct_personal_intent(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                payment_method="cash",
                subscription_id=None,
                offer_digest=digest,
                idempotency_key=key,
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
            )
            return "result", result.receipt["booking_id"]
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                [(first_student, "pg-direct-one"), (second_student, "pg-direct-two")],
            )
        )

    assert sum(kind == "result" for kind, _value in outcomes) == 1, outcomes
    assert all(kind == "result" or code == "personal_booking_slot_conflict" for kind, code in outcomes), outcomes
    assert not {
        code for kind, code in outcomes if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }
    bookings = list(PersonalDropInBooking.objects.for_club(club).order_by("id"))
    assert len(bookings) == 1
    links = list(PersonalDropInPaymentLink.objects.for_club(club).filter(booking_id=bookings[0].id))
    assert len(links) == 1
    assert Payment.objects.for_club(club).filter(id=links[0].payment_id).count() == 1
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_cash_vs_sbp_keeps_one_live_origin(settings, club):
    """Cash and SBP contend before either can mint a second complete origin."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1780.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    gate = Barrier(2)

    def submit(method, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_personal_intent(
                club_id=club.id,
                slot_id=slot.id,
                student_id=student.id,
                payment_method=method,
                subscription_id=None,
                offer_digest=digest,
                idempotency_key=key,
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
            )
            return "result", method, result.receipt
        except BusinessLogicError as exc:
            return "business_error", method, exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                [("cash", "pg-cross-method-cash"), ("sbp", "pg-cross-method-sbp")],
            )
        )

    assert sum(kind == "result" for kind, _method, _value in outcomes) == 1, outcomes
    assert all(
        kind == "result"
        or code
        in {
            "personal_slot_not_available",
            "personal_availability_slot_taken",
            "personal_availability_slot_unavailable",
            "personal_booking_conflict",
            "personal_booking_slot_conflict",
            # The cash winner has already minted its pending entitlement;
            # the SBP contender is correctly refused before a second family.
            "personal_subscription_already_available",
        }
        for kind, _method, code in outcomes
    ), outcomes
    assert not {
        code
        for kind, _method, code in outcomes
        if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }
    bookings = list(PersonalDropInBooking.objects.for_club(club).order_by("id"))
    reservations = list(PersonalBookingPaymentReservation.objects.for_club(club).order_by("id"))
    assert len(bookings) + len(reservations) == 1
    if bookings:
        links = list(PersonalDropInPaymentLink.objects.for_club(club).filter(booking=bookings[0]))
        assert len(links) == 1
        assert Payment.objects.for_club(club).filter(id=links[0].payment_id).count() == 1
        assert not BankPaymentOrder.objects.for_club(club).exists()
    else:
        reservation = reservations[0]
        assert reservation.payment_id is not None
        assert reservation.subscription_id is not None
        assert reservation.bank_payment_order_id is not None
        assert BankPaymentOrder.objects.for_club(club).filter(id=reservation.bank_payment_order_id).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_direct_cash_vs_sbp_keeps_one_live_origin(settings, club):
    """Direct-time cash and SBP share the same Club-first origin boundary."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1785.00"))
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=9)).replace(hour=12, minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]
    gate = Barrier(2)

    def submit(method, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_direct_personal_intent(
                club_id=club.id,
                student_id=student.id,
                trainer_id=trainer.id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location.id,
                training_type_id=training_type.id,
                payment_method=method,
                subscription_id=None,
                offer_digest=digest,
                idempotency_key=key,
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
            )
            return "result", method, result.receipt
        except BusinessLogicError as exc:
            return "business_error", method, exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                [("cash", "pg-direct-cross-cash"), ("sbp", "pg-direct-cross-sbp")],
            )
        )

    assert sum(kind == "result" for kind, _method, _value in outcomes) == 1, outcomes
    assert all(
        kind == "result"
        or code
        in {
            "personal_booking_slot_conflict",
            "personal_payment_reservation_slot_conflict",
            "personal_subscription_already_available",
        }
        for kind, _method, code in outcomes
    ), outcomes
    assert not {
        code
        for kind, _method, code in outcomes
        if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }
    bookings = list(PersonalDropInBooking.objects.for_club(club).order_by("id"))
    reservations = list(PersonalBookingPaymentReservation.objects.for_club(club).order_by("id"))
    assert len(bookings) + len(reservations) == 1
    if bookings:
        links = list(PersonalDropInPaymentLink.objects.for_club(club).filter(booking=bookings[0]))
        assert len(links) == 1
        assert Payment.objects.for_club(club).filter(id=links[0].payment_id).count() == 1
        assert not BankPaymentOrder.objects.for_club(club).exists()
    else:
        reservation = reservations[0]
        assert reservation.payment_id is not None
        assert reservation.subscription_id is not None
        assert reservation.bank_payment_order_id is not None
        assert BankPaymentOrder.objects.for_club(club).filter(id=reservation.bank_payment_order_id).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_same_trainer_time_keeps_one_booking_and_financial_family(
    settings,
    club,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1775.00"))
    first_student = StudentFactory(club=club, status="lead")
    second_student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    gate = Barrier(2)

    def submit(student, key):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_personal_intent(
                club_id=club.id,
                slot_id=slot.id,
                student_id=student.id,
                payment_method="cash",
                subscription_id=None,
                offer_digest=digest,
                idempotency_key=key,
                actor_user_id=owner.id,
                bank_source="owner",
            )
            return "result", result.receipt["booking_id"]
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: submit(*pair),
                [
                    (first_student, "pg-personal-same-time-one"),
                    (second_student, "pg-personal-same-time-two"),
                ],
            )
        )

    assert not {
        code
        for kind, code in outcomes
        if kind == "business_error" and code in {"deadlock_detected", "database_locked"}
    }
    bookings = list(PersonalDropInBooking.objects.for_club(club).order_by("id"))
    assert len(bookings) == 1
    links = list(PersonalDropInPaymentLink.objects.for_club(club).filter(booking=bookings[0]).order_by("id"))
    assert len(links) == 1
    assert Payment.objects.for_club(club).filter(id=links[0].payment_id).count() == 1
    assert sum(kind == "result" for kind, _value in outcomes) == 1
    assert all(
        kind == "result"
        or code
        in {
            "personal_slot_not_available",
            "personal_availability_slot_taken",
            "personal_availability_slot_unavailable",
            "personal_booking_conflict",
            "personal_booking_slot_conflict",
        }
        for kind, code in outcomes
    ), outcomes


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_provider_confirm_vs_cancel_keeps_coherent_family(
    settings,
    club,
    monkeypatch,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1825.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-personal-provider-cancel",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    order = BankPaymentOrder.objects.get(id=created.receipt["bank_payment_order_id"])
    provider_scope_locked = Event()
    original_personal_lock = lock_complete_personal_scopes

    def signal_provider_scope(*args, **kwargs):
        scope = original_personal_lock(*args, **kwargs)
        # The callback owns a single outer transaction.  Start cancellation
        # only after it holds the complete origin so the test exercises the
        # contended ordered-lock path, rather than a late approval after a
        # terminal local cancellation (which correctly needs manual review of
        # external-money evidence).
        if kwargs.get("order_ids") == [order.id]:
            provider_scope_locked.set()
        return scope

    monkeypatch.setattr(
        "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
        signal_provider_scope,
    )

    def approve():
        close_old_connections()
        try:
            event = process_bank_payment_webhook(
                provider=BankPaymentOrder.Provider.MOCK,
                request_body=json.dumps(
                    {
                        "webhookType": "acquiringInternetPayment",
                        "event_id": f"pg-personal-provider-cancel-{order.id}",
                        "status": "APPROVED",
                        "paymentLinkId": order.provider_payment_link_id,
                        "operationId": f"pg-personal-provider-cancel-operation-{order.id}",
                        "amount": str(order.amount_snapshot),
                        "paid_at": timezone.now().isoformat(),
                    }
                ).encode(),
                headers={},
                request_id="pg-personal-provider-cancel",
            )
            return "approved", event.processing_status
        except BusinessLogicError as exc:
            return "approve_error", exc.code
        finally:
            close_old_connections()

    def cancel():
        close_old_connections()
        try:
            assert provider_scope_locked.wait(timeout=10)
            cancelled = cancel_bank_payment_order(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner.id,
                reason="PostgreSQL complete-personal provider/cancel race",
            )
            return "cancelled", cancelled.status
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in [executor.submit(approve), executor.submit(cancel)]]

    assert not {
        code
        for kind, code in outcomes
        if kind.endswith("error") and code in {"deadlock_detected", "database_locked"}
    }
    order.refresh_from_db()
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=created.receipt["reservation_id"])
    assert ("approved", "processed") in outcomes
    assert ("cancel_error", "bank_payment_order_not_cancellable") in outcomes
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.subscription.status == "active"
    assert reservation.status == PersonalBookingPaymentReservation.Status.BOOKED
    assert reservation.enrollment_id is not None
    assert ScheduleEnrollment.objects.for_club(club).filter(id=reservation.enrollment_id).count() == 1
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.BOOKED


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_provider_manual_review_vs_cancel_is_coherent(
    settings,
    club,
    monkeypatch,
):
    """A late provider manual-review result locks its personal origin first."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1830.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-provider-manual-review-cancel",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    order = BankPaymentOrder.objects.get(id=created.receipt["bank_payment_order_id"])
    provider_scope_locked = Event()
    original_personal_lock = lock_complete_personal_scopes

    def signal_provider_scope(*args, **kwargs):
        scope = original_personal_lock(*args, **kwargs)
        if kwargs.get("order_ids") == [order.id]:
            provider_scope_locked.set()
        return scope

    monkeypatch.setattr(
        "apps.attendance.services.personal_locking.lock_complete_personal_scopes",
        signal_provider_scope,
    )

    def mark_manual_review():
        close_old_connections()
        try:
            reviewed = record_provider_creation_manual_review(
                club_id=club.id,
                order_id=order.id,
                link=ProviderLinkResult(
                    payment_url="https://pay.example.test/manual-review",
                    payment_link_id=order.provider_payment_link_id,
                    provider_status="CREATED",
                    operation_id=f"pg-manual-review-{order.id}",
                    payment_modes=["cash"],
                ),
                error_code="sbp_only_payment_mode_required",
            )
            return "manual_review", reviewed.status
        except BusinessLogicError as exc:
            return "manual_review_error", exc.code
        finally:
            close_old_connections()

    def cancel():
        close_old_connections()
        try:
            assert provider_scope_locked.wait(timeout=10)
            cancelled = cancel_bank_payment_order(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner.id,
                reason="PostgreSQL manual-review/cancel race",
            )
            return "cancelled", cancelled.status
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(mark_manual_review), executor.submit(cancel)]
        outcomes = [future.result(timeout=20) for future in futures]

    assert ("manual_review", BankPaymentOrder.Status.MANUAL_REVIEW) in outcomes
    assert ("cancel_error", "bank_payment_order_not_cancellable") in outcomes
    assert not {
        code
        for kind, code in outcomes
        if kind.endswith("error") and code in {"deadlock_detected", "database_locked"}
    }
    order.refresh_from_db()
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=created.receipt["reservation_id"])
    assert order.status == BankPaymentOrder.Status.MANUAL_REVIEW
    assert reservation.status == PersonalBookingPaymentReservation.Status.MANUAL_REVIEW
    assert order.payment.status == Payment.Status.PENDING
    assert order.subscription.status == "pending"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL two-connection row-lock semantics",
)
def test_postgresql_complete_personal_confirmed_reservation_checkin_vs_cancel_is_coherent(
    settings,
    club,
    monkeypatch,
):
    """Confirmed reservation cancellation races check-in through slot -> origin -> enrollment."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1840.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-confirmed-checkin-cancel",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    order = BankPaymentOrder.objects.get(id=created.receipt["bank_payment_order_id"])
    process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=json.dumps(
            {
                "webhookType": "acquiringInternetPayment",
                "event_id": f"pg-confirmed-checkin-cancel-{order.id}",
                "status": "APPROVED",
                "paymentLinkId": order.provider_payment_link_id,
                "operationId": f"pg-confirmed-checkin-cancel-operation-{order.id}",
                "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            }
        ).encode(),
        headers={},
        request_id="pg-confirmed-checkin-cancel",
    )
    reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=created.receipt["reservation_id"])
    assert reservation.status == PersonalBookingPaymentReservation.Status.BOOKED
    gate = Barrier(2)

    def check_in():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=reservation.enrollment.schedule_id,
                training_type_id=training_type.id,
                source=Checkin.Source.KIOSK,
                checkin_date=reservation.enrollment.starts_on,
            )
            return "checkin", result["checkin_id"]
        except BusinessLogicError as exc:
            return "checkin_error", exc.code
        finally:
            close_old_connections()

    def cancel():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = cancel_personal_booking(
                club_id=club.id,
                enrollment_id=reservation.enrollment_id,
                actor_user_id=owner.id,
                origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
                reason="PostgreSQL confirmed reservation check-in/cancel race",
            )
            return "cancel", result.enrollment.status
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in [executor.submit(check_in), executor.submit(cancel)]]

    assert not {
        code
        for kind, code in outcomes
        if kind.endswith("error") and code in {"deadlock_detected", "database_locked"}
    }
    reservation.refresh_from_db()
    slot.refresh_from_db()
    checkin_exists = Checkin.objects.for_club(club).filter(
        student_id=student.id,
        schedule_id=reservation.enrollment.schedule_id,
        date=reservation.enrollment.starts_on,
        deleted_at__isnull=True,
    ).exists()
    if checkin_exists:
        assert ("cancel_error", "booking_already_checked_in") in outcomes
        assert reservation.enrollment.status == ScheduleEnrollment.Status.ACTIVE
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
    else:
        assert ("cancel", ScheduleEnrollment.Status.CANCELLED) in outcomes
        assert any(kind == "checkin_error" for kind, _value in outcomes)
        assert reservation.enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL partial unique constraint semantics",
)
def test_postgresql_0043_terminal_personal_order_history_allows_one_new_live_attempt(settings, club):
    """0043's live-origin index retains terminal history but admits a retry."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1850.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    first = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-0043-terminal-history",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    terminal_order = BankPaymentOrder.objects.get(id=first.receipt["bank_payment_order_id"])
    cancel_bank_payment_order(
        club_id=club.id,
        order_id=terminal_order.id,
        actor_user_id=owner.id,
        reason="make pre-existing terminal order history",
    )
    retry_offer = resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    retry_slot = PersonalAvailabilitySlot.objects.get(id=slot.id)
    retry_digest = personal_offer_payload(slot=retry_slot, offer=retry_offer)["offer_digest"]
    retry = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=retry_digest,
        idempotency_key="pg-0043-new-live-attempt",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    live_order = BankPaymentOrder.objects.get(id=retry.receipt["bank_payment_order_id"])
    orders = list(
        BankPaymentOrder.objects.for_club(club)
        .filter(
            personal_booking_reservation_id_snapshot__in=[
                first.receipt["reservation_id"],
                retry.receipt["reservation_id"],
            ]
        )
        .order_by("id")
    )
    terminal_order.refresh_from_db()
    live_order.refresh_from_db()
    assert terminal_order.id != live_order.id
    assert terminal_order.status == BankPaymentOrder.Status.CANCELLED
    assert live_order.status in {BankPaymentOrder.Status.CREATED, BankPaymentOrder.Status.PENDING}
    assert [order.id for order in orders] == [terminal_order.id, live_order.id]


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL durable command-claim collision semantics",
)
def test_postgresql_staff_command_claim_rejects_cross_family_key_collision(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1860.00"))
    entitlement_student = StudentFactory(club=club, status="active")
    subscription = SubscriptionFactory(
        club=club,
        student=entitlement_student,
        tariff=tariff,
        trainings_left=1,
        status=Subscription.Status.ACTIVE,
        scope=Tariff.Scope.LOCATION,
        location=location,
        expires_at=timezone.now() + timedelta(days=30),
    )
    paid_student = StudentFactory(club=club, status="lead")
    slot, _digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    direct_starts_at = slot.starts_at + timedelta(hours=2)
    direct_digest = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=direct_starts_at,
        ends_at=direct_starts_at + timedelta(hours=1),
        location_id=location.id,
        training_type_id=training_type.id,
    )["offer_digest"]
    gate = Barrier(2)

    def entitlement():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_personal_intent(
                club_id=club.id,
                slot_id=slot.id,
                student_id=entitlement_student.id,
                payment_method="entitlement",
                subscription_id=subscription.id,
                offer_digest=None,
                idempotency_key="pg-cross-family-command-key",
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
            )
            return "result", result.receipt["enrollment_id"]
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    def paid_direct():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = submit_staff_direct_personal_intent(
                club_id=club.id,
                student_id=paid_student.id,
                trainer_id=trainer.id,
                starts_at=direct_starts_at,
                ends_at=direct_starts_at + timedelta(hours=1),
                location_id=location.id,
                training_type_id=training_type.id,
                payment_method="cash",
                subscription_id=None,
                offer_digest=direct_digest,
                idempotency_key="pg-cross-family-command-key",
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
            )
            return "result", result.receipt["booking_id"]
        except BusinessLogicError as exc:
            return "business_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(entitlement), executor.submit(paid_direct)]
        outcomes = [future.result(timeout=20) for future in futures]

    assert sum(kind == "result" for kind, _value in outcomes) == 1, outcomes
    assert ("business_error", "idempotency_conflict") in outcomes
    assert PersonalStaffIntentCommand.objects.for_club(club).filter(
        command_key="pg-cross-family-command-key"
    ).count() == 1
    assert not {
        value
        for kind, value in outcomes
        if kind == "business_error" and value in {"deadlock_detected", "database_locked"}
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL snapshot-order checkin/cancel race semantics",
)
def test_postgresql_unattached_snapshot_order_checkin_vs_cancel_is_coherent(settings, club, monkeypatch):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1870.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(club=club, trainer=trainer, location=location, training_type=training_type)
    intent = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-snapshot-race-booking",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    booking = PersonalDropInBooking.objects.get(id=intent.receipt["booking_id"])
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner.id,
        seller_trainer_id=trainer.id,
        package_owner_trainer_id=trainer.id,
        debt_ids=[],
        allow_reuse=False,
        personal_drop_in_booking_id=booking.id,
        command_idempotency_key="pg-snapshot-race-order",
    )
    assert not PersonalDropInPaymentLink.objects.for_club(club).filter(bank_payment_order=order).exists()
    gate = Barrier(2)

    def check_in():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=booking.enrollment.schedule_id,
                training_type_id=training_type.id,
                source=Checkin.Source.KIOSK,
                checkin_date=booking.enrollment.starts_on,
            )
            return "checkin", result["checkin_id"]
        except BusinessLogicError as exc:
            return "checkin_error", exc.code
        finally:
            close_old_connections()

    def cancel():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            result = cancel_personal_drop_in_booking(
                club_id=club.id,
                booking_id=booking.id,
                actor_user_id=owner.id,
                reason="pg snapshot owned order race",
            )
            return "cancel", result.state
        except BusinessLogicError as exc:
            return "cancel_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in [executor.submit(check_in), executor.submit(cancel)]]

    assert not {
        value
        for kind, value in outcomes
        if kind.endswith("error") and value in {"deadlock_detected", "database_locked"}
    }
    booking.refresh_from_db()
    order.refresh_from_db()
    if booking.state == PersonalDropInBooking.State.ATTENDED:
        debt = Debt.objects.for_club(club).get(checkin_id=booking.checkin_id)
        assert debt.settlement_payment_id == order.payment_id
        assert any(kind == "cancel_error" for kind, _value in outcomes)
    else:
        assert booking.state == PersonalDropInBooking.State.CANCELLED
        assert order.status == BankPaymentOrder.Status.CANCELLED
        assert order.payment.status == Payment.Status.REJECTED
        assert any(kind == "checkin_error" for kind, _value in outcomes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL confirmation/catalog mutation race semantics",
)
def test_postgresql_complete_reservation_confirmation_grandfathers_terms_during_default_mutation(
    settings,
    club,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    tariff = _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1880.00"))
    # Provider verification normally converts a paid lead before confirmation;
    # use the resulting active projection to isolate catalog concurrency.
    student = StudentFactory(club=club, status="active")
    slot, digest = _slot_and_digest(club=club, trainer=trainer, location=location, training_type=training_type)
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-confirm-default-mutation",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )
    reservation = PersonalBookingPaymentReservation.objects.get(id=created.receipt["reservation_id"])
    reservation.payment.status = Payment.Status.CONFIRMED
    reservation.payment.save(update_fields=["status", "updated_at"])
    reservation.subscription.status = Subscription.Status.ACTIVE
    reservation.subscription.expires_at = timezone.now() + timedelta(days=30)
    reservation.subscription.save(update_fields=["status", "expires_at", "updated_at"])
    reservation.bank_payment_order.status = BankPaymentOrder.Status.APPROVED
    reservation.bank_payment_order.save(update_fields=["status", "updated_at"])
    frozen_amount = PersonalServiceTermsSnapshot.objects.get(reservation=reservation).payable_amount
    gate = Barrier(2)

    def confirm():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            confirmed = confirm_personal_booking_payment_reservation_for_order(
                club_id=club.id,
                order_id=reservation.bank_payment_order_id,
                actor_user_id=owner.id,
            )
            return "confirmed", confirmed.status if confirmed is not None else None
        except BusinessLogicError as exc:
            return "confirm_error", exc.code
        finally:
            close_old_connections()

    def mutate_default():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                is_personal_booking_default=False,
            )
            return "mutated", "ok"
        except BusinessLogicError as exc:
            return "mutation_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in [executor.submit(confirm), executor.submit(mutate_default)]]

    assert ("confirmed", PersonalBookingPaymentReservation.Status.BOOKED) in outcomes
    assert ("mutated", "ok") in outcomes
    assert not {
        value
        for kind, value in outcomes
        if kind.endswith("error") and value in {"deadlock_detected", "database_locked"}
    }
    reservation.refresh_from_db()
    assert reservation.status == PersonalBookingPaymentReservation.Status.BOOKED
    assert reservation.schedule_id is not None
    assert PersonalServiceTermsSnapshot.objects.get(reservation=reservation).payable_amount == frozen_amount


@pytest.mark.django_db
def test_flag_off_complete_terms_manual_drop_in_keeps_its_immutable_disposition(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1900.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="staff-flag-off-pending",
        actor_user_id=owner.id,
        bank_source="owner",
    )
    payment = Payment.objects.get(id=created.receipt["payment_id"])
    booking = PersonalDropInBooking.objects.get(id=created.receipt["booking_id"])

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    cancel_personal_drop_in_booking(
        club_id=club.id,
        booking_id=booking.id,
        actor_user_id=owner.id,
        reason="complete terms remains the lifecycle authority while rollout is off",
    )
    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == Payment.Status.REJECTED
    assert booking.state == PersonalDropInBooking.State.CANCELLED


@pytest.mark.django_db
def test_staff_intent_endpoint_returns_persistent_receipt(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )

    response = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json={
            "student_id": student.id,
            "payment_method": "pay_at_visit",
            "offer_digest": digest,
            "idempotency_key": "endpoint-visit-1",
        },
        **make_auth_params(owner_user, club),
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "personal_staff_intent"
    assert response.json()["status"] == "pay_at_visit"
    assert response.json()["resource_route"].startswith("/api/personal-drop-in-bookings/")


@pytest.mark.django_db
def test_v2_personal_manual_route_gates_new_v1_writes_and_admits_exact_cash_lead(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status="lead", lead_status="new", assigned_trainer=trainer)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    payload = {
        "student_id": student.id,
        "payment_method": "cash",
        "offer_digest": digest,
        "idempotency_key": "v2-personal-route",
    }

    legacy = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json=payload,
        **make_auth_params(owner_user, club),
    )

    assert legacy.status_code == 400
    assert legacy.json()["code"] == "client_upgrade_required"
    assert not PersonalDropInBooking.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()

    created = client.post(
        f"/personal-availability/v2/slots/{slot.id}/staff-intents/",
        json={"protocol_version": "v2", **payload},
        **make_auth_params(owner_user, club),
    )

    assert created.status_code == 201
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert Payment.objects.for_club(club).count() == 1
    assert created.json()["workspace_state"] == "student"
    assert created.json()["finance_state"] == "pending_manual"
    assert created.json()["command_replayed"] is False

    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
    replay_after_gate_flip = client.post(
        f"/personal-availability/v2/slots/{slot.id}/staff-intents/",
        json={"protocol_version": "v2", **payload},
        **make_auth_params(owner_user, club),
    )
    assert replay_after_gate_flip.status_code == 200
    assert replay_after_gate_flip.json()["command_replayed"] is True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

    confirmed = verify_payment(
        payment_id=created.json()["payment_id"],
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
    )
    assert confirmed.status == Payment.Status.CONFIRMED
    replay = client.post(
        f"/personal-availability/v2/slots/{slot.id}/staff-intents/",
        json={"protocol_version": "v2", **payload},
        **make_auth_params(owner_user, club),
    )
    assert replay.status_code == 200
    assert replay.json()["workspace_state"] == "student"
    assert replay.json()["finance_state"] == "confirmed"
    assert replay.json()["command_replayed"] is True


@pytest.mark.django_db
def test_failed_unbound_v1_personal_claim_cannot_bypass_v2_cutover(
    settings,
    club,
    owner_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
        },
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status=Student.Status.LEAD)
    slot, _digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    payload = {
        "student_id": student.id,
        "payment_method": "cash",
        "offer_digest": "invalid-offer",
        "idempotency_key": "failed-v1-personal-claim",
    }
    failed = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert failed.status_code == 400
    assert not PersonalDropInBooking.objects.for_club(club).exists()

    settings_row = ClubSettings.objects.get(club=club)
    settings_row.commercial_journey_protocol_version = ClubSettings.CommercialJourneyProtocol.V2
    settings_row.save(update_fields=["commercial_journey_protocol_version", "updated_at"])
    blocked = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json=payload,
        **make_auth_params(owner_user, club),
    )

    assert blocked.status_code == 400
    assert blocked.json()["code"] == "client_upgrade_required"
    assert not PersonalDropInBooking.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()


@pytest.mark.django_db
def test_v2_manual_service_gate_rejects_new_personal_artifacts_but_allows_exact_replay(
    settings,
    club,
):
    """The second, locked gate is authoritative even after an HTTP preflight."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1210.00"))
    student = StudentFactory(club=club, status=Student.Status.LEAD, lead_status=Student.LeadStatus.NEW)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    command = {
        "club_id": club.id,
        "slot_id": slot.id,
        "student_id": student.id,
        "payment_method": Payment.Method.CASH,
        "subscription_id": None,
        "offer_digest": digest,
        "idempotency_key": "v2-personal-service-gate",
        "actor_user_id": owner.id,
        "bank_source": BankPaymentOrder.Source.OWNER,
        "v2_manual_admission_command": True,
    }

    with pytest.raises(BusinessLogicError) as denied:
        submit_staff_personal_intent(**command)

    assert denied.value.code == "commercial_journey_unavailable"
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
    assert not PersonalDropInBooking.objects.for_club(club).exists()
    assert not PersonalDropInPaymentLink.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()

    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    created = submit_staff_personal_intent(**command)
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
    replayed = submit_staff_personal_intent(**command)
    assert replayed.created is False
    assert replayed.receipt["payment_id"] == created.receipt["payment_id"]
    assert PersonalDropInBooking.objects.for_club(club).count() == 1
    assert PersonalDropInPaymentLink.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_v2_direct_sbp_keeps_lead_and_replays_current_provider_state(
    settings,
    club,
    trainer_user,
):
    """A cached v1 direct client cannot create artifacts after v2 cutover."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    trainer = TrainerFactory(club=club, user=trainer_user, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1220.00"))
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
        assigned_trainer=trainer,
    )
    starts_at = (timezone.now() + timedelta(days=10)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    ends_at = starts_at + timedelta(hours=1)
    preview = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    payload = {
        "student_id": student.id,
        "trainer_id": trainer.id,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "location_id": location.id,
        "training_type_id": training_type.id,
        "payment_method": "sbp",
        "offer_digest": preview["offer_digest"],
        "idempotency_key": "v2-direct-sbp-replay",
    }

    legacy = client.post(
        "/personal-availability/staff-intents/direct/",
        json=payload,
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert legacy.status_code == 400
    assert legacy.json()["code"] == "client_upgrade_required"
    assert not PersonalDropInBooking.objects.for_club(club).exists()
    assert not Payment.objects.for_club(club).exists()

    created = client.post(
        "/personal-availability/v2/staff-intents/direct/",
        json={"protocol_version": "v2", **payload},
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert created.status_code == 201
    assert created.json()["workspace_state"] == "lead"
    assert created.json()["finance_state"] == "provider_pending"
    assert created.json()["command_replayed"] is False
    student.refresh_from_db()
    assert student.status == Student.Status.LEAD
    assert student.lead_status == Student.LeadStatus.NEW

    replay = client.post(
        "/personal-availability/v2/staff-intents/direct/",
        json={"protocol_version": "v2", **payload},
        **make_auth_params(trainer_user, club, role="trainer"),
    )
    assert replay.status_code == 200
    assert replay.json()["workspace_state"] == "lead"
    assert replay.json()["finance_state"] == "provider_pending"
    assert replay.json()["command_replayed"] is True
    assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1


def _personal_correction_sbp_attempt(*, settings, club, price=Decimal("1800.00")):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=price,
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Correction discount",
        discount_type="fixed",
        value=Decimal("300.00"),
    )
    student = StudentFactory(club=club, status="lead")
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=timezone.now() + timedelta(days=12),
        ends_at=timezone.now() + timedelta(days=12, hours=1),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        training_type_id=training_type.id,
        location_id=location.id,
        discount_id=discount.id,
    )
    receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=personal_offer_payload(slot=slot, offer=offer)["offer_digest"],
        discount_id=discount.id,
        idempotency_key="personal-correction-sbp-origin",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    return owner, student, slot, PersonalBookingPaymentReservation.objects.get(id=receipt["reservation_id"])


def _personal_correction_direct_sbp_attempt(*, settings, club, price=Decimal("2100.00")):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(
        club=club,
        training_type=training_type,
        location=location,
        price=price,
        personal_booking_trainer=trainer,
    )
    discount = DiscountFactory(
        club=club,
        name="Direct correction discount",
        discount_type="fixed",
        value=Decimal("400.00"),
    )
    student = StudentFactory(club=club, status="lead")
    starts_at = (timezone.now() + timedelta(days=13)).replace(minute=0, second=0, microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    preview = get_staff_direct_personal_offer(
        club_id=club.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        discount_id=discount.id,
    )
    receipt = submit_staff_direct_personal_intent(
        club_id=club.id,
        student_id=student.id,
        trainer_id=trainer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location_id=location.id,
        training_type_id=training_type.id,
        payment_method="sbp",
        subscription_id=None,
        offer_digest=preview["offer_digest"],
        discount_id=discount.id,
        idempotency_key="personal-direct-correction-sbp-origin",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    reservation = PersonalBookingPaymentReservation.objects.get(id=receipt["reservation_id"])
    assert reservation.availability_slot_id is None
    return owner, student, reservation


@pytest.mark.django_db
@pytest.mark.parametrize("replacement_method", [Payment.Method.CASH, Payment.Method.TRANSFER, "pay_at_visit"])
def test_safe_undispatched_sbp_method_correction_reuses_exact_slot_and_frozen_terms(
    settings, club, replacement_method
):
    correction_key = f"personal-correction-{replacement_method}".ljust(120, "x")
    owner, student, slot, reservation = _personal_correction_sbp_attempt(settings=settings, club=club)
    original_order = reservation.bank_payment_order
    original_payment = reservation.payment
    original_terms = PersonalServiceTermsSnapshot.objects.get(reservation=reservation)
    owner_receipt = next(
        item
        for item in get_personal_commercial_context(
            club_id=club.id,
            student_id=student.id,
            actor_role="owner",
        )
        if item["reservation_id"] == reservation.id
    )
    trainer_receipt = next(
        item
        for item in get_personal_commercial_context(
            club_id=club.id,
            student_id=student.id,
            trainer_id=reservation.trainer_id,
            actor_role="trainer",
        )
        if item["reservation_id"] == reservation.id
    )
    assert owner_receipt["allowed_actions"] == [
        "open_bank_payment_order",
        "cancel_if_safe",
        "replace_payment_method",
    ]
    assert trainer_receipt["allowed_actions"] == ["owner_review"]
    assert trainer_receipt["provider_payment_url"] == ""

    receipt = replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        reservation_id=reservation.id,
        replacement_payment_method=replacement_method,
        reason="Wrong payment method selected",
        idempotency_key=correction_key,
    )

    reservation.refresh_from_db()
    original_order.refresh_from_db()
    original_payment.refresh_from_db()
    slot.refresh_from_db()
    replacement = PersonalDropInBooking.objects.get(id=receipt["booking_id"])
    replacement_terms = PersonalServiceTermsSnapshot.objects.get(booking=replacement)
    correction = PersonalPaymentMethodCorrection.objects.get(
        club=club,
        idempotency_key=correction_key,
    )
    assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
    assert original_order.status == BankPaymentOrder.Status.CANCELLED
    assert original_payment.status == Payment.Status.REJECTED
    assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
    assert slot.booked_enrollment_id == replacement.enrollment_id
    assert replacement.price_snapshot == original_terms.payable_amount
    assert replacement_terms.payable_amount == original_terms.payable_amount
    assert replacement_terms.discount_id_snapshot == original_terms.discount_id_snapshot
    assert correction.original_reservation_id == reservation.id
    assert correction.replacement_booking_id == replacement.id
    assert correction.replacement_payment_method == replacement_method
    assert correction.command_shape == {
        "origin": "reservation",
        "reservation_id": reservation.id,
        "bank_payment_order_id": original_order.id,
        "source_terms_id": original_terms.id,
        "terms_version": PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2,
        "tariff_id": original_terms.tariff_id_snapshot,
        "discount_id": original_terms.discount_id_snapshot,
        "base_amount": str(original_terms.base_amount),
        "discount_amount": str(original_terms.discount_amount),
        "payable_amount": str(original_terms.payable_amount),
        "replacement_payment_method": replacement_method,
        "reason": "Wrong payment method selected",
    }
    if replacement_method == "pay_at_visit":
        assert receipt["payment_id"] is None
        assert correction.replacement_payment_link_id is None
        reload_receipt = next(
            item
            for item in get_personal_commercial_context(
                club_id=club.id,
                student_id=student.id,
                actor_role="owner",
            )
            if item["booking_id"] == replacement.id and item["payment_method"] == "pay_at_visit"
        )
        assert reload_receipt["status"] == "pay_at_visit"
    else:
        assert receipt["payment_id"] == correction.replacement_payment_link.payment_id
        assert correction.replacement_payment_link.payment.amount == original_terms.payable_amount

    replay = replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        reservation_id=reservation.id,
        replacement_payment_method=replacement_method,
        reason="Wrong payment method selected",
        idempotency_key=correction_key,
    )
    assert replay["booking_id"] == replacement.id
    with pytest.raises(BusinessLogicError) as exc_info:
        replace_personal_payment_method(
            club_id=club.id,
            student_id=student.id,
            actor_user_id=owner.id,
            actor_role="owner",
            reservation_id=reservation.id,
            replacement_payment_method="transfer" if replacement_method != "transfer" else "cash",
            reason="Wrong payment method selected",
            idempotency_key=correction_key,
        )
    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.django_db
@pytest.mark.parametrize("replacement_method", [Payment.Method.CASH, Payment.Method.TRANSFER, "pay_at_visit"])
def test_safe_undispatched_direct_sbp_correction_preserves_exact_time_and_terms(
    settings, club, replacement_method
):
    club.timezone = "Asia/Yekaterinburg"
    club.save(update_fields=["timezone", "updated_at"])
    owner, student, reservation = _personal_correction_direct_sbp_attempt(settings=settings, club=club)
    original_terms = PersonalServiceTermsSnapshot.objects.get(reservation=reservation)

    receipt = replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        reservation_id=reservation.id,
        replacement_payment_method=replacement_method,
        reason="Direct booking used the wrong payment method",
        idempotency_key=f"personal-direct-correction-{replacement_method}",
    )

    reservation.refresh_from_db()
    replacement = PersonalDropInBooking.objects.select_related("enrollment__schedule").get(
        id=receipt["booking_id"]
    )
    replacement_terms = PersonalServiceTermsSnapshot.objects.get(booking=replacement)
    local_starts_at = timezone.localtime(reservation.starts_at, club_zoneinfo(club))
    local_ends_at = timezone.localtime(reservation.ends_at, club_zoneinfo(club))
    assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
    assert replacement.enrollment.schedule.one_time_date == local_starts_at.date()
    assert replacement.enrollment.schedule.start_time == local_starts_at.timetz().replace(tzinfo=None)
    assert replacement.enrollment.schedule.end_time == local_ends_at.timetz().replace(tzinfo=None)
    assert replacement.price_snapshot == original_terms.payable_amount == Decimal("1700.00")
    assert replacement_terms.payable_amount == original_terms.payable_amount
    assert replacement_terms.discount_id_snapshot == original_terms.discount_id_snapshot
    assert receipt["payment_method"] == replacement_method
    assert not PersonalAvailabilitySlot.objects.filter(booked_enrollment=replacement.enrollment).exists()


@pytest.mark.django_db
def test_personal_payment_method_correction_allows_one_finalization_then_is_immutable(
    settings,
    club,
    other_club,
):
    owner, student, _slot, reservation = _personal_correction_sbp_attempt(settings=settings, club=club)
    receipt = replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        reservation_id=reservation.id,
        replacement_payment_method="pay_at_visit",
        reason="Wrong payment method selected",
        idempotency_key="immutable-personal-correction",
    )
    correction = PersonalPaymentMethodCorrection.objects.get(replacement_booking_id=receipt["booking_id"])

    correction.club_id = other_club.id
    with pytest.raises(ValidationError, match="append-only"):
        correction.save()
    correction.club_id = club.id
    correction.reason = "Rewritten history"
    with pytest.raises(ValidationError, match="append-only"):
        correction.save()
    with pytest.raises(ValidationError, match="append-only"):
        PersonalPaymentMethodCorrection.objects.filter(id=correction.id).update(reason="Rewritten history")
    with pytest.raises(ValidationError, match="append-only"):
        correction.delete()
    with pytest.raises(ValidationError, match="append-only"):
        PersonalPaymentMethodCorrection.objects.filter(id=correction.id).delete()


@pytest.mark.django_db
def test_payment_correction_locks_catalog_prefix_before_mutable_financial_scope(
    settings,
    club,
    monkeypatch,
):
    owner, student, _slot, reservation = _personal_correction_sbp_attempt(
        settings=settings,
        club=club,
    )
    lock_calls: list[str] = []
    original_select_for_update = QuerySet.select_for_update

    def record_select_for_update(queryset, *args, **kwargs):
        if queryset.model.__name__ in {
            "TrainingType",
            "Trainer",
            "Tariff",
            "Student",
            "Payment",
        }:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", record_select_for_update)
    replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        reservation_id=reservation.id,
        replacement_payment_method="pay_at_visit",
        reason="Ordered catalog prefix proof",
        idempotency_key="personal-correction-catalog-prefix-proof",
    )

    expected_prefix = ["TrainingType", "Trainer", "Tariff", "Student", "Payment"]
    assert [lock_calls.index(model) for model in expected_prefix] == sorted(
        lock_calls.index(model) for model in expected_prefix
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL observable transaction wait semantics",
)
def test_postgresql_catalog_validation_waits_on_ordered_payment_correction_prefix(
    settings,
    club,
    monkeypatch,
):
    """Catalog may wait on correction's prefix, but correction never waits back."""

    owner, student, _slot, reservation = _personal_correction_sbp_attempt(
        settings=settings,
        club=club,
    )
    terms = PersonalServiceTermsSnapshot.objects.get(reservation=reservation)
    correction_at_cancel = Event()
    release_correction = Event()
    catalog_started = Event()
    backend_pids: dict[str, int] = {}
    original_cancel = cancel_bank_payment_order

    def current_backend_pid() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def catalog_waits_on_correction() -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks AS waiter
                    JOIN pg_locks AS holder
                        ON holder.locktype = 'transactionid'
                        AND holder.transactionid = waiter.transactionid
                        AND holder.granted
                    WHERE waiter.pid = %s
                        AND holder.pid = %s
                        AND waiter.locktype = 'transactionid'
                        AND NOT waiter.granted
                )
                """,
                [backend_pids["catalog"], backend_pids["correction"]],
            )
            return cursor.fetchone()[0]

    def wait_for_observable_catalog_wait() -> bool:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if catalog_waits_on_correction():
                return True
        return False

    def pause_after_ordered_correction_scope(**kwargs):
        backend_pids["correction"] = current_backend_pid()
        correction_at_cancel.set()
        if not release_correction.wait(timeout=10):
            raise TimeoutError("test did not release payment correction")
        return original_cancel(**kwargs)

    monkeypatch.setattr(
        "apps.attendance.services.personal_payment_corrections.cancel_bank_payment_order",
        pause_after_ordered_correction_scope,
    )

    def correct_method():
        close_old_connections()
        try:
            return replace_personal_payment_method(
                club_id=club.id,
                student_id=student.id,
                actor_user_id=owner.id,
                actor_role="owner",
                reservation_id=reservation.id,
                replacement_payment_method="cash",
                reason="Observable correction/catalog lock proof",
                idempotency_key="pg-correction-catalog-lock-proof",
            )
        finally:
            close_old_connections()

    def validate_catalog_default():
        close_old_connections()
        try:
            backend_pids["catalog"] = current_backend_pid()
            catalog_started.set()
            return update_tariff(
                tariff_id=terms.tariff_id_snapshot,
                club_id=club.id,
                is_personal_booking_default=True,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        correction_future = executor.submit(correct_method)
        assert correction_at_cancel.wait(timeout=10)
        catalog_future = executor.submit(validate_catalog_default)
        assert catalog_started.wait(timeout=10)
        assert wait_for_observable_catalog_wait()
        release_correction.set()
        correction_receipt = correction_future.result(timeout=20)
        catalog_tariff = catalog_future.result(timeout=20)

    assert correction_receipt["payment_method"] == Payment.Method.CASH
    assert catalog_tariff.id == terms.tariff_id_snapshot


@pytest.mark.django_db
def test_pending_manual_method_correction_rejects_old_payment_before_appending_replacement(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    owner = UserFactory()
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1600.00"))
    student = StudentFactory(club=club, status="lead")
    slot, digest = _slot_and_digest(
        club=club, trainer=trainer, location=location, training_type=training_type
    )
    original = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="personal-correction-manual-origin",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    original_payment = Payment.objects.get(id=original["payment_id"])

    replacement = replace_personal_payment_method(
        club_id=club.id,
        student_id=student.id,
        actor_user_id=owner.id,
        actor_role="owner",
        payment_id=original_payment.id,
        replacement_payment_method="transfer",
        reason="Client paid by transfer",
        idempotency_key="personal-correction-manual-transfer",
    )
    original_payment.refresh_from_db()
    replacement_payment = Payment.objects.get(id=replacement["payment_id"])
    assert original_payment.status == Payment.Status.REJECTED
    assert replacement_payment.status == Payment.Status.PENDING
    assert replacement_payment.payment_method == Payment.Method.TRANSFER
    assert replacement_payment.amount == original_payment.amount


@pytest.mark.django_db
def test_dispatched_sbp_and_confirmed_manual_are_refused_without_replacement(settings, club):
    owner, student, slot, reservation = _personal_correction_sbp_attempt(settings=settings, club=club)
    order = reservation.bank_payment_order
    order.provider = BankPaymentOrder.Provider.TOCHKA
    order.link_creation_state = BankPaymentOrder.LinkCreationState.DISPATCHED
    order.save(update_fields=["provider", "link_creation_state", "updated_at"])
    reconciliation_receipt = next(
        item
        for item in get_personal_commercial_context(
            club_id=club.id,
            student_id=student.id,
            actor_role="owner",
        )
        if item["reservation_id"] == reservation.id
    )
    assert reconciliation_receipt["allowed_actions"] == ["refresh_or_reconcile", "owner_review"]

    with pytest.raises(BusinessLogicError) as exc_info:
        replace_personal_payment_method(
            club_id=club.id,
            student_id=student.id,
            actor_user_id=owner.id,
            actor_role="owner",
            reservation_id=reservation.id,
            replacement_payment_method="cash",
            reason="Wrong payment method selected",
            idempotency_key="personal-correction-dispatched",
        )
    assert exc_info.value.code == "personal_payment_replacement_reconciliation_required"
    assert PersonalDropInBooking.objects.for_club(club).count() == 0
    slot.refresh_from_db()
    assert slot.status == PersonalAvailabilitySlot.Status.HELD

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=True)
    manual_owner = UserFactory()
    manual_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    manual_location = LocationFactory(club=club)
    manual_trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=manual_trainer, location=manual_location, rate_personal=50)
    _complete_default(club=club, training_type=manual_type, location=manual_location, price=Decimal("1400.00"))
    manual_student = StudentFactory(club=club, status="lead")
    manual_slot, manual_digest = _slot_and_digest(
        club=club, trainer=manual_trainer, location=manual_location, training_type=manual_type
    )
    manual_receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=manual_slot.id,
        student_id=manual_student.id,
        payment_method="cash",
        subscription_id=None,
        offer_digest=manual_digest,
        idempotency_key="personal-correction-confirmed-origin",
        actor_user_id=manual_owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    ).receipt
    confirmed_payment = Payment.objects.get(id=manual_receipt["payment_id"])
    confirmed_payment.status = Payment.Status.CONFIRMED
    confirmed_payment.save(update_fields=["status", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        replace_personal_payment_method(
            club_id=club.id,
            student_id=manual_student.id,
            actor_user_id=manual_owner.id,
            actor_role="owner",
            payment_id=confirmed_payment.id,
            replacement_payment_method="transfer",
            reason="Wrong method",
            idempotency_key="personal-correction-confirmed",
        )
    assert exc_info.value.code == "personal_payment_replacement_refund_required"


@pytest.mark.django_db
def test_payment_method_correction_endpoint_conceals_foreign_student_and_trainer_scope(
    settings,
    club,
    other_club,
):
    owner, student, _slot, reservation = _personal_correction_sbp_attempt(settings=settings, club=club)
    route = f"/students/{student.id}/personal-commercial-attempts/replace-payment-method/"
    payload = {
        "reservation_id": reservation.id,
        "replacement_payment_method": "cash",
        "reason": "Client chose cash",
        "idempotency_key": "personal-correction-endpoint",
    }
    unrelated_trainer_user = UserFactory()
    TrainerFactory(club=club, user=unrelated_trainer_user, is_active=True)
    denied_trainer = client.post(
        route,
        json=payload,
        **make_auth_params(unrelated_trainer_user, club, role="trainer"),
    )
    assert denied_trainer.status_code == 403

    denied_role = client.post(
        route,
        json=payload,
        **make_auth_params(UserFactory(), club, role="student"),
    )
    assert denied_role.status_code == 403

    foreign_club = client.post(
        route,
        json=payload,
        **make_auth_params(owner, other_club),
    )
    assert foreign_club.status_code == 404

    corrected = client.post(route, json=payload, **make_auth_params(owner, club))
    assert corrected.status_code == 200
    assert corrected.json()["reservation_id"] is None
    assert corrected.json()["payment_method"] == Payment.Method.CASH


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL manual-admission replay/review contention semantics",
)
def test_postgresql_manual_admission_replay_confirm_reject_keeps_one_exact_person_transition(
    settings,
    club,
):
    """Replay and competing owner decisions cannot duplicate person effects."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1530.00"))
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
        phone="8 900 123 45 67",
    )
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    created = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method=Payment.Method.CASH,
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-manual-admission-replay-review",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
        v2_manual_admission_command=True,
        command_protocol_version="v2",
    )
    payment_id = created.receipt["payment_id"]
    gate = Barrier(3)

    def replay():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            replayed = submit_staff_personal_intent(
                club_id=club.id,
                slot_id=slot.id,
                student_id=student.id,
                payment_method=Payment.Method.CASH,
                subscription_id=None,
                offer_digest=digest,
                idempotency_key="pg-manual-admission-replay-review",
                actor_user_id=owner.id,
                bank_source=BankPaymentOrder.Source.OWNER,
                v2_manual_admission_command=True,
                command_protocol_version="v2",
            )
            return "replay", replayed.receipt["payment_id"]
        except BusinessLogicError as exc:
            return "replay_error", exc.code
        finally:
            close_old_connections()

    def review(action):
        close_old_connections()
        try:
            gate.wait(timeout=10)
            reviewed = verify_payment(
                payment_id=payment_id,
                club_id=club.id,
                verified_by_id=owner.id,
                action=action,
                rejection_reason="race review" if action == "reject" else "",
            )
            return action, reviewed.status
        except BusinessLogicError as exc:
            return f"{action}_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = [
            future.result(timeout=20)
            for future in (
                executor.submit(replay),
                executor.submit(review, "confirm"),
                executor.submit(review, "reject"),
            )
        ]

    assert ("replay", payment_id) in outcomes
    assert not {
        value
        for kind, value in outcomes
        if kind.endswith("error") and value in {"deadlock_detected", "database_locked"}
    }
    payment = Payment.objects.for_club(club).select_related("subscription").get(id=payment_id)
    assert payment.status in {Payment.Status.CONFIRMED, Payment.Status.REJECTED}
    assert PersonalDropInPaymentLink.objects.for_club(club).filter(payment=payment).count() == 1
    assert PersonalDropInBooking.objects.for_club(club).count() == 1
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=student,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        metadata__payment_id=payment.id,
    ).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL account-access/rejection contention semantics",
)
def test_postgresql_manual_admission_account_access_vs_reject_preserves_atomic_eligibility(
    settings,
    club,
):
    """Access may win while evidence is live, but reject never revokes it."""

    from apps.students.access_services import open_account_access_for_student
    from apps.students.models import AccountAccess
    from apps.students.selectors import is_account_access_eligible

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1540.00"))
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
        phone="8 900 123 45 68",
    )
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method=Payment.Method.TRANSFER,
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-manual-admission-access-reject",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
        v2_manual_admission_command=True,
        command_protocol_version="v2",
    ).receipt
    gate = Barrier(2)

    def open_access():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            access = open_account_access_for_student(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=owner.id,
            )
            return "access", access.created_access
        except BusinessLogicError as exc:
            return "access_error", exc.code
        finally:
            close_old_connections()

    def reject():
        close_old_connections()
        try:
            gate.wait(timeout=10)
            reviewed = verify_payment(
                payment_id=receipt["payment_id"],
                club_id=club.id,
                verified_by_id=owner.id,
                action="reject",
                rejection_reason="race reject",
            )
            return "reject", reviewed.status
        except BusinessLogicError as exc:
            return "reject_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result(timeout=20) for future in (executor.submit(open_access), executor.submit(reject))]

    assert ("reject", Payment.Status.REJECTED) in outcomes
    assert not {
        value
        for kind, value in outcomes
        if kind.endswith("error") and value in {"deadlock_detected", "database_locked"}
    }
    assert all(
        kind != "access_error" or value == "account_access_requires_paid_subscription"
        for kind, value in outcomes
    )
    access_won = any(kind == "access" for kind, _value in outcomes)
    assert AccountAccess.objects.for_club(club).filter(student_id=student.id).count() == int(access_won)
    payment = Payment.objects.for_club(club).select_related("subscription").get(id=receipt["payment_id"])
    assert payment.status == Payment.Status.REJECTED
    assert payment.subscription is not None
    assert payment.subscription.deleted_at is not None
    student.refresh_from_db()
    assert student.status == Student.Status.ACTIVE
    assert student.lead_status is None
    assert student.became_student_at is not None
    assert not is_account_access_eligible(club=club, student=student)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL correction/rejection lock contention semantics",
)
def test_postgresql_manual_admission_correction_vs_reject_keeps_one_replacement(
    settings,
    club,
    monkeypatch,
):
    """Correction holds the exact personal origin while a competing reject waits."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )
    owner = UserFactory()
    ClubMembershipFactory(user=owner, club=club, role=ClubMembership.Role.OWNER)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1550.00"))
    student = StudentFactory(
        club=club,
        status=Student.Status.LEAD,
        lead_status=Student.LeadStatus.NEW,
    )
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    receipt = submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=student.id,
        payment_method=Payment.Method.CASH,
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="pg-manual-admission-correction-reject",
        actor_user_id=owner.id,
        bank_source=BankPaymentOrder.Source.OWNER,
        v2_manual_admission_command=True,
        command_protocol_version="v2",
    ).receipt
    payment_id = receipt["payment_id"]
    correction_ready = Event()
    release_correction = Event()
    reject_started = Event()
    backend_pids: dict[str, int] = {}
    from apps.attendance.services.personal_payment_corrections import verify_payment as correction_verify

    def current_backend_pid() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def reject_waits_on_correction() -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks AS waiter
                    JOIN pg_locks AS holder
                        ON holder.locktype = 'transactionid'
                        AND holder.transactionid = waiter.transactionid
                        AND holder.granted
                    WHERE waiter.pid = %s
                        AND holder.pid = %s
                        AND waiter.locktype = 'transactionid'
                        AND NOT waiter.granted
                )
                """,
                [backend_pids["reject"], backend_pids["correction"]],
            )
            return cursor.fetchone()[0]

    def wait_for_observable_reject_wait() -> bool:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if reject_waits_on_correction():
                return True
        return False

    def pause_correction_review(**kwargs):
        backend_pids["correction"] = current_backend_pid()
        correction_ready.set()
        if not release_correction.wait(timeout=10):
            raise TimeoutError("test did not release payment correction")
        return correction_verify(**kwargs)

    monkeypatch.setattr(
        "apps.attendance.services.personal_payment_corrections.verify_payment",
        pause_correction_review,
    )

    def correct():
        close_old_connections()
        try:
            corrected = replace_personal_payment_method(
                club_id=club.id,
                student_id=student.id,
                actor_user_id=owner.id,
                actor_role="owner",
                payment_id=payment_id,
                replacement_payment_method=Payment.Method.TRANSFER,
                reason="PostgreSQL manual correction",
                idempotency_key="pg-manual-admission-correction-replacement",
            )
            return "correction", corrected["payment_id"]
        except BusinessLogicError as exc:
            return "correction_error", exc.code
        finally:
            close_old_connections()

    def reject():
        close_old_connections()
        try:
            assert correction_ready.wait(timeout=10)
            backend_pids["reject"] = current_backend_pid()
            reject_started.set()
            reviewed = verify_payment(
                payment_id=payment_id,
                club_id=club.id,
                verified_by_id=owner.id,
                action="reject",
                rejection_reason="competing review",
            )
            return "reject", reviewed.status
        except BusinessLogicError as exc:
            return "reject_error", exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        correction_future = executor.submit(correct)
        assert correction_ready.wait(timeout=10)
        reject_future = executor.submit(reject)
        assert reject_started.wait(timeout=10)
        assert wait_for_observable_reject_wait()
        release_correction.set()
        outcomes = [correction_future.result(timeout=20), reject_future.result(timeout=20)]

    assert outcomes[0][0] == "correction"
    assert outcomes[1] == ("reject", Payment.Status.REJECTED)
    assert not {
        value
        for kind, value in outcomes
        if kind.endswith("error") and value in {"deadlock_detected", "database_locked"}
    }
    original = Payment.objects.for_club(club).get(id=payment_id)
    replacement = Payment.objects.for_club(club).get(id=outcomes[0][1])
    assert original.status == Payment.Status.REJECTED
    assert replacement.status == Payment.Status.PENDING
    assert replacement.payment_method == Payment.Method.TRANSFER
    assert PersonalDropInPaymentLink.objects.for_club(club).filter(payment=replacement).count() == 1
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student_id=student.id,
        event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
    ).count() == 1


@pytest.mark.django_db
def test_accepted_v1_slot_manual_command_replays_after_v2_cutover_but_new_v1_key_is_denied(
    settings,
    club,
    owner_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
        },
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1200.00"))
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    payload = {
        "student_id": student.id,
        "payment_method": Payment.Method.CASH,
        "offer_digest": digest,
        "idempotency_key": "v1-personal-slot-replay-after-v2",
    }

    created = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert created.status_code == 201, created.json()
    assert PersonalDropInBooking.objects.for_club(club).count() == 1
    assert PersonalDropInPaymentLink.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1

    settings_row = ClubSettings.objects.get(club=club)
    settings_row.commercial_journey_protocol_version = ClubSettings.CommercialJourneyProtocol.V2
    settings_row.save(update_fields=["commercial_journey_protocol_version", "updated_at"])

    replay = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json()["booking_id"] == created.json()["booking_id"]
    assert replay.json()["payment_id"] == created.json()["payment_id"]

    denied = client.post(
        f"/personal-availability/slots/{slot.id}/staff-intents/",
        json={**payload, "idempotency_key": "v1-personal-slot-new-after-v2"},
        **make_auth_params(owner_user, club),
    )
    assert denied.status_code == 400
    assert denied.json()["code"] == "client_upgrade_required"
    assert PersonalDropInBooking.objects.for_club(club).count() == 1
    assert PersonalDropInPaymentLink.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_staff_sbp_attachment_handoff_retries_once_and_binds_the_durable_reservation(
    settings,
    club,
    owner_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club, is_active=True)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    _complete_default(club=club, training_type=training_type, location=location, price=Decimal("1250.00"))
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    slot, digest = _slot_and_digest(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    command = {
        "club_id": club.id,
        "slot_id": slot.id,
        "student_id": student.id,
        "payment_method": "sbp",
        "subscription_id": None,
        "offer_digest": digest,
        "idempotency_key": "staff-sbp-attachment-handoff",
        "actor_user_id": owner_user.id,
        "bank_source": BankPaymentOrder.Source.OWNER,
    }
    reservation_owner = create_personal_booking_payment_reservation
    durable_reservation = None
    owner_call_count = 0
    handoff = BusinessLogicError(
        "Personal payment reservation attachment is in progress; retry the same request",
        code="personal_payment_reservation_attachment_retry",
    )

    def reservation_owner_handoff(**kwargs):
        nonlocal durable_reservation, owner_call_count
        owner_call_count += 1
        if owner_call_count == 1:
            durable_reservation = reservation_owner(**kwargs)
            raise handoff
        assert durable_reservation is not None
        return durable_reservation

    with patch(
        "apps.attendance.services.staff_intents.create_personal_booking_payment_reservation",
        side_effect=reservation_owner_handoff,
    ) as owner:
        result = submit_staff_personal_intent(**command)

    command_row = PersonalStaffIntentCommand.objects.for_club(club).get(
        command_key=command["idempotency_key"]
    )
    assert owner.call_count == 2
    assert result.created is False
    assert result.receipt["reservation_id"] == durable_reservation.id
    assert command_row.reservation_id_snapshot == durable_reservation.id
    assert command_row.result_bound_at is not None
    assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1

    replay = submit_staff_personal_intent(**command)
    assert replay.created is False
    assert replay.receipt["reservation_id"] == durable_reservation.id
    assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1
    assert Payment.objects.for_club(club).count() == 1
