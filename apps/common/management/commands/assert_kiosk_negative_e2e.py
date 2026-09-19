from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, Schedule, TrainingGroupMembership
from apps.attendance.selectors import lookup_by_phone_suffix
from apps.billing.models import Debt, Subscription
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert kiosk negative E2E duplicate/match/blocked side effects."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_kiosk_negative_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for kiosk negative side effects before failing.",
        )
        parser.add_argument(
            "--expect-shared-child-a-checkin",
            action="store_true",
            help="Require the optional second shared-guardian child check-in in its exact canonical group.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(
                    fixture,
                    expect_shared_child_a_checkin=bool(options["expect_shared_child_a_checkin"]),
                )
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"kiosk negative E2E assertion failed: {exc}") from exc
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
            "shared_guardian",
            "frozen_student_id",
            "blocked_student_id",
            "training_type_id",
            "checkin_date",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        shared_guardian = fixture["shared_guardian"]
        if not isinstance(shared_guardian, dict) or not isinstance(shared_guardian.get("phone_suffix"), str):
            raise CommandError("fixture shared guardian data is invalid")
        for child_key in ("child_a", "child_b"):
            child = shared_guardian.get(child_key)
            if not isinstance(child, dict):
                raise CommandError(f"fixture shared guardian {child_key} data is invalid")
            required_child_fields = {
                "student_id",
                "subscription_id",
                "schedule_id",
                "training_group_id",
                "group_name",
            }
            missing_child_fields = sorted(required_child_fields - set(child))
            if missing_child_fields:
                raise CommandError(
                    f"fixture shared guardian {child_key} is missing required fields: "
                    f"{', '.join(missing_child_fields)}"
                )
        return fixture

    def _collect_evidence(self, fixture: dict, *, expect_shared_child_a_checkin: bool) -> dict:
        club_id = int(fixture["club_id"])
        target_date = date.fromisoformat(fixture["checkin_date"])
        expected = fixture["expected"]
        shared_guardian = fixture["shared_guardian"]
        child_a = shared_guardian["child_a"]
        child_b = shared_guardian["child_b"]

        child_b_evidence = self._shared_guardian_child_evidence(
            club_id=club_id,
            target_date=target_date,
            child=child_b,
            sibling=child_a,
            expected_checkin_count=1,
            expected=expected,
        )
        child_a_evidence = self._shared_guardian_child_evidence(
            club_id=club_id,
            target_date=target_date,
            child=child_a,
            sibling=child_b,
            expected_checkin_count=1 if expect_shared_child_a_checkin else 0,
            expected=expected,
        )

        matches = lookup_by_phone_suffix(club_id=club_id, phone_suffix=shared_guardian["phone_suffix"])
        match_ids = sorted(item["id"] for item in matches)
        expected_match_ids = sorted([int(child_a["student_id"]), int(child_b["student_id"])])
        if match_ids != expected_match_ids:
            raise CommandError(f"shared suffix match ids mismatch: expected {expected_match_ids}, got {match_ids}")
        group_names_by_student_id = {item["id"]: item["group_name"] for item in matches}
        for child in (child_a, child_b):
            student_id = int(child["student_id"])
            if group_names_by_student_id.get(student_id) != child["group_name"]:
                raise CommandError("shared guardian lookup group label mismatch")

        frozen = self._blocked_student_evidence(
            club_id=club_id,
            student_id=int(fixture["frozen_student_id"]),
            target_date=target_date,
        )
        blocked = self._blocked_student_evidence(
            club_id=club_id,
            student_id=int(fixture["blocked_student_id"]),
            target_date=target_date,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "multiple_matches": {
                "shared_suffix_count": len(matches),
                "student_ids": match_ids,
            },
            "duplicate": child_b_evidence,
            "shared_guardian": {
                "child_a": child_a_evidence,
                "child_b": child_b_evidence,
            },
            "blocked_students": {
                "frozen": frozen,
                "blocked": blocked,
            },
        }

    def _shared_guardian_child_evidence(
        self,
        *,
        club_id: int,
        target_date: date,
        child: dict,
        sibling: dict,
        expected_checkin_count: int,
        expected: dict,
    ) -> dict:
        student_id = int(child["student_id"])
        subscription_id = int(child["subscription_id"])
        schedule_id = int(child["schedule_id"])
        sibling_schedule_id = int(sibling["schedule_id"])
        training_group_id = int(child["training_group_id"])
        membership = TrainingGroupMembership.objects.for_club(club_id).filter(
            student_id=student_id,
            training_group_id=training_group_id,
            status=TrainingGroupMembership.Status.ACTIVE,
        )
        if membership.count() != 1:
            raise CommandError("shared guardian child must have one active exact canonical membership")
        if not Schedule.objects.for_club(club_id).filter(
            id=schedule_id,
            training_group_id=training_group_id,
        ).exists():
            raise CommandError("shared guardian child schedule lost canonical group identity")

        checkins = list(
            Checkin.objects.for_club(club_id)
            .filter(
                student_id=student_id,
                schedule_id__in=[schedule_id, sibling_schedule_id],
                date=target_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .order_by("id")
        )
        if len(checkins) != expected_checkin_count:
            raise CommandError(
                "shared guardian child check-in count mismatch: "
                f"expected {expected_checkin_count}, got {len(checkins)}"
            )
        if any(checkin.schedule_id != schedule_id for checkin in checkins):
            raise CommandError("shared guardian child checked in to the sibling canonical group")
        if any(checkin.subscription_id != subscription_id or checkin.is_debt for checkin in checkins):
            raise CommandError("shared guardian child check-in lost exact subscription entitlement")

        subscription = Subscription.objects.for_club(club_id).get(id=subscription_id)
        expected_trainings_left = int(expected["shared_child_trainings_left_after"])
        if expected_checkin_count == 0:
            expected_trainings_left = int(expected["shared_child_trainings_left_before"])
        if subscription.trainings_left != expected_trainings_left:
            raise CommandError(
                "shared guardian child subscription trainings_left mismatch: "
                f"expected {expected_trainings_left}, got {subscription.trainings_left}"
            )
        if subscription.trainings_used != expected_checkin_count:
            raise CommandError(
                "shared guardian child subscription trainings_used mismatch: "
                f"expected {expected_checkin_count}, got {subscription.trainings_used}"
            )

        checkin_ids = [checkin.id for checkin in checkins]
        cascade_event_count = CheckinCascadeEvent.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        expected_cascade_event_count = int(expected["cascade_event_count"]) * expected_checkin_count
        if cascade_event_count != expected_cascade_event_count:
            raise CommandError(
                "shared guardian child cascade event count mismatch: "
                f"expected {expected_cascade_event_count}, got {cascade_event_count}"
            )
        earning_count = TrainerEarning.objects.for_club(club_id).filter(
            checkin_id__in=checkin_ids,
            cancelled=False,
        ).count()
        expected_earning_count = int(expected["earning_count"]) * expected_checkin_count
        if earning_count != expected_earning_count:
            raise CommandError(
                "shared guardian child earning count mismatch: "
                f"expected {expected_earning_count}, got {earning_count}"
            )
        grade_progress_count = GradeProgressEvent.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        if grade_progress_count != expected_checkin_count:
            raise CommandError(
                "shared guardian child grade progress count mismatch: "
                f"expected {expected_checkin_count}, got {grade_progress_count}"
            )
        student_grade = StudentGrade.objects.for_club(club_id).get(student_id=student_id)
        if student_grade.trainings_since_last_grade != expected_checkin_count:
            raise CommandError(
                "shared guardian child grade trainings_since_last_grade mismatch: "
                f"expected {expected_checkin_count}, got {student_grade.trainings_since_last_grade}"
            )
        debt_count = Debt.objects.for_club(club_id).filter(student_id=student_id).count()
        if debt_count:
            raise CommandError(f"shared guardian child unexpectedly has debts: {debt_count}")

        return {
            "student_id": student_id,
            "schedule_id": schedule_id,
            "training_group_id": training_group_id,
            "checkin_count": len(checkins),
            "checkin_id": checkin_ids[0] if checkin_ids else None,
            "cascade_event_count": cascade_event_count,
            "earning_count": earning_count,
            "grade_progress_count": grade_progress_count,
            "subscription": {
                "id": subscription.id,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
        }

    def _blocked_student_evidence(self, *, club_id: int, student_id: int, target_date: date) -> dict:
        checkins = Checkin.objects.for_club(club_id).filter(
            student_id=student_id,
            date=target_date,
            deleted_at__isnull=True,
        )
        checkin_ids = list(checkins.values_list("id", flat=True))
        debt_count = Debt.objects.for_club(club_id).filter(student_id=student_id).count()
        cascade_count = CheckinCascadeEvent.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        earning_count = TrainerEarning.objects.for_club(club_id).filter(checkin_id__in=checkin_ids).count()
        if checkin_ids:
            raise CommandError(f"blocked student unexpectedly has check-ins: {checkin_ids}")
        if debt_count:
            raise CommandError(f"blocked student unexpectedly has debts: {debt_count}")
        if cascade_count:
            raise CommandError(f"blocked student unexpectedly has cascade events: {cascade_count}")
        if earning_count:
            raise CommandError(f"blocked student unexpectedly has trainer earnings: {earning_count}")
        return {
            "student_id": student_id,
            "checkin_count": 0,
            "debt_count": 0,
            "cascade_event_count": 0,
            "earning_count": 0,
        }
