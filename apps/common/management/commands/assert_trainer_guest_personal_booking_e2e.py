from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleBookingEvent, ScheduleEnrollment
from apps.billing.models import Debt, Subscription
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert trainer guest/personal booking E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_guest_personal_booking_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for booking state before failing.",
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
                    raise CommandError(f"trainer guest/personal booking E2E assertion failed: {exc}") from exc
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
            "location",
            "group_schedule_id",
            "booking_date",
            "guest_student",
            "personal_student",
            "personal_training_type",
            "personal_subscription_id",
            "personal_booking",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        trainer_user_id = int(fixture["trainer"]["user_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        location_id = int(fixture["location"]["id"])
        group_schedule_id = int(fixture["group_schedule_id"])
        guest_student_id = int(fixture["guest_student"]["student_id"])
        personal_student_id = int(fixture["personal_student"]["student_id"])
        personal_training_type_id = int(fixture["personal_training_type"]["id"])
        personal_subscription_id = int(fixture["personal_subscription_id"])
        booking_date = date.fromisoformat(fixture["booking_date"])
        personal_date = date.fromisoformat(fixture["personal_booking"]["date"])
        expected = fixture["expected"]
        personal_idempotency_key_prefix = expected.get("personal_idempotency_key_prefix")
        if personal_idempotency_key_prefix != "trainer-personal-booking:":
            raise CommandError("fixture personal idempotency key prefix mismatch")

        guest_enrollment = self._get_enrollment(
            club_id=club_id,
            student_id=guest_student_id,
            schedule_id=group_schedule_id,
            starts_on=booking_date,
            ends_on=booking_date,
            created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
            missing_message="guest enrollment not found",
        )
        guest_event = self._get_booking_event(
            club_id=club_id,
            enrollment=guest_enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED,
            origin=ScheduleBookingEvent.Origin.WALK_IN_CHECKIN,
            actor_user_id=trainer_user_id,
            idempotency_key=expected["guest_idempotency_key"],
            missing_message="guest booking event not found",
        )

        personal_enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .select_related("schedule", "schedule__trainer", "schedule__location", "schedule__training_type")
            .filter(
                student_id=personal_student_id,
                starts_on=personal_date,
                ends_on=personal_date,
                status=ScheduleEnrollment.Status.ACTIVE,
                created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if personal_enrollment is None:
            raise CommandError("personal enrollment not found")

        personal_schedule = personal_enrollment.schedule
        if personal_schedule.trainer_id != trainer_id:
            raise CommandError(
                f"personal schedule trainer mismatch: expected {trainer_id}, got {personal_schedule.trainer_id}"
            )
        if personal_schedule.location_id != location_id:
            raise CommandError(
                f"personal schedule location mismatch: expected {location_id}, got {personal_schedule.location_id}"
            )
        if personal_schedule.training_type_id != personal_training_type_id:
            raise CommandError(
                "personal schedule training type mismatch: "
                f"expected {personal_training_type_id}, got {personal_schedule.training_type_id}"
            )
        if personal_schedule.one_time_date != personal_date:
            raise CommandError(
                "personal schedule one_time_date mismatch: "
                f"expected {personal_date}, got {personal_schedule.one_time_date}"
            )
        if personal_schedule.start_time.strftime("%H:%M") != fixture["personal_booking"]["start_time"]:
            raise CommandError("personal schedule start_time mismatch")
        if personal_schedule.end_time.strftime("%H:%M") != fixture["personal_booking"]["end_time"]:
            raise CommandError("personal schedule end_time mismatch")

        personal_event = self._get_booking_event(
            club_id=club_id,
            enrollment=personal_enrollment,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            actor_user_id=trainer_user_id,
            idempotency_key_prefix=personal_idempotency_key_prefix,
            missing_message="personal booking event not found",
        )
        if personal_event.metadata.get("subscription_id") != personal_subscription_id:
            raise CommandError(
                "personal booking subscription metadata mismatch: "
                f"expected {personal_subscription_id}, got {personal_event.metadata.get('subscription_id')}"
            )

        subscription = Subscription.objects.for_club(club_id).get(id=personal_subscription_id)
        if subscription.trainings_left != int(expected["personal_trainings_left"]):
            raise CommandError(
                "personal subscription trainings_left changed before check-in: "
                f"expected {expected['personal_trainings_left']}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != 0:
            raise CommandError(
                f"personal subscription trainings_used changed before check-in: got {subscription.trainings_used}"
            )

        checkin_count = Checkin.objects.for_club(club_id).filter(
            student_id__in=[guest_student_id, personal_student_id],
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).count()
        if checkin_count != 0:
            raise CommandError(f"booking unexpectedly created check-ins: got {checkin_count}")

        debt_count = Debt.objects.for_club(club_id).filter(
            student_id__in=[guest_student_id, personal_student_id],
        ).count()
        if debt_count != 0:
            raise CommandError(f"booking unexpectedly created debts: got {debt_count}")

        earning_count = TrainerEarning.objects.for_club(club_id).filter(cancelled=False).count()
        if earning_count != 0:
            raise CommandError(f"booking unexpectedly created trainer earnings: got {earning_count}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "guest_booking": {
                "enrollment_id": guest_enrollment.id,
                "event_id": guest_event.id,
                "origin": guest_event.origin,
                "created_from": guest_enrollment.created_from,
            },
            "personal_booking": {
                "schedule_id": personal_schedule.id,
                "enrollment_id": personal_enrollment.id,
                "event_id": personal_event.id,
                "origin": personal_event.origin,
                "created_from": personal_enrollment.created_from,
                "subscription_id": personal_event.metadata.get("subscription_id"),
            },
            "no_premature_side_effects": {
                "checkins": checkin_count,
                "debts": debt_count,
                "trainer_earnings": earning_count,
            },
            "subscription": {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
        }

    def _get_enrollment(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        starts_on: date,
        ends_on: date,
        created_from: str,
        missing_message: str,
    ) -> ScheduleEnrollment:
        enrollment = (
            ScheduleEnrollment.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                starts_on=starts_on,
                ends_on=ends_on,
                status=ScheduleEnrollment.Status.ACTIVE,
                created_from=created_from,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if enrollment is None:
            raise CommandError(missing_message)
        return enrollment

    def _get_booking_event(
        self,
        *,
        club_id: int,
        enrollment: ScheduleEnrollment,
        event_type: str,
        origin: str,
        actor_user_id: int,
        missing_message: str,
        idempotency_key: str | None = None,
        idempotency_key_prefix: str | None = None,
    ) -> ScheduleBookingEvent:
        event = (
            ScheduleBookingEvent.objects.for_club(club_id)
            .filter(
                enrollment=enrollment,
                schedule_id=enrollment.schedule_id,
                student_id=enrollment.student_id,
                event_type=event_type,
                origin=origin,
                actor_id=actor_user_id,
                effective_date=enrollment.starts_on,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if event is None:
            raise CommandError(missing_message)
        event_idempotency_key = event.metadata.get("idempotency_key")
        if idempotency_key is not None and event_idempotency_key != idempotency_key:
            raise CommandError(
                f"{event_type} idempotency metadata mismatch: "
                f"expected {idempotency_key}, got {event_idempotency_key}"
            )
        if idempotency_key_prefix is not None and (
            not isinstance(event_idempotency_key, str)
            or not event_idempotency_key
            or not event_idempotency_key.startswith(idempotency_key_prefix)
        ):
            raise CommandError(
                f"{event_type} idempotency metadata prefix mismatch: "
                f"expected non-empty key beginning {idempotency_key_prefix}, got {event_idempotency_key}"
            )
        return event
