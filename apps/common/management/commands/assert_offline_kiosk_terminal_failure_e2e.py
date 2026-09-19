from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent
from apps.billing.models import Debt, Subscription
from apps.grades.models import GradeProgressEvent
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert offline kiosk terminal failure did not create check-in side effects."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON.")
        parser.add_argument(
            "--target",
            choices=["primary", "terminal"],
            default="primary",
            help="Fixture identity whose terminal failure must have no side effects.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        evidence = self._collect_evidence(fixture, target_name=options["target"])
        self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))

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
            "subscription_id",
            "checkin_date",
            "expected",
            "terminal",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        terminal_required = {
            "student_id",
            "schedule_id",
            "subscription_id",
            "checkin_date",
            "expected",
        }
        terminal = fixture["terminal"]
        terminal_missing = sorted(terminal_required - set(terminal))
        if terminal_missing:
            raise CommandError(
                f"fixture terminal is missing required fields: {', '.join(terminal_missing)}"
            )
        return fixture

    def _collect_evidence(self, fixture: dict, *, target_name: str) -> dict:
        club_id = int(fixture["club_id"])
        target = fixture if target_name == "primary" else fixture["terminal"]
        student_id = int(target["student_id"])
        schedule_id = int(target["schedule_id"])
        subscription_id = int(target["subscription_id"])
        target_date = date.fromisoformat(target["checkin_date"])

        checkins = Checkin.objects.for_club(club_id).filter(
            student_id=student_id,
            schedule_id=schedule_id,
            date=target_date,
        )
        checkin_ids = list(checkins.values_list("id", flat=True))
        debts_count = Debt.objects.for_club(club_id).filter(student_id=student_id, checkin__date=target_date).count()
        cascade_count = CheckinCascadeEvent.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        earning_count = TrainerEarning.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        progress_count = GradeProgressEvent.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        expected = target["expected"]
        if checkin_ids:
            raise CommandError(f"terminal failure unexpectedly created check-ins: {checkin_ids}")
        if debts_count:
            raise CommandError(f"terminal failure unexpectedly created debts: count={debts_count}")
        if cascade_count:
            raise CommandError(f"terminal failure unexpectedly created cascade events: count={cascade_count}")
        if earning_count:
            raise CommandError(f"terminal failure unexpectedly created earnings: count={earning_count}")
        if progress_count:
            raise CommandError(f"terminal failure unexpectedly created grade progress events: count={progress_count}")
        if subscription.trainings_left != int(expected["trainings_left_before"]):
            raise CommandError(
                "terminal failure changed trainings_left: "
                f"expected {expected['trainings_left_before']}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != 0:
            raise CommandError(f"terminal failure changed trainings_used: got {subscription.trainings_used}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "target": target_name,
            "checked_at": timezone.now().isoformat(),
            "terminal_failure": {
                "checkin_count": 0,
                "debt_count": debts_count,
                "cascade_count": cascade_count,
                "earning_count": earning_count,
                "grade_progress_event_count": progress_count,
            },
            "subscription": {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
        }
