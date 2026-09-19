from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.leads.models import LeadIntakeEvent
from apps.leads.services import create_landing_lead_intake
from apps.students.models import Student
from apps.trainers.models import Trainer


def _masked_phone(phone: str) -> str:
    if len(phone) <= 4:
        return "****"
    return f"{phone[:4]}****{phone[-2:]}"


class Command(BaseCommand):
    help = "Prepare an isolated fixture for trainer lead pool lifecycle E2E."

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
                "Prepared trainer lead pool lifecycle E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"trainer-lead-pool-lifecycle-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"
        trainer_password = f"TrainerLeadPool-{unique}-pass"
        other_trainer_password = f"OtherTrainerLeadPool-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Lead Pool E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Lead Pool E2E",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="LeadPool",
            last_name="Trainer",
            phone=f"+79010{numeric_suffix}01",
            user=trainer_user,
        )

        other_trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="other-trainer",
            password=other_trainer_password,
        )
        ClubMembership.objects.create(
            user=other_trainer_user,
            club=club,
            role=ClubMembership.Role.TRAINER,
        )
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="LeadPoolOther",
            last_name="Trainer",
            phone=f"+79010{numeric_suffix}02",
            user=other_trainer_user,
        )

        pool_intake = self._create_public_intake(
            club=club,
            name="PoolClaim",
            phone=f"+79020{numeric_suffix}01",
            goal="Хочу прийти на пробную тренировку",
            idempotency_key=uuid.uuid4(),
        )
        pool_lead = pool_intake.student
        conflict_intake = self._create_public_intake(
            club=club,
            name="PoolConflict",
            phone=f"+79020{numeric_suffix}02",
            goal="Нужна консультация по расписанию",
            idempotency_key=uuid.uuid4(),
        )
        conflict_lead = conflict_intake.student
        loss_lead = self._create_lead(
            club=club,
            first_name="PoolLoss",
            phone=f"+79020{numeric_suffix}03",
            assigned_trainer=trainer,
            lead_status=Student.LeadStatus.THINKING,
            source=Student.Source.INSTAGRAM,
        )
        hidden_other_trainer_lead = self._create_lead(
            club=club,
            first_name="HiddenOtherLead",
            phone=f"+79020{numeric_suffix}04",
            assigned_trainer=other_trainer,
            lead_status=Student.LeadStatus.NEW,
            source=Student.Source.OTHER,
        )

        foreign_club = Club.objects.create(
            name=f"Foreign Lead Pool E2E {fixture_id}",
            city="E2E",
            disciplines=["boxing"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=foreign_club,
            primary_color="#222222",
            accent_color="#00AA88",
            club_name_display="Foreign Lead Pool E2E",
        )
        foreign_pool_lead = self._create_lead(
            club=foreign_club,
            first_name="ForeignPool",
            phone=f"+79020{numeric_suffix}05",
            assigned_trainer=None,
            lead_status=Student.LeadStatus.NEW,
            source=Student.Source.WEBSITE,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "foreign_club_id": foreign_club.id,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "other_trainer": {
                "user_id": other_trainer_user.id,
                "trainer_id": other_trainer.id,
                "email": other_trainer_user.email,
                "password": other_trainer_password,
            },
            "pool_lead": self._lead_fixture(pool_lead, intake_event=pool_intake),
            "conflict_lead": self._lead_fixture(conflict_lead, intake_event=conflict_intake),
            "loss_lead": self._lead_fixture(loss_lead),
            "hidden_other_trainer_lead": self._lead_fixture(hidden_other_trainer_lead),
            "foreign_pool_lead": self._lead_fixture(foreign_pool_lead),
            "expected": {
                "release_reason": "needs admin reassignment",
                "loss_reason": Student.LossReason.EXPENSIVE,
                "profile_trainer_name": "LeadPool Trainer",
                "profile_club_name": "Jaguar Trainer Lead Pool E2E",
                "profile_disabled_categories": ["trainer_tasks"],
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-lead-pool-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)

    def _create_lead(
        self,
        *,
        club: Club,
        first_name: str,
        phone: str,
        assigned_trainer: Trainer | None,
        lead_status: str,
        source: str,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name=first_name,
            last_name="Lead",
            phone=phone,
            is_child=False,
            status=Student.Status.LEAD,
            source=source,
            lead_status=lead_status,
            assigned_trainer=assigned_trainer,
        )

    def _create_public_intake(
        self,
        *,
        club: Club,
        name: str,
        phone: str,
        goal: str,
        idempotency_key: uuid.UUID,
    ) -> LeadIntakeEvent:
        return create_landing_lead_intake(
            club_id=club.id,
            name=name,
            phone=phone,
            goal=goal,
            preferred_format=LeadIntakeEvent.PreferredFormat.UNSURE,
            is_child=False,
            consent={
                "personal_data": True,
                "privacy_policy_version": "2026-06-22",
                "consent_text_hash": f"sha256:{idempotency_key.hex}",
            },
            source={
                "page": "/",
                "utm_source": "e2e",
                "utm_medium": "test",
                "utm_campaign": "lead-pool-lifecycle",
                "utm_content": "",
                "utm_term": "",
            },
            request_id=f"req-{idempotency_key.hex[:12]}",
            client_ip_hash=f"iphash-{idempotency_key.hex[:12]}",
            user_agent="Playwright real-stack E2E",
            idempotency_key=idempotency_key,
        )

    def _lead_fixture(self, lead: Student, *, intake_event: LeadIntakeEvent | None = None) -> dict:
        data = {
            "id": lead.id,
            "first_name": lead.first_name,
            "phone": lead.phone,
            "masked_phone": _masked_phone(lead.phone),
        }
        if intake_event is not None:
            data["intake_event_id"] = intake_event.id
        return data
