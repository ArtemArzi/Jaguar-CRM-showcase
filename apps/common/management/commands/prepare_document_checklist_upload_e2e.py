from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.documents.models import DocumentType
from apps.students.models import Student


class Command(BaseCommand):
    help = "Prepare an isolated fixture for dashboard document checklist/upload E2E."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        output_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared document checklist upload E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"document-checklist-upload-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        club = Club.objects.create(
            name=f"Jaguar Document E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Document E2E",
        )

        owner_password = f"DocumentOwner-{unique}-pass"
        admin_password = f"DocumentAdmin-{unique}-pass"
        parent_password = f"DocumentParent-{unique}-pass"
        student_password = f"DocumentStudent-{unique}-pass"
        foreign_parent_password = f"DocumentForeignParent-{unique}-pass"

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        admin_user = self._create_user(fixture_id=fixture_id, role="admin", password=admin_password)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=parent_password)
        student_user = self._create_user(fixture_id=fixture_id, role="student", password=student_password)
        foreign_parent_user = self._create_user(
            fixture_id=fixture_id,
            role="foreign-parent",
            password=foreign_parent_password,
        )
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        ClubMembership.objects.create(user=admin_user, club=club, role=ClubMembership.Role.ADMIN)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=foreign_parent_user, club=club, role=ClubMembership.Role.PARENT)

        student = Student.objects.create(
            club=club,
            first_name="Document",
            last_name="Student",
            phone=f"+15553{phone_seed}1",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
        )
        foreign_student = Student.objects.create(
            club=club,
            first_name="Foreign",
            last_name="Document",
            phone=f"+15553{phone_seed}2",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=foreign_parent_user,
        )
        document_type = DocumentType.objects.create(
            club=club,
            name=f"E2E Consent {fixture_id}",
            description="Required document for deterministic E2E upload.",
            is_required=True,
            scope=DocumentType.Scope.CHILDREN,
            is_active=True,
        )
        student_upload_document_type = DocumentType.objects.create(
            club=club,
            name=f"E2E Student Upload {fixture_id}",
            description="Required document uploaded from student profile in E2E.",
            is_required=True,
            scope=DocumentType.Scope.CHILDREN,
            is_active=True,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "email": owner_user.email,
                "password": owner_password,
                "user_id": owner_user.id,
            },
            "admin": {
                "email": admin_user.email,
                "password": admin_password,
                "user_id": admin_user.id,
                "role": ClubMembership.Role.ADMIN,
            },
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "student_user": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
            },
            "foreign_parent": {
                "email": foreign_parent_user.email,
                "password": foreign_parent_password,
                "user_id": foreign_parent_user.id,
            },
            "student": {
                "student_id": student.id,
                "name": str(student),
            },
            "foreign_student": {
                "student_id": foreign_student.id,
                "name": str(foreign_student),
            },
            "document_type_id": document_type.id,
            "student_upload_document_type_id": student_upload_document_type.id,
            "expected": {
                "document_name": document_type.name,
                "student_upload_document_name": student_upload_document_type.name,
                "staff_note": f"STAFF_ONLY_DOCUMENT_NOTE_{unique}",
                "upload_filename": f"consent-{unique}.pdf",
                "upload_content": f"%PDF-1.4 document upload e2e {fixture_id}\n",
                "student_upload_filename": f"student-upload-{unique}.pdf",
                "student_upload_content": f"%PDF-1.4 student profile upload e2e {fixture_id}\n",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@document-checklist-upload-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
