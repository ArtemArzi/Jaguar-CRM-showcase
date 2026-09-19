from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership
from apps.students.models import ParentInvite, Student, StudentNote


class Command(BaseCommand):
    help = "Assert owner dashboard student management E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_dashboard_student_management_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for student management state before failing.",
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
                    raise CommandError(f"dashboard student management E2E assertion failed: {exc}") from exc
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
            "control_club_id",
            "control_student_id",
            "owner",
            "manual_student",
            "import_file",
            "imported_student",
            "control_student",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        control_club = Club.objects.get(id=int(fixture["control_club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        manual = self._manual_student_evidence(club=club, fixture=fixture)
        imported = self._imported_student_evidence(club=club, fixture=fixture)
        control = self._control_student_evidence(control_club=control_club, fixture=fixture)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "owner": {
                "user_id": owner_user_id,
                "membership_role": membership.role,
            },
            "students": {
                "club_count": Student.objects.for_club(club).filter(deleted_at__isnull=True).count(),
                "manual": manual,
                "imported": imported,
            },
            "control": control,
        }

    def _manual_student_evidence(self, *, club: Club, fixture: dict) -> dict:
        expected = fixture["manual_student"]
        qs = Student.objects.for_club(club).filter(deleted_at__isnull=True)
        if expected["is_child"]:
            qs = qs.filter(Q(guardian_phone=expected["phone"]) | Q(phone=expected["phone"]))
        else:
            qs = qs.filter(phone=expected["phone"])
        try:
            student = qs.get()
        except Student.DoesNotExist as exc:
            raise CommandError("manual browser-created student not found") from exc

        checks = {
            "first_name": expected["edited_first_name"],
            "last_name": expected["edited_last_name"],
            "email": expected["edited_email"],
            "contraindications": expected["contraindications"],
            "status": expected["final_status"],
            "source": expected["source"],
            "is_child": expected["is_child"],
        }
        for field, value in checks.items():
            if getattr(student, field) != value:
                raise CommandError(f"manual student {field} mismatch")

        notes = set(
            StudentNote.objects.for_club(club)
            .filter(student=student)
            .values_list("text", flat=True)
        )
        for text in (expected["initial_note"], expected["follow_up_note"]):
            if text not in notes:
                raise CommandError(f"manual student note missing: {text}")
        invite_count = ParentInvite.objects.for_club(club).filter(student=student).count()
        if invite_count != 1:
            raise CommandError(f"manual student parent invite count mismatch: got {invite_count}")

        return {
            "student_id": student.id,
            "full_name": str(student),
            "phone": student.guardian_phone or student.phone,
            "student_phone": student.phone,
            "guardian_phone": student.guardian_phone,
            "status": student.status,
            "source": student.source,
            "is_child": student.is_child,
            "note_count": len(notes),
            "parent_invite_count": invite_count,
        }

    def _imported_student_evidence(self, *, club: Club, fixture: dict) -> dict:
        expected = fixture["imported_student"]
        try:
            student = Student.objects.for_club(club).get(phone=expected["phone"], deleted_at__isnull=True)
        except Student.DoesNotExist as exc:
            raise CommandError("imported student not found") from exc

        if student.first_name != expected["first_name"]:
            raise CommandError("imported student first_name mismatch")
        if student.last_name != expected["last_name"]:
            raise CommandError("imported student last_name mismatch")
        if student.status != expected["status"]:
            raise CommandError("imported student status mismatch")
        if student.lead_status != expected["lead_status"]:
            raise CommandError("imported student lead_status mismatch")

        return {
            "student_id": student.id,
            "full_name": str(student),
            "phone": student.phone,
            "status": student.status,
            "lead_status": student.lead_status,
        }

    def _control_student_evidence(self, *, control_club: Club, fixture: dict) -> dict:
        expected = fixture["control_student"]
        control_student = Student.objects.for_club(control_club).get(id=int(fixture["control_student_id"]))
        if control_student.phone != expected["phone"]:
            raise CommandError("control student phone changed")
        if control_student.status != expected["status"]:
            raise CommandError("control student status changed")

        if Student.objects.for_club(fixture["club_id"]).filter(phone=expected["phone"]).exists():
            raise CommandError("control student leaked into owner club")

        return {
            "student_id": control_student.id,
            "full_name": str(control_student),
            "status": control_student.status,
        }
