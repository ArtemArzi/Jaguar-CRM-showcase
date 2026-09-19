from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from ninja.testing import TestClient

from apps.clubs.models import Club, ClubMembership
from apps.documents.models import DocumentType, StudentDocument
from apps.documents.selectors import get_document_checklist, get_missing_documents_count
from apps.students.models import Student
from config.api import api


class Command(BaseCommand):
    help = "Assert dashboard document checklist/upload state and safe document API access."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_document_checklist_upload_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for uploaded student document before failing.",
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
                    raise CommandError(f"document checklist upload E2E assertion failed: {exc}") from exc
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
            "parent",
            "student_user",
            "foreign_parent",
            "student",
            "foreign_student",
            "document_type_id",
            "student_upload_document_type_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        student = Student.objects.for_club(club).get(id=int(fixture["student"]["student_id"]))
        document_type = DocumentType.objects.for_club(club).get(id=int(fixture["document_type_id"]))
        student_upload_document_type = DocumentType.objects.for_club(club).get(
            id=int(fixture["student_upload_document_type_id"])
        )
        document = self._document_evidence(
            club=club,
            student=student,
            document_type=document_type,
            expected_filename=fixture["expected"]["upload_filename"],
        )
        student_upload_document = self._document_evidence(
            club=club,
            student=student,
            document_type=student_upload_document_type,
            expected_filename=fixture["expected"]["student_upload_filename"],
        )
        checklist = self._checklist_evidence(
            club=club,
            student=student,
            document_type=document_type,
            student_upload_document_type=student_upload_document_type,
        )
        api_evidence = self._api_evidence(club=club, fixture=fixture)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "document": document,
            "student_upload_document": student_upload_document,
            "checklist": checklist,
            "api": api_evidence,
        }

    def _document_evidence(
        self,
        *,
        club: Club,
        student: Student,
        document_type: DocumentType,
        expected_filename: str,
    ) -> dict:
        documents = list(
            StudentDocument.objects.for_club(club)
            .filter(student=student, document_type=document_type, deleted_at__isnull=True)
            .order_by("id")
        )
        if not documents:
            raise CommandError("student document not found")
        if len(documents) != 1:
            raise CommandError(f"student document count mismatch: expected 1, got {len(documents)}")

        document = documents[0]
        if not document.is_provided:
            raise CommandError("student document is not marked provided")
        if not document.file:
            raise CommandError("student document file was not uploaded")
        if document.uploaded_at is None:
            raise CommandError("student document uploaded_at is empty")
        if not document.is_private:
            raise CommandError("student document is not private")

        stored_name = Path(document.file.name).name
        safe_stored_name = stored_name != expected_filename and stored_name.endswith(".pdf")

        return {
            "id": document.id,
            "count": len(documents),
            "is_provided": document.is_provided,
            "has_file": bool(document.file),
            "uploaded_at": document.uploaded_at.isoformat(),
            "is_private": document.is_private,
            "safe_stored_name": safe_stored_name,
        }

    def _checklist_evidence(
        self,
        *,
        club: Club,
        student: Student,
        document_type: DocumentType,
        student_upload_document_type: DocumentType,
    ) -> dict:
        checklist = get_document_checklist(club=club, student_id=student.id)
        item = next((item for item in checklist if item["document_type"].id == document_type.id), None)
        student_upload_item = next(
            (item for item in checklist if item["document_type"].id == student_upload_document_type.id),
            None,
        )
        if item is None:
            raise CommandError("document checklist item not found")
        if student_upload_item is None:
            raise CommandError("student upload document checklist item not found")
        if not item["is_provided"] or not item["has_file"]:
            raise CommandError("document checklist item is not provided with file")
        if not student_upload_item["is_provided"] or not student_upload_item["has_file"]:
            raise CommandError("student upload checklist item is not provided with file")

        return {
            "item_count": len(checklist),
            "is_provided": item["is_provided"],
            "has_file": item["has_file"],
            "student_upload_is_provided": student_upload_item["is_provided"],
            "student_upload_has_file": student_upload_item["has_file"],
            "missing_count": get_missing_documents_count(club=club, student_id=student.id),
        }

    def _api_evidence(self, *, club: Club, fixture: dict) -> dict:
        client = TestClient(api)
        parent_auth = self._auth_params(
            user_id=int(fixture["parent"]["user_id"]),
            club=club,
            role=ClubMembership.Role.PARENT,
        )
        student_auth = self._auth_params(
            user_id=int(fixture["student_user"]["user_id"]),
            club=club,
            role=ClubMembership.Role.STUDENT,
        )
        foreign_parent_auth = self._auth_params(
            user_id=int(fixture["foreign_parent"]["user_id"]),
            club=club,
            role=ClubMembership.Role.PARENT,
        )

        document = StudentDocument.objects.for_club(club).get(
            student_id=int(fixture["student"]["student_id"]),
            document_type_id=int(fixture["document_type_id"]),
            deleted_at__isnull=True,
        )
        if document.notes != fixture["expected"]["staff_note"]:
            document.notes = fixture["expected"]["staff_note"]
            document.save(update_fields=["notes", "updated_at"])

        with patch("apps.common.auth.TenantJWTAuth.__call__", side_effect=self._mock_auth):
            parent_checklist_response = client.get(
                f"/documents/students/{fixture['student']['student_id']}/checklist/",
                **parent_auth,
            )
            student_checklist_response = client.get(
                f"/documents/students/{fixture['student']['student_id']}/checklist/",
                **student_auth,
            )
            parent_upload_response = client.post(
                f"/documents/students/{fixture['student']['student_id']}/upload/",
                POST={"document_type_id": str(fixture["document_type_id"])},
                FILES={
                    "file": SimpleUploadedFile(
                        "parent-safe-probe.pdf",
                        b"%PDF-1.4 parent safe probe",
                        content_type="application/pdf",
                    )
                },
                **parent_auth,
            )
            foreign_parent_upload_response = client.post(
                f"/documents/students/{fixture['student']['student_id']}/upload/",
                POST={"document_type_id": str(fixture["document_type_id"])},
                FILES={
                    "file": SimpleUploadedFile(
                        "foreign-parent-probe.pdf",
                        b"%PDF-1.4 foreign parent probe",
                        content_type="application/pdf",
                    )
                },
                **foreign_parent_auth,
            )
            parent_foreign_checklist_response = client.get(
                f"/documents/students/{fixture['foreign_student']['student_id']}/checklist/",
                **parent_auth,
            )
            student_foreign_checklist_response = client.get(
                f"/documents/students/{fixture['foreign_student']['student_id']}/checklist/",
                **student_auth,
            )

        parent_checklist = self._safe_json(parent_checklist_response)
        student_checklist = self._safe_json(student_checklist_response)
        parent_upload = self._safe_json(parent_upload_response)
        parent_item = self._find_checklist_item(parent_checklist, document_type_id=int(fixture["document_type_id"]))
        student_item = self._find_checklist_item(student_checklist, document_type_id=int(fixture["document_type_id"]))
        student_upload_document_type_id = int(fixture["student_upload_document_type_id"])
        parent_student_upload_item = self._find_checklist_item(
            parent_checklist,
            document_type_id=student_upload_document_type_id,
        )
        student_upload_item = self._find_checklist_item(
            student_checklist,
            document_type_id=student_upload_document_type_id,
        )
        staff_note = fixture["expected"]["staff_note"]
        parent_payload = json.dumps(parent_checklist, ensure_ascii=False, sort_keys=True)
        student_payload = json.dumps(student_checklist, ensure_ascii=False, sort_keys=True)

        if parent_checklist_response.status_code != 200 or not parent_item["has_file"]:
            raise CommandError(f"parent checklist did not expose uploaded document safely: {parent_checklist}")
        if not parent_student_upload_item["has_file"]:
            raise CommandError(f"parent checklist did not expose student-uploaded document safely: {parent_checklist}")
        if staff_note in parent_payload:
            raise CommandError("parent checklist exposed staff-only document note")
        if student_checklist_response.status_code != 200 or not student_item["has_file"]:
            raise CommandError(f"student checklist did not expose uploaded document safely: {student_checklist}")
        if not student_upload_item["has_file"]:
            raise CommandError(
                f"student checklist did not expose student-uploaded document safely: {student_checklist}"
            )
        if staff_note in student_payload:
            raise CommandError("student checklist exposed staff-only document note")
        if parent_upload_response.status_code != 200 or parent_upload.get("notes") != "":
            raise CommandError(f"parent upload safe response mismatch: {parent_upload}")
        if foreign_parent_upload_response.status_code != 404:
            raise CommandError(
                f"foreign parent upload status mismatch: {foreign_parent_upload_response.status_code}"
            )
        if parent_foreign_checklist_response.status_code != 404:
            raise CommandError(
                f"parent foreign checklist status mismatch: {parent_foreign_checklist_response.status_code}"
            )
        if student_foreign_checklist_response.status_code != 404:
            raise CommandError(
                f"student foreign checklist status mismatch: {student_foreign_checklist_response.status_code}"
            )

        return {
            "parent_checklist": {
                "status_code": parent_checklist_response.status_code,
                "has_file": parent_item["has_file"],
                "is_provided": parent_item["is_provided"],
                "student_upload_has_file": parent_student_upload_item["has_file"],
                "student_upload_is_provided": parent_student_upload_item["is_provided"],
                "staff_note_visible": staff_note in parent_payload,
            },
            "student_checklist": {
                "status_code": student_checklist_response.status_code,
                "has_file": student_item["has_file"],
                "is_provided": student_item["is_provided"],
                "student_upload_has_file": student_upload_item["has_file"],
                "student_upload_is_provided": student_upload_item["is_provided"],
                "staff_note_visible": staff_note in student_payload,
            },
            "parent_upload_status": parent_upload_response.status_code,
            "parent_upload_safe_notes": parent_upload.get("notes"),
            "foreign_parent_upload_status": foreign_parent_upload_response.status_code,
            "parent_foreign_checklist_status": parent_foreign_checklist_response.status_code,
            "student_foreign_checklist_status": student_foreign_checklist_response.status_code,
        }

    def _find_checklist_item(self, payload, *, document_type_id: int) -> dict:
        if not isinstance(payload, list):
            raise CommandError(f"checklist payload is not a list: {payload}")
        for item in payload:
            if item.get("document_type", {}).get("id") == document_type_id:
                return item
        raise CommandError(f"document type {document_type_id} not found in checklist payload")

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
            return response.content.decode("utf-8", errors="replace")
