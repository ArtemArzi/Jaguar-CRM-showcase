import json
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.common.management.commands.prepare_trainer_settlements_e2e import load_fixture
from apps.trainers.services import correct_trainer_earning


class Command(BaseCommand):
    help = "Run one real historical recipient correction on the owned synthetic fixture."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)

    def handle(self, *args, **options):
        data = load_fixture(options["fixture"])
        correct_trainer_earning(
            club_id=data["club_id"],
            earning_id=data["historical_earning_id"],
            target_trainer_id=data["second_trainer_id"],
            actor_user_id=data["owner"]["id"],
            amount=Decimal("500"),
            reason="Историческая сверка E2E",
            idempotency_key="e2e-correction",
        )
        self.stdout.write(json.dumps({"ok": True}))
