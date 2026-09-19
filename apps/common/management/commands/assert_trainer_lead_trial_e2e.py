from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession, ScheduleEnrollment
from apps.feedback.models import FeedbackForm
from apps.leads.selectors import get_leads
from apps.pipelines.models import Pipeline, PipelineExecution
from apps.retention.models import RetentionTask
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert trainer lead/trial E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_lead_trial_e2e.",
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
                    raise CommandError(f"trainer lead/trial E2E assertion failed: {exc}") from exc
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
            "hidden_lead_id",
            "schedule_id",
            "training_type_id",
            "new_lead",
            "trial",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        schedule_id = int(fixture["schedule_id"])
        training_type_id = int(fixture["training_type_id"])
        checkin_date = date.fromisoformat(fixture["trial"]["checkin_date"])

        lead = self._get_target_lead(club_id=club_id, fixture=fixture)
        visible_leads = list(get_leads(club=lead.club, assigned_trainer_id=trainer_id).order_by("id"))
        visible_lead_ids = [item.id for item in visible_leads]
        if visible_lead_ids != [lead.id]:
            raise CommandError(
                "trainer visible lead scope mismatch: "
                f"expected {[lead.id]}, got {visible_lead_ids}"
            )
        if int(fixture["hidden_lead_id"]) in visible_lead_ids:
            raise CommandError("hidden other-trainer lead leaked into trainer lead scope")

        enrollment = self._get_trial_enrollment(
            club_id=club_id,
            lead=lead,
            schedule_id=schedule_id,
            checkin_date=checkin_date,
        )
        checkin = self._get_trial_checkin(
            club_id=club_id,
            lead=lead,
            schedule_id=schedule_id,
            training_type_id=training_type_id,
            checkin_date=checkin_date,
        )
        post_trial_event = self._get_post_trial_event(
            club_id=club_id,
            checkin=checkin,
            lead=lead,
            trainer_id=trainer_id,
        )
        group_session = GroupSession.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            date=checkin_date,
        ).first()
        if group_session is None:
            raise CommandError("trial batch group session not found")
        if group_session.attendee_count != 1:
            raise CommandError(
                "trial group session attendee_count mismatch: "
                f"expected 1, got {group_session.attendee_count}"
            )

        feedback_form = FeedbackForm.objects.for_club(club_id).filter(
            trigger_type=FeedbackForm.TriggerType.TRIAL,
            is_active=True,
        ).first()
        if feedback_form is None:
            raise CommandError("active trial feedback form not found")

        pipeline_execution = PipelineExecution.objects.for_club(club_id).filter(
            student=lead,
            pipeline__pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
            completed_at__isnull=True,
            cancelled_at__isnull=True,
        ).first()
        if pipeline_execution is None:
            raise CommandError("follow-up pipeline execution not found")

        retention_task = RetentionTask.objects.for_club(club_id).filter(
            student=lead,
            trainer_id=trainer_id,
            task_type=RetentionTask.TaskType.POST_TRIAL,
            resolved_at__isnull=True,
        ).first()
        if retention_task is None:
            raise CommandError("post-trial retention task not found")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "lead": {
                "id": lead.id,
                "status": lead.status,
                "lead_status": lead.lead_status,
                "assigned_trainer_id": lead.assigned_trainer_id,
                "trial_date": lead.trial_date.isoformat() if lead.trial_date else None,
            },
            "scope": {
                "trainer_visible_lead_ids": visible_lead_ids,
                "hidden_lead_id": int(fixture["hidden_lead_id"]),
            },
            "enrollment": {
                "id": enrollment.id,
                "status": enrollment.status,
                "created_from": enrollment.created_from,
                "starts_on": enrollment.starts_on.isoformat() if enrollment.starts_on else None,
                "ends_on": enrollment.ends_on.isoformat() if enrollment.ends_on else None,
                "trial_at": enrollment.trial_at.isoformat() if enrollment.trial_at else None,
            },
            "checkin": {
                "id": checkin.id,
                "is_debt": checkin.is_debt,
                "subscription_id": checkin.subscription_id,
                "post_trial_task_queued": post_trial_event.expected,
            },
            "cascade_event": {
                "id": post_trial_event.id,
                "effect": post_trial_event.effect,
                "status": post_trial_event.status,
                "expected": post_trial_event.expected,
                "task_name": post_trial_event.task_name,
            },
            "group_session": {
                "id": group_session.id,
                "attendee_count": group_session.attendee_count,
            },
            "feedback": {
                "active_form_id": feedback_form.id,
            },
            "pipeline": {
                "execution_count": 1,
                "execution_id": pipeline_execution.id,
                "pipeline_type": pipeline_execution.pipeline.pipeline_type,
            },
            "retention_task": {
                "id": retention_task.id,
                "task_type": retention_task.task_type,
                "trainer_id": retention_task.trainer_id,
            },
        }

    def _get_target_lead(self, *, club_id: int, fixture: dict) -> Student:
        lead = Student.objects.for_club(club_id).filter(
            phone=fixture["new_lead"]["phone"],
            deleted_at__isnull=True,
        ).first()
        if lead is None:
            raise CommandError("target lead not found")
        if lead.first_name != fixture["new_lead"]["first_name"]:
            raise CommandError(
                f"target lead first_name mismatch: expected {fixture['new_lead']['first_name']}, got {lead.first_name}"
            )
        if lead.last_name != fixture["new_lead"]["last_name"]:
            raise CommandError(
                f"target lead last_name mismatch: expected {fixture['new_lead']['last_name']}, got {lead.last_name}"
            )
        if lead.assigned_trainer_id != int(fixture["trainer"]["trainer_id"]):
            raise CommandError(
                "target lead assigned_trainer mismatch: "
                f"expected {fixture['trainer']['trainer_id']}, got {lead.assigned_trainer_id}"
            )
        if lead.status != Student.Status.TRIAL:
            raise CommandError(f"target lead student status mismatch: expected trial, got {lead.status}")
        if lead.lead_status != Student.LeadStatus.TRIAL_DONE:
            raise CommandError(f"target lead lead_status mismatch: expected trial_done, got {lead.lead_status}")
        return lead

    def _get_trial_enrollment(
        self,
        *,
        club_id: int,
        lead: Student,
        schedule_id: int,
        checkin_date: date,
    ) -> ScheduleEnrollment:
        enrollment = ScheduleEnrollment.objects.for_club(club_id).filter(
            student=lead,
            schedule_id=schedule_id,
        ).first()
        if enrollment is None:
            raise CommandError("trial enrollment not found")
        if enrollment.status != ScheduleEnrollment.Status.TRIAL:
            raise CommandError(f"trial enrollment status mismatch: expected trial, got {enrollment.status}")
        if enrollment.created_from != ScheduleEnrollment.CreatedFrom.LEAD_BOOKING:
            raise CommandError(
                "trial enrollment created_from mismatch: "
                f"expected lead_booking, got {enrollment.created_from}"
            )
        if enrollment.starts_on != checkin_date:
            raise CommandError(
                f"trial enrollment starts_on mismatch: expected {checkin_date}, got {enrollment.starts_on}"
            )
        if enrollment.ends_on != checkin_date:
            raise CommandError(
                f"trial enrollment ends_on mismatch: expected {checkin_date}, got {enrollment.ends_on}"
            )
        if enrollment.trial_at is None:
            raise CommandError("trial enrollment trial_at not set")
        return enrollment

    def _get_trial_checkin(
        self,
        *,
        club_id: int,
        lead: Student,
        schedule_id: int,
        training_type_id: int,
        checkin_date: date,
    ) -> Checkin:
        checkin = Checkin.objects.for_club(club_id).filter(
            student=lead,
            schedule_id=schedule_id,
            date=checkin_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).first()
        if checkin is None:
            raise CommandError("trial check-in not found")
        if checkin.training_type_id != training_type_id:
            raise CommandError(
                f"trial check-in training_type mismatch: expected {training_type_id}, got {checkin.training_type_id}"
            )
        if checkin.is_debt:
            raise CommandError("free trial check-in unexpectedly created debt")
        if checkin.subscription_id is not None:
            raise CommandError("free trial check-in unexpectedly used a subscription")
        return checkin

    def _get_post_trial_event(
        self,
        *,
        club_id: int,
        checkin: Checkin,
        lead: Student,
        trainer_id: int,
    ) -> CheckinCascadeEvent:
        event = CheckinCascadeEvent.objects.for_club(club_id).filter(
            checkin=checkin,
            effect=CheckinCascadeEvent.Effect.POST_TRIAL_TASK,
        ).first()
        if event is None:
            raise CommandError("post-trial cascade event not found")
        if event.status != CheckinCascadeEvent.Status.QUEUED:
            raise CommandError(
                f"post-trial cascade event status mismatch: expected queued, got {event.status}"
            )
        if event.expected is not True:
            raise CommandError("post-trial cascade event expected flag is not true")
        if event.task_name != "apps.retention.tasks.create_post_trial_task":
            raise CommandError(
                "post-trial cascade event task_name mismatch: "
                f"expected apps.retention.tasks.create_post_trial_task, got {event.task_name}"
            )
        payload = event.payload or {}
        expected_payload = {
            "checkin_id": checkin.id,
            "student_id": lead.id,
            "club_id": club_id,
            "trainer_id": trainer_id,
        }
        for key, expected in expected_payload.items():
            if payload.get(key) != expected:
                raise CommandError(
                    f"post-trial cascade payload {key} mismatch: expected {expected}, got {payload.get(key)}"
                )
        return event
