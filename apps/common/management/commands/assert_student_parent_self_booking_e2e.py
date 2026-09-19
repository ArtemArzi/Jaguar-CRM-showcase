from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalSelfServiceCommand,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.attendance.selectors import get_student_schedule_occurrences_for_range
from apps.attendance.services.self_service import self_service_personal_command_card
from apps.billing.models import BankPaymentOrder, Debt
from apps.clubs.models import Club, ClubSettings
from apps.students.parent_selectors import get_child_profile, get_parent_children


class Command(BaseCommand):
    help = "Assert student/parent self-booking creates and cancels group and personal bookings safely."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_student_parent_self_booking_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for side effects before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"student/parent self-booking E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {"fixture_id", "legacy", "unified"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "legacy": self._collect_legacy_evidence(fixture["legacy"]),
            "unified": self._collect_unified_evidence(fixture["unified"]),
        }

    def _collect_legacy_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        target_date = date.fromisoformat(fixture["booking_date"])
        student_id = int(fixture["student"]["student_id"])
        student_user_id = int(fixture["student"]["user_id"])
        child_id = int(fixture["parent"]["child_id"])
        parent_user_id = int(fixture["parent"]["user_id"])
        group_schedule_id = int(fixture["group_schedule_id"])

        student_group = self._assert_group_booking(
            club=club,
            student_id=student_id,
            actor_user_id=student_user_id,
            schedule_id=group_schedule_id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            idempotency_key=f"student-self-booking-{student_id}-{group_schedule_id}-{target_date.isoformat()}",
        )
        parent_group = self._assert_group_booking(
            club=club,
            student_id=child_id,
            actor_user_id=parent_user_id,
            schedule_id=group_schedule_id,
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
            idempotency_key=f"parent-self-booking-{child_id}-{group_schedule_id}-{target_date.isoformat()}",
        )
        student_personal = self._assert_personal_booking(
            club=club,
            student_id=student_id,
            actor_user_id=student_user_id,
            slot_id=int(fixture["student_personal_slot_id"]),
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING,
            idempotency_key=f"student-personal-self-booking-{student_id}-{fixture['student_personal_slot_id']}",
        )
        parent_personal = self._assert_personal_booking(
            club=club,
            student_id=child_id,
            actor_user_id=parent_user_id,
            slot_id=int(fixture["parent_personal_slot_id"]),
            target_date=target_date,
            origin=ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING,
            idempotency_key=f"parent-personal-self-booking-{child_id}-{fixture['parent_personal_slot_id']}",
        )
        reopened_personal_slot = self._assert_personal_slot_available(
            club=club,
            slot_id=int(fixture["student_personal_slot_id"]),
        )
        reopened_parent_personal_slot = self._assert_personal_slot_available(
            club=club,
            slot_id=int(fixture["parent_personal_slot_id"]),
        )
        visibility = self._assert_cancellation_visibility(
            club=club,
            student_id=student_id,
            child_id=child_id,
            parent_user_id=parent_user_id,
            foreign_child_id=int(fixture["foreign_child_id"]),
            target_date=target_date,
            expected=fixture["expected"],
        )

        if Checkin.objects.for_club(club).filter(student_id__in=[student_id, child_id]).exists():
            raise CommandError("self-booking should not create check-ins")
        if Debt.objects.for_club(club).filter(student_id__in=[student_id, child_id]).exists():
            raise CommandError("self-booking should not create debts")

        return {
            "student_group": student_group,
            "parent_group": parent_group,
            "student_personal": student_personal,
            "parent_personal": parent_personal,
            "reopened_personal_slot": reopened_personal_slot,
            "reopened_parent_personal_slot": reopened_parent_personal_slot,
            "visibility": visibility,
        }

    def _collect_unified_evidence(self, fixture: dict) -> dict:
        required = {
            "club_id",
            "student",
            "parent",
            "payer",
            "other_actor",
            "student_slot_id",
            "parent_slot_id",
            "payer_slot_id",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"unified fixture is missing required fields: {', '.join(missing)}")

        club = Club.objects.get(id=int(fixture["club_id"]))
        settings_row = ClubSettings.objects.get(club=club)
        if settings_row.unified_client_journey_enabled:
            raise CommandError("unified fixture club must be disabled after the rollback proof")
        student = fixture["student"]
        parent = fixture["parent"]
        payer = fixture["payer"]
        student_command = self._get_unified_command(
            club=club,
            actor_user_id=int(student["user_id"]),
            student_id=int(student["student_id"]),
            source=PersonalSelfServiceCommand.Source.STUDENT,
            slot_id=int(fixture["student_slot_id"]),
            action=PersonalSelfServiceCommand.Action.BOOK,
        )
        parent_command = self._get_unified_command(
            club=club,
            actor_user_id=int(parent["user_id"]),
            student_id=int(parent["child_id"]),
            source=PersonalSelfServiceCommand.Source.PARENT,
            slot_id=int(fixture["parent_slot_id"]),
            action=PersonalSelfServiceCommand.Action.BOOK,
        )
        for label, command, subscription_id in [
            ("student", student_command, int(student["subscription_id"])),
            ("parent", parent_command, int(parent["subscription_id"])),
        ]:
            if command.enrollment_id_snapshot is None or command.enrollment_id != command.enrollment_id_snapshot:
                raise CommandError(f"{label} entitlement command did not bind its exact booking")
            if command.entitlement_subscription_id != subscription_id:
                raise CommandError(f"{label} entitlement command lost its server-selected subscription")
            enrollment = ScheduleEnrollment.objects.for_club(club).filter(id=command.enrollment_id_snapshot).first()
            if enrollment is None or enrollment.status != ScheduleEnrollment.Status.ACTIVE:
                raise CommandError(f"{label} entitlement command booking is not active")
            if command.offer_digest:
                raise CommandError(f"{label} entitlement command incorrectly retained a paid digest")

        payer_commands = list(
            PersonalSelfServiceCommand.objects.for_club(club)
            .filter(
                actor_id=int(payer["user_id"]),
                student_id=int(payer["student_id"]),
                source=PersonalSelfServiceCommand.Source.STUDENT,
                availability_slot_id=int(fixture["payer_slot_id"]),
                action=PersonalSelfServiceCommand.Action.PAY,
            )
            .select_related("availability_slot")
            .order_by("created_at", "id")
        )
        if len(payer_commands) != 2:
            raise CommandError(f"terminal SBP retry must retain exactly two commands, got {len(payer_commands)}")
        if len({command.command_key for command in payer_commands}) != 2:
            raise CommandError("terminal SBP retry reused its idempotency key")
        if any(len(command.offer_digest) != 64 for command in payer_commands):
            raise CommandError("SBP commands must retain exact opaque offer digests")
        if any(command.reservation_id_snapshot is None for command in payer_commands):
            raise CommandError("SBP commands did not bind their reservation evidence")

        reservations = [
            PersonalBookingPaymentReservation.objects.for_club(club)
            .select_related("bank_payment_order")
            .get(id=command.reservation_id_snapshot)
            for command in payer_commands
        ]
        terminal = [
            reservation
            for reservation in reservations
            if reservation.status
            in {
                PersonalBookingPaymentReservation.Status.CANCELLED,
                PersonalBookingPaymentReservation.Status.EXPIRED,
            }
        ]
        live = [
            reservation
            for reservation in reservations
            if reservation.status
            in {
                PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
                PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
                PersonalBookingPaymentReservation.Status.BOOKED,
            }
        ]
        if len(terminal) != 2 or live:
            raise CommandError("flag-off drain must retain exactly two terminal SBP attempts")
        terminal_orders = [reservation.bank_payment_order for reservation in terminal]
        if any(
            order is None or order.status != BankPaymentOrder.Status.CANCELLED
            for order in terminal_orders
        ):
            raise CommandError("flag-off drain did not cancel every retained SBP order")
        command_cards = {
            command.id: self_service_personal_command_card(club=club, command=command)
            for command in payer_commands
        }
        if any(
            "retry_bank_payment" in card["allowed_actions"]
            for card in command_cards.values()
        ):
            raise CommandError("flag-off terminal command still advertises a new retry")
        return {
            "unified_client_journey_enabled": False,
            "student_command_id": student_command.id,
            "parent_command_id": parent_command.id,
            "terminal_command_ids": sorted(command.id for command in payer_commands),
            "terminal_order_ids": sorted(order.id for order in terminal_orders if order is not None),
            "live_command_count": 0,
        }

    @staticmethod
    def _get_unified_command(
        *,
        club: Club,
        actor_user_id: int,
        student_id: int,
        source: str,
        slot_id: int,
        action: str,
    ) -> PersonalSelfServiceCommand:
        commands = list(
            PersonalSelfServiceCommand.objects.for_club(club)
            .filter(
                actor_id=actor_user_id,
                student_id=student_id,
                source=source,
                availability_slot_id=slot_id,
                action=action,
            )
            .order_by("id")
        )
        if len(commands) != 1:
            raise CommandError(
                f"expected one {source} {action} command for student={student_id}, slot={slot_id}; "
                f"got {len(commands)}"
            )
        return commands[0]

    def _assert_group_booking(
        self,
        *,
        club: Club,
        student_id: int,
        actor_user_id: int,
        schedule_id: int,
        target_date: date,
        origin: str,
        idempotency_key: str,
    ) -> dict:
        enrollment = self._get_enrollment(
            club=club,
            student_id=student_id,
            schedule_id=schedule_id,
            target_date=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )
        if enrollment.status != ScheduleEnrollment.Status.CANCELLED:
            raise CommandError(f"group enrollment status mismatch: got {enrollment.status}")
        booking_event = self._get_event(
            club=club,
            enrollment=enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED,
            origin=origin,
            actor_user_id=actor_user_id,
            metadata={"idempotency_key": idempotency_key},
        )
        cancel_event = self._get_event(
            club=club,
            enrollment=enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_CANCELLED,
            origin=origin,
            actor_user_id=actor_user_id,
            metadata={},
        )
        return {
            "enrollment_id": enrollment.id,
            "booking_event_id": booking_event.id,
            "cancel_event_id": cancel_event.id,
            "origin": booking_event.origin,
            "status": enrollment.status,
        }

    def _assert_personal_booking(
        self,
        *,
        club: Club,
        student_id: int,
        actor_user_id: int,
        slot_id: int,
        target_date: date,
        origin: str,
        idempotency_key: str,
    ) -> dict:
        booking_event, subscription_id = self._get_personal_booking_event(
            club=club,
            student_id=student_id,
            actor_user_id=actor_user_id,
            slot_id=slot_id,
            target_date=target_date,
            origin=origin,
            idempotency_key=idempotency_key,
        )
        enrollment = booking_event.enrollment
        if enrollment.student_id != student_id:
            raise CommandError(
                f"personal slot {slot_id} student mismatch: expected {student_id}, got {enrollment.student_id}"
            )
        if enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING:
            raise CommandError(f"personal enrollment created_from mismatch: got {enrollment.created_from}")
        if enrollment.starts_on != target_date or enrollment.ends_on != target_date:
            raise CommandError(
                f"personal enrollment date mismatch: expected {target_date}, "
                f"got {enrollment.starts_on}/{enrollment.ends_on}"
            )
        if enrollment.status != ScheduleEnrollment.Status.CANCELLED:
            raise CommandError(f"personal enrollment status mismatch: got {enrollment.status}")
        if enrollment.schedule.is_active:
            raise CommandError(f"personal schedule {enrollment.schedule_id} should be inactive after cancellation")

        cancel_event = self._get_event(
            club=club,
            enrollment=enrollment,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
            origin=origin,
            actor_user_id=actor_user_id,
            metadata={},
        )
        return {
            "slot_id": slot_id,
            "enrollment_id": enrollment.id,
            "booking_event_id": booking_event.id,
            "cancel_event_id": cancel_event.id,
            "origin": booking_event.origin,
            "subscription_id": subscription_id,
            "status": enrollment.status,
            "schedule_active": enrollment.schedule.is_active,
        }

    def _get_personal_booking_event(
        self,
        *,
        club: Club,
        student_id: int,
        actor_user_id: int,
        slot_id: int,
        target_date: date,
        origin: str,
        idempotency_key: str,
    ) -> tuple[ScheduleBookingEvent, int]:
        events = list(
            ScheduleBookingEvent.objects.for_club(club)
            .select_related("enrollment", "enrollment__schedule")
            .filter(
                student_id=student_id,
                actor_id=actor_user_id,
                origin=origin,
                effective_date=target_date,
                event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            )
            .order_by("id")
        )
        matches = [
            event
            for event in events
            if event.metadata.get("availability_slot_id") == slot_id
            and event.metadata.get("idempotency_key") == idempotency_key
        ]
        if not matches:
            raise CommandError(
                f"personal booking event not found for student={student_id}, slot={slot_id}, "
                f"idempotency_key={idempotency_key}"
            )
        if len(matches) != 1:
            raise CommandError(
                f"personal booking event duplicate count mismatch for student={student_id}, "
                f"slot={slot_id}: got {len(matches)}"
            )
        event = matches[0]
        subscription_id = event.metadata.get("subscription_id")
        if not isinstance(subscription_id, int):
            raise CommandError(f"personal slot {slot_id} event missing subscription_id metadata")
        return event, subscription_id

    def _assert_personal_slot_available(self, *, club: Club, slot_id: int) -> dict:
        slot = PersonalAvailabilitySlot.objects.for_club(club).get(id=slot_id)
        if slot.status != PersonalAvailabilitySlot.Status.PUBLISHED:
            raise CommandError(f"personal slot {slot_id} status mismatch: got {slot.status}")
        if slot.booked_enrollment_id is not None:
            raise CommandError(f"personal slot {slot_id} should not keep booked enrollment")
        booked_events = ScheduleBookingEvent.objects.for_club(club).filter(
            metadata__availability_slot_id=slot_id,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
        ).count()
        return {
            "slot_id": slot.id,
            "status": slot.status,
            "booked_enrollment_id": slot.booked_enrollment_id,
            "booked_events": booked_events,
        }

    def _get_enrollment(
        self,
        *,
        club: Club,
        student_id: int,
        schedule_id: int,
        target_date: date,
        created_from: str,
    ) -> ScheduleEnrollment:
        enrollments = list(
            ScheduleEnrollment.objects.for_club(club)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                starts_on=target_date,
                ends_on=target_date,
                created_from=created_from,
            )
            .order_by("id")
        )
        if not enrollments:
            raise CommandError(f"booking enrollment not found for student={student_id}, schedule={schedule_id}")
        if len(enrollments) != 1:
            raise CommandError(
                f"booking enrollment duplicate count mismatch for student={student_id}, "
                f"schedule={schedule_id}: got {len(enrollments)}"
            )
        return enrollments[0]

    def _get_event(
        self,
        *,
        club: Club,
        enrollment: ScheduleEnrollment,
        event_type: str,
        origin: str,
        actor_user_id: int,
        metadata: dict,
    ) -> ScheduleBookingEvent:
        event = (
            ScheduleBookingEvent.objects.for_club(club)
            .filter(enrollment=enrollment, event_type=event_type)
            .first()
        )
        if event is None:
            raise CommandError(f"booking event not found: enrollment={enrollment.id}, type={event_type}")
        if event.origin != origin:
            raise CommandError(f"booking event origin mismatch: expected {origin}, got {event.origin}")
        if event.actor_id != actor_user_id:
            raise CommandError(f"booking event actor mismatch: expected {actor_user_id}, got {event.actor_id}")
        if event.metadata != metadata:
            raise CommandError(f"booking event metadata mismatch: expected {metadata}, got {event.metadata}")
        return event

    def _assert_cancellation_visibility(
        self,
        *,
        club: Club,
        student_id: int,
        child_id: int,
        parent_user_id: int,
        foreign_child_id: int,
        target_date: date,
        expected: dict,
    ) -> dict:
        student_names = self._schedule_names(club=club, student_id=student_id, target_date=target_date)
        child_names = self._schedule_names(club=club, student_id=child_id, target_date=target_date)
        for label, names in [("student", student_names), ("parent child", child_names)]:
            for expected_name in [
                expected["group_name"],
                expected[f"{'student' if label == 'student' else 'parent'}_personal_group_name"],
            ]:
                if expected_name in names:
                    raise CommandError(f"{label} cancelled schedule still visible: {expected_name}, got {names}")

        children = get_parent_children(user_id=parent_user_id, club=club)
        child_ids = [child["id"] for child in children]
        if child_ids != [child_id]:
            raise CommandError(f"parent children mismatch: expected {[child_id]}, got {child_ids}")
        if foreign_child_id in child_ids:
            raise CommandError("parent can see foreign child")
        profile = get_child_profile(user_id=parent_user_id, club=club, student_id=child_id)
        profile_names = [item["group_name"] for item in profile["schedule"]]
        for expected_name in [expected["group_name"], expected["parent_personal_group_name"]]:
            if expected_name in profile_names:
                raise CommandError(
                    f"parent profile cancelled schedule still visible: {expected_name}, got {profile_names}"
                )

        return {
            "student_schedule": student_names,
            "parent_child_schedule": child_names,
            "parent_child_ids": child_ids,
        }

    def _schedule_names(self, *, club: Club, student_id: int, target_date: date) -> list[str]:
        occurrences = get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student_id,
            date_from=target_date,
            date_to=target_date,
        )
        return [occurrence.group_name for occurrence in occurrences]
