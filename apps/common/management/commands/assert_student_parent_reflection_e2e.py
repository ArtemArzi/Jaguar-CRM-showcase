from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.attendance.selectors import (
    get_student_attendance,
    get_student_schedule,
    get_student_schedule_occurrences_for_range,
)
from apps.billing.selectors import get_student_open_debts, get_student_subscriptions
from apps.clubs.models import Club
from apps.grades.models import GradeProgressEvent
from apps.grades.selectors import get_student_all_grades
from apps.students.parent_selectors import get_child_attendance, get_child_profile, get_parent_children
from apps.students.selectors import get_student_by_user


class Command(BaseCommand):
    help = "Assert student and parent surfaces reflect a prepared trainer check-in."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_student_parent_reflection_e2e.",
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
                    raise CommandError(f"student/parent reflection E2E assertion failed: {exc}") from exc
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
            "student",
            "parent",
            "foreign_child_id",
            "schedule_id",
            "upcoming_schedule_id",
            "debt_checkin_id",
            "debt_id",
            "training_type_id",
            "subscription_id",
            "checkin_date",
            "upcoming_date",
            "attention_child",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _money(self, value) -> str | None:
        if value is None:
            return None
        return f"{value:.2f}"

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        student_id = int(fixture["student"]["student_id"])
        parent_user_id = int(fixture["parent"]["user_id"])
        student_user_id = int(fixture["student"]["user_id"])
        schedule_id = int(fixture["schedule_id"])
        target_date = date.fromisoformat(fixture["checkin_date"])
        training_type_id = int(fixture["training_type_id"])

        checkin = (
            Checkin.objects.for_club(club)
            .filter(
                student_id=student_id,
                schedule_id=schedule_id,
                training_type_id=training_type_id,
                date=target_date,
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .select_related("subscription")
            .first()
        )
        if checkin is None:
            raise CommandError("reflection check-in not found")
        if checkin.is_debt:
            raise CommandError("reflection check-in unexpectedly created debt")

        student_api = self._student_surface(
            club=club,
            student_id=student_id,
            student_user_id=student_user_id,
            fixture=fixture,
        )
        parent_api = self._parent_surface(
            club=club,
            parent_user_id=parent_user_id,
            student_id=student_id,
            fixture=fixture,
        )
        privacy = self._privacy_evidence(
            student_api=student_api,
            parent_api=parent_api,
            fixture=fixture,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "checkin": {
                "id": checkin.id,
                "subscription_id": checkin.subscription_id,
                "is_debt": checkin.is_debt,
            },
            "student_api": student_api,
            "parent_api": parent_api,
            "privacy": privacy,
        }

    def _student_surface(self, *, club: Club, student_id: int, student_user_id: int, fixture: dict) -> dict:
        student = get_student_by_user(club=club, user_id=student_user_id)
        if student.id != student_id:
            raise CommandError(f"student self endpoint resolved wrong student: expected {student_id}, got {student.id}")

        schedule_id = int(fixture["schedule_id"])
        target_date = date.fromisoformat(fixture["checkin_date"])
        subscriptions = list(get_student_subscriptions(club=club, student_id=student_id))
        target_subscription = next(
            (subscription for subscription in subscriptions if subscription.id == int(fixture["subscription_id"])),
            None,
        )
        if target_subscription is None:
            raise CommandError("student subscription not visible")

        expected = fixture["expected"]
        if target_subscription.trainings_left != int(expected["trainings_left_after"]):
            raise CommandError(
                "student subscription trainings_left mismatch: "
                f"expected {expected['trainings_left_after']}, got {target_subscription.trainings_left}"
            )
        if target_subscription.trainings_used != int(expected["trainings_used_after"]):
            raise CommandError(
                "student subscription trainings_used mismatch: "
                f"expected {expected['trainings_used_after']}, got {target_subscription.trainings_used}"
            )
        if target_subscription.tariff.trainings_limit != int(expected["trainings_total"]):
            raise CommandError(
                "student subscription trainings_total mismatch: "
                f"expected {expected['trainings_total']}, got {target_subscription.tariff.trainings_limit}"
            )

        attendance = list(get_student_attendance(club=club, student_id=student_id))
        if len(attendance) != int(expected["attendance_count_after"]):
            raise CommandError(
                "student attendance count mismatch: "
                f"expected {expected['attendance_count_after']}, got {len(attendance)}"
            )
        schedules = list(get_student_schedule(club=club, student_id=student_id))
        if not schedules:
            raise CommandError("student schedule not visible")
        debts = list(get_student_open_debts(club=club, student_id=student_id))
        if len(debts) != int(expected["open_debt_count"]):
            raise CommandError(
                "student open debt count mismatch: "
                f"expected {expected['open_debt_count']}, got {len(debts)}"
            )
        target_debt = next(
            (debt for debt in debts if debt.id == int(fixture["debt_id"])),
            None,
        )
        if target_debt is None:
            raise CommandError("student open debt not visible")
        if target_debt.checkin_id != int(fixture["debt_checkin_id"]):
            raise CommandError(
                "student open debt checkin mismatch: "
                f"expected {fixture['debt_checkin_id']}, got {target_debt.checkin_id}"
            )
        if target_debt.reason != "no_subscription":
            raise CommandError(f"student open debt reason mismatch: got {target_debt.reason}")
        if target_debt.checkin.training_type.name != expected["debt_training_type_name"]:
            raise CommandError(
                "student open debt training type mismatch: "
                f"expected {expected['debt_training_type_name']}, got {target_debt.checkin.training_type.name}"
            )
        if self._money(target_debt.tariff_price) != expected["debt_amount"]:
            raise CommandError(
                "student open debt amount mismatch: "
                f"expected {expected['debt_amount']}, got {self._money(target_debt.tariff_price)}"
            )

        grades = get_student_all_grades(club_id=club.id, student_id=student_id)
        target_grade = next(
            (grade for grade in grades if grade["student_grade_id"] == int(fixture["student_grade_id"])),
            None,
        )
        if target_grade is None:
            raise CommandError("student grade progress not visible")
        if target_grade["current_grade"]["name"] != expected["current_grade_name"]:
            raise CommandError(
                "student current grade mismatch: "
                f"expected {expected['current_grade_name']}, got {target_grade['current_grade']['name']}"
            )
        if target_grade["next_grade"]["name"] != expected["next_grade_name"]:
            raise CommandError(
                "student next grade mismatch: "
                f"expected {expected['next_grade_name']}, got {target_grade['next_grade']['name']}"
            )
        if target_grade["trainings_since_last_grade"] != int(expected["grade_trainings_since_last_after"]):
            raise CommandError(
                "student grade trainings_since_last_grade mismatch: "
                f"expected {expected['grade_trainings_since_last_after']}, "
                f"got {target_grade['trainings_since_last_grade']}"
            )
        if target_grade["trainings_to_next"] != int(expected["grade_trainings_to_next_after"]):
            raise CommandError(
                "student grade trainings_to_next mismatch: "
                f"expected {expected['grade_trainings_to_next_after']}, got {target_grade['trainings_to_next']}"
            )
        if not GradeProgressEvent.objects.for_club(club).filter(
            student_grade_id=target_grade["student_grade_id"],
            checkin__student_id=student_id,
            checkin__schedule_id=schedule_id,
            checkin__date=target_date,
        ).exists():
            raise CommandError("student grade progress event not recorded")

        upcoming_date = date.fromisoformat(fixture["upcoming_date"])
        upcoming_occurrences = get_student_schedule_occurrences_for_range(
            club=club,
            student_id=student_id,
            date_from=timezone.localdate(),
            date_to=upcoming_date,
        )
        target_upcoming = next(
            (
                occurrence
                for occurrence in upcoming_occurrences
                if occurrence.schedule_id == int(fixture["upcoming_schedule_id"])
                and occurrence.effective_date == upcoming_date
            ),
            None,
        )
        if target_upcoming is None:
            raise CommandError("student upcoming training not visible")
        if target_upcoming.group_name != expected["upcoming_group_name"]:
            raise CommandError(
                "student upcoming group mismatch: "
                f"expected {expected['upcoming_group_name']}, got {target_upcoming.group_name}"
            )
        if target_upcoming.effective_start_time.strftime("%H:%M:%S") != expected["upcoming_start_time"]:
            raise CommandError(
                "student upcoming start time mismatch: "
                f"expected {expected['upcoming_start_time']}, got {target_upcoming.effective_start_time}"
            )

        return {
            "id": student.id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "subscription": {
                "id": target_subscription.id,
                "tariff_name": target_subscription.tariff.name,
                "trainings_left": target_subscription.trainings_left,
                "trainings_used": target_subscription.trainings_used,
                "trainings_total": target_subscription.tariff.trainings_limit,
                "status": target_subscription.status,
            },
            "attendance_count": len(attendance),
            "attendance": [
                {
                    "id": item.id,
                    "group_name": item.schedule.group_name,
                    "training_type_name": item.training_type.name,
                    "date": item.date.isoformat(),
                }
                for item in attendance
            ],
            "schedule_count": len(schedules),
            "schedule": [
                {
                    "id": item.id,
                    "group_name": item.group_name,
                    "trainer_name": f"{item.trainer.first_name} {item.trainer.last_name}",
                    "location_name": item.location.name,
                }
                for item in schedules
            ],
            "open_debt_count": len(debts),
            "open_debt": {
                "id": target_debt.id,
                "checkin_id": target_debt.checkin_id,
                "tariff_price": self._money(target_debt.tariff_price),
                "reason": target_debt.reason,
                "training_type_name": target_debt.checkin.training_type.name,
                "checkin_date": target_debt.checkin.date.isoformat(),
            },
            "grade": target_grade,
            "upcoming": {
                "schedule_id": target_upcoming.schedule_id,
                "group_name": target_upcoming.group_name,
                "effective_date": target_upcoming.effective_date.isoformat(),
                "effective_start_time": target_upcoming.effective_start_time.strftime("%H:%M:%S"),
                "effective_end_time": target_upcoming.effective_end_time.strftime("%H:%M:%S"),
                "trainer_name": target_upcoming.trainer_name,
                "location_name": target_upcoming.location_name,
            },
        }

    def _parent_surface(self, *, club: Club, parent_user_id: int, student_id: int, fixture: dict) -> dict:
        children = get_parent_children(user_id=parent_user_id, club=club)
        child_ids = [child["id"] for child in children]
        attention_child_id = int(fixture["attention_child"]["student_id"])
        expected_child_ids = {student_id, attention_child_id}
        if set(child_ids) != expected_child_ids:
            raise CommandError(f"parent children mismatch: expected {sorted(expected_child_ids)}, got {child_ids}")
        if int(fixture["foreign_child_id"]) in child_ids:
            raise CommandError("foreign child is visible to parent")

        children_by_id = {child["id"]: child for child in children}
        primary_child = children_by_id[student_id]
        attention_child = children_by_id[attention_child_id]
        expected = fixture["expected"]
        upcoming_day = date.fromisoformat(fixture["upcoming_date"]).weekday()
        upcoming_start = expected["upcoming_start_time"][:5]

        self._assert_child_summary(
            child=primary_child,
            grade_name=expected["current_grade_name"],
            subscription_remaining=int(expected["trainings_left_after"]),
            subscription_total=int(expected["trainings_total"]),
            subscription_status="active",
            next_training_day_of_week=upcoming_day,
            next_training_start_time=upcoming_start,
            next_training_group_name=expected["upcoming_group_name"],
            next_training_trainer_name=expected["upcoming_trainer_name"],
        )
        self._assert_child_summary(
            child=attention_child,
            grade_name=expected["attention_grade_name"],
            subscription_remaining=int(expected["attention_trainings_left"]),
            subscription_total=int(expected["attention_trainings_total"]),
            subscription_status="active",
            next_training_day_of_week=upcoming_day,
            next_training_start_time=upcoming_start,
            next_training_group_name=expected["upcoming_group_name"],
            next_training_trainer_name=expected["upcoming_trainer_name"],
        )

        profile = get_child_profile(user_id=parent_user_id, club=club, student_id=student_id)
        parent_attendance = get_child_attendance(user_id=parent_user_id, club=club, student_id=student_id)
        subscription = profile["active_subscription"]
        if subscription is None:
            raise CommandError("parent child subscription not visible")

        if profile["attendance_count"] != int(expected["attendance_count_after"]):
            raise CommandError(
                "parent profile attendance_count mismatch: "
                f"expected {expected['attendance_count_after']}, got {profile['attendance_count']}"
            )
        if subscription["trainings_left"] != int(expected["trainings_left_after"]):
            raise CommandError(
                "parent profile trainings_left mismatch: "
                f"expected {expected['trainings_left_after']}, got {subscription['trainings_left']}"
            )
        if len(parent_attendance) != int(expected["attendance_count_after"]):
            raise CommandError(
                "parent attendance count mismatch: "
                f"expected {expected['attendance_count_after']}, got {len(parent_attendance)}"
            )
        if not profile["schedule"]:
            raise CommandError("parent child schedule not visible")
        profile_debt = next(
            (debt for debt in profile["open_debts"] if debt["id"] == int(fixture["debt_id"])),
            None,
        )
        if profile_debt is None:
            raise CommandError("parent child open debt not visible")
        if profile_debt["checkin_id"] != int(fixture["debt_checkin_id"]):
            raise CommandError(
                "parent child debt checkin mismatch: "
                f"expected {fixture['debt_checkin_id']}, got {profile_debt['checkin_id']}"
            )
        if profile_debt["reason"] != "no_subscription":
            raise CommandError(f"parent child debt reason mismatch: got {profile_debt['reason']}")
        if profile_debt["training_type_name"] != expected["debt_training_type_name"]:
            raise CommandError(
                "parent child debt training type mismatch: "
                f"expected {expected['debt_training_type_name']}, got {profile_debt['training_type_name']}"
            )
        if self._money(profile_debt["tariff_price"]) != expected["debt_amount"]:
            raise CommandError(
                "parent child debt amount mismatch: "
                f"expected {expected['debt_amount']}, got {self._money(profile_debt['tariff_price'])}"
            )

        attention_profile = get_child_profile(
            user_id=parent_user_id,
            club=club,
            student_id=attention_child_id,
        )
        attention_subscription = attention_profile["active_subscription"]
        if attention_subscription is None:
            raise CommandError("parent attention child subscription not visible")
        if attention_subscription["trainings_left"] != int(expected["attention_trainings_left"]):
            raise CommandError(
                "parent attention child trainings_left mismatch: "
                f"expected {expected['attention_trainings_left']}, got {attention_subscription['trainings_left']}"
            )

        return {
            "child_ids": child_ids,
            "children": children,
            "primary_child": primary_child,
            "attention_child": attention_child,
            "profile": {
                "id": profile["id"],
                "first_name": profile["first_name"],
                "last_name": profile["last_name"],
                "attendance_count": profile["attendance_count"],
                "subscription": {
                    "id": subscription["id"],
                    "tariff_name": subscription["tariff_name"],
                    "trainings_left": subscription["trainings_left"],
                    "trainings_used": subscription["trainings_used"],
                    "status": subscription["status"],
                },
                "schedule_count": len(profile["schedule"]),
                "schedule": profile["schedule"],
                "open_debt_count": len(profile["open_debts"]),
                "open_debts": profile["open_debts"],
            },
            "attention_profile": {
                "id": attention_profile["id"],
                "attendance_count": attention_profile["attendance_count"],
                "subscription": attention_subscription,
                "schedule_count": len(attention_profile["schedule"]),
            },
            "attendance_count": len(parent_attendance),
            "attendance": parent_attendance,
        }

    def _assert_child_summary(
        self,
        *,
        child: dict,
        grade_name: str,
        subscription_remaining: int,
        subscription_total: int,
        subscription_status: str,
        next_training_day_of_week: int,
        next_training_start_time: str,
        next_training_group_name: str,
        next_training_trainer_name: str,
    ) -> None:
        checks = {
            "grade_name": grade_name,
            "subscription_remaining": subscription_remaining,
            "subscription_total": subscription_total,
            "subscription_status": subscription_status,
            "next_training_day_of_week": next_training_day_of_week,
            "next_training_start_time": next_training_start_time,
            "next_training_group_name": next_training_group_name,
            "next_training_trainer_name": next_training_trainer_name,
        }
        for field, expected_value in checks.items():
            if child.get(field) != expected_value:
                raise CommandError(
                    "parent child summary mismatch: "
                    f"{field} expected {expected_value!r}, got {child.get(field)!r}"
                )

    def _privacy_evidence(self, *, student_api: dict, parent_api: dict, fixture: dict) -> dict:
        safe_payload = json.dumps(
            {
                "student_api": student_api,
                "parent_api": parent_api,
            },
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        staff_note = fixture["expected"]["staff_only_note"]
        private_medical_marker = fixture["expected"]["private_medical_marker"]
        foreign_child_name = fixture["expected"]["foreign_child_name"]
        foreign_child_id = int(fixture["foreign_child_id"])

        return {
            "staff_note_visible": staff_note in safe_payload,
            "private_medical_marker_visible": private_medical_marker in safe_payload,
            "foreign_child_visible": foreign_child_id in parent_api["child_ids"] or foreign_child_name in safe_payload,
        }
