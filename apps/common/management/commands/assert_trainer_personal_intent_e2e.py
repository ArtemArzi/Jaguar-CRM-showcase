from __future__ import annotations

import json
import time
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalPaymentMethodCorrection,
    PersonalServiceTermsSnapshot,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    is_complete_personal_terms,
)
from apps.attendance.services.staff_intents import get_personal_commercial_context
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
)
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.clubs.models import Club, ClubSettings
from apps.clubs.timezones import club_zoneinfo


class Command(BaseCommand):
    help = "Assert flag-on staff personal intent E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")
        parser.add_argument("--timeout-seconds", type=float, default=30)

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        deadline = time.monotonic() + max(float(options["timeout_seconds"]), 0)
        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"trainer personal intent E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue
            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return

    @staticmethod
    def _load_fixture(path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc
        required = {
            "fixture_id", "club_id", "trainer", "cash_lead", "sbp_student",
            "entitlement_student", "pay_at_visit_student", "pay_at_visit_sbp_student",
            "terminal_sbp_student", "direct_student", "direct_terminal_sbp_student",
            "correction_student", "direct_correction_student",
            "location", "personal_training_type", "personal_tariff", "cash_slot", "sbp_slot",
            "entitlement_slot", "pay_at_visit_slot", "pay_at_visit_sbp_slot", "terminal_sbp_slot",
            "correction_slot", "correction_destination_slot", "personal_discount", "direct_booking",
            "direct_terminal_booking", "direct_correction_booking",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        tariff_id = int(fixture["personal_tariff"]["id"])
        training_type_id = int(fixture["personal_training_type"]["id"])
        location_id = int(fixture["location"]["id"])
        expected_amount = Decimal(fixture["expected"]["amount"])
        discounted_amount = Decimal(fixture["expected"]["discounted_amount"])
        discount_id = int(fixture["personal_discount"]["id"])

        if not settings.UNIFIED_CLIENT_JOURNEY_ENABLED:
            raise CommandError("process unified journey capability is not enabled")
        settings_row = ClubSettings.objects.filter(
            club_id=club_id,
            unified_client_journey_enabled=True,
        ).first()
        if settings_row is None:
            raise CommandError("club unified journey capability is not enabled")
        tariff = Tariff.objects.for_club(club_id).filter(id=tariff_id, is_personal_booking_default=True).first()
        if tariff is None:
            raise CommandError("designated personal tariff is missing")
        offer = resolve_personal_booking_offer(
            club_id=club_id,
            training_type_id=training_type_id,
            location_id=location_id,
            trainer_id=trainer_id,
        )
        if offer.tariff.id != tariff_id:
            raise CommandError("resolved personal offer does not match the designated tariff")

        cash = self._assert_cash(
            club_id=club_id,
            trainer_id=trainer_id,
            lead_id=int(fixture["cash_lead"]["student_id"]),
            slot_id=int(fixture["cash_slot"]["id"]),
            tariff_id=tariff_id,
            expected_amount=expected_amount,
        )
        sbp = self._assert_sbp(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["sbp_student"]["student_id"]),
            slot_id=int(fixture["sbp_slot"]["id"]),
            tariff_id=tariff_id,
            expected_amount=expected_amount,
        )
        entitlement = self._assert_entitlement(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["entitlement_student"]["student_id"]),
            subscription_id=int(fixture["entitlement_student"]["subscription_id"]),
            training_type_id=training_type_id,
            slot_id=int(fixture["entitlement_slot"]["id"]),
        )
        pay_at_visit = self._assert_pay_at_visit(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["pay_at_visit_student"]["student_id"]),
            slot_id=int(fixture["pay_at_visit_slot"]["id"]),
            tariff_id=tariff_id,
            expected_amount=discounted_amount,
            expected_discount_id=discount_id,
        )
        pay_at_visit_sbp = self._assert_pay_at_visit_sbp(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["pay_at_visit_sbp_student"]["student_id"]),
            slot_id=int(fixture["pay_at_visit_sbp_slot"]["id"]),
            tariff_id=tariff_id,
            expected_amount=expected_amount,
        )
        terminal_sbp = self._assert_terminal_sbp(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["terminal_sbp_student"]["student_id"]),
            slot_id=int(fixture["terminal_sbp_slot"]["id"]),
        )
        payment_correction = self._assert_payment_correction(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["correction_student"]["student_id"]),
            source_slot_id=int(fixture["correction_slot"]["id"]),
            destination_slot_id=int(fixture["correction_destination_slot"]["id"]),
            tariff_id=tariff_id,
            expected_amount=discounted_amount,
            expected_discount_id=discount_id,
        )
        direct = self._assert_direct(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["direct_student"]["student_id"]),
            booking_date=fixture["direct_booking"]["date"],
            start_time=fixture["direct_booking"]["start_time"],
            end_time=fixture["direct_booking"]["end_time"],
            tariff_id=tariff_id,
            expected_amount=expected_amount,
        )
        direct_terminal_sbp = self._assert_direct_terminal_sbp(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["direct_terminal_sbp_student"]["student_id"]),
            booking=fixture["direct_terminal_booking"],
        )
        direct_payment_correction = self._assert_direct_payment_correction(
            club_id=club_id,
            trainer_id=trainer_id,
            student_id=int(fixture["direct_correction_student"]["student_id"]),
            booking=fixture["direct_correction_booking"],
            tariff_id=tariff_id,
            expected_amount=discounted_amount,
            expected_discount_id=discount_id,
        )
        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "cash": cash,
            "sbp": sbp,
            "entitlement": entitlement,
            "pay_at_visit": pay_at_visit,
            "pay_at_visit_sbp": pay_at_visit_sbp,
            "terminal_sbp": terminal_sbp,
            "payment_correction": payment_correction,
            "direct_payment_correction": direct_payment_correction,
            "direct": direct,
            "direct_terminal_sbp": direct_terminal_sbp,
        }

    def _assert_cash(self, *, club_id, trainer_id, lead_id, slot_id, tariff_id, expected_amount):
        booking = PersonalDropInBooking.objects.for_club(club_id).select_related("enrollment__schedule").filter(
            enrollment__student_id=lead_id,
            enrollment__schedule__trainer_id=trainer_id,
            tariff_id=tariff_id,
        ).first()
        if booking is None:
            raise CommandError("cash staff intent did not create an exact booking")
        slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(id=slot_id).first()
        if slot is None or slot.booked_enrollment_id != booking.enrollment_id:
            raise CommandError("cash staff intent did not bind the published slot to its booking")
        terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking.id).first()
        if terms is None or not is_complete_personal_terms(terms):
            raise CommandError("cash booking is missing complete immutable terms")
        if terms.payable_amount != expected_amount or terms.discount_amount != Decimal("0"):
            raise CommandError("cash booking terms amount or discount differs from server offer")
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment__subscription")
            .filter(booking_id=booking.id)
            .first()
        )
        if link is None or link.payment.payment_method != Payment.Method.CASH:
            raise CommandError("cash booking is missing its manual payment bridge")
        if link.payment.status != Payment.Status.PENDING or link.payment.amount != expected_amount:
            raise CommandError("cash payment is not pending at the frozen server amount")
        if not link.payment.command_idempotency_key or not link.payment.command_fingerprint:
            raise CommandError("cash payment is missing command idempotency evidence")
        context = get_personal_commercial_context(club_id=club_id, student_id=lead_id, trainer_id=trainer_id)
        receipt = next((item for item in context if item["slot_id"] == slot_id), None)
        if receipt is None or receipt["payment_method"] != "cash" or receipt["payment_id"] != link.payment_id:
            raise CommandError("cash commercial context did not retain the exact receipt")
        return {"booking_id": booking.id, "payment_id": link.payment_id, "status": link.payment.status}

    def _assert_sbp(self, *, club_id, trainer_id, student_id, slot_id, tariff_id, expected_amount):
        reservation = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order")
            .filter(
                student_id=student_id,
                trainer_id=trainer_id,
                availability_slot_id=slot_id,
                tariff_id=tariff_id,
            )
            .first()
        )
        if reservation is None:
            raise CommandError("SBP staff intent did not create a slot reservation")
        if reservation.status != PersonalBookingPaymentReservation.Status.PENDING_PAYMENT:
            raise CommandError(f"SBP reservation is not pending payment: {reservation.status}")
        terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(reservation_id=reservation.id).first()
        if terms is None or not is_complete_personal_terms(terms):
            raise CommandError("SBP reservation is missing complete immutable terms")
        if terms.payable_amount != expected_amount or terms.discount_amount != Decimal("0"):
            raise CommandError("SBP reservation terms amount or discount differs from server offer")
        order = reservation.bank_payment_order
        if order is None or order.status not in {BankPaymentOrder.Status.CREATED, BankPaymentOrder.Status.PENDING}:
            raise CommandError("SBP reservation is missing a live bank payment order")
        if not order.provider_payment_url:
            raise CommandError("SBP bank payment order is missing its persisted provider link")
        context = get_personal_commercial_context(club_id=club_id, student_id=student_id, trainer_id=trainer_id)
        receipt = next((item for item in context if item["slot_id"] == slot_id), None)
        if receipt is None or receipt["payment_method"] != "sbp" or receipt["bank_payment_order_id"] != order.id:
            raise CommandError("SBP commercial context did not retain the exact payment link")
        return {"reservation_id": reservation.id, "bank_payment_order_id": order.id, "status": order.status}

    @staticmethod
    def _assert_entitlement(*, club_id, trainer_id, student_id, subscription_id, training_type_id, slot_id):
        subscription = Subscription.objects.for_club(club_id).filter(
            id=subscription_id,
            student_id=student_id,
            status=Subscription.Status.ACTIVE,
        ).first()
        if subscription is None:
            raise CommandError("entitlement intent has no active subscription")
        component = SubscriptionComponent.objects.for_club(club_id).filter(
            subscription_id=subscription.id,
            training_type_id=training_type_id,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            is_active=True,
        ).first()
        if component is None or component.credits_left != 1 or component.credits_used != 0:
            raise CommandError("entitlement intent changed the credit before its future check-in")
        slot = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .select_related("booked_enrollment")
            .filter(id=slot_id)
            .first()
        )
        if (
            slot is None
            or slot.booked_enrollment_id is None
            or slot.booked_enrollment.student_id != student_id
            or slot.booked_enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
        ):
            raise CommandError("entitlement intent did not book its exact published slot")
        context = get_personal_commercial_context(
            club_id=club_id,
            student_id=student_id,
            trainer_id=trainer_id,
        )
        receipt = next((item for item in context if item["slot_id"] == slot_id), None)
        if receipt is None or receipt["payment_method"] != "entitlement" or receipt["status"] != "booked":
            raise CommandError("entitlement commercial context did not survive reload")
        return {
            "subscription_id": subscription.id,
            "subscription_component_id": component.id,
            "enrollment_id": slot.booked_enrollment_id,
        }

    def _assert_pay_at_visit(
        self,
        *,
        club_id,
        trainer_id,
        student_id,
        slot_id,
        tariff_id,
        expected_amount,
        expected_discount_id,
    ):
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment__schedule", "debt")
            .filter(
                enrollment__student_id=student_id,
                enrollment__schedule__trainer_id=trainer_id,
                tariff_id=tariff_id,
            )
            .first()
        )
        if booking is None or booking.state != PersonalDropInBooking.State.ATTENDED:
            raise CommandError("pay-at-visit booking was not checked in")
        slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(id=slot_id).first()
        if slot is None or slot.booked_enrollment_id != booking.enrollment_id:
            raise CommandError("pay-at-visit booking did not bind its exact published slot")
        terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking.id).first()
        if (
            terms is None
            or terms.payable_amount != expected_amount
            or terms.discount_id_snapshot != expected_discount_id
            or terms.discount_amount != Decimal("500.00")
        ):
            raise CommandError("pay-at-visit booking terms differ from the frozen server offer")
        debt = booking.debt
        if debt is None or debt.tariff_price != expected_amount or debt.required_tariff_id != tariff_id:
            raise CommandError("pay-at-visit check-in did not open the exact frozen debt")
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment")
            .filter(booking_id=booking.id, payment__payment_method=Payment.Method.CASH)
            .order_by("-id")
            .first()
        )
        if link is None or link.payment.amount != expected_amount or debt.settlement_payment_id != link.payment_id:
            raise CommandError("cash settlement did not retain the exact booking debt and frozen amount")
        if not Debt.objects.for_club(club_id).filter(id=debt.id, settlement_payment_id=link.payment_id).exists():
            raise CommandError("cash settlement lost the debt-payment association")
        context = get_personal_commercial_context(club_id=club_id, student_id=student_id, trainer_id=trainer_id)
        receipt = next((item for item in context if item["booking_id"] == booking.id), None)
        if receipt is None or receipt["debt_id"] != debt.id or receipt["amount"] != str(expected_amount):
            raise CommandError("pay-at-visit commercial context did not retain the exact debt receipt")
        return {"booking_id": booking.id, "debt_id": debt.id, "payment_id": link.payment_id}

    @staticmethod
    def _assert_payment_correction(
        *,
        club_id,
        trainer_id,
        student_id,
        source_slot_id,
        destination_slot_id,
        tariff_id,
        expected_amount,
        expected_discount_id,
    ):
        correction = (
            PersonalPaymentMethodCorrection.objects.for_club(club_id)
            .select_related(
                "original_reservation__bank_payment_order",
                "replacement_booking__enrollment__schedule",
                "source_terms",
            )
            .filter(
                original_reservation__student_id=student_id,
                original_reservation__trainer_id=trainer_id,
                original_reservation__availability_slot_id=source_slot_id,
                replacement_payment_method="pay_at_visit",
            )
            .first()
        )
        if correction is None or correction.replacement_booking_id is None:
            raise CommandError("safe payment-method correction is missing its append-only lineage")
        reservation = correction.original_reservation
        order = reservation.bank_payment_order
        booking = correction.replacement_booking
        if (
            reservation.status != PersonalBookingPaymentReservation.Status.CANCELLED
            or order is None
            or order.status != BankPaymentOrder.Status.CANCELLED
        ):
            raise CommandError("safe payment-method correction did not terminalize the original SBP family")
        if (
            booking.tariff_id != tariff_id
            or booking.price_snapshot != expected_amount
            or booking.state != PersonalDropInBooking.State.SCHEDULED
            or booking.enrollment.schedule.trainer_id != trainer_id
        ):
            raise CommandError("safe payment-method correction did not create the exact replacement booking")
        replacement_terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
            booking_id=booking.id,
        ).first()
        if (
            replacement_terms is None
            or replacement_terms.payable_amount != expected_amount
            or replacement_terms.discount_id_snapshot != expected_discount_id
            or replacement_terms.discount_amount != Decimal("500.00")
            or correction.source_terms.payable_amount != replacement_terms.payable_amount
        ):
            raise CommandError("safe payment-method correction did not preserve frozen discounted terms")
        if correction.replacement_payment_link_id is not None:
            raise CommandError("pay-at-visit correction unexpectedly created a payment row")
        source_slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(id=source_slot_id).first()
        destination_slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(
            id=destination_slot_id,
        ).first()
        if (
            source_slot is None
            or source_slot.status != PersonalAvailabilitySlot.Status.PUBLISHED
            or source_slot.booked_enrollment_id is not None
            or destination_slot is None
            or destination_slot.status != PersonalAvailabilitySlot.Status.BOOKED
            or destination_slot.booked_enrollment_id != booking.enrollment_id
        ):
            raise CommandError("safe payment-method correction reschedule lost exact slot ownership")
        event = ScheduleBookingEvent.objects.for_club(club_id).filter(
            enrollment_id=booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_RESCHEDULED,
            metadata__source_availability_slot_id=source_slot_id,
            metadata__destination_availability_slot_id=destination_slot_id,
        ).first()
        if event is None:
            raise CommandError("safe payment-method correction reschedule is missing audit evidence")
        return {
            "correction_id": correction.id,
            "original_reservation_id": reservation.id,
            "replacement_booking_id": booking.id,
            "status": booking.state,
        }

    @staticmethod
    def _assert_direct_payment_correction(
        *,
        club_id,
        trainer_id,
        student_id,
        booking,
        tariff_id,
        expected_amount,
        expected_discount_id,
    ):
        correction = (
            PersonalPaymentMethodCorrection.objects.for_club(club_id)
            .select_related(
                "original_reservation__bank_payment_order",
                "replacement_booking__enrollment__schedule",
                "replacement_payment_link__payment",
                "source_terms",
            )
            .filter(
                original_reservation__student_id=student_id,
                original_reservation__trainer_id=trainer_id,
                original_reservation__availability_slot__isnull=True,
                replacement_payment_method=Payment.Method.CASH,
            )
            .first()
        )
        if correction is None or correction.replacement_booking_id is None:
            raise CommandError("direct payment-method correction is missing append-only lineage")
        reservation = correction.original_reservation
        order = reservation.bank_payment_order
        replacement = correction.replacement_booking
        schedule = replacement.enrollment.schedule
        club = Club.objects.only("id", "timezone").get(id=club_id)
        zone = club_zoneinfo(club)
        expected_starts_at = timezone.make_aware(
            datetime.combine(
                date.fromisoformat(booking["date"]),
                datetime_time.fromisoformat(booking["start_time"]),
            ),
            zone,
        )
        expected_ends_at = timezone.make_aware(
            datetime.combine(
                date.fromisoformat(booking["date"]),
                datetime_time.fromisoformat(booking["end_time"]),
            ),
            zone,
        )
        local_starts_at = timezone.localtime(reservation.starts_at, zone)
        local_ends_at = timezone.localtime(reservation.ends_at, zone)
        if (
            reservation.status != PersonalBookingPaymentReservation.Status.CANCELLED
            or order is None
            or order.status != BankPaymentOrder.Status.CANCELLED
            or replacement.tariff_id != tariff_id
            or replacement.price_snapshot != expected_amount
            or schedule.trainer_id != trainer_id
        ):
            raise CommandError("direct payment-method correction changed commercial identity")
        if reservation.starts_at != expected_starts_at or reservation.ends_at != expected_ends_at:
            raise CommandError(
                "direct payment-method correction origin differs from the requested club-local time"
            )
        if (
            schedule.one_time_date != local_starts_at.date()
            or schedule.start_time != local_starts_at.time().replace(tzinfo=None)
            or schedule.end_time != local_ends_at.time().replace(tzinfo=None)
        ):
            raise CommandError("direct payment-method correction changed the frozen exact time")
        replacement_terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
            booking_id=replacement.id,
        ).first()
        link = correction.replacement_payment_link
        if (
            replacement_terms is None
            or replacement_terms.payable_amount != expected_amount
            or replacement_terms.discount_id_snapshot != expected_discount_id
            or correction.source_terms.payable_amount != replacement_terms.payable_amount
            or link is None
            or link.payment.payment_method != Payment.Method.CASH
            or link.payment.amount != expected_amount
        ):
            raise CommandError("direct payment-method correction lost frozen discounted payment terms")
        if PersonalAvailabilitySlot.objects.for_club(club_id).filter(
            booked_enrollment_id=replacement.enrollment_id,
        ).exists():
            raise CommandError("direct payment-method correction unexpectedly consumed a published slot")
        return {
            "correction_id": correction.id,
            "original_reservation_id": reservation.id,
            "replacement_booking_id": replacement.id,
            "payment_id": link.payment_id,
        }

    def _assert_pay_at_visit_sbp(
        self, *, club_id, trainer_id, student_id, slot_id, tariff_id, expected_amount
    ):
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment__schedule", "debt")
            .filter(
                enrollment__student_id=student_id,
                enrollment__schedule__trainer_id=trainer_id,
                tariff_id=tariff_id,
                state=PersonalDropInBooking.State.ATTENDED,
            )
            .first()
        )
        if booking is None or booking.debt_id is None:
            raise CommandError("exact-debt SBP booking was not checked in with an open debt")
        slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(id=slot_id).first()
        if slot is None or slot.booked_enrollment_id != booking.enrollment_id:
            raise CommandError("exact-debt SBP booking did not bind its exact published slot")
        terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking.id).first()
        if terms is None or terms.payable_amount != expected_amount:
            raise CommandError("exact-debt SBP booking terms differ from the frozen server offer")
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment", "bank_payment_order")
            .filter(booking_id=booking.id, bank_payment_order__isnull=False)
            .order_by("-id")
            .first()
        )
        if link is None or link.bank_payment_order is None:
            raise CommandError("exact-debt SBP settlement did not create a bank order")
        if link.payment.amount != expected_amount or booking.debt.settlement_payment_id != link.payment_id:
            raise CommandError("exact-debt SBP settlement lost the frozen amount or debt-payment association")
        if link.bank_payment_order.status not in {BankPaymentOrder.Status.CREATED, BankPaymentOrder.Status.PENDING}:
            raise CommandError("exact-debt SBP settlement is not live")
        if not link.bank_payment_order.provider_payment_url:
            raise CommandError("exact-debt SBP settlement is missing its persisted provider link")
        context = get_personal_commercial_context(club_id=club_id, student_id=student_id, trainer_id=trainer_id)
        receipt = next((item for item in context if item["booking_id"] == booking.id), None)
        if (
            receipt is None
            or receipt["debt_id"] != booking.debt_id
            or receipt["payment_method"] != "sbp"
            or receipt["bank_payment_order_id"] != link.bank_payment_order_id
        ):
            raise CommandError("exact-debt SBP commercial context did not retain the payment link")
        return {
            "booking_id": booking.id,
            "debt_id": booking.debt_id,
            "bank_payment_order_id": link.bank_payment_order_id,
        }

    @staticmethod
    def _assert_terminal_sbp(*, club_id, trainer_id, student_id, slot_id):
        reservations = list(
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order")
            .filter(student_id=student_id, trainer_id=trainer_id, availability_slot_id=slot_id)
            .order_by("id")
        )
        live = [
            reservation
            for reservation in reservations
            if reservation.status
            in {
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.BOOKED,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            }
        ]
        terminal = [reservation for reservation in reservations if reservation not in live]
        if len(live) != 1 or not terminal:
            raise CommandError("SBP retry did not retain a terminal attempt and exactly one live attempt")
        if live[0].bank_payment_order is None or not live[0].bank_payment_order.provider_payment_url:
            raise CommandError("SBP retry live attempt is missing its persisted provider link")
        return {
            "live_reservation_id": live[0].id,
            "live_bank_payment_order_id": live[0].bank_payment_order_id,
            "terminal_reservation_ids": [reservation.id for reservation in terminal],
        }

    def _assert_direct(
        self, *, club_id, trainer_id, student_id, booking_date, start_time, end_time, tariff_id, expected_amount
    ):
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment__schedule")
            .filter(
                enrollment__student_id=student_id,
                enrollment__schedule__trainer_id=trainer_id,
                enrollment__schedule__one_time_date=booking_date,
                enrollment__schedule__start_time=start_time,
                enrollment__schedule__end_time=end_time,
                tariff_id=tariff_id,
            )
            .first()
        )
        if booking is None:
            raise CommandError("direct trainer/time intent did not create its exact booking")
        if (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .filter(booked_enrollment_id=booking.enrollment_id)
            .exists()
        ):
            raise CommandError("direct trainer/time intent incorrectly used a published slot")
        terms = PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(booking_id=booking.id).first()
        if terms is None or terms.payable_amount != expected_amount:
            raise CommandError("direct trainer/time intent did not persist the server offer terms")
        link = (
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment")
            .filter(booking_id=booking.id)
            .first()
        )
        if link is None or link.payment.payment_method != Payment.Method.CASH or link.payment.amount != expected_amount:
            raise CommandError("direct trainer/time intent did not retain its server-priced cash payment")
        context = get_personal_commercial_context(club_id=club_id, student_id=student_id, trainer_id=trainer_id)
        receipt = next((item for item in context if item["booking_id"] == booking.id), None)
        if receipt is None or receipt["slot_id"] is not None or receipt["amount"] != str(expected_amount):
            raise CommandError("direct trainer/time commercial context did not survive reload")
        return {"booking_id": booking.id, "payment_id": link.payment_id}

    @staticmethod
    def _assert_direct_terminal_sbp(*, club_id, trainer_id, student_id, booking):
        club = Club.objects.only("id", "timezone").get(id=club_id)
        starts_at = timezone.make_aware(
            datetime.combine(
                date.fromisoformat(booking["date"]),
                datetime_time.fromisoformat(booking["start_time"]),
            ),
            club_zoneinfo(club),
        )
        expected_utc = datetime.fromisoformat(booking["starts_at_utc"])
        if starts_at.astimezone(expected_utc.tzinfo) != expected_utc:
            raise CommandError("direct terminal fixture UTC instant does not match its club wall time")
        reservations = list(
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_related("bank_payment_order")
            .filter(
                student_id=student_id,
                trainer_id=trainer_id,
                availability_slot_id__isnull=True,
                starts_at=starts_at,
            )
            .order_by("id")
        )
        live = [
            reservation
            for reservation in reservations
            if reservation.status
            in {
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.BOOKED,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
            }
        ]
        terminal = [reservation for reservation in reservations if reservation not in live]
        if len(live) != 1 or len(terminal) != 1:
            raise CommandError("direct SBP retry did not retain one terminal and one live attempt")
        if live[0].bank_payment_order is None or not live[0].bank_payment_order.provider_payment_url:
            raise CommandError("direct SBP retry live attempt is missing its persisted provider link")
        return {
            "live_reservation_id": live[0].id,
            "live_bank_payment_order_id": live[0].bank_payment_order_id,
            "terminal_reservation_id": terminal[0].id,
            "starts_at_utc": live[0].starts_at.astimezone(expected_utc.tzinfo).isoformat(),
        }
