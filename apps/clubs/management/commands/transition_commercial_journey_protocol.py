from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.clubs.commercial_journey import (
    commercial_journey_activation_readiness,
    transition_commercial_journey_protocol,
)
from apps.clubs.models import Club, ClubSettings
from apps.common.exceptions import BusinessLogicError


class Command(BaseCommand):
    help = "Dry-run or apply one tenant commercial-journey protocol transition."

    def add_arguments(self, parser):
        parser.add_argument("--club-id", type=int, required=True)
        parser.add_argument(
            "--to",
            dest="target_version",
            choices=ClubSettings.CommercialJourneyProtocol.values,
            required=True,
        )
        parser.add_argument("--rationale", required=True)
        parser.add_argument("--idempotency-key", required=True)
        parser.add_argument("--actor-user-id", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        club = Club.objects.filter(id=options["club_id"]).first()
        if club is None:
            raise CommandError("club not found")
        if not options["apply"]:
            readiness = commercial_journey_activation_readiness(club=club)
            self.stdout.write(
                json.dumps(
                    {
                        "applied": False,
                        "club_id": club.id,
                        "current_version": ClubSettings.objects.get(
                            club_id=club.id
                        ).commercial_journey_protocol_version,
                        "target_version": options["target_version"],
                        "readiness": readiness,
                    },
                    sort_keys=True,
                )
            )
            return
        try:
            result = transition_commercial_journey_protocol(
                club_id=club.id,
                target_version=options["target_version"],
                rationale=options["rationale"],
                idempotency_key=options["idempotency_key"],
                actor_user_id=options["actor_user_id"],
            )
        except BusinessLogicError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(
            json.dumps(
                {
                    "applied": not result.replayed,
                    "club_id": club.id,
                    "previous_version": result.receipt.previous_version,
                    "target_version": result.receipt.target_version,
                    "current_version": result.effective_version,
                    "state_matches_receipt_target": (
                        result.effective_version == result.receipt.target_version
                    ),
                    "receipt_id": result.receipt.id,
                    "replayed": result.replayed,
                },
                sort_keys=True,
            )
        )
