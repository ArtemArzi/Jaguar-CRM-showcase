from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import KioskDevice
from apps.attendance.services import deactivate_kiosk


class Command(BaseCommand):
    help = "Deactivate the kiosk device for a prepared kiosk negative E2E fixture."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_kiosk_negative_e2e.")

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        club_id = int(fixture["club_id"])
        deactivate_kiosk(club_id=club_id)
        evidence = {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "club_id": club_id,
            "active_kiosk_count": KioskDevice.objects.filter(club_id=club_id, is_active=True).count(),
        }
        self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc
        required = {"fixture_id", "club_id"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture
