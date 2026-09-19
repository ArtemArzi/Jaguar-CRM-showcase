from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import Checkin, ScheduleEnrollment
from apps.billing.models import Payment, Subscription
from apps.clubs.models import Club, ClubMembership
from apps.students.models import AccountAccess, Student, StudentIntakeCommand
from config.api import api


class Command(BaseCommand):
    help = "Assert trainer student create/edit E2E side effects and scope."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_student_create_edit_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for browser-created side effects before failing.",
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
                    raise CommandError(f"trainer student create/edit E2E assertion failed: {exc}") from exc
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
            "assigned_student",
            "conflict_student",
            "unassigned_student",
            "new_student",
            "existing_student_intake",
            "duplicate_attempt",
            "edit",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        trainer_id = int(fixture["trainer"]["trainer_id"])
        assigned_id = int(fixture["assigned_student"]["id"])
        unassigned_id = int(fixture["unassigned_student"]["id"])

        created = self._get_created_student(club_id=club.id, fixture=fixture)
        existing_student = self._get_existing_student(club_id=club.id, fixture=fixture)
        edited = Student.objects.for_club(club).get(id=assigned_id, deleted_at__isnull=True)
        conflict_count = Student.objects.for_club(club).filter(
            phone=fixture["conflict_student"]["phone"],
            deleted_at__isnull=True,
        ).count()

        if created.assigned_trainer_id != trainer_id:
            raise CommandError("created student is not assigned to current trainer")
        if created.status != Student.Status.LEAD:
            raise CommandError(f"created student status mismatch: expected lead, got {created.status}")
        if created.crm_entry_kind != Student.CrmEntryKind.LEAD_INTAKE:
            raise CommandError("created lead provenance mismatch")
        if existing_student.status != Student.Status.ACTIVE or existing_student.lead_status is not None:
            raise CommandError("existing-student intake did not create active non-lead state")
        if existing_student.crm_entry_kind != Student.CrmEntryKind.EXISTING_STUDENT:
            raise CommandError("existing-student provenance mismatch")
        if existing_student.became_student_at != existing_student.created_at:
            raise CommandError("existing-student first-state timestamp mismatch")
        if existing_student.assigned_trainer_id != trainer_id:
            raise CommandError("existing student is not assigned to current trainer")
        artifact_counts = {
            "payments": Payment.objects.for_club(club).filter(student=existing_student).count(),
            "subscriptions": Subscription.objects.for_club(club).filter(student=existing_student).count(),
            "enrollments": ScheduleEnrollment.objects.for_club(club).filter(student=existing_student).count(),
            "checkins": Checkin.objects.for_club(club).filter(student=existing_student).count(),
            "account_accesses": AccountAccess.objects.for_club(club).filter(student=existing_student).count(),
        }
        if any(artifact_counts.values()):
            raise CommandError(f"existing-student intake created forbidden artifacts: {artifact_counts}")
        command_results = list(
            StudentIntakeCommand.objects.for_club(club)
            .filter(student_id__in=[created.id, existing_student.id])
            .order_by("created_at", "id")
            .values_list("result_kind", flat=True)
        )
        if command_results != ["created_new_contact", "created_existing_student"]:
            raise CommandError(f"intake command audit mismatch: {command_results}")
        if edited.first_name != fixture["edit"]["first_name"]:
            raise CommandError("assigned student edit first_name was not persisted")
        if edited.last_name != fixture["edit"]["last_name"]:
            raise CommandError("assigned student edit last_name was not persisted")
        if edited.phone != fixture["edit"]["phone"]:
            raise CommandError("assigned student edit phone was not persisted")
        if edited.contraindications != fixture["edit"]["contraindications"]:
            raise CommandError("assigned student edit contraindications were not persisted")
        if conflict_count != 1:
            raise CommandError("duplicate create changed conflict phone cardinality")
        if Student.objects.for_club(club).filter(
            first_name=fixture["duplicate_attempt"]["first_name"],
            last_name=fixture["duplicate_attempt"]["last_name"],
            phone=fixture["duplicate_attempt"]["phone"],
            deleted_at__isnull=True,
        ).exists():
            raise CommandError("duplicate create persisted a conflicting student")

        client = TestClient(api)
        trainer_auth = self._auth_params(
            user_id=int(fixture["trainer"]["user_id"]),
            club=club,
            role=ClubMembership.Role.TRAINER,
        )
        with patch("apps.common.auth.TenantJWTAuth.__call__", side_effect=self._mock_auth):
            lead_list_response = client.get(
                "/leads/?scope=mine&limit=50&offset=0",
                **trainer_auth,
            )
            lead_list_payload = self._safe_json(lead_list_response)
            lead_visible_ids = [item["id"] for item in lead_list_payload.get("items", [])]
            student_list_response = client.get(
                "/students/?workspace=students&limit=50&offset=0",
                **trainer_auth,
            )
            student_list_payload = self._safe_json(student_list_response)
            student_visible_ids = [
                item["id"]
                for item in student_list_payload.get("items", student_list_payload)
            ]
            created_detail = client.get(f"/leads/{created.id}", **trainer_auth)
            existing_detail = client.get(f"/students/{existing_student.id}/", **trainer_auth)
            edited_detail = client.get(f"/students/{assigned_id}/", **trainer_auth)
            forbidden_update = client.put(
                f"/students/{unassigned_id}/",
                json={"first_name": "ForbiddenEdit"},
                **trainer_auth,
            )

        unassigned = Student.objects.for_club(club).get(id=unassigned_id, deleted_at__isnull=True)
        if unassigned.first_name != fixture["unassigned_student"]["first_name"]:
            raise CommandError("unassigned student was mutated by forbidden trainer edit")
        if lead_list_response.status_code != 200:
            raise CommandError(
                f"trainer lead list status mismatch: {lead_list_response.status_code}"
            )
        if student_list_response.status_code != 200:
            raise CommandError(
                f"trainer student list status mismatch: {student_list_response.status_code}"
            )
        if created.id not in lead_visible_ids:
            raise CommandError("created lead is not visible in trainer lead list")
        if created.id in student_visible_ids:
            raise CommandError("created lead leaked into trainer student workspace")
        if assigned_id not in student_visible_ids:
            raise CommandError("edited assigned student is not visible in trainer list")
        if existing_student.id not in student_visible_ids:
            raise CommandError("existing-student intake is not visible in trainer list")
        if created_detail.status_code != 200:
            raise CommandError(f"created detail status mismatch: {created_detail.status_code}")
        if edited_detail.status_code != 200:
            raise CommandError(f"edited detail status mismatch: {edited_detail.status_code}")
        if existing_detail.status_code != 200:
            raise CommandError(f"existing detail status mismatch: {existing_detail.status_code}")
        if forbidden_update.status_code != 403:
            raise CommandError(f"unassigned edit status mismatch: {forbidden_update.status_code}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "created": {
                "id": created.id,
                "status": created.status,
                "assigned_trainer_id": created.assigned_trainer_id,
            },
            "existing_student": {
                "id": existing_student.id,
                "status": existing_student.status,
                "crm_entry_kind": existing_student.crm_entry_kind,
                "became_student_at": existing_student.became_student_at.isoformat(),
                "artifact_counts": artifact_counts,
            },
            "edited": {
                "id": edited.id,
                "first_name": edited.first_name,
                "phone": edited.phone,
            },
            "scope": {
                "lead_visible_ids": lead_visible_ids,
                "student_visible_ids": student_visible_ids,
                "created_detail_status": created_detail.status_code,
                "edited_detail_status": edited_detail.status_code,
                "existing_detail_status": existing_detail.status_code,
                "unassigned_update_status": forbidden_update.status_code,
            },
            "duplicate": {
                "conflict_phone_count": conflict_count,
            },
        }

    def _get_created_student(self, *, club_id: int, fixture: dict) -> Student:
        student = Student.objects.for_club(club_id).filter(
            first_name=fixture["new_student"]["first_name"],
            last_name=fixture["new_student"]["last_name"],
            phone=fixture["new_student"]["phone"],
            deleted_at__isnull=True,
        ).first()
        if student is None:
            raise CommandError("created student not found")
        return student

    def _get_existing_student(self, *, club_id: int, fixture: dict) -> Student:
        expected = fixture["existing_student_intake"]
        student = Student.objects.for_club(club_id).filter(
            first_name=expected["first_name"],
            last_name=expected["last_name"],
            phone=expected["phone"],
            deleted_at__isnull=True,
        ).first()
        if student is None:
            raise CommandError("existing-student intake not found")
        return student

    def _auth_params(self, *, user_id: int, club: Club, role: str) -> dict:
        user = get_user_model().objects.get(id=user_id)
        membership = ClubMembership.objects.filter(user=user, club=club, is_active=True).first()
        if membership is None:
            membership = ClubMembership.objects.create(user=user, club=club, role=role)
        return {
            "user": user,
            "club": club,
            "_membership": membership,
            "auth": {"user_id": user.id, "club_id": club.id, "role": membership.role},
        }

    def _mock_auth(self, request):
        return request.auth if getattr(request, "auth", None) else None

    def _safe_json(self, response):
        try:
            return response.json()
        except ValueError:
            return {}
