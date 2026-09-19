from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.clubs.models import Club, ClubSettings


def _assert_isolated_e2e_database() -> None:
    raw_url = os.environ.get("REAL_STACK_E2E_DATABASE_URL", "")
    parsed = urlparse(raw_url)
    database_name = parsed.path.removeprefix("/")
    if (
        connection.vendor != "postgresql"
        or parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or bool(parsed.query)
        or not re.search(r"(?:^|[_-])(test|e2e)(?:[_-]|$)", database_name, re.IGNORECASE)
    ):
        raise CommandError("isolated_e2e_database_required")

    try:
        expected_port = parsed.port or 5432
    except ValueError as exc:
        raise CommandError("isolated_e2e_database_required") from exc
    connection.ensure_connection()
    connection_info = getattr(connection.connection, "info", None)
    active_database = str(getattr(connection_info, "dbname", "") or "")
    active_host = str(getattr(connection_info, "host", "") or "")
    active_port = int(getattr(connection_info, "port", 0) or 0)
    if (
        active_database != database_name
        or active_host not in {"127.0.0.1", "localhost", "::1"}
        or active_port != expected_port
    ):
        raise CommandError("isolated_e2e_database_required")


class Command(BaseCommand):
    help = "Disable the unified journey for one validated isolated E2E fixture club."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to a student/parent self-booking fixture JSON.",
        )

    def handle(self, *args, **options) -> None:
        _assert_isolated_e2e_database()
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        unified = fixture.get("unified")
        club_id = unified.get("club_id") if isinstance(unified, dict) else None
        if not isinstance(club_id, int) or club_id <= 0:
            raise CommandError("fixture must contain unified.club_id")

        with transaction.atomic():
            club = Club.objects.select_for_update().filter(id=club_id).first()
            if club is None:
                raise CommandError("fixture club does not exist")
            settings_row = ClubSettings.objects.select_for_update().filter(club=club).first()
            if settings_row is None:
                raise CommandError("fixture club settings do not exist")
            previous_enabled = settings_row.unified_client_journey_enabled
            if previous_enabled:
                settings_row.unified_client_journey_enabled = False
                settings_row.save(
                    update_fields=["unified_client_journey_enabled", "updated_at"]
                )

        self.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "club_id": club_id,
                    "previous_enabled": previous_enabled,
                    "enabled": False,
                },
                sort_keys=True,
            )
        )

    @staticmethod
    def _load_fixture(path: Path) -> dict:
        if not path.exists():
            raise CommandError("fixture file not found")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError("fixture file is not valid JSON") from exc
        if not isinstance(fixture, dict) or not fixture.get("fixture_id"):
            raise CommandError("fixture identity is missing")
        return fixture
