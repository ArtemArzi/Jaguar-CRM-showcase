from __future__ import annotations

import base64
import json
import uuid
from io import BytesIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from openpyxl import Workbook

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student


class Command(BaseCommand):
    help = "Prepare an isolated owner dashboard student management E2E fixture."

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
                "Prepared dashboard student management E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"dashboard-student-management-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"DashboardStudents-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Dashboard Students E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Dashboard Students E2E",
        )
        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)

        control_club = Club.objects.create(
            name=f"Jaguar Dashboard Students Control {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=control_club,
            primary_color="#222222",
            accent_color="#222222",
            club_name_display="Control Dashboard Students E2E",
        )
        control_student = Student.objects.create(
            club=control_club,
            first_name="Foreign",
            last_name=f"DashboardStudent {unique}",
            phone=f"+155590{phone_seed}",
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            source=Student.Source.OTHER,
        )

        manual_phone = f"+155540{phone_seed}"
        imported_phone = f"+155541{phone_seed}"
        imported_first_name = "Imported"
        imported_last_name = f"Student {unique}"
        xlsx_bytes = self._build_import_xlsx(
            name=f"{imported_first_name} {imported_last_name}",
            phone=imported_phone,
            duplicate_name="Duplicate Browser Student",
            duplicate_phone=manual_phone,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "control_club_id": control_club.id,
            "control_student_id": control_student.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "manual_student": {
                "first_name": "Browser",
                "last_name": f"Student {unique}",
                "full_name": f"Browser Student {unique}",
                "phone": manual_phone,
                "is_child": True,
                "source": Student.Source.WEBSITE,
                "initial_note": f"Initial browser note {unique}",
                "edited_first_name": "Edited",
                "edited_last_name": f"Student {unique}",
                "edited_full_name": f"Edited Student {unique}",
                "edited_email": f"edited-student-{fixture_id}@dashboard-students-e2e.local",
                "contraindications": f"Dashboard student management note {unique}",
                "follow_up_note": f"Follow-up dashboard note {unique}",
                "final_status": Student.Status.TRIAL,
            },
            "import_file": {
                "filename": f"students-{unique}.xlsx",
                "base64": base64.b64encode(xlsx_bytes).decode("ascii"),
            },
            "imported_student": {
                "first_name": imported_first_name,
                "last_name": imported_last_name,
                "full_name": f"{imported_first_name} {imported_last_name}",
                "phone": imported_phone,
                "status": Student.Status.LEAD,
                "lead_status": Student.LeadStatus.NEW,
                "preview_total": 2,
                "preview_errors_count": 1,
                "preview_valid_count": 1,
            },
            "control_student": {
                "student_id": control_student.id,
                "full_name": str(control_student),
                "phone": control_student.phone,
                "status": control_student.status,
            },
            "created_at": now.isoformat(),
        }

    def _build_import_xlsx(
        self,
        *,
        name: str,
        phone: str,
        duplicate_name: str,
        duplicate_phone: str,
    ) -> bytes:
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["Имя", "Телефон"])
        worksheet.append([name, phone])
        worksheet.append([duplicate_name, duplicate_phone])
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        return buffer.getvalue()

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@dashboard-student-management-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
