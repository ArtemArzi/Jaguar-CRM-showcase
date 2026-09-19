from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubSettings
from apps.feedback.models import FeedbackForm, FeedbackQuestion
from apps.students.models import Student
from apps.students.parent_services import create_parent_invite


class Command(BaseCommand):
    help = "Prepare an isolated fixture for parent invite acceptance and child feedback E2E."

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
                "Prepared parent invite feedback E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, child_id={fixture['child']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"parent-invite-feedback-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        parent_password = f"ParentInviteFeedback-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Parent Invite Feedback E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Parent Invite Feedback E2E",
        )

        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=parent_password,
        )
        child = Student.objects.create(
            club=club,
            first_name="Invite",
            last_name="Child",
            phone=f"+15554{phone_seed}1",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=None,
        )
        invite = create_parent_invite(club_id=club.id, student_id=child.id)

        form = FeedbackForm.objects.create(
            club=club,
            name=f"Parent Feedback Proof {fixture_id}",
            trigger_type=FeedbackForm.TriggerType.TRIAL,
            is_active=True,
        )
        yes_no_question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type=FeedbackQuestion.QuestionType.YES_NO,
            text=f"Ребёнку понравилась тренировка {fixture_id}?",
            order=1,
            is_required=True,
        )
        text_question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type=FeedbackQuestion.QuestionType.TEXT,
            text=f"Комментарий родителя {fixture_id}",
            order=2,
            is_required=False,
        )
        text_value = f"PARENT_FEEDBACK_E2E_TEXT_{unique}"

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "child": {
                "student_id": child.id,
                "name": str(child),
            },
            "invite": {
                "id": invite.id,
                "token": str(invite.token),
                "expires_at": invite.expires_at.isoformat(),
            },
            "form_id": form.id,
            "questions": {
                "yes_no_id": yes_no_question.id,
                "text_id": text_question.id,
            },
            "expected": {
                "club_name": club.name,
                "form_name": form.name,
                "yes_no_question": yes_no_question.text,
                "text_question": text_question.text,
                "bool_value": True,
                "text_value": text_value,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@parent-invite-feedback-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
