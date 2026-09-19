from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment, ScheduleException, TrainingGroup, TrainingGroupMembership
from apps.attendance.selectors import get_schedule_occurrences_for_date, get_student_schedule_occurrences_for_range
from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date
from apps.billing.models import Payment
from apps.clubs.models import Club
from apps.students.parent_selectors import get_child_profile, get_parent_children


class Command(BaseCommand):
    help = "Assert schedule exception visibility across trainer, student, and parent surfaces."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_schedule_exception_visibility_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for schedule exception visibility before failing.",
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
                    raise CommandError(f"schedule exception visibility E2E assertion failed: {exc}") from exc
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
            "trainer",
            "substitute_trainer",
            "student",
            "parent",
            "cancel_schedule_id",
            "reschedule_schedule_id",
            "substitute_schedule_id",
            "cancel_date",
            "reschedule_old_date",
            "reschedule_new_date",
            "substitute_date",
            "week_start",
            "training_group_substitute",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        cancel_date = date.fromisoformat(fixture["cancel_date"])
        reschedule_old_date = date.fromisoformat(fixture["reschedule_old_date"])
        reschedule_new_date = date.fromisoformat(fixture["reschedule_new_date"])
        substitute_date = date.fromisoformat(fixture["substitute_date"])
        week_start = date.fromisoformat(fixture["week_start"])
        week_end = week_start + timezone.timedelta(days=6)

        exceptions = self._exception_evidence(club=club, fixture=fixture)
        training_group_substitute = self._training_group_substitute_evidence(
            club=club,
            fixture=fixture,
            substitute_date=substitute_date,
        )
        trainer = self._trainer_evidence(
            club=club,
            fixture=fixture,
            cancel_date=cancel_date,
            reschedule_old_date=reschedule_old_date,
            reschedule_new_date=reschedule_new_date,
            substitute_date=substitute_date,
        )
        student = self._student_evidence(
            club=club,
            fixture=fixture,
            week_start=week_start,
            week_end=week_end,
        )
        parent = self._parent_evidence(club=club, fixture=fixture)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "exceptions": exceptions,
            "training_group_substitute": training_group_substitute,
            "trainer": trainer,
            "student": student,
            "parent": parent,
        }

    def _exception_evidence(self, *, club: Club, fixture: dict) -> dict:
        expected_by_schedule = {
            int(fixture["cancel_schedule_id"]): ScheduleException.ExceptionType.CANCELLED,
            int(fixture["reschedule_schedule_id"]): ScheduleException.ExceptionType.RESCHEDULED,
            int(fixture["substitute_schedule_id"]): ScheduleException.ExceptionType.SUBSTITUTE,
        }
        rows = list(
            ScheduleException.objects.for_club(club)
            .filter(schedule_id__in=expected_by_schedule)
            .select_related("substitute_trainer")
            .order_by("date", "id")
        )
        if len(rows) != 3:
            raise CommandError(f"schedule exceptions count mismatch: expected 3, got {len(rows)}")

        by_schedule = {item.schedule_id: item for item in rows}
        missing = sorted(set(expected_by_schedule) - set(by_schedule))
        if missing:
            raise CommandError(f"missing schedule exceptions for schedules: {missing}")

        for schedule_id, expected_type in expected_by_schedule.items():
            actual_type = by_schedule[schedule_id].exception_type
            if actual_type != expected_type:
                raise CommandError(
                    f"schedule {schedule_id} exception type mismatch: expected {expected_type}, got {actual_type}"
                )

        return {
            "ids": [item.id for item in rows],
            "types": [
                ScheduleException.ExceptionType.CANCELLED,
                ScheduleException.ExceptionType.RESCHEDULED,
                ScheduleException.ExceptionType.SUBSTITUTE,
            ],
        }

    def _training_group_substitute_evidence(
        self,
        *,
        club: Club,
        fixture: dict,
        substitute_date: date,
    ) -> dict:
        expected = fixture["training_group_substitute"]
        if not settings.TRAINING_GROUP_NEW_WRITES_ENABLED:
            raise CommandError("substitute fixture requires TRAINING_GROUP_NEW_WRITES_ENABLED")
        if not settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED:
            raise CommandError("substitute fixture requires MANUAL_OPERATIONAL_ADMISSION_ENABLED")

        group = (
            TrainingGroup.objects.for_club(club)
            .select_related("responsible_trainer")
            .filter(id=int(expected["training_group_id"]))
            .first()
        )
        if group is None or group.responsible_trainer_id != int(expected["replacement_responsible_trainer_id"]):
            raise CommandError("substitute did not retain the reassigned canonical group responsibility")
        membership = (
            TrainingGroupMembership.objects.for_club(club)
            .filter(id=int(expected["membership_id"]), training_group=group)
            .first()
        )
        if (
            membership is None
            or membership.student_id != int(expected["student_id"])
            or membership.status != TrainingGroupMembership.Status.ACTIVE
            or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
            or membership.source != TrainingGroupMembership.Source.PAID_CONVERSION
        ):
            raise CommandError("substitute did not preserve the payment-owned membership authority")

        schedule_id = int(expected["schedule_id"])
        projection_ids = list(
            ScheduleEnrollment.objects.for_club(club)
            .filter(training_group_membership=membership)
            .order_by("schedule_id", "id")
            .values_list("schedule_id", flat=True)
        )
        if projection_ids != [schedule_id]:
            raise CommandError("substitute did not preserve the canonical membership projection")
        roster = resolve_expected_roster_by_schedule_date(
            club=club,
            schedule_ids=[schedule_id],
            target_date=substitute_date,
        )
        entry = roster.get(schedule_id, {}).get(membership.student_id)
        if (
            entry is None
            or entry.source != "training_group_membership"
            or entry.membership_id != membership.id
            or entry.enrollment_status != TrainingGroupMembership.Status.ACTIVE
        ):
            raise CommandError("substitute schedule no longer resolves through its original membership authority")

        payment = (
            Payment.objects.for_club(club)
            .filter(id=int(expected["payment_id"]), conversion_group_membership=membership)
            .first()
        )
        if payment is None:
            raise CommandError("substitute payment is no longer linked to its original membership")
        if (
            payment.status != Payment.Status.CONFIRMED
            or payment.seller_trainer_id != int(expected["seller_trainer_id"])
            or payment.sale_trainer_id_snapshot != int(expected["sale_trainer_id_snapshot"])
            or payment.sale_attribution_source != expected["sale_attribution_source"]
            or payment.target_training_group_id != group.id
            or payment.target_group_membership_id != membership.id
        ):
            raise CommandError("substitute mutated immutable canonical sale attribution")

        return {
            "rollout_mode": expected["rollout_mode"],
            "new_writes_enabled": settings.TRAINING_GROUP_NEW_WRITES_ENABLED,
            "manual_operational_admission_enabled": settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED,
            "training_group_id": group.id,
            "responsible_trainer_id": group.responsible_trainer_id,
            "membership_id": membership.id,
            "membership_authority": membership.authority,
            "roster_authority": entry.source,
            "payment_id": payment.id,
            "seller_trainer_id": payment.seller_trainer_id,
            "sale_trainer_id_snapshot": payment.sale_trainer_id_snapshot,
            "sale_attribution_source": payment.sale_attribution_source,
        }

    def _trainer_evidence(
        self,
        *,
        club: Club,
        fixture: dict,
        cancel_date: date,
        reschedule_old_date: date,
        reschedule_new_date: date,
        substitute_date: date,
    ) -> dict:
        trainer_id = int(fixture["trainer"]["trainer_id"])
        substitute_trainer_id = int(fixture["substitute_trainer"]["trainer_id"])
        cancel_schedule_id = int(fixture["cancel_schedule_id"])
        reschedule_schedule_id = int(fixture["reschedule_schedule_id"])
        substitute_schedule_id = int(fixture["substitute_schedule_id"])

        cancel_day = get_schedule_occurrences_for_date(club=club, target_date=cancel_date, trainer_id=trainer_id)
        reschedule_old_day = get_schedule_occurrences_for_date(
            club=club,
            target_date=reschedule_old_date,
            trainer_id=trainer_id,
        )
        reschedule_new_day = get_schedule_occurrences_for_date(
            club=club,
            target_date=reschedule_new_date,
            trainer_id=trainer_id,
        )
        owner_substitute_day = get_schedule_occurrences_for_date(
            club=club,
            target_date=substitute_date,
            trainer_id=trainer_id,
        )
        substitute_day = get_schedule_occurrences_for_date(
            club=club,
            target_date=substitute_date,
            trainer_id=substitute_trainer_id,
        )

        cancelled_hidden = all(item.schedule_id != cancel_schedule_id for item in cancel_day)
        old_reschedule_hidden = all(item.schedule_id != reschedule_schedule_id for item in reschedule_old_day)
        rescheduled = next(
            (item for item in reschedule_new_day if item.schedule_id == reschedule_schedule_id),
            None,
        )
        substitute = next(
            (item for item in substitute_day if item.schedule_id == substitute_schedule_id),
            None,
        )
        owner_substitute_hidden = all(item.schedule_id != substitute_schedule_id for item in owner_substitute_day)
        if not cancelled_hidden:
            raise CommandError("cancelled trainer occurrence is still visible")
        if not old_reschedule_hidden:
            raise CommandError("original rescheduled trainer occurrence is still visible")
        if not rescheduled or not rescheduled.is_rescheduled:
            raise CommandError("rescheduled trainer occurrence not visible")
        if not owner_substitute_hidden:
            raise CommandError("original trainer still sees substituted occurrence")
        if not substitute or not substitute.is_substitute:
            raise CommandError("substitute trainer occurrence not visible")

        return {
            "cancelled_hidden": cancelled_hidden,
            "old_reschedule_hidden": old_reschedule_hidden,
            "rescheduled_visible": True,
            "substitute_hidden_from_original": owner_substitute_hidden,
            "substitute_visible_to_substitute": True,
            "rescheduled": {
                "schedule_id": rescheduled.schedule_id,
                "effective_date": rescheduled.effective_date.isoformat(),
                "effective_start_time": rescheduled.effective_start_time.strftime("%H:%M"),
                "effective_end_time": rescheduled.effective_end_time.strftime("%H:%M"),
            },
            "substitute": {
                "schedule_id": substitute.schedule_id,
                "trainer_id": substitute.trainer_id,
                "trainer_name": substitute.trainer_name,
            },
        }

    def _student_evidence(self, *, club: Club, fixture: dict, week_start: date, week_end: date) -> dict:
        student_id = int(fixture["student"]["student_id"])
        cancel_schedule_id = int(fixture["cancel_schedule_id"])
        reschedule_schedule_id = int(fixture["reschedule_schedule_id"])
        substitute_schedule_id = int(fixture["substitute_schedule_id"])
        occurrences = get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student_id,
            date_from=week_start,
            date_to=week_end,
        )
        cancelled_hidden = all(item.schedule_id != cancel_schedule_id for item in occurrences)
        rescheduled = next(
            (item for item in occurrences if item.schedule_id == reschedule_schedule_id and item.is_rescheduled),
            None,
        )
        substitute = next(
            (item for item in occurrences if item.schedule_id == substitute_schedule_id and item.is_substitute),
            None,
        )
        if not cancelled_hidden:
            raise CommandError("cancelled student occurrence is still visible")
        if rescheduled is None:
            raise CommandError("student rescheduled occurrence not visible")
        if substitute is None:
            raise CommandError("student substitute occurrence not visible")

        payload_safety = self._student_payload_safety(occurrences=occurrences, fixture=fixture)

        return {
            "cancelled_hidden": cancelled_hidden,
            "rescheduled_visible": True,
            "substitute_visible": True,
            "occurrence_count": len(occurrences),
            "payload_safety": payload_safety,
            "rescheduled": {
                "effective_date": rescheduled.effective_date.isoformat(),
                "effective_start_time": rescheduled.effective_start_time.strftime("%H:%M"),
                "effective_end_time": rescheduled.effective_end_time.strftime("%H:%M"),
            },
            "substitute": {
                "trainer_id": substitute.trainer_id,
                "trainer_name": substitute.trainer_name,
            },
        }

    def _student_payload_safety(self, *, occurrences: list, fixture: dict) -> dict:
        forbidden_fields = {
            "reason",
            "exception_type",
            "substitute_trainer_id",
            "trainer_phone",
            "trainer_email",
            "student_phone",
            "student_email",
            "notes",
            "contraindications",
        }
        reason_values = [
            value
            for key, value in fixture.get("expected", {}).items()
            if key.endswith("_reason") and isinstance(value, str) and value
        ]
        field_names: set[str] = set()

        for occurrence in occurrences:
            payload = asdict(occurrence)
            field_names.update(payload)
            exposed_fields = forbidden_fields & set(payload)
            if exposed_fields:
                raise CommandError(
                    "student schedule occurrence exposes staff-only fields: "
                    f"{', '.join(sorted(exposed_fields))}"
                )
            payload_text = json.dumps(payload, ensure_ascii=False, default=str)
            leaked_reasons = sorted(reason for reason in reason_values if reason in payload_text)
            if leaked_reasons:
                raise CommandError("student schedule occurrence exposes exception reason text")

        return {
            "staff_only_fields_absent": True,
            "reason_values_hidden": True,
            "field_names": sorted(field_names),
        }

    def _parent_evidence(self, *, club: Club, fixture: dict) -> dict:
        parent_user_id = int(fixture["parent"]["user_id"])
        student_id = int(fixture["student"]["student_id"])
        children = get_parent_children(user_id=parent_user_id, club=club)
        child_ids = [child["id"] for child in children]
        if child_ids != [student_id]:
            raise CommandError(f"parent children mismatch: expected {[student_id]}, got {child_ids}")

        profile = get_child_profile(user_id=parent_user_id, club=club, student_id=student_id)
        exception_types = {
            exception["exception_type"]
            for schedule in profile["schedule"]
            for exception in schedule.get("upcoming_exceptions", [])
        }
        occurrence_flags = {
            "rescheduled": any(
                occurrence.get("is_rescheduled")
                for schedule in profile["schedule"]
                for occurrence in schedule.get("upcoming_occurrences", [])
            ),
            "substitute": any(
                occurrence.get("is_substitute")
                for schedule in profile["schedule"]
                for occurrence in schedule.get("upcoming_occurrences", [])
            ),
        }
        visible = {
            "cancelled": ScheduleException.ExceptionType.CANCELLED in exception_types,
            "rescheduled": ScheduleException.ExceptionType.RESCHEDULED in exception_types
            and occurrence_flags["rescheduled"],
            "substitute": ScheduleException.ExceptionType.SUBSTITUTE in exception_types
            and occurrence_flags["substitute"],
        }
        if visible != {"cancelled": True, "rescheduled": True, "substitute": True}:
            raise CommandError(f"parent schedule exceptions mismatch: got {visible}")

        return {
            "child_ids": child_ids,
            "exceptions_visible": visible,
            "schedule_count": len(profile["schedule"]),
        }
