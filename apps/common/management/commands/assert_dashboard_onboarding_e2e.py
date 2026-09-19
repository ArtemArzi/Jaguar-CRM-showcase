from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Schedule
from apps.billing.models import Tariff
from apps.clubs.models import Club, ClubMembership
from apps.onboarding.models import OnboardingDraft
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Assert dashboard onboarding wizard E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_dashboard_onboarding_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for onboarding state before failing.",
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
                    raise CommandError(f"dashboard onboarding E2E assertion failed: {exc}") from exc
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
            "location_id",
            "control_club_id",
            "control_draft_id",
            "owner",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        control_club = Club.objects.get(id=int(fixture["control_club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        expected = fixture["expected"]

        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        drafts = OnboardingDraft.objects.for_club(club)
        completed_drafts = drafts.filter(is_completed=True)
        active_drafts = drafts.filter(is_completed=False)
        if completed_drafts.count() != int(expected["completed_draft_count"]):
            raise CommandError("completed onboarding draft count mismatch")
        if active_drafts.count() != int(expected["active_draft_count"]):
            raise CommandError("active onboarding draft count mismatch")

        completed_draft = completed_drafts.get()
        if completed_draft.current_step != int(expected["completed_current_step"]):
            raise CommandError("completed onboarding draft step mismatch")
        trainer_rows = completed_draft.data.get("2", {}).get("trainers", [])
        schedule_rows = completed_draft.data.get("3", {}).get("schedules", [])
        if len(trainer_rows) != int(expected["trainer_count"]):
            raise CommandError("onboarding draft trainer count mismatch")
        if len(schedule_rows) != int(expected["schedule_count"]):
            raise CommandError("onboarding draft schedule count mismatch")
        if schedule_rows[0].get("trainer_ref") != trainer_rows[0].get("client_ref"):
            raise CommandError("onboarding draft schedule trainer reference mismatch")
        if int(schedule_rows[0].get("location_id")) != int(fixture["location_id"]):
            raise CommandError("onboarding draft schedule location mismatch")

        control_draft = OnboardingDraft.objects.for_club(control_club).get(id=int(fixture["control_draft_id"]))
        if control_draft.is_completed:
            raise CommandError("control onboarding draft was completed")
        if control_draft.current_step != int(expected["control_current_step"]):
            raise CommandError("control onboarding draft step changed")

        if Student.objects.for_club(club).exists():
            raise CommandError("onboarding unexpectedly created students")
        if Tariff.objects.for_club(club).exists():
            raise CommandError("onboarding unexpectedly created tariffs")

        trainers = Trainer.objects.for_club(club)
        schedules = Schedule.objects.for_club(club)
        if trainers.count() != int(expected["trainer_count"]):
            raise CommandError("created onboarding trainer count mismatch")
        if schedules.count() != int(expected["schedule_count"]):
            raise CommandError("created onboarding schedule count mismatch")
        trainer = trainers.get()
        schedule = schedules.get()
        if schedule.trainer_id != trainer.id:
            raise CommandError("created onboarding schedule trainer mismatch")
        if schedule.location_id != int(fixture["location_id"]):
            raise CommandError("created onboarding schedule location mismatch")
        if schedule.group_name != expected["schedule_group"]:
            raise CommandError("created onboarding schedule group mismatch")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "owner": {
                "user_id": owner_user_id,
                "membership_role": membership.role,
            },
            "onboarding": {
                "completed_draft_count": completed_drafts.count(),
                "active_draft_count": active_drafts.count(),
                "completed_current_step": completed_draft.current_step,
                "completed_data": completed_draft.data,
            },
            "control": {
                "draft_id": control_draft.id,
                "is_completed": control_draft.is_completed,
                "current_step": control_draft.current_step,
            },
            "created_objects": {
                "students": Student.objects.for_club(club).count(),
                "trainers": trainers.count(),
                "tariffs": Tariff.objects.for_club(club).count(),
                "schedules": schedules.count(),
            },
            "assignment": {
                "trainer_id": trainer.id,
                "schedule_id": schedule.id,
                "schedule_trainer_id": schedule.trainer_id,
                "schedule_location_id": schedule.location_id,
            },
        }
