from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.clubs.models import Club
from apps.feedback.models import FeedbackForm
from apps.feedback.selectors import get_active_form


class Command(BaseCommand):
    help = "Deactivate the active feedback form from a student feedback E2E fixture."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_student_feedback_e2e.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        club = Club.objects.get(id=int(fixture["club_id"]))
        form = FeedbackForm.objects.for_club(club).get(id=int(fixture["form_id"]))
        form.is_active = False
        form.save(update_fields=["is_active", "updated_at"])

        active_form = get_active_form(club=club)
        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "fixture_id": fixture["fixture_id"],
                    "active_form": {
                        "id": active_form.id if active_form else None,
                        "deactivated_form_id": form.id,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {"fixture_id", "club_id", "form_id"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture
