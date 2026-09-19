from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Prepare an isolated fixture for trainer student create/edit E2E."

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
                "Prepared trainer student create/edit E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = int(unique, 16) % 8_000_000 + 1_000_000
        fixture_id = f"trainer-student-create-edit-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerStudentCreateEdit-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Student Create Edit E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Student Create Edit E2E",
            unified_client_journey_enabled=True,
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="CreateEdit",
            last_name="Trainer",
            phone=self._phone(phone_seed, 0),
            user=trainer_user,
        )

        assigned_student = Student.objects.create(
            club=club,
            first_name="Editable",
            last_name="Student",
            phone=self._phone(phone_seed, 1),
            email=f"editable-{fixture_id}@trainer-student-create-edit-e2e.local",
            status=Student.Status.ACTIVE,
            lead_status=None,
            crm_entry_kind=Student.CrmEntryKind.EXISTING_STUDENT,
            became_student_at=now,
            source="other",
            assigned_trainer=trainer,
        )
        conflict_student = Student.objects.create(
            club=club,
            first_name="Conflict",
            last_name="Student",
            phone=self._phone(phone_seed, 2),
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            source="other",
        )
        unassigned_student = Student.objects.create(
            club=club,
            first_name="Unassigned",
            last_name="Student",
            phone=self._phone(phone_seed, 3),
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            source="other",
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "assigned_student": self._student_fixture(assigned_student),
            "conflict_student": self._student_fixture(conflict_student),
            "unassigned_student": self._student_fixture(unassigned_student),
            "new_student": {
                "first_name": "Created",
                "last_name": "",
                "phone": self._phone(phone_seed, 4),
            },
            "existing_student_intake": {
                "first_name": "Already",
                "last_name": "Training",
                "phone": self._phone(phone_seed, 6),
            },
            "duplicate_attempt": {
                "first_name": "Duplicate",
                "last_name": "",
                "phone": conflict_student.phone,
            },
            "edit": {
                "first_name": "Edited",
                "last_name": "Student",
                "phone": self._phone(phone_seed, 5),
                "contraindications": f"E2E limitations {fixture_id}",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-student-create-edit-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)

    def _phone(self, seed: int, offset: int) -> str:
        return f"+7901{(seed + offset) % 10_000_000:07d}"

    def _student_fixture(self, student: Student) -> dict:
        return {
            "id": student.id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "full_name": f"{student.first_name} {student.last_name}",
            "phone": student.phone,
        }
