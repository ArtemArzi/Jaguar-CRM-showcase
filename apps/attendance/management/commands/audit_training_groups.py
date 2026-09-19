from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.services.training_group_reconciliation import audit_training_groups
from apps.clubs.models import Club


class Command(BaseCommand):
    help = "Report aggregate-only Training Group reconciliation readiness."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--club-id", type=int)
        scope.add_argument("--all-clubs", action="store_true")
        parser.add_argument("--fail-on-invalid", action="store_true")

    def handle(self, *args, **options):
        if options["all_clubs"]:
            clubs = Club.objects.order_by("id")
        else:
            clubs = Club.objects.filter(id=options["club_id"])
            if not clubs.exists():
                raise CommandError("Unknown club ID.")

        reports = [audit_training_groups(club=club) for club in clubs]
        invalid_club_ids = [report["club_id"] for report in reports if not report["valid"]]
        report = {
            "club_count": len(reports),
            "clubs": reports,
            "invalid_club_ids": invalid_club_ids,
            "valid": not invalid_club_ids,
        }
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_invalid"] and invalid_club_ids:
            raise CommandError("Training Group audit found invalid aggregate state.")
