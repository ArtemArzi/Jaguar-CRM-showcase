from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    ScheduleBookingEvent,
    ScheduleEnrollment,
)
from apps.billing.models import Debt, Subscription
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.notifications.models import SentNotification
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert kiosk guest book-and-check-in created one booking and one check-in."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_kiosk_guest_book_checkin_e2e.",
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
                    raise CommandError(f"kiosk guest book-and-check-in E2E assertion failed: {exc}") from exc
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

        required = {
            "fixture_id",
            "club_id",
            "student_id",
            "schedule_id",
            "training_type_id",
            "subscription_id",
            "checkin_date",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        scenarios = fixture.get("scenarios") or {
            "subscription": {
                "student_id": fixture["student_id"],
                "schedule_id": fixture["schedule_id"],
                "training_type_id": fixture["training_type_id"],
                "subscription_id": fixture["subscription_id"],
                "checkin_date": fixture["checkin_date"],
                "expected": {
                    **fixture["expected"],
                    "is_debt": False,
                    "debt_effect": "none",
                    "subscription_effect": "deducted",
                },
            }
        }
        scenario_evidence = {
            name: self._collect_scenario_evidence(
                club_id=club_id,
                scenario_name=name,
                scenario=scenario,
            )
            for name, scenario in scenarios.items()
        }
        primary = scenario_evidence.get("subscription") or next(iter(scenario_evidence.values()))

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "scenarios": scenario_evidence,
            "booking": primary["booking"],
            "checkin": primary["checkin"],
            "subscription": primary["subscription"],
            "cascade_events": primary["cascade_events"],
            "debts": primary["debts"],
            "trainer_earning_count": primary["trainer_earning_count"],
            "group_session": primary["group_session"],
            "grade_progress_event_count": primary["grade_progress_event_count"],
            "parent_notification_id": primary["parent_notification_id"],
        }

    def _collect_scenario_evidence(
        self,
        *,
        club_id: int,
        scenario_name: str,
        scenario: dict,
    ) -> dict:
        student_id = int(scenario["student_id"])
        schedule_id = int(scenario["schedule_id"])
        training_type_id = int(scenario["training_type_id"])
        subscription_id = scenario.get("subscription_id")
        expected_subscription_id = int(subscription_id) if subscription_id is not None else None
        target_date = date.fromisoformat(scenario["checkin_date"])
        expected = scenario["expected"]

        enrollment = self._get_guest_enrollment(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            target_date=target_date,
            expected_status=expected.get("enrollment_status", ScheduleEnrollment.Status.ACTIVE),
        )
        booking_event = self._get_booking_event(
            club_id=club_id,
            enrollment=enrollment,
            student_id=student_id,
            schedule_id=schedule_id,
            target_date=target_date,
        )
        checkin = self._get_checkin(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
            target_date=target_date,
        )
        if checkin.subscription_id != expected_subscription_id:
            raise CommandError(
                f"{scenario_name} check-in subscription mismatch: "
                f"expected {expected_subscription_id}, got {checkin.subscription_id}"
            )
        expected_is_debt = bool(expected["is_debt"])
        if checkin.is_debt != expected_is_debt:
            raise CommandError(
                f"{scenario_name} check-in debt flag mismatch: "
                f"expected {expected_is_debt}, got {checkin.is_debt}"
            )

        subscription = None
        subscription_evidence = None
        if expected_subscription_id is not None:
            subscription = Subscription.objects.for_club(club_id).get(id=expected_subscription_id)
            expected_left_after = int(expected["trainings_left_after"])
            expected_used_after = int(expected["trainings_used_after"])
            if subscription.trainings_left != expected_left_after:
                raise CommandError(
                    f"{scenario_name} subscription trainings_left mismatch after guest check-in: "
                    f"expected {expected_left_after}, got {subscription.trainings_left}"
                )
            if subscription.trainings_used != expected_used_after:
                raise CommandError(
                    f"{scenario_name} subscription trainings_used mismatch after guest check-in: "
                    f"expected {expected_used_after}, got {subscription.trainings_used}"
                )
            subscription_evidence = {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            }

        debts = Debt.objects.for_club(club_id).filter(student_id=student_id, checkin=checkin)
        if expected_is_debt:
            if debts.count() != 1:
                raise CommandError(f"{scenario_name} debt count mismatch: expected 1, got {debts.count()}")
            expected_debt_amount = Decimal(str(expected["debt_amount"])).quantize(Decimal("0.01"))
            actual_debt_amount = Decimal(str(debts.get().tariff_price)).quantize(Decimal("0.01"))
            if actual_debt_amount != expected_debt_amount:
                raise CommandError(
                    f"{scenario_name} debt amount mismatch: expected {expected_debt_amount}, got {actual_debt_amount}"
                )
        elif debts.exists():
            raise CommandError(
                f"{scenario_name} guest book-and-check-in unexpectedly created debts: count={debts.count()}"
            )

        other_checkin_count = (
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                date=target_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .exclude(schedule_id=schedule_id)
            .count()
        )
        if other_checkin_count:
            raise CommandError(f"{scenario_name} created check-ins for non-selected schedules")

        earning_count = TrainerEarning.objects.for_club(club_id).filter(checkin=checkin).count()
        if earning_count:
            raise CommandError(
                f"{scenario_name} group guest check-in should not create check-in salary earning: count={earning_count}"
            )

        cascade_events = self._cascade_event_evidence(
            club_id=club_id,
            checkin=checkin,
            post_trial_task_expected=bool(expected.get("post_trial_task_expected", False)),
        )
        group_session = (
            GroupSession.objects.for_club(club_id)
            .filter(schedule_id=schedule_id, date=target_date)
            .first()
        )
        if group_session is None:
            raise CommandError(f"{scenario_name} group session for guest check-in not found")
        if group_session.attendee_count != 1:
            raise CommandError(
                f"{scenario_name} group session attendee_count mismatch: "
                f"expected 1, got {group_session.attendee_count}"
            )

        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date != target_date:
            raise CommandError(
                f"{scenario_name} student last_visit_date mismatch: "
                f"expected {target_date}, got {student.last_visit_date}"
            )

        grade_progress_count = (
            GradeProgressEvent.objects.for_club(club_id)
            .filter(checkin=checkin)
            .count()
        )
        if grade_progress_count != 1:
            raise CommandError(
                f"{scenario_name} grade progress event count mismatch: "
                f"expected 1, got {grade_progress_count}"
            )
        student_grade = StudentGrade.objects.for_club(club_id).get(student_id=student_id)
        if student_grade.trainings_since_last_grade != 1:
            raise CommandError(
                f"{scenario_name} student grade progress mismatch after guest check-in: "
                f"expected 1, got {student_grade.trainings_since_last_grade}"
            )

        notification = (
            SentNotification.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                notification_type=f"parent_checkin:{checkin.id}",
                sent_date=target_date,
            )
            .first()
        )
        if notification is None:
            raise CommandError(f"{scenario_name} parent check-in notification record not found")

        return {
            "booking": {
                "enrollment_id": enrollment.id,
                "enrollment_status": enrollment.status,
                "event_id": booking_event.id,
                "origin": booking_event.origin,
            },
            "checkin": {
                "id": checkin.id,
                "date": checkin.date.isoformat(),
                "is_debt": checkin.is_debt,
                "source": checkin.source,
                "subscription_id": checkin.subscription_id,
            },
            "subscription": subscription_evidence,
            "cascade_events": cascade_events,
            "debts": {
                "count": debts.count(),
                "amount": str(Decimal(str(debts.get().tariff_price)).quantize(Decimal("0.01")))
                if debts.exists()
                else "0.00",
            },
            "trainer_earning_count": earning_count,
            "group_session": {
                "id": group_session.id,
                "attendee_count": group_session.attendee_count,
            },
            "grade_progress_event_count": grade_progress_count,
            "parent_notification_id": notification.id,
        }

    def _get_guest_enrollment(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        target_date: date,
        expected_status: str,
    ) -> ScheduleEnrollment:
        enrollments = list(
            ScheduleEnrollment.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                starts_on=target_date,
                ends_on=target_date,
                status=expected_status,
                created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
            )
            .order_by("id")
        )
        if not enrollments:
            raise CommandError("guest one-day enrollment not found")
        if len(enrollments) != 1:
            raise CommandError(f"guest one-day enrollment duplicate count mismatch: got {len(enrollments)}")
        return enrollments[0]

    def _get_booking_event(
        self,
        *,
        club_id: int,
        enrollment: ScheduleEnrollment,
        student_id: int,
        schedule_id: int,
        target_date: date,
    ) -> ScheduleBookingEvent:
        event = ScheduleBookingEvent.objects.for_club(club_id).filter(enrollment=enrollment).first()
        if event is None:
            raise CommandError("guest booking event not found")
        if event.event_type != ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED:
            raise CommandError(f"booking event type mismatch: got {event.event_type}")
        if event.origin != ScheduleBookingEvent.Origin.WALK_IN_CHECKIN:
            raise CommandError(f"booking event origin mismatch: got {event.origin}")
        if event.actor_id is not None:
            raise CommandError("walk-in kiosk booking must not record staff actor")
        expected_idempotency_key = f"kiosk-guest-booking-{student_id}-{schedule_id}-{target_date.isoformat()}"
        if event.metadata != {"idempotency_key": expected_idempotency_key}:
            raise CommandError(f"booking event metadata mismatch: got {event.metadata}")
        return event

    def _get_checkin(
        self,
        *,
        club_id: int,
        student_id: int,
        schedule_id: int,
        training_type_id: int,
        target_date: date,
    ) -> Checkin:
        checkins = list(
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                date=target_date,
                source=Checkin.Source.KIOSK,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("id")
        )
        if not checkins:
            raise CommandError("guest book-and-check-in check-in not found")
        if len(checkins) != 1:
            raise CommandError(f"guest book-and-check-in duplicate check-in count mismatch: got {len(checkins)}")
        return checkins[0]

    def _cascade_event_evidence(
        self,
        *,
        club_id: int,
        checkin: Checkin,
        post_trial_task_expected: bool,
    ) -> dict:
        expected_effects = {
            CheckinCascadeEvent.Effect.SALARY: False,
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: True,
            CheckinCascadeEvent.Effect.GROUP_ANALYTICS: True,
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: True,
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: False,
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK: post_trial_task_expected,
            CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: False,
        }
        events = {
            event.effect: event
            for event in CheckinCascadeEvent.objects.for_club(club_id).filter(checkin=checkin)
        }
        missing = sorted(set(expected_effects) - set(events))
        unexpected = sorted(set(events) - set(expected_effects))
        if missing:
            raise CommandError(f"missing cascade events: {', '.join(missing)}")
        if unexpected:
            raise CommandError(f"unexpected cascade events: {', '.join(unexpected)}")

        evidence = {}
        for effect, expected in expected_effects.items():
            event = events[effect]
            if event.status != CheckinCascadeEvent.Status.QUEUED:
                raise CommandError(
                    f"cascade event {effect} status mismatch: "
                    f"expected {CheckinCascadeEvent.Status.QUEUED}, got {event.status}"
                )
            if event.expected is not expected:
                raise CommandError(
                    f"cascade event {effect} expected flag mismatch: expected {expected}, got {event.expected}"
                )
            evidence[effect] = {
                "id": event.id,
                "expected": event.expected,
                "status": event.status,
                "task_name": event.task_name,
            }
        return evidence
