from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession
from apps.billing.models import Subscription
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.notifications.models import SentNotification
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert same-checkin lifecycle E2E forward and cancellation side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_checkin_lifecycle_e2e.",
        )
        parser.add_argument(
            "--stage",
            choices=("forward", "cancelled"),
            required=True,
            help="Lifecycle stage to assert.",
        )
        parser.add_argument(
            "--checkin-id",
            type=int,
            required=True,
            help="Check-in id returned by the kiosk browser request.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for lifecycle side effects before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        stage = options["stage"]
        checkin_id = int(options["checkin_id"])
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                if stage == "forward":
                    evidence = self._collect_forward_evidence(fixture, checkin_id=checkin_id)
                else:
                    evidence = self._collect_cancelled_evidence(fixture, checkin_id=checkin_id)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(
                        f"check-in lifecycle E2E {stage} assertion failed: {exc}"
                    ) from exc
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
            "owner",
            "student",
            "schedule_id",
            "training_type_id",
            "subscription_id",
            "student_grade_id",
            "retention_task_id",
            "checkin_date",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_forward_evidence(self, fixture: dict, *, checkin_id: int) -> dict:
        club_id = int(fixture["club_id"])
        checkin = self._get_checkin(
            club_id=club_id,
            checkin_id=checkin_id,
            live=True,
        )
        student_id = int(fixture["student"]["student_id"])
        schedule_id = int(fixture["schedule_id"])
        training_type_id = int(fixture["training_type_id"])
        subscription_id = int(fixture["subscription_id"])
        expected = fixture["expected"]

        if checkin.student_id != student_id:
            raise CommandError("check-in student mismatch")
        if checkin.schedule_id != schedule_id:
            raise CommandError("check-in schedule mismatch")
        if checkin.training_type_id != training_type_id:
            raise CommandError("check-in training type mismatch")
        if checkin.subscription_id != subscription_id:
            raise CommandError("check-in subscription mismatch")
        if checkin.source != Checkin.Source.KIOSK:
            raise CommandError(f"check-in source mismatch: expected kiosk, got {checkin.source}")
        if checkin.is_debt:
            raise CommandError("check-in unexpectedly created debt")

        cascade_events = self._cascade_event_evidence(club_id=club_id, checkin=checkin)
        subscription = self._assert_subscription(
            club_id=club_id,
            subscription_id=subscription_id,
            trainings_left=int(expected["trainings_left_after_checkin"]),
            trainings_used=int(expected["trainings_used_after_checkin"]),
        )
        earning = self._assert_earning(club_id=club_id, checkin=checkin, cancelled=False)
        grade = self._assert_grade_forward(club_id=club_id, fixture=fixture, checkin=checkin)
        group_session = self._assert_group_session(
            club_id=club_id,
            schedule_id=schedule_id,
            checkin_date=fixture["checkin_date"],
            attendee_count=int(expected["group_session_attendee_count_after_checkin"]),
        )
        retention_task = self._assert_retention_forward(club_id=club_id, fixture=fixture)
        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date != checkin.date:
            raise CommandError(
                f"student last_visit_date mismatch: expected {checkin.date}, got {student.last_visit_date}"
            )
        parent_notification = self._assert_parent_notification(
            club_id=club_id,
            student_id=student_id,
            checkin_id=checkin.id,
            notification_type=f"parent_checkin:{checkin.id}",
        )

        return {
            "ok": True,
            "stage": "forward",
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkin": {
                "id": checkin.id,
                "source": checkin.source,
                "subscription_id": checkin.subscription_id,
                "is_debt": checkin.is_debt,
            },
            "cascade_events": cascade_events,
            "subscription": subscription,
            "earning": earning,
            "grade": grade,
            "group_session": group_session,
            "retention_task": retention_task,
            "student": {"last_visit_date": student.last_visit_date.isoformat()},
            "parent_notification": parent_notification,
        }

    def _collect_cancelled_evidence(self, fixture: dict, *, checkin_id: int) -> dict:
        club_id = int(fixture["club_id"])
        checkin = self._get_checkin(
            club_id=club_id,
            checkin_id=checkin_id,
            live=False,
        )
        student_id = int(fixture["student"]["student_id"])
        schedule_id = int(fixture["schedule_id"])
        subscription_id = int(fixture["subscription_id"])
        expected = fixture["expected"]

        if checkin.student_id != student_id:
            raise CommandError("cancelled check-in student mismatch")
        if checkin.schedule_id != schedule_id:
            raise CommandError("cancelled check-in schedule mismatch")
        if checkin.cancelled_at is None:
            raise CommandError("check-in is not cancelled")
        if checkin.deleted_at is None:
            raise CommandError("check-in is not soft-deleted")
        if checkin.cancelled_by_id != int(fixture["owner"]["user_id"]):
            raise CommandError("check-in cancellation actor mismatch")

        cascade_events = self._cascade_event_evidence(club_id=club_id, checkin=checkin)
        subscription = self._assert_subscription(
            club_id=club_id,
            subscription_id=subscription_id,
            trainings_left=int(expected["trainings_left_after_cancel"]),
            trainings_used=int(expected["trainings_used_after_cancel"]),
        )
        earning = self._assert_earning(club_id=club_id, checkin=checkin, cancelled=True)
        grade = self._assert_grade_cancelled(club_id=club_id, fixture=fixture, checkin=checkin)
        group_session = self._assert_group_session(
            club_id=club_id,
            schedule_id=schedule_id,
            checkin_date=fixture["checkin_date"],
            attendee_count=int(expected["group_session_attendee_count_after_cancel"]),
        )
        retention_task = self._assert_retention_cancelled(club_id=club_id, fixture=fixture)
        student = Student.objects.for_club(club_id).get(id=student_id)
        if student.last_visit_date is not None:
            raise CommandError("student last_visit_date was not recalculated after cancellation")

        parent_checkin_notification = self._assert_parent_notification(
            club_id=club_id,
            student_id=student_id,
            checkin_id=checkin.id,
            notification_type=f"parent_checkin:{checkin.id}",
        )
        parent_cancel_notification = self._assert_parent_notification(
            club_id=club_id,
            student_id=student_id,
            checkin_id=checkin.id,
            notification_type=f"parent_checkin_cancelled:{checkin.id}",
        )
        live_checkins = Checkin.objects.for_club(club_id).filter(
            id=checkin.id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).count()
        if live_checkins != 0:
            raise CommandError("cancelled check-in is still visible as live")

        return {
            "ok": True,
            "stage": "cancelled",
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkin": {
                "id": checkin.id,
                "cancelled": True,
                "soft_deleted": True,
                "live_count": live_checkins,
            },
            "cascade_events": cascade_events,
            "subscription": subscription,
            "earning": earning,
            "grade": grade,
            "group_session": group_session,
            "retention_task": retention_task,
            "student": {"last_visit_date": None},
            "parent_notifications": {
                "checkin": parent_checkin_notification,
                "cancelled": parent_cancel_notification,
            },
        }

    def _get_checkin(self, *, club_id: int, checkin_id: int, live: bool) -> Checkin:
        manager = Checkin.objects if live else Checkin._base_manager
        checkin = (
            manager.filter(id=checkin_id, club_id=club_id)
            .select_related("training_type", "subscription", "student")
            .first()
        )
        if checkin is None:
            raise CommandError("check-in row not found")
        if live and (checkin.cancelled_at is not None or checkin.deleted_at is not None):
            raise CommandError("check-in is not live")
        return checkin

    def _cascade_event_evidence(self, *, club_id: int, checkin: Checkin) -> dict:
        expected_effects = {
            CheckinCascadeEvent.Effect.SALARY: True,
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: True,
            CheckinCascadeEvent.Effect.GROUP_ANALYTICS: True,
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: True,
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: True,
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
            for event in CheckinCascadeEvent.objects.for_club(club_id).filter(checkin_id=checkin.id)
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
                self._assert_salary_snapshot(event=event, checkin=checkin)
            evidence[effect] = {
                "id": event.id,
                "expected": event.expected,
                "status": event.status,
                "task_name": event.task_name,
            }
        return evidence

    def _assert_salary_snapshot(self, *, event: CheckinCascadeEvent, checkin: Checkin) -> None:
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

    def _assert_subscription(
        self,
        *,
        club_id: int,
        subscription_id: int,
        trainings_left: int,
        trainings_used: int,
    ) -> dict:
        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        if subscription.trainings_left != trainings_left:
            raise CommandError(
                "subscription trainings_left mismatch: "
                f"expected {trainings_left}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != trainings_used:
            raise CommandError(
                "subscription trainings_used mismatch: "
                f"expected {trainings_used}, got {subscription.trainings_used}"
            )
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError("subscription status is not active")
        return {
            "id": subscription.id,
            "trainings_left": subscription.trainings_left,
            "trainings_used": subscription.trainings_used,
            "status": subscription.status,
        }

    def _assert_earning(self, *, club_id: int, checkin: Checkin, cancelled: bool) -> dict:
        earning = TrainerEarning.objects.for_club(club_id).filter(checkin_id=checkin.id).first()
        if earning is None:
            raise CommandError("trainer earning for check-in not found")
        if earning.cancelled is not cancelled:
            raise CommandError(
                f"trainer earning cancelled mismatch: expected {cancelled}, got {earning.cancelled}"
            )
        return {
            "id": earning.id,
            "cancelled": earning.cancelled,
            "amount": str(earning.amount),
        }

    def _assert_grade_forward(self, *, club_id: int, fixture: dict, checkin: Checkin) -> dict:
        student_grade = StudentGrade.objects.for_club(club_id).get(id=int(fixture["student_grade_id"]))
        if student_grade.trainings_since_last_grade != 1:
            raise CommandError("student grade progress counter was not incremented")
        progress_event = (
            GradeProgressEvent.objects.for_club(club_id)
            .filter(student_grade=student_grade, checkin_id=checkin.id)
            .first()
        )
        if progress_event is None:
            raise CommandError("grade progress event for check-in not found")
        return {
            "student_grade_id": student_grade.id,
            "trainings_since_last_grade": student_grade.trainings_since_last_grade,
            "progress_event_id": progress_event.id,
        }

    def _assert_grade_cancelled(self, *, club_id: int, fixture: dict, checkin: Checkin) -> dict:
        student_grade = StudentGrade.objects.for_club(club_id).get(id=int(fixture["student_grade_id"]))
        if student_grade.trainings_since_last_grade != 0:
            raise CommandError("grade progress counter was not reversed")
        if GradeProgressEvent.objects.for_club(club_id).filter(checkin_id=checkin.id).exists():
            raise CommandError("grade progress event was not removed")
        return {
            "student_grade_id": student_grade.id,
            "trainings_since_last_grade": student_grade.trainings_since_last_grade,
            "progress_event_removed": True,
        }

    def _assert_group_session(
        self,
        *,
        club_id: int,
        schedule_id: int,
        checkin_date: str,
        attendee_count: int,
    ) -> dict:
        group_session = GroupSession.objects.for_club(club_id).get(
            schedule_id=schedule_id,
            date=checkin_date,
        )
        if group_session.attendee_count != attendee_count:
            raise CommandError(
                "group session attendee count mismatch: "
                f"expected {attendee_count}, got {group_session.attendee_count}"
            )
        return {
            "id": group_session.id,
            "attendee_count": group_session.attendee_count,
        }

    def _assert_retention_forward(self, *, club_id: int, fixture: dict) -> dict:
        retention_task = RetentionTask.objects.for_club(club_id).get(id=int(fixture["retention_task_id"]))
        if retention_task.status != RetentionTask.TaskStatus.CLOSED:
            raise CommandError("retention task was not auto-closed")
        if retention_task.resolution != RetentionTask.Resolution.AUTO_CHECKIN:
            raise CommandError("retention task resolution is not auto_checkin")
        if retention_task.resolved_at is None:
            raise CommandError("retention task resolved_at was not set")
        return {
            "id": retention_task.id,
            "status": retention_task.status,
            "resolution": retention_task.resolution,
            "resolved": True,
        }

    def _assert_retention_cancelled(self, *, club_id: int, fixture: dict) -> dict:
        retention_task = RetentionTask.objects.for_club(club_id).get(id=int(fixture["retention_task_id"]))
        if retention_task.status != RetentionTask.TaskStatus.OPEN:
            raise CommandError("retention task status was not reopened")
        if retention_task.resolved_at is not None:
            raise CommandError("retention task resolved_at was not cleared")
        if retention_task.resolution:
            raise CommandError("retention task resolution was not cleared")
        return {
            "id": retention_task.id,
            "status": retention_task.status,
            "resolved_at": retention_task.resolved_at,
            "resolution": retention_task.resolution,
        }

    def _assert_parent_notification(
        self,
        *,
        club_id: int,
        student_id: int,
        checkin_id: int,
        notification_type: str,
    ) -> dict:
        notification = (
            SentNotification.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                notification_type=notification_type,
            )
            .first()
        )
        if notification is None:
            raise CommandError(f"parent notification record not found: {notification_type}")
        return {
            "id": notification.id,
            "checkin_id": checkin_id,
            "notification_type": notification.notification_type,
        }
