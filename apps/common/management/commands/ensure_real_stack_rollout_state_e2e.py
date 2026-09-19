from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import TrainingGroupRolloutState
from apps.clubs.models import Club


class Command(BaseCommand):
    help = "Ensure real-stack fixture clubs have their required off rollout state."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to a prepared real-stack fixture JSON.")
        parser.add_argument(
            "--all-clubs",
            action="store_true",
            help=(
                "Normalize every club in the database. Use only after the runner has "
                "validated an isolated E2E database target."
            ),
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        club_ids = self._fixture_club_ids(fixture)
        if options["all_clubs"]:
            club_ids = sorted(
                {
                    *club_ids,
                    *Club.objects.order_by("id").values_list("id", flat=True),
                }
            )
        state_evidence = []
        for club_id in club_ids:
            club = Club.objects.filter(id=club_id).first()
            if club is None:
                raise CommandError("fixture club does not exist")
            state, created = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
                club=club,
                defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
            )
            state_evidence.append({"club_id": club.id, "created": created, "mode": state.mode})
        evidence = state_evidence[0] if len(state_evidence) == 1 else {"clubs": state_evidence}
        self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))

    def _fixture_club_ids(self, fixture: dict) -> list[int]:
        club_id = fixture.get("club_id")
        if isinstance(club_id, int) and club_id > 0:
            return [club_id]
        club_ids = [
            side.get("club_id")
            for key in ("club_a", "club_b")
            if isinstance((side := fixture.get(key)), dict)
        ]
        if not club_ids or any(not isinstance(value, int) or value <= 0 for value in club_ids):
            raise CommandError("fixture must contain a positive integer club_id or valid club_a and club_b ids")
        return club_ids

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc
