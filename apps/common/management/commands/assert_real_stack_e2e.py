from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession
from apps.billing.models import Subscription, TrainingType
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.notifications.models import SentNotification
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert real-stack browser E2E check-in side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_real_stack_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for qcluster side effects before failing.",
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
                    raise CommandError(f"real-stack E2E assertion failed: {exc}") from exc
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
            "student_id",
            "schedule_id",
            "training_type_id",
            "subscription_id",
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
        expected = fixture["expected"]
        today = timezone.localdate()

        checkin = self._get_checkin(
            club_id=club_id,
            student_id=student_id,
            schedule_id=schedule_id,
            target_date=today,
        )
        if checkin.training_type_id != training_type_id:
            raise CommandError(
                f"check-in training type mismatch: expected {training_type_id}, got {checkin.training_type_id}"
            )
        if checkin.subscription_id != subscription_id:
            raise CommandError(
                f"check-in subscription mismatch: expected {subscription_id}, got {checkin.subscription_id}"
            )
        if checkin.is_debt:
            raise CommandError("check-in unexpectedly created debt")
        cascade_events = self._cascade_event_evidence(
            club_id=club_id,
            checkin=checkin,
        )

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        expected_left_after = int(expected["trainings_left_after"])
        if subscription.trainings_left != expected_left_after:
            raise CommandError(
                "subscription trainings_left mismatch: "
                f"expected {expected_left_after}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != 1:
            raise CommandError(f"subscription trainings_used mismatch: expected 1, got {subscription.trainings_used}")

        earning = TrainerEarning.objects.for_club(club_id).filter(checkin=checkin, cancelled=False).first()
        if earning is None:
            raise CommandError("trainer earning for check-in not found")

        training_type = TrainingType.objects.for_club(club_id).select_related("grade_system").get(id=training_type_id)
        if training_type.grade_system_id is None:
            raise CommandError("training type is not mapped to grade system")
        student_grade = StudentGrade.objects.for_club(club_id).get(
            student_id=student_id,
            grade_system_id=training_type.grade_system_id,
        )
        progress_event = GradeProgressEvent.objects.for_club(club_id).filter(
            student_grade=student_grade,
            checkin=checkin,
        ).first()
        if progress_event is None:
            raise CommandError("grade progress event for check-in not found")
        if student_grade.trainings_since_last_grade != 1:
            raise CommandError(
                "student grade progress mismatch: "
                f"expected 1, got {student_grade.trainings_since_last_grade}"
            )

        group_session = GroupSession.objects.for_club(club_id).filter(schedule_id=schedule_id, date=today).first()
        if group_session is None:
            raise CommandError("group session for schedule/date not found")
        if group_session.attendee_count < 1:
            raise CommandError(f"group session attendee_count < 1: got {group_session.attendee_count}")

        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date != today:
            raise CommandError(f"student last_visit_date mismatch: expected {today}, got {student.last_visit_date}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkin_id": checkin.id,
            "checkin": {
                "subscription_id": checkin.subscription_id,
                "is_debt": checkin.is_debt,
                "date": checkin.date.isoformat(),
            },
            "cascade_events": cascade_events,
            "subscription": {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
            "trainer_earning_id": earning.id,
            "grade_progress_event_id": progress_event.id,
            "student_grade": {
                "id": student_grade.id,
                "trainings_since_last_grade": student_grade.trainings_since_last_grade,
            },
            "group_session": {
                "id": group_session.id,
                "attendee_count": group_session.attendee_count,
            },
            "student": {
                "id": student.id,
                "last_visit_date": student.last_visit_date.isoformat(),
            },
            "parent_notification": self._parent_notification_evidence(
                club_id=club_id,
                student=student,
                checkin=checkin,
                sent_date=today,
            ),
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
        expected_task_names = {
            CheckinCascadeEvent.Effect.SALARY: "apps.attendance.tasks.calculate_salary",
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: "apps.attendance.tasks.update_grade_progress",
            CheckinCascadeEvent.Effect.GROUP_ANALYTICS: "apps.attendance.tasks.update_group_analytics",
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: "apps.attendance.tasks.log_parent_event",
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: "apps.retention.tasks.auto_close_retention_on_checkin",
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK: "apps.retention.tasks.create_post_trial_task",
            CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: "apps.notifications.tasks.check_trainings_left_push",
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
            expected_task_name = expected_task_names[effect]
            if event.task_name != expected_task_name:
                raise CommandError(
                    f"cascade event {effect} task_name mismatch: "
                    f"expected {expected_task_name}, got {event.task_name}"
                )
            payload = event.payload or {}
            if payload.get("checkin_id") != checkin.id:
                raise CommandError(
                    f"cascade event {effect} payload checkin_id mismatch: "
                    f"expected {checkin.id}, got {payload.get('checkin_id')}"
                )
            if payload.get("club_id") != club_id:
                raise CommandError(
                    f"cascade event {effect} payload club_id mismatch: "
                    f"expected {club_id}, got {payload.get('club_id')}"
                )
            if effect == CheckinCascadeEvent.Effect.SALARY:
                self._assert_salary_cascade_snapshot(event=event, checkin=checkin)
            evidence[effect] = {
                "id": event.id,
                "expected": event.expected,
                "status": event.status,
                "task_name": event.task_name,
            }
        return evidence

    def _assert_salary_cascade_snapshot(self, *, event: CheckinCascadeEvent, checkin: Checkin) -> None:
        payload = event.payload or {}
        expected_pairs = {
            "trainer_id_snapshot": checkin.trainer_id,
            "training_type_id_snapshot": checkin.training_type_id,
            "training_type_kind_snapshot": checkin.training_type.kind,
            "calculation_basis": "checkin_salary_snapshot",
            "snapshot_provenance": "checkin_queue",
        }
        for key, expected in expected_pairs.items():
            if payload.get(key) != expected:
                raise CommandError(
                    f"salary cascade payload {key} mismatch: expected {expected}, got {payload.get(key)}"
                )

    def _get_checkin(self, *, club_id: int, student_id: int, schedule_id: int, target_date):
        checkins = Checkin.objects.for_club(club_id).filter(
            student_id=student_id,
            schedule_id=schedule_id,
            date=target_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        count = checkins.count()
        if count == 0:
            raise CommandError("check-in for fixture student/schedule/today not found")
        if count > 1:
            raise CommandError(f"expected one live check-in, found {count}")
        return checkins.select_related("subscription").get()

    def _parent_notification_evidence(self, *, club_id: int, student: Student, checkin: Checkin, sent_date):
        if not student.is_child or not student.parent_user_id:
            return {
                "status": "skipped",
                "reason": "fixture student has no parent user",
            }

        notification = (
            SentNotification.objects.for_club(club_id)
            .filter(
                student=student,
                notification_type=f"parent_checkin:{checkin.id}",
                sent_date=sent_date,
            )
            .first()
        )
        if notification is None:
            raise CommandError("parent check-in notification record not found")
        return {
            "status": "recorded",
            "sent_notification_id": notification.id,
        }
