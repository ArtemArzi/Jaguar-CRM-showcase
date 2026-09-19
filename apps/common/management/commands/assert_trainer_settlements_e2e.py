import json
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.clubs.models import Club
from apps.common.management.commands.prepare_trainer_settlements_e2e import load_fixture
from apps.trainers.models import TrainerSettlementEntry, TrainerSettlementReconciliation, TrainerSettlementResolution
from apps.trainers.settlement_selectors import get_trainer_settlement_summary


class Command(BaseCommand):
    help = "Assert actual settlement amounts and the second trainer reconciliation independently of the UI."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--phase", choices=["paid", "final"], required=True)

    def handle(self, *args, **options):
        data = load_fixture(options["fixture"])
        day = date.fromisoformat(data["today"])
        club = Club.objects.get(id=data["club_id"])
        summary = get_trainer_settlement_summary(club=club, trainer_id=data["trainer_id"], date_from=day, date_to=day)
        entries = TrainerSettlementEntry.objects.for_club(club).filter(trainer_id=data["trainer_id"])
        evidence = {
            "payout_once": entries.filter(kind="payout").count() == 1,
            "paid": summary["paid"] == Decimal("300"),
            "today_earned": summary["earned"] == Decimal("500"),
        }
        if options["phase"] == "paid":
            evidence["balance"] = summary["balance"] == Decimal("1200")
        else:
            evidence.update(
                balance=summary["balance"] == Decimal("1000"),
                reversal=entries.filter(kind="payout_reversal").count() == 1,
                correction=entries.filter(kind="opening_correction").count() == 1,
                resolution=TrainerSettlementResolution.objects.for_club(club).count() == 1,
                second_unresolved=TrainerSettlementReconciliation.objects.for_club(club)
                .filter(trainer_id=data["second_trainer_id"], resolution__isnull=True)
                .count()
                == 1,
            )
        if not all(evidence.values()):
            raise CommandError(json.dumps({"ok": False, "checks": evidence}))
        self.stdout.write(json.dumps({"ok": True, "checks": evidence}))
