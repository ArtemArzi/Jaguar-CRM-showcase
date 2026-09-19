from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession, Schedule
from apps.attendance.selectors import get_unclosed_sessions
from apps.billing.models import Debt, Subscription
from apps.students.models import Student
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert trainer batch check-in E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_trainer_batch_e2e.")
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
                    raise CommandError(f"trainer batch E2E assertion failed: {exc}") from exc
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
            "trainer_id",
            "schedule_id",
            "training_type_id",
            "checkin_date",
            "active_student_ids",
            "active_subscriptions",
            "frozen_student_id",
            "unclosed",
            "schedule_form",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        schedule_id = int(fixture["schedule_id"])
        training_type_id = int(fixture["training_type_id"])
        target_date = date.fromisoformat(fixture["checkin_date"])
        active_student_ids = [int(student_id) for student_id in fixture["active_student_ids"]]
        frozen_student_id = int(fixture["frozen_student_id"])
        expected_attendee_count = int(fixture["expected"]["group_session_attendee_count"])

        group_session = GroupSession.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            date=target_date,
        ).first()
        if group_session is None:
            raise CommandError("group session for trainer batch schedule/date not found")
        if group_session.attendee_count != expected_attendee_count:
            raise CommandError(
                "group session attendee_count mismatch: "
                f"expected {expected_attendee_count}, got {group_session.attendee_count}"
            )

        checkins = list(
            Checkin.objects.for_club(club_id)
            .filter(
                schedule_id=schedule_id,
                date=target_date,
                student_id__in=active_student_ids,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .select_related("subscription", "training_type")
            .order_by("student_id")
        )
        if len(checkins) != len(active_student_ids):
            raise CommandError(
                f"active batch check-in count mismatch: expected {len(active_student_ids)}, got {len(checkins)}"
            )
        checkin_student_ids = [checkin.student_id for checkin in checkins]
        if sorted(checkin_student_ids) != sorted(active_student_ids):
            raise CommandError(
                "active batch check-in students mismatch: "
                f"expected {sorted(active_student_ids)}, got {sorted(checkin_student_ids)}"
            )
        for checkin in checkins:
            if checkin.training_type_id != training_type_id:
                raise CommandError(
                    "batch check-in training type mismatch: "
                    f"expected {training_type_id}, got {checkin.training_type_id}"
                )
            if checkin.is_debt:
                raise CommandError(f"batch check-in {checkin.id} unexpectedly created debt")

        active_subscription_evidence = self._active_subscription_evidence(
            club_id=club_id,
            fixture=fixture,
            checkins=checkins,
        )
        cascade_events = {
            str(checkin.id): self._cascade_event_evidence(club_id=club_id, checkin=checkin)
            for checkin in checkins
        }
        frozen_evidence = self._frozen_student_evidence(
            club_id=club_id,
            frozen_student_id=frozen_student_id,
            schedule_id=schedule_id,
            target_date=target_date,
        )
        schedule_form_evidence = self._schedule_form_evidence(
            club_id=club_id,
            trainer_id=int(fixture["trainer_id"]),
            fixture=fixture,
        )
        unclosed_evidence = self._unclosed_evidence(club_id=club_id, fixture=fixture)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "group_session": {
                "id": group_session.id,
                "attendee_count": group_session.attendee_count,
            },
            "checkins": {
                "ids": [checkin.id for checkin in checkins],
                "student_ids": checkin_student_ids,
            },
            "subscriptions": active_subscription_evidence,
            "cascade_events": cascade_events,
            "frozen_student": frozen_evidence,
            "unclosed": unclosed_evidence,
            "schedule_form": schedule_form_evidence,
        }

    def _active_subscription_evidence(self, *, club_id: int, fixture: dict, checkins: list[Checkin]) -> list[dict]:
        checkin_by_student_id = {checkin.student_id: checkin for checkin in checkins}
        evidence = []
        for expected in fixture["active_subscriptions"]:
            student_id = int(expected["student_id"])
            subscription_id = int(expected["subscription_id"])
            subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
            checkin = checkin_by_student_id.get(student_id)
            if checkin is None:
                raise CommandError(f"check-in for active student {student_id} not found")
            if checkin.subscription_id != subscription_id:
                raise CommandError(
                    "active check-in subscription mismatch: "
                    f"expected {subscription_id}, got {checkin.subscription_id}"
                )
            expected_left_after = int(expected["trainings_left_after"])
            if subscription.trainings_left != expected_left_after:
                raise CommandError(
                    "active subscription trainings_left mismatch: "
                    f"expected {expected_left_after}, got {subscription.trainings_left}"
                )
            if subscription.trainings_used != 1:
                raise CommandError(
                    f"active subscription trainings_used mismatch: expected 1, got {subscription.trainings_used}"
                )
            evidence.append(
                {
                    "student_id": student_id,
                    "subscription_id": subscription.id,
                    "trainings_left": subscription.trainings_left,
                    "trainings_used": subscription.trainings_used,
                }
            )
        return evidence

    def _cascade_event_evidence(self, *, club_id: int, checkin: Checkin) -> dict:
        expected_effects = {
            CheckinCascadeEvent.Effect.SALARY: False,
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: True,
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: False,
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: False,
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK: False,
            CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: False,
        }
        expected_task_names = {
            CheckinCascadeEvent.Effect.SALARY: "apps.attendance.tasks.calculate_salary",
            CheckinCascadeEvent.Effect.GRADE_PROGRESS: "apps.attendance.tasks.update_grade_progress",
            CheckinCascadeEvent.Effect.PARENT_NOTIFICATION: "apps.attendance.tasks.log_parent_event",
            CheckinCascadeEvent.Effect.RETENTION_AUTO_CLOSE: "apps.retention.tasks.auto_close_retention_on_checkin",
            CheckinCascadeEvent.Effect.POST_TRIAL_TASK: "apps.retention.tasks.create_post_trial_task",
            CheckinCascadeEvent.Effect.TRAININGS_LEFT_PUSH: "apps.notifications.tasks.check_trainings_left_push",
        }
        events = {
            event.effect: event
            for event in CheckinCascadeEvent.objects.for_club(club_id).filter(checkin=checkin)
        }
        if CheckinCascadeEvent.Effect.GROUP_ANALYTICS in events:
            raise CommandError(f"batch check-in {checkin.id} unexpectedly has per-checkin group analytics event")
        missing = sorted(set(expected_effects) - set(events))
        unexpected = sorted(set(events) - set(expected_effects))
        if missing:
            raise CommandError(f"missing batch cascade events: {', '.join(missing)}")
        if unexpected:
            raise CommandError(f"unexpected batch cascade events: {', '.join(unexpected)}")

        if TrainerEarning.objects.for_club(club_id).filter(checkin=checkin, cancelled=False).exists():
            raise CommandError(f"group batch check-in {checkin.id} unexpectedly created trainer earning")

        student = Student.objects.for_club(club_id).get(id=checkin.student_id)
        if student.last_visit_date != checkin.date:
            raise CommandError(
                f"student last_visit_date mismatch: expected {checkin.date}, got {student.last_visit_date}"
            )

        evidence = {}
        for effect, expected in expected_effects.items():
            event = events[effect]
            if event.status != CheckinCascadeEvent.Status.QUEUED:
                raise CommandError(
                    f"batch cascade event {effect} status mismatch: "
                    f"expected {CheckinCascadeEvent.Status.QUEUED}, got {event.status}"
                )
            if event.expected is not expected:
                raise CommandError(
                    f"batch cascade event {effect} expected flag mismatch: expected {expected}, got {event.expected}"
                )
            expected_task_name = expected_task_names[effect]
            if event.task_name != expected_task_name:
                raise CommandError(
                    f"batch cascade event {effect} task_name mismatch: "
                    f"expected {expected_task_name}, got {event.task_name}"
                )
            payload = event.payload or {}
            if payload.get("checkin_id") != checkin.id:
                raise CommandError(
                    f"batch cascade event {effect} payload checkin_id mismatch: "
                    f"expected {checkin.id}, got {payload.get('checkin_id')}"
                )
            if payload.get("club_id") != club_id:
                raise CommandError(
                    f"batch cascade event {effect} payload club_id mismatch: "
                    f"expected {club_id}, got {payload.get('club_id')}"
                )
            evidence[effect] = {
                "id": event.id,
                "expected": event.expected,
                "status": event.status,
                "task_name": event.task_name,
            }
        return evidence

    def _frozen_student_evidence(
        self,
        *,
        club_id: int,
        frozen_student_id: int,
        schedule_id: int,
        target_date: date,
    ) -> dict:
        frozen_checkins = Checkin.objects.for_club(club_id).filter(
            student_id=frozen_student_id,
            schedule_id=schedule_id,
            date=target_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        frozen_checkin_ids = list(frozen_checkins.values_list("id", flat=True))
        debt_count = Debt.objects.for_club(club_id).filter(student_id=frozen_student_id).count()
        cascade_count = CheckinCascadeEvent.objects.for_club(club_id).filter(
            checkin_id__in=frozen_checkin_ids,
        ).count()
        if frozen_checkin_ids:
            raise CommandError(f"frozen student unexpectedly has check-ins: {frozen_checkin_ids}")
        if debt_count:
            raise CommandError(f"frozen student unexpectedly has debts: {debt_count}")
        if cascade_count:
            raise CommandError(f"frozen student unexpectedly has cascade events: {cascade_count}")
        return {
            "student_id": frozen_student_id,
            "checkin_count": 0,
            "debt_count": 0,
            "cascade_event_count": 0,
        }

    def _schedule_form_evidence(self, *, club_id: int, trainer_id: int, fixture: dict) -> dict:
        expected = fixture["schedule_form"]
        form_date = date.fromisoformat(expected["date"])
        schedule = (
            Schedule.objects.for_club(club_id)
            .filter(
                group_name=expected["edited_group_name"],
                one_time_date=form_date,
            )
            .select_related("location", "training_type", "trainer")
            .first()
        )
        if schedule is None:
            raise CommandError("schedule form edited schedule not found")

        original_exists = Schedule.objects.for_club(club_id).filter(
            group_name=expected["created_group_name"],
            one_time_date=form_date,
        ).exists()
        if original_exists:
            raise CommandError("schedule form original group name still exists after edit")

        expected_start = expected["edited_start_time"]
        expected_end = expected["edited_end_time"]
        if schedule.start_time.strftime("%H:%M") != expected_start:
            raise CommandError(
                "schedule form start_time mismatch: "
                f"expected {expected_start}, got {schedule.start_time:%H:%M}"
            )
        if schedule.end_time.strftime("%H:%M") != expected_end:
            raise CommandError(
                "schedule form end_time mismatch: "
                f"expected {expected_end}, got {schedule.end_time:%H:%M}"
            )
        if schedule.day_of_week != form_date.weekday():
            raise CommandError(
                "schedule form day_of_week mismatch: "
                f"expected {form_date.weekday()}, got {schedule.day_of_week}"
            )
        if schedule.trainer_id != trainer_id:
            raise CommandError(
                f"schedule form trainer mismatch: expected {trainer_id}, got {schedule.trainer_id}"
            )
        if schedule.location_id != int(expected["edited_location_id"]):
            raise CommandError(
                "schedule form location mismatch: "
                f"expected {expected['edited_location_id']}, got {schedule.location_id}"
            )
        if schedule.training_type_id != int(expected["edited_training_type_id"]):
            raise CommandError(
                "schedule form training type mismatch: "
                f"expected {expected['edited_training_type_id']}, got {schedule.training_type_id}"
            )

        return {
            "id": schedule.id,
            "group_name": schedule.group_name,
            "one_time_date": schedule.one_time_date.isoformat() if schedule.one_time_date else None,
            "start_time": schedule.start_time.strftime("%H:%M"),
            "end_time": schedule.end_time.strftime("%H:%M"),
            "trainer_id": schedule.trainer_id,
            "location_id": schedule.location_id,
            "training_type_id": schedule.training_type_id,
        }

    def _unclosed_evidence(self, *, club_id: int, fixture: dict) -> dict:
        expected = fixture["unclosed"]
        schedule_id = int(expected["schedule_id"])
        student_id = int(expected["student_id"])
        subscription_id = int(expected["subscription_id"])
        session_date = date.fromisoformat(expected["date"])

        schedule = Schedule.objects.for_club(club_id).select_related("club").get(id=schedule_id)
        group_session = GroupSession.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            date=session_date,
        ).first()
        if group_session is None:
            raise CommandError("unclosed schedule group session was not created")
        if group_session.attendee_count != 1:
            raise CommandError(
                "unclosed group session attendee_count mismatch: "
                f"expected 1, got {group_session.attendee_count}"
            )

        checkin = (
            Checkin.objects.for_club(club_id)
            .filter(
                schedule_id=schedule_id,
                student_id=student_id,
                date=session_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .select_related("subscription")
            .first()
        )
        if checkin is None:
            raise CommandError("unclosed schedule student check-in was not created")
        if checkin.subscription_id != subscription_id:
            raise CommandError(
                "unclosed schedule check-in subscription mismatch: "
                f"expected {subscription_id}, got {checkin.subscription_id}"
            )

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        expected_left_after = int(expected["trainings_left_after"])
        if subscription.trainings_left != expected_left_after:
            raise CommandError(
                "unclosed subscription trainings_left mismatch: "
                f"expected {expected_left_after}, got {subscription.trainings_left}"
            )

        remaining_unclosed_ids = [
            item.id for item in get_unclosed_sessions(club=schedule.club, session_date=session_date)
        ]
        if schedule_id in remaining_unclosed_ids:
            raise CommandError("unclosed schedule still appears after trainer confirmation")

        return {
            "schedule_id": schedule_id,
            "date": session_date.isoformat(),
            "student_id": student_id,
            "checkin_id": checkin.id,
            "group_session_id": group_session.id,
            "remaining_unclosed_ids": remaining_unclosed_ids,
        }
