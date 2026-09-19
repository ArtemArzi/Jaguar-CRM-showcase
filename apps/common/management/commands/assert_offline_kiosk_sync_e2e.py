from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession
from apps.billing.models import Debt, Subscription, TrainingType
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert offline kiosk sync replay created one check-in and one subscription deduction."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_offline_kiosk_sync_e2e.",
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
                    raise CommandError(f"offline kiosk sync E2E assertion failed: {exc}") from exc
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
        student_id = int(fixture["student_id"])
        schedule_id = int(fixture["schedule_id"])
        training_type_id = int(fixture["training_type_id"])
        subscription_id = int(fixture["subscription_id"])
        target_date = date.fromisoformat(fixture["checkin_date"])
        expected = fixture["expected"]

        checkins = list(
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                date=target_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("id")
        )
        if not checkins:
            raise CommandError("offline sync check-in not found")
        if len(checkins) != 1:
            raise CommandError(f"offline sync check-in duplicate count mismatch: expected 1, got {len(checkins)}")

        checkin = checkins[0]
        if checkin.subscription_id != subscription_id:
            raise CommandError(
                "offline sync check-in subscription mismatch: "
                f"expected {subscription_id}, got {checkin.subscription_id}"
            )
        if checkin.is_debt:
            raise CommandError("offline sync check-in unexpectedly created debt")

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        expected_left_after = int(expected["trainings_left_after"])
        expected_used_after = int(expected["trainings_used_after"])
        if subscription.trainings_left != expected_left_after:
            raise CommandError(
                "subscription trainings_left mismatch after offline replay: "
                f"expected {expected_left_after}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != expected_used_after:
            raise CommandError(
                "subscription trainings_used mismatch after offline replay: "
                f"expected {expected_used_after}, got {subscription.trainings_used}"
            )

        debts = Debt.objects.for_club(club_id).filter(student_id=student_id, checkin__date=target_date)
        if debts.exists():
            raise CommandError(f"offline sync unexpectedly created debts: count={debts.count()}")

        cascade_events = self._cascade_event_evidence(club_id=club_id, checkin=checkin)
        earning_count = TrainerEarning.objects.for_club(club_id).filter(checkin=checkin, cancelled=False).count()
        if earning_count != 1:
            raise CommandError(f"trainer earning count mismatch: expected 1, got {earning_count}")

        training_type = TrainingType.objects.for_club(club_id).select_related("grade_system").get(id=training_type_id)
        if training_type.grade_system_id is None:
            raise CommandError("training type is not mapped to grade system")
        student_grade = StudentGrade.objects.for_club(club_id).get(
            student_id=student_id,
            grade_system_id=training_type.grade_system_id,
        )
        progress_count = GradeProgressEvent.objects.for_club(club_id).filter(
            student_grade=student_grade,
            checkin=checkin,
        ).count()
        if progress_count != 1:
            raise CommandError(f"grade progress event count mismatch: expected 1, got {progress_count}")
        if student_grade.trainings_since_last_grade != 1:
            raise CommandError(
                "student grade progress mismatch after offline replay: "
                f"expected 1, got {student_grade.trainings_since_last_grade}"
            )

        group_session = GroupSession.objects.for_club(club_id).filter(schedule_id=schedule_id, date=target_date).first()
        if group_session is None:
            raise CommandError("group session for offline sync check-in not found")

        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date != target_date:
            raise CommandError(
                f"student last_visit_date mismatch: expected {target_date}, got {student.last_visit_date}"
            )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkins": {
                "count": len(checkins),
                "ids": [item.id for item in checkins],
            },
            "idempotency": {
                "duplicate_replay_safe": len(checkins) == 1
                and subscription.trainings_left == expected_left_after
                and subscription.trainings_used == expected_used_after,
            },
            "subscription": {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
            "debts": {
                "count": debts.count(),
            },
            "cascade_events": cascade_events,
            "trainer_earning_count": earning_count,
            "grade_progress_event_count": progress_count,
            "group_session": {
                "id": group_session.id,
                "attendee_count": group_session.attendee_count,
            },
            "student": {
                "id": student.id,
                "last_visit_date": student.last_visit_date.isoformat(),
            },
        }

    def _cascade_event_evidence(self, *, club_id: int, checkin: Checkin) -> dict:
        expected_effects = {
            CheckinCascadeEvent.Effect.SALARY: True,
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: True,
            CheckinCascadeEvent.Effect.GROUP_ANALYTICS: True,
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: True,
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: False,
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK: False,
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
