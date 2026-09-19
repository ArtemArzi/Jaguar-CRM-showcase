from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.feedback.models import FeedbackForm, FeedbackQuestion
from apps.students.models import Student


class Command(BaseCommand):
    help = "Prepare an isolated fixture for student feedback E2E."

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
                "Prepared student feedback E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, form_id={fixture['form_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"student-feedback-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        student_password = f"FeedbackStudent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Student Feedback E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Student Feedback E2E",
        )

        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=student_password,
        )
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        student = Student.objects.create(
            club=club,
            first_name="Feedback",
            last_name="Student",
            phone=f"+15559{phone_seed}1",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
        )

        form = FeedbackForm.objects.create(
            club=club,
            name=f"Feedback Trial Proof {fixture_id}",
            trigger_type=FeedbackForm.TriggerType.TRIAL,
            is_active=True,
        )
        rating_question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type=FeedbackQuestion.QuestionType.RATING,
            text=f"Оцените тренировку от 1 до 5 {fixture_id}",
            order=1,
            is_required=True,
        )
        yes_no_question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type=FeedbackQuestion.QuestionType.YES_NO,
            text=f"Хотите продолжить занятия {fixture_id}?",
            order=2,
            is_required=True,
        )
        text_question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type=FeedbackQuestion.QuestionType.TEXT,
            text=f"Комментарий для команды {fixture_id}",
            order=3,
            is_required=False,
        )
        text_value = f"FEEDBACK_E2E_TEXT_{unique}"

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "student": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
                "student_id": student.id,
                "name": str(student),
            },
            "form_id": form.id,
            "questions": {
                "rating_id": rating_question.id,
                "yes_no_id": yes_no_question.id,
                "text_id": text_question.id,
            },
            "expected": {
                "form_name": form.name,
                "rating_question": rating_question.text,
                "yes_no_question": yes_no_question.text,
                "text_question": text_question.text,
                "rating_value": 5,
                "bool_value": True,
                "text_value": text_value,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@student-feedback-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
