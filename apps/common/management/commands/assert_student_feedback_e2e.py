from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club
from apps.feedback.models import FeedbackAnswer, FeedbackForm, FeedbackQuestion, FeedbackResponse
from apps.feedback.selectors import get_active_form


class Command(BaseCommand):
    help = "Assert student feedback submit, duplicate safety, and active form state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_student_feedback_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for feedback response before failing.",
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
                    raise CommandError(f"student feedback E2E assertion failed: {exc}") from exc
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

        required = {"fixture_id", "club_id", "student", "form_id", "questions", "expected"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        form = FeedbackForm.objects.for_club(club).get(id=int(fixture["form_id"]))
        active_form = get_active_form(club=club)
        response = self._response_evidence(club=club, form=form, fixture=fixture)
        answers = self._answer_evidence(club=club, response_id=response["id"], fixture=fixture)

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "active_form": {
                "id": active_form.id if active_form else None,
                "is_active": form.is_active,
            },
            "response": response,
            "answers": answers,
        }

    def _response_evidence(self, *, club: Club, form: FeedbackForm, fixture: dict) -> dict:
        responses = list(
            FeedbackResponse.objects.for_club(club)
            .filter(form=form, student_id=int(fixture["student"]["student_id"]))
            .order_by("id")
        )
        if not responses:
            raise CommandError("feedback response not found")
        if len(responses) != 1:
            raise CommandError(f"feedback response count mismatch: expected 1, got {len(responses)}")

        return {
            "id": responses[0].id,
            "count": len(responses),
            "duplicate_safe": True,
            "submitted_at": responses[0].submitted_at.isoformat(),
        }

    def _answer_evidence(self, *, club: Club, response_id: int, fixture: dict) -> dict:
        answers = {
            answer.question_id: answer
            for answer in FeedbackAnswer.objects.for_club(club)
            .filter(response_id=response_id)
            .select_related("question")
        }
        questions = fixture["questions"]
        expected = fixture["expected"]
        rating_answer = self._get_answer(
            answers=answers,
            question_id=int(questions["rating_id"]),
            question_type=FeedbackQuestion.QuestionType.RATING,
        )
        yes_no_answer = self._get_answer(
            answers=answers,
            question_id=int(questions["yes_no_id"]),
            question_type=FeedbackQuestion.QuestionType.YES_NO,
        )
        text_answer = self._get_answer(
            answers=answers,
            question_id=int(questions["text_id"]),
            question_type=FeedbackQuestion.QuestionType.TEXT,
        )

        if rating_answer.rating_value != int(expected["rating_value"]):
            raise CommandError(
                "rating answer mismatch: "
                f"expected {expected['rating_value']}, got {rating_answer.rating_value}"
            )
        if yes_no_answer.bool_value is not bool(expected["bool_value"]):
            raise CommandError(
                "yes/no answer mismatch: "
                f"expected {expected['bool_value']}, got {yes_no_answer.bool_value}"
            )
        if text_answer.text_value != expected["text_value"]:
            raise CommandError("text answer mismatch")

        return {
            "rating_value": rating_answer.rating_value,
            "bool_value": yes_no_answer.bool_value,
            "text_value": text_answer.text_value,
        }

    def _get_answer(
        self,
        *,
        answers: dict[int, FeedbackAnswer],
        question_id: int,
        question_type: str,
    ) -> FeedbackAnswer:
        answer = answers.get(question_id)
        if not answer:
            raise CommandError(f"missing feedback answer for question {question_id}")
        if answer.question.question_type != question_type:
            raise CommandError(
                f"feedback answer question {question_id} type mismatch: "
                f"expected {question_type}, got {answer.question.question_type}"
            )
        return answer
