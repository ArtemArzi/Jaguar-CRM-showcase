from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.attendance.services.checkin import upsert_salary_snapshot_for_checkin
from apps.attendance.tasks import calculate_salary
from apps.billing.models import Subscription, TrainingType
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment, TrainerPayrollPeriodClose
from apps.trainers.services import correct_trainer_earning


class Command(BaseCommand):
    help = "Assert trainer payroll close/correction E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_payroll_close_correction_e2e.",
        )
        parser.add_argument(
            "--stage",
            choices=["corrected", "closed"],
            default="closed",
            help="Assertion stage: corrected checks first correction; closed checks period close and block.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for side effects before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        stage = options["stage"]
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture, stage=stage)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"trainer payroll close/correction E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
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
            "owner",
            "trainer",
            "target_trainer",
            "period",
            "earnings",
            "salary_mutation",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict, *, stage: str) -> dict:
        club_id = int(fixture["club_id"])
        source_trainer_id = int(fixture["trainer"]["trainer_id"])
        target_trainer_id = int(fixture["target_trainer"]["trainer_id"])
        corrected_earning_id = int(fixture["earnings"]["corrected_id"])
        blocked_earning_id = int(fixture["earnings"]["blocked_id"])
        period_start = date.fromisoformat(fixture["period"]["date_from"])
        period_end = date.fromisoformat(fixture["period"]["date_to"])

        debit = self._get_manual_adjustment(
            club_id=club_id,
            earning_id=corrected_earning_id,
            trainer_id=source_trainer_id,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
        )
        credit = self._get_manual_adjustment(
            club_id=club_id,
            earning_id=corrected_earning_id,
            trainer_id=target_trainer_id,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
        )
        if debit.payable_amount_delta != -credit.payable_amount_delta:
            raise CommandError("manual correction debit/credit amount mismatch")

        evidence = {
            "ok": True,
            "stage": stage,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "correction": {
                "debit_id": debit.id,
                "credit_id": credit.id,
                "amount": str(credit.payable_amount_delta),
            },
        }
        if stage == "corrected":
            return evidence

        close = TrainerPayrollPeriodClose.objects.for_club(club_id).filter(
            period_start=period_start,
            period_end=period_end,
        ).first()
        if close is None:
            raise CommandError("payroll close not found")
        expected = fixture["expected"]
        if str(close.salary_total_snapshot) != expected["salary_total_snapshot"]:
            raise CommandError(
                "payroll close salary total mismatch: "
                f"expected {expected['salary_total_snapshot']}, got {close.salary_total_snapshot}"
            )
        source_snapshot = close.trainer_totals_snapshot.get(str(source_trainer_id))
        target_snapshot = close.trainer_totals_snapshot.get(str(target_trainer_id))
        if not source_snapshot or not target_snapshot:
            raise CommandError("payroll close trainer snapshots missing")
        if source_snapshot.get("total") != expected["source_trainer_total_after_correction"]:
            raise CommandError("payroll close source trainer total mismatch")
        if target_snapshot.get("total") != expected["target_trainer_total_after_correction"]:
            raise CommandError("payroll close target trainer total mismatch")

        self._assert_blocked_correction(
            club_id=club_id,
            earning_id=blocked_earning_id,
            target_trainer_id=target_trainer_id,
            actor_user_id=int(fixture["owner"]["user_id"]),
            reason=expected["blocked_reason"],
        )
        blocked_salary = self._assert_blocked_salary_calculation(fixture)

        evidence.update(
            {
                "close": {
                    "id": close.id,
                    "period_start": close.period_start.isoformat(),
                    "period_end": close.period_end.isoformat(),
                    "salary_total_snapshot": str(close.salary_total_snapshot),
                    "source_trainer_total": source_snapshot.get("total"),
                    "target_trainer_total": target_snapshot.get("total"),
                },
                "blocked_correction": {
                    "earning_id": blocked_earning_id,
                    "code": "payroll_period_closed",
                },
                "blocked_salary_calculation": blocked_salary,
            }
        )
        return evidence

    def _get_manual_adjustment(
        self,
        *,
        club_id: int,
        earning_id: int,
        trainer_id: int,
        direction: str,
    ) -> TrainerEarningAdjustment:
        adjustment = TrainerEarningAdjustment.objects.for_club(club_id).filter(
            source_checkin__trainerearning__id=earning_id,
            trainer_id=trainer_id,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            direction=direction,
            affects_payroll=True,
        ).first()
        if adjustment is None:
            raise CommandError(f"manual correction {direction} adjustment not found")
        return adjustment

    def _assert_blocked_correction(
        self,
        *,
        club_id: int,
        earning_id: int,
        target_trainer_id: int,
        actor_user_id: int,
        reason: str,
    ) -> None:
        try:
            correct_trainer_earning(
                club_id=club_id,
                earning_id=earning_id,
                target_trainer_id=target_trainer_id,
                reason=reason,
                actor_user_id=actor_user_id,
                idempotency_key=f"e2e-blocked-after-close-{earning_id}",
            )
        except BusinessLogicError as exc:
            if exc.code != "payroll_period_closed":
                raise CommandError(f"unexpected blocked correction error code: {exc.code}") from exc
            return
        raise CommandError("blocked earning correction unexpectedly succeeded")

    def _assert_blocked_salary_calculation(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        payload = fixture["salary_mutation"]
        checkin = self._get_or_create_salary_mutation_checkin(club_id=club_id, payload=payload)
        upsert_salary_snapshot_for_checkin(checkin=checkin, club_id=club_id)

        try:
            calculate_salary(checkin.id, club_id)
        except BusinessLogicError as exc:
            if exc.code != payload["expected_error_code"]:
                raise CommandError(f"unexpected blocked salary calculation error code: {exc.code}") from exc
        else:
            raise CommandError("blocked salary calculation unexpectedly succeeded")

        if TrainerEarning.objects.for_club(club_id).filter(checkin=checkin).exists():
            raise CommandError("blocked salary calculation unexpectedly created trainer earning")

        return {
            "checkin_id": checkin.id,
            "code": payload["expected_error_code"],
            "earning_created": False,
        }

    def _get_or_create_salary_mutation_checkin(self, *, club_id: int, payload: dict) -> Checkin:
        existing = Checkin.objects.for_club(club_id).filter(
            student_id=payload["student_id"],
            schedule_id=payload["schedule_id"],
            date=date.fromisoformat(payload["date"]),
            source=Checkin.Source.MANUAL,
        ).first()
        if existing is not None:
            return existing

        student = Student.objects.for_club(club_id).get(id=payload["student_id"])
        schedule = Schedule.objects.for_club(club_id).select_related(
            "trainer",
            "location",
            "training_type",
        ).get(id=payload["schedule_id"])
        subscription = Subscription.objects.for_club(club_id).get(id=payload["subscription_id"])
        training_type = TrainingType.objects.for_club(club_id).get(id=payload["training_type_id"])
        return Checkin.objects.create(
            club_id=club_id,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=schedule.trainer,
            location=schedule.location,
            subscription=subscription,
            date=date.fromisoformat(payload["date"]),
            source=Checkin.Source.MANUAL,
            is_debt=False,
        )
