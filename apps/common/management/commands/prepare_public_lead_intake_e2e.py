from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubSettings
from apps.leads.models import LeadIntakeEvent
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Prepare an isolated public lead intake E2E fixture."

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
                "Prepared public lead intake E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"public-lead-intake-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        lead_phone = f"+15559{phone_seed}"
        active_phone = f"+15557{phone_seed}"
        lost_phone = f"+15556{phone_seed}"

        club = Club.objects.create(
            name=f"Jaguar Public Lead Intake E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Public Lead Intake E2E",
        )
        trainer = Trainer.objects.create(
            club=club,
            first_name="Public",
            last_name="Lead Repeat Trainer",
            phone=f"+15555{phone_seed}",
        )
        active_student = Student.objects.create(
            club=club,
            first_name="Existing Active",
            last_name="Public Repeat",
            phone=active_phone,
            is_child=False,
            source=Student.Source.OTHER,
            status=Student.Status.ACTIVE,
            lead_status=None,
            assigned_trainer=trainer,
        )
        lost_student = Student.objects.create(
            club=club,
            first_name="Existing Lost",
            last_name="Public Repeat",
            phone=lost_phone,
            is_child=False,
            source=Student.Source.OTHER,
            status=Student.Status.LOST,
            lead_status=None,
            loss_reason=Student.LossReason.EXPENSIVE,
            assigned_trainer=trainer,
        )

        first_key = uuid.uuid4()
        repeat_key = uuid.uuid4()
        active_existing_key = uuid.uuid4()
        lost_existing_key = uuid.uuid4()
        invalid_key = uuid.uuid4()

        first_payload = self._payload(
            fixture_id=fixture_id,
            phone=lead_phone,
            idempotency_key=first_key,
            goal="First public intake trial request",
            preferred_format=LeadIntakeEvent.PreferredFormat.GROUP,
        )
        repeat_payload = self._payload(
            fixture_id=fixture_id,
            phone=lead_phone,
            idempotency_key=repeat_key,
            goal="Repeat public intake trial request",
            preferred_format=LeadIntakeEvent.PreferredFormat.PERSONAL,
        )
        active_existing_payload = self._payload(
            fixture_id=fixture_id,
            phone=active_phone,
            idempotency_key=active_existing_key,
            goal="Existing active public intake repeat request",
            preferred_format=LeadIntakeEvent.PreferredFormat.GROUP,
        )
        lost_existing_payload = self._payload(
            fixture_id=fixture_id,
            phone=lost_phone,
            idempotency_key=lost_existing_key,
            goal="Existing lost public intake repeat request",
            preferred_format=LeadIntakeEvent.PreferredFormat.UNSURE,
        )
        invalid_consent_payload = self._payload(
            fixture_id=fixture_id,
            phone=f"+15558{phone_seed}",
            idempotency_key=invalid_key,
            goal="Invalid consent public intake request",
            preferred_format=LeadIntakeEvent.PreferredFormat.UNSURE,
        )
        invalid_consent_payload["consent"]["personal_data"] = False

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "landing_default_club_id": club.id,
            "trainer": {
                "trainer_id": trainer.id,
            },
            "existing_students": {
                "active": {
                    "student_id": active_student.id,
                    "status": Student.Status.ACTIVE,
                    "lead_status": None,
                    "assigned_trainer_id": trainer.id,
                    "in_pool": False,
                    "in_trainer_mine": False,
                },
                "lost": {
                    "student_id": lost_student.id,
                    "status": Student.Status.LEAD,
                    "lead_status": Student.LeadStatus.NEW,
                    "loss_reason": None,
                    "assigned_trainer_id": None,
                    "in_pool": True,
                    "in_trainer_mine": False,
                },
            },
            "payloads": {
                "first": first_payload,
                "repeat_same_phone": repeat_payload,
                "existing_active": active_existing_payload,
                "existing_lost": lost_existing_payload,
                "invalid_consent": invalid_consent_payload,
            },
            "expected": {
                "first_preferred_format": LeadIntakeEvent.PreferredFormat.GROUP,
                "repeat_preferred_format": LeadIntakeEvent.PreferredFormat.PERSONAL,
                "existing_active_preferred_format": LeadIntakeEvent.PreferredFormat.GROUP,
                "existing_lost_preferred_format": LeadIntakeEvent.PreferredFormat.UNSURE,
                "invalid_consent_code": "consent_required",
            },
            "created_at": now.isoformat(),
        }

    def _payload(
        self,
        *,
        fixture_id: str,
        phone: str,
        idempotency_key: uuid.UUID,
        goal: str,
        preferred_format: str,
    ) -> dict:
        return {
            "name": f"Public Lead {fixture_id}",
            "phone": phone,
            "goal": goal,
            "preferred_format": preferred_format,
            "is_child": False,
            "consent": {
                "personal_data": True,
                "privacy_policy_version": "2026-06-22",
                "consent_text_hash": f"sha256:{fixture_id}",
            },
            "source": {
                "page": "/",
                "utm_source": "e2e",
                "utm_medium": "browser",
                "utm_campaign": fixture_id,
                "utm_content": "public-intake",
                "utm_term": "trial",
            },
            "idempotency_key": str(idempotency_key),
            "hp_field": "",
        }
