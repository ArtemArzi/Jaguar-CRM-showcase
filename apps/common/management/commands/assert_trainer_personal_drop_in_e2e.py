from __future__ import annotations

import json
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    PersonalAvailabilitySlot,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.services import validate_personal_drop_in_tariff_contract
from apps.billing.models import Debt, DebtSettlementEvent, Payment, Subscription, SubscriptionComponent
from apps.feedback.models import FeedbackResponse
from apps.leads.models import LeadLifecycleEvent
from apps.retention.models import RetentionTask
from apps.students.models import AccountAccess, Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert trainer personal drop-in E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_personal_drop_in_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for asynchronous side effects before failing.",
        )
        parser.add_argument(
            "--require-grandfathered-completion",
            action="store_true",
            help="Require the seeded legacy personal trial to complete exactly once through kiosk check-in.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        deadline = time.monotonic() + max(float(options["timeout_seconds"]), 0)
        require_grandfathered_completion = bool(options["require_grandfathered_completion"]) or (
            os.environ.get("REAL_STACK_E2E_REQUIRE_GRANDFATHERED_COMPLETION") == "1"
        )

        while True:
            try:
                evidence = self._collect_evidence(
                    fixture,
                    require_grandfathered_completion=require_grandfathered_completion,
                )
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"trainer personal drop-in E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue
            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {
            "fixture_id",
            "club_id",
            "trainer",
            "owner",
            "assigned_lead",
            "unrelated_lead",
            "location",
            "personal_training_type",
            "personal_tariff",
            "booking",
            "availability_slot",
            "past_booking",
            "grandfathered_personal_trial",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict, *, require_grandfathered_completion: bool) -> dict:
        club_id = int(fixture["club_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        lead_id = int(fixture["assigned_lead"]["student_id"])
        tariff_id = int(fixture["personal_tariff"]["id"])
        training_type_id = int(fixture["personal_training_type"]["id"])
        location_id = int(fixture["location"]["id"])
        price_snapshot = Decimal(fixture["expected"]["price_snapshot"])
        booking_date = date.fromisoformat(fixture["booking"]["date"])

        validate_personal_drop_in_tariff_contract(
            club_id=club_id,
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            tariff_id=tariff_id,
        )

        primary_booking = self._get_exact_primary_booking(
            club_id=club_id,
            lead_id=lead_id,
            booking_date=booking_date,
            start_time=fixture["booking"]["start_time"],
            end_time=fixture["booking"]["end_time"],
            trainer_id=trainer_id,
            location_id=location_id,
            training_type_id=training_type_id,
            tariff_id=tariff_id,
        )
        primary_evidence = self._assert_primary_lifecycle(
            club_id=club_id,
            lead_id=lead_id,
            primary_booking=primary_booking,
            tariff_id=tariff_id,
            price_snapshot=price_snapshot,
            trainer_id=trainer_id,
        )
        slot_evidence = self._assert_slot_cancellation(club_id=club_id, fixture=fixture)
        past_evidence = self._assert_past_no_show(club_id=club_id, fixture=fixture)
        grandfathered_evidence = self._assert_grandfathered_personal_trial(
            club_id=club_id,
            fixture=fixture,
            training_type_id=training_type_id,
            require_completion=require_grandfathered_completion,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "grandfathered_personal_trial_preserved": True,
            "grandfathered_personal_trial_completed": require_grandfathered_completion,
            "grandfathered_personal_trial": grandfathered_evidence,
            "primary": primary_evidence,
            "slot_booking": slot_evidence,
            "past_booking": past_evidence,
        }

    def _get_exact_primary_booking(
        self,
        *,
        club_id: int,
        lead_id: int,
        booking_date: date,
        start_time: str,
        end_time: str,
        trainer_id: int,
        location_id: int,
        training_type_id: int,
        tariff_id: int,
    ) -> PersonalDropInBooking:
        bookings = list(
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related(
                "enrollment",
                "enrollment__schedule",
                "checkin",
                "debt",
                "tariff",
            )
            .filter(enrollment__student_id=lead_id)
            .order_by("id")
        )
        if len(bookings) != 1:
            raise CommandError(f"primary drop-in booking expected exactly one row, got {len(bookings)}")
        booking = bookings[0]
        enrollment = booking.enrollment
        schedule = enrollment.schedule
        if booking.state != PersonalDropInBooking.State.ATTENDED:
            raise CommandError(f"primary drop-in state mismatch: expected attended, got {booking.state}")
        if booking.tariff_id != tariff_id:
            raise CommandError("primary drop-in tariff mismatch")
        if booking.price_snapshot != booking.tariff.price:
            raise CommandError("primary drop-in price snapshot does not match configured tariff")
        if (
            enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN
            or enrollment.status != ScheduleEnrollment.Status.ACTIVE
            or enrollment.starts_on != booking_date
            or enrollment.ends_on != booking_date
        ):
            raise CommandError("primary drop-in enrollment contract mismatch")
        if (
            schedule.trainer_id != trainer_id
            or schedule.location_id != location_id
            or schedule.training_type_id != training_type_id
            or schedule.one_time_date != booking_date
            or schedule.start_time.strftime("%H:%M") != start_time
            or schedule.end_time.strftime("%H:%M") != end_time
        ):
            raise CommandError("primary drop-in schedule contract mismatch")
        return booking

    def _assert_primary_lifecycle(
        self,
        *,
        club_id: int,
        lead_id: int,
        primary_booking: PersonalDropInBooking,
        tariff_id: int,
        price_snapshot: Decimal,
        trainer_id: int,
    ) -> dict:
        checkin = primary_booking.checkin
        debt = primary_booking.debt
        if checkin is None or debt is None:
            raise CommandError("attended primary drop-in must link one check-in and one debt")
        if checkin.source != Checkin.Source.KIOSK:
            raise CommandError("primary drop-in check-in source must be kiosk")
        if debt.checkin_id != checkin.id or debt.student_id != lead_id:
            raise CommandError("primary drop-in debt is not linked to its exact check-in and lead")
        if (
            debt.reason != "personal_drop_in"
            or debt.required_tariff_id != tariff_id
            or debt.tariff_price != price_snapshot
            or debt.resolved_at is None
            or debt.resolution_type != "payment"
        ):
            raise CommandError("primary drop-in debt settlement contract mismatch")

        links = list(
            PersonalDropInPaymentLink.objects.for_club(club_id)
            .select_related("payment", "payment__subscription")
            .filter(booking_id=primary_booking.id)
            .order_by("id")
        )
        if len(links) != 1:
            raise CommandError(f"primary drop-in payment link expected exactly one row, got {len(links)}")
        link = links[0]
        payment = link.payment
        if (
            payment.status != Payment.Status.CONFIRMED
            or payment.student_id != lead_id
            or payment.tariff_id != tariff_id
            or payment.subscription_id is None
            or debt.settlement_payment_id != payment.id
        ):
            raise CommandError("primary drop-in payment was not confirmed against the exact debt and tariff")

        subscriptions = list(
            Subscription.objects.for_club(club_id)
            .filter(student_id=lead_id, tariff_id=tariff_id, deleted_at__isnull=True)
            .order_by("id")
        )
        if len(subscriptions) != 1 or subscriptions[0].id != payment.subscription_id:
            raise CommandError("primary drop-in expected exactly one paid single-session subscription")
        subscription = subscriptions[0]
        components = list(
            SubscriptionComponent.objects.for_club(club_id)
            .filter(subscription_id=subscription.id, is_active=True)
            .order_by("id")
        )
        if (
            len(components) != 1
            or components[0].credits_total != 1
            or components[0].credits_left != 0
            or subscription.trainings_left != 0
            or subscription.status != Subscription.Status.EXPIRED
        ):
            raise CommandError("primary drop-in paid single-session entitlement was not consumed exactly once")

        earnings = list(
            TrainerEarning.objects.for_club(club_id)
            .filter(checkin_id=checkin.id, cancelled=False)
            .order_by("id")
        )
        if len(earnings) != 1:
            raise CommandError(f"primary drop-in salary expected exactly one row, got {len(earnings)}")
        earning = earnings[0]
        if earning.trainer_id != trainer_id or earning.earning_type != TrainerEarning.EarningType.PERSONAL:
            raise CommandError("primary drop-in salary snapshot mismatch")

        lead = Student.objects.for_club(club_id).get(id=lead_id)
        if lead.status != Student.Status.ACTIVE or lead.lead_status is not None:
            raise CommandError("confirmed primary drop-in payment did not convert lead to active")
        if lead.assigned_trainer_id != trainer_id:
            raise CommandError("confirmed primary drop-in payment changed assigned trainer")
        if lead.user_id is not None or AccountAccess.objects.for_club(club_id).filter(student_id=lead_id).exists():
            raise CommandError("primary drop-in unexpectedly opened account access")
        if LeadLifecycleEvent.objects.for_club(club_id).filter(
            student_id=lead_id,
            event_type__in=[
                LeadLifecycleEvent.EventType.TRIAL_BOOKED,
                LeadLifecycleEvent.EventType.TRIAL_DONE,
            ],
        ).exists():
            raise CommandError("primary drop-in unexpectedly created trial lifecycle events")
        if RetentionTask.objects.for_club(club_id).filter(student_id=lead_id, task_type="post_trial").exists():
            raise CommandError("primary drop-in unexpectedly created a post-trial task")
        if FeedbackResponse.objects.for_club(club_id).filter(student_id=lead_id).exists():
            raise CommandError("primary drop-in unexpectedly created trial feedback")
        if CheckinCascadeEvent.objects.for_club(club_id).filter(
            checkin_id=checkin.id,
            effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
            expected=True,
        ).exists():
            raise CommandError("primary drop-in unexpectedly queued an expected post-trial task")

        booking_event_count = ScheduleBookingEvent.objects.for_club(club_id).filter(
            enrollment_id=primary_booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        ).count()
        if booking_event_count != 1:
            raise CommandError(f"primary drop-in booking event expected exactly one row, got {booking_event_count}")
        primary_checkin_count = Checkin.objects.for_club(club_id).filter(
            student_id=lead_id,
            schedule_id=checkin.schedule_id,
            training_type_id=checkin.training_type_id,
            date=checkin.date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).count()
        if primary_checkin_count != 1:
            raise CommandError("primary drop-in check-in cardinality mismatch")
        if Debt.objects.for_club(club_id).filter(checkin_id=checkin.id).count() != 1:
            raise CommandError("primary drop-in debt cardinality mismatch")
        settlement_event_counts = {
            "reserved": DebtSettlementEvent.objects.for_club(club_id)
            .filter(
                debt_id=debt.id,
                payment_id=payment.id,
                event_type=DebtSettlementEvent.EventType.RESERVED,
            )
            .count(),
            "confirmed": DebtSettlementEvent.objects.for_club(club_id)
            .filter(
                debt_id=debt.id,
                payment_id=payment.id,
                event_type=DebtSettlementEvent.EventType.CONFIRMED,
            )
            .count(),
        }
        if settlement_event_counts != {"reserved": 1, "confirmed": 1}:
            raise CommandError(
                "primary drop-in debt settlement event cardinality mismatch: "
                f"expected one reserve and confirm, got {settlement_event_counts}"
            )

        return {
            "booking_id": primary_booking.id,
            "checkin_id": checkin.id,
            "debt_id": debt.id,
            "payment_id": payment.id,
            "subscription_id": subscription.id,
            "earning_id": earning.id,
            "cardinalities": {
                "booking": 1,
                "checkin": primary_checkin_count,
                "debt": 1,
                "debt_settlement": settlement_event_counts["confirmed"],
                "payment": 1,
                "payment_link": 1,
                "subscription": 1,
                "subscription_component": 1,
                "salary_effect": 1,
            },
        }

    def _assert_slot_cancellation(self, *, club_id: int, fixture: dict) -> dict:
        student_id = int(fixture["availability_slot"]["student_id"])
        bookings = list(
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment", "enrollment__schedule")
            .filter(enrollment__student_id=student_id)
            .order_by("id")
        )
        if len(bookings) != 1:
            raise CommandError(f"availability-slot drop-in expected exactly one row, got {len(bookings)}")
        booking = bookings[0]
        if (
            booking.state != PersonalDropInBooking.State.CANCELLED
            or booking.checkin_id is not None
            or booking.debt_id is not None
            or booking.enrollment.status != ScheduleEnrollment.Status.CANCELLED
            or booking.enrollment.schedule.is_active
        ):
            raise CommandError("availability-slot drop-in cancellation contract mismatch")
        slot = PersonalAvailabilitySlot.objects.for_club(club_id).filter(
            id=int(fixture["expected"]["availability_slot_id"])
        ).first()
        if (
            slot is None
            or slot.status != PersonalAvailabilitySlot.Status.PUBLISHED
            or slot.booked_enrollment_id is not None
        ):
            raise CommandError("cancelled future availability slot was not reopened")
        event_count = ScheduleBookingEvent.objects.for_club(club_id).filter(
            enrollment_id=booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_DROP_IN_CANCELLED,
        ).count()
        if event_count != 1:
            raise CommandError(f"availability-slot cancellation expected exactly one event, got {event_count}")
        return {"booking_id": booking.id, "availability_slot_id": slot.id, "cancel_event_count": event_count}

    def _assert_past_no_show(self, *, club_id: int, fixture: dict) -> dict:
        booking_id = int(fixture["past_booking"]["booking_id"])
        booking = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment", "enrollment__schedule")
            .filter(id=booking_id, enrollment__student_id=int(fixture["past_booking"]["student_id"]))
            .first()
        )
        if booking is None:
            raise CommandError("past drop-in booking not found")
        if (
            booking.state != PersonalDropInBooking.State.NO_SHOW
            or booking.checkin_id is not None
            or booking.debt_id is not None
            or booking.enrollment.status != ScheduleEnrollment.Status.CANCELLED
            or booking.enrollment.schedule.is_active
        ):
            raise CommandError("past drop-in no-show contract mismatch")
        event_count = ScheduleBookingEvent.objects.for_club(club_id).filter(
            enrollment_id=booking.enrollment_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_DROP_IN_NO_SHOW,
        ).count()
        if event_count != 1:
            raise CommandError(f"past drop-in no-show expected exactly one event, got {event_count}")
        return {"booking_id": booking.id, "no_show_event_count": event_count}

    def _assert_grandfathered_personal_trial(
        self,
        *,
        club_id: int,
        fixture: dict,
        training_type_id: int,
        require_completion: bool,
    ) -> dict:
        student_id = int(fixture["grandfathered_personal_trial"]["student_id"])
        enrollment_id = int(fixture["grandfathered_personal_trial"]["enrollment_id"])
        student = Student.objects.for_club(club_id).get(id=student_id)
        enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_related("schedule")
            .filter(id=enrollment_id, student_id=student_id)
            .first()
        )
        if enrollment is None:
            raise CommandError("grandfathered personal trial enrollment not found")
        if (
            enrollment.status != ScheduleEnrollment.Status.TRIAL
            or enrollment.created_from != ScheduleEnrollment.CreatedFrom.LEAD_BOOKING
            or enrollment.schedule.training_type_id != training_type_id
            or enrollment.trial_at is None
        ):
            raise CommandError("grandfathered personal trial was not preserved")
        if PersonalDropInBooking.objects.for_club(club_id).filter(enrollment_id=enrollment.id).exists():
            raise CommandError("grandfathered personal trial was rewritten as a drop-in booking")
        if not require_completion:
            if student.status != Student.Status.TRIAL or student.lead_status != Student.LeadStatus.TRIAL_BOOKED:
                raise CommandError("grandfathered personal trial was not preserved")
            return {"enrollment_id": enrollment.id, "completed": False}

        checkin_date = date.fromisoformat(fixture["grandfathered_personal_trial"]["checkin_date"])
        checkins = list(
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=enrollment.schedule_id,
                training_type_id=training_type_id,
                date=checkin_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("id")
        )
        if len(checkins) != 1:
            raise CommandError(f"grandfathered personal trial expected exactly one check-in, got {len(checkins)}")
        checkin = checkins[0]
        if checkin.source != Checkin.Source.KIOSK or checkin.is_debt or checkin.subscription_id is not None:
            raise CommandError("grandfathered personal trial kiosk check-in contract mismatch")
        if student.status != Student.Status.TRIAL or student.lead_status != Student.LeadStatus.TRIAL_DONE:
            raise CommandError("grandfathered personal trial did not complete")

        trial_done_events = list(
            LeadLifecycleEvent.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                event_type=LeadLifecycleEvent.EventType.TRIAL_DONE,
                old_lead_status=Student.LeadStatus.TRIAL_BOOKED,
                new_lead_status=Student.LeadStatus.TRIAL_DONE,
            )
            .order_by("id")
        )
        if len(trial_done_events) != 1:
            raise CommandError(
                f"grandfathered personal trial expected exactly one completion event, got {len(trial_done_events)}"
            )
        trial_done_event = trial_done_events[0]
        if (
            trial_done_event.old_trainer_id != enrollment.schedule.trainer_id
            or trial_done_event.new_trainer_id != enrollment.schedule.trainer_id
        ):
            raise CommandError("grandfathered personal trial completion trainer snapshots mismatch")

        cascade_events = list(
            CheckinCascadeEvent.objects.for_club(club_id)
            .filter(checkin_id=checkin.id, effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK)
            .order_by("id")
        )
        if len(cascade_events) != 1:
            raise CommandError(
                f"grandfathered personal trial expected exactly one post-trial cascade event, got {len(cascade_events)}"
            )
        cascade_event = cascade_events[0]
        if not cascade_event.expected:
            raise CommandError("grandfathered personal trial post-trial cascade event is not expected")
        payload = cascade_event.payload or {}
        if payload.get("student_id") != student_id or payload.get("trainer_id") != enrollment.schedule.trainer_id:
            raise CommandError("grandfathered personal trial post-trial cascade payload mismatch")
        return {
            "enrollment_id": enrollment.id,
            "checkin_id": checkin.id,
            "checkin_count": len(checkins),
            "completion_event_count": len(trial_done_events),
            "post_trial_cascade_count": len(cascade_events),
            "completed": True,
        }
