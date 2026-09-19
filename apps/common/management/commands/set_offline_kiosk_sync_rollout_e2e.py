from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import TrainingGroupRolloutEvent, TrainingGroupRolloutState
from apps.attendance.services.training_groups import (
    approved_reconciliation_rollout_gate_digest,
    transition_training_group_rollout_for_owner,
)
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError


class Command(BaseCommand):
    help = "Set the isolated offline kiosk E2E fixture rollout mode."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to offline kiosk fixture JSON.")
        parser.add_argument(
            "--mode",
            required=True,
            choices=(TrainingGroupRolloutState.Mode.RECONCILING, TrainingGroupRolloutState.Mode.SHADOW),
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        club_id = fixture.get("club_id")
        if not isinstance(club_id, int) or club_id <= 0:
            raise CommandError("fixture must contain a positive integer club_id")
        club = Club.objects.filter(id=club_id).first()
        if club is None:
            raise CommandError("fixture club does not exist")
        actor_id = fixture.get("rollout_actor_id")
        if not isinstance(actor_id, int) or actor_id <= 0:
            raise CommandError("fixture must contain a positive integer rollout_actor_id")
        state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
            club=club,
            defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
        )
        previous_mode = state.mode
        target_mode = options["mode"]
        try:
            transition = transition_training_group_rollout_for_owner(
                club_id=club.id,
                target_mode=target_mode,
                actor_id=actor_id,
                rationale="Isolated offline kiosk retry E2E rollout transition.",
                idempotency_key=f"{fixture['fixture_id']}-offline-{target_mode}",
                rollout_gate_digest=approved_reconciliation_rollout_gate_digest(club_id=club.id),
            )
        except BusinessLogicError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        event = TrainingGroupRolloutEvent.objects.for_club(club).get(
            idempotency_key=f"{fixture['fixture_id']}-offline-{target_mode}",
        )
        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "club_id": club.id,
                    "previous_mode": previous_mode,
                    "mode": transition.mode,
                    "event_id": event.id,
                    "event_previous_mode": event.previous_mode,
                    "event_new_mode": event.new_mode,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc
