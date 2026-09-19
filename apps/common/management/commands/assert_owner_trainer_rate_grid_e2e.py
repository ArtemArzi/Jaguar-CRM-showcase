from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin
from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import calculate_salary
from apps.billing.models import TrainingType
from apps.clubs.models import Club, ClubMembership
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import Trainer, TrainerEarning, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Assert owner trainer rate grid E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_owner_trainer_rate_grid_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for rate-grid state before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"owner trainer rate grid E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {
            "fixture_id",
            "club_id",
            "control_club_id",
            "owner",
            "location",
            "training_types",
            "seeded_trainer",
            "checkin_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        control_club_id = int(fixture["control_club_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        location_id = int(fixture["location"]["location_id"])
        group_type_id = int(fixture["training_types"]["group"]["training_type_id"])
        personal_type_id = int(fixture["training_types"]["personal"]["training_type_id"])
        expected = fixture["expected"]

        new_trainer = (
            Trainer.objects.for_club(club)
            .filter(
                first_name=expected["new_trainer_first_name"],
                last_name=expected["new_trainer_last_name"],
            )
            .first()
        )
        if new_trainer is None:
            raise CommandError("UI-created trainer not found")
        if Trainer.objects.for_club(control_club_id).filter(
            first_name=expected["new_trainer_first_name"],
            last_name=expected["new_trainer_last_name"],
        ).exists():
            raise CommandError("UI-created trainer leaked into control club")
        if not TrainerLocation.objects.for_club(club).filter(
            trainer=new_trainer,
            location_id=location_id,
        ).exists():
            raise CommandError("UI-created trainer location missing")

        group_rate = self._get_rate(
            club=club,
            trainer_id=new_trainer.id,
            location_id=location_id,
            training_type_id=group_type_id,
            label="UI-created trainer group rate",
        )
        personal_rate = self._get_rate(
            club=club,
            trainer_id=new_trainer.id,
            location_id=location_id,
            training_type_id=personal_type_id,
            label="UI-created trainer personal rate",
        )
        self._assert_decimal("UI-created trainer group rate", group_rate.percent, expected["new_trainer_group_rate"])
        self._assert_decimal(
            "UI-created trainer personal rate",
            personal_rate.percent,
            expected["new_trainer_personal_rate"],
        )

        seeded_trainer_id = int(fixture["seeded_trainer"]["trainer_id"])
        seeded_trainer = Trainer.objects.for_club(club).get(id=seeded_trainer_id)
        missing = self._missing_active_rate_pairs(club=club, trainer=seeded_trainer)
        if missing:
            raise CommandError(f"seeded trainer has missing rates: {len(missing)}")

        checkin = (
            Checkin.objects.for_club(club)
            .select_related("training_type", "subscription__tariff")
            .get(id=int(fixture["checkin_id"]))
        )
        try:
            upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club.id)
            calculate_salary(checkin.id, club.id)
        except BusinessLogicError as exc:
            raise CommandError(f"salary calculation failed: {exc.code}") from exc

        earning = TrainerEarning.objects.for_club(club).filter(checkin=checkin, cancelled=False).first()
        if earning is None:
            raise CommandError("salary earning not created")
        self._assert_decimal("salary rate", earning.rate_percent, expected["seeded_personal_rate"])
        self._assert_decimal("salary amount", earning.amount, expected["salary_amount"])

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "owner": {
                "user_id": owner_user_id,
                "membership_role": membership.role,
            },
            "new_trainer": {
                "trainer_id": new_trainer.id,
                "location_count": TrainerLocation.objects.for_club(club).filter(trainer=new_trainer).count(),
                "rate_count": TrainerRate.objects.for_club(club).filter(trainer=new_trainer).count(),
            },
            "seeded_trainer": {
                "trainer_id": seeded_trainer.id,
                "missing_rate_count": len(self._missing_active_rate_pairs(club=club, trainer=seeded_trainer)),
            },
            "salary": {
                "earning_id": earning.id,
                "rate_percent": self._money(earning.rate_percent),
                "amount": self._money(earning.amount),
            },
        }

    def _get_rate(
        self,
        *,
        club,
        trainer_id: int,
        location_id: int,
        training_type_id: int,
        label: str,
    ) -> TrainerRate:
        rate = (
            TrainerRate.objects.for_club(club)
            .filter(
                trainer_id=trainer_id,
                location_id=location_id,
                training_type_id=training_type_id,
            )
            .first()
        )
        if rate is None:
            raise CommandError(f"{label} missing")
        return rate

    def _missing_active_rate_pairs(self, *, club, trainer: Trainer) -> list[tuple[int, int]]:
        trainer_location_ids = list(
            TrainerLocation.objects.for_club(club)
            .filter(trainer=trainer)
            .values_list("location_id", flat=True)
        )
        training_types = list(
            TrainingType.objects.for_club(club.id)
            .filter(is_active=True)
        )
        training_type_ids = [training_type.id for training_type in training_types]
        existing_pairs = set(
            TrainerRate.objects.for_club(club)
            .filter(
                trainer=trainer,
                location_id__in=trainer_location_ids,
                training_type_id__in=training_type_ids,
            )
            .values_list("location_id", "training_type_id")
        )
        existing_training_type_ids = {training_type_id for _, training_type_id in existing_pairs}
        missing: list[tuple[int, int]] = []
        for location_id in trainer_location_ids:
            for training_type in training_types:
                if (location_id, training_type.id) in existing_pairs:
                    continue
                if (
                    training_type.kind == TrainingType.Kind.GROUP
                    and training_type.id in existing_training_type_ids
                ):
                    continue
                missing.append((location_id, training_type.id))
        return missing

    def _assert_decimal(self, label: str, actual, expected: str) -> None:
        if Decimal(str(actual)).quantize(Decimal("0.01")) != Decimal(expected).quantize(Decimal("0.01")):
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")

    def _money(self, value) -> str:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
