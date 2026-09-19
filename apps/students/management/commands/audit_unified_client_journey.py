from __future__ import annotations

import json
import logging

from django.core.management.base import BaseCommand, CommandError

from apps.clubs.models import Club
from apps.students.journey_readiness import audit_unified_client_journey_readiness

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Audit aggregate rollout readiness for the unified client journey."

    def add_arguments(self, parser) -> None:
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--club-id", type=int)
        scope.add_argument("--all-clubs", action="store_true")
        parser.add_argument("--fail-on-invalid", action="store_true")

    def handle(self, *args, **options) -> None:
        club_id = options.get("club_id")
        if club_id is not None and not Club.objects.filter(id=club_id).exists():
            raise CommandError("club_not_found")

        report = audit_unified_client_journey_readiness(
            club_ids=(club_id,) if club_id is not None else None,
        )
        logger.info(
            "unified_client_journey_readiness_audited",
            extra={
                "audited_club_count": report["audited_club_count"],
                "invalid_club_count": report["invalid_club_count"],
                "is_ready": report["is_ready"],
            },
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_invalid"] and not report["is_ready"]:
            raise CommandError("unified_client_journey_readiness_invalid")
