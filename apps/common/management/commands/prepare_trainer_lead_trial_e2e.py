from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule, TrainingGroupRolloutState
from apps.attendance.services import generate_kiosk_pin
from apps.billing.models import TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.feedback.services import create_default_feedback_form
from apps.pipelines.services import seed_default_pipelines
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for trainer lead/trial E2E."

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
                "Prepared trainer lead/trial E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, schedule_id={fixture['schedule_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"trainer-lead-trial-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerLeadTrialE2E-{unique}-pass"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Trainer Lead Trial E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        trial_day = club_localdate(club) + timedelta(days=1)
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Lead Trial E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Lead Trial Hall {fixture_id}",
            address="Trainer lead trial E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="LeadTrial",
            last_name="Trainer",
            phone=f"+79001{numeric_suffix}01",
            user=trainer_user,
        )

        other_trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="other-trainer",
            password=None,
        )
        ClubMembership.objects.create(
            user=other_trainer_user,
            club=club,
            role=ClubMembership.Role.TRAINER,
        )
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="Other",
            last_name="Trainer",
            phone=f"+79001{numeric_suffix}02",
            user=other_trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Lead Trial Free E2E {fixture_id}",
            slug=f"lead-trial-free-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1200.00"),
            trial_free=True,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("0.00"),
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=trial_day.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Lead Trial Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=trial_day,
            is_active=True,
        )

        hidden_lead = Student.objects.create(
            club=club,
            first_name="Hidden",
            last_name="Lead",
            phone=f"+79002{numeric_suffix}01",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=other_trainer,
        )
        feedback_form = create_default_feedback_form(club_id=club.id)
        seed_default_pipelines(club_id=club.id)
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        trial_at = timezone.make_aware(
            timezone.datetime.combine(trial_day, time(18, 0)),
            timezone=club_zoneinfo(club),
        )
        lead_phone = f"+79003{numeric_suffix}01"

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "phone_suffix": lead_phone[-4:],
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "other_trainer_id": other_trainer.id,
            "hidden_lead_id": hidden_lead.id,
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "new_lead": {
                "first_name": "LeadTrial",
                "last_name": "",
                "phone": lead_phone,
                "is_child": False,
                "source": Student.Source.OTHER,
            },
            "trial": {
                "trial_date": trial_at.isoformat(),
                "checkin_date": trial_day.isoformat(),
                "time": "18:00",
            },
            "expected": {
                "group_name": schedule.group_name,
                "visible_lead_count_for_trainer": 1,
                "post_trial_task_type": "post_trial",
                "pipeline_type": "follow_up",
                "feedback_form_id": feedback_form.id,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-lead-trial-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
