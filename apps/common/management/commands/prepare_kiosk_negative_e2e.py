from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule, ScheduleEnrollment, TrainingGroupRolloutState
from apps.attendance.services import enroll_student_in_schedule, generate_kiosk_pin
from apps.attendance.services.training_group_reconciliation import (
    apply_training_group_reconciliation,
    audit_training_groups,
    build_training_group_reconciliation_preview,
)
from apps.attendance.services.training_groups import transition_training_group_rollout_for_owner
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for kiosk negative E2E probes."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        child_b_schedule_id = fixture["shared_guardian"]["child_b"]["schedule_id"]
        output_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared kiosk negative E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, child_b_schedule_id={child_b_schedule_id})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"kiosk-negative-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        shared_suffix = "7401"
        frozen_suffix = "7402"
        blocked_suffix = "7403"

        club = Club.objects.create(
            name=f"Jaguar Kiosk Negative E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Kiosk Negative E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Kiosk Negative Hall {fixture_id}",
            address="Kiosk negative E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer")
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Kiosk",
            last_name="Trainer",
            phone=f"+155510{unique[:7]}",
            user=trainer_user,
        )

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Muay Thai {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Kiosk Group {fixture_id}",
            slug=f"kiosk-negative-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1500.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Kiosk Negative 5-Pack {fixture_id}",
            price=Decimal("10000.00"),
            trainings_limit=10,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        shared_child_a_group_name = f"Shared Guardian Child A {fixture_id}"
        shared_child_a_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=shared_child_a_group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        shared_child_b_group_name = f"Shared Guardian Child B {fixture_id}"
        shared_child_b_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=shared_child_b_group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )

        shared_child_a = self._create_student(
            club=club,
            first_name="Shared",
            last_name="Child A",
            phone="",
            guardian_phone=f"+1555001{shared_suffix}",
            is_child=True,
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=shared_child_a_schedule,
            enrollment_status=ScheduleEnrollment.Status.ACTIVE,
            trainings_left_before=5,
            today=today,
        )
        shared_child_b = self._create_student(
            club=club,
            first_name="Shared",
            last_name="Child B",
            phone="",
            guardian_phone=f"+1555001{shared_suffix}",
            is_child=True,
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=shared_child_b_schedule,
            enrollment_status=ScheduleEnrollment.Status.ACTIVE,
            trainings_left_before=5,
            today=today,
        )
        frozen_student = self._create_student(
            club=club,
            first_name="Frozen",
            last_name="Student",
            phone=f"+1555003{frozen_suffix}",
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=shared_child_a_schedule,
            enrollment_status=ScheduleEnrollment.Status.FROZEN,
            trainings_left_before=5,
            today=today,
        )
        blocked_student = self._create_student(
            club=club,
            first_name="Blocked",
            last_name="Student",
            phone=f"+1555004{blocked_suffix}",
            tariff=tariff,
            grade_system=grade_system,
            grade=grade,
            schedule=None,
            enrollment_status=None,
            trainings_left_before=5,
            today=today,
            subscription_expired=True,
            student_status=Student.Status.AT_RISK,
        )
        shared_child_a_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=trainer_user.id,
            schedule_ids=[shared_child_a_schedule.id],
            canonical_name=shared_child_a_group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-shared-child-a-group",
        )
        shared_child_b_group = self._reconcile_additional_group_to_active(
            club=club,
            actor_user_id=trainer_user.id,
            schedule_ids=[shared_child_b_schedule.id],
            canonical_name=shared_child_b_group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-shared-child-b-group",
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "shared_guardian": {
                "phone_suffix": shared_suffix,
                "child_a": {
                    "student_id": shared_child_a["student_id"],
                    "subscription_id": shared_child_a["subscription_id"],
                    "schedule_id": shared_child_a_schedule.id,
                    "training_group_id": shared_child_a_group["training_group_id"],
                    "group_name": shared_child_a_group_name,
                    "name": "Shared Child A",
                },
                "child_b": {
                    "student_id": shared_child_b["student_id"],
                    "subscription_id": shared_child_b["subscription_id"],
                    "schedule_id": shared_child_b_schedule.id,
                    "training_group_id": shared_child_b_group["training_group_id"],
                    "group_name": shared_child_b_group_name,
                    "name": "Shared Child B",
                },
            },
            "frozen_phone_suffix": frozen_suffix,
            "blocked_phone_suffix": blocked_suffix,
            "frozen_student_id": frozen_student["student_id"],
            "blocked_student_id": blocked_student["student_id"],
            "training_type_id": training_type.id,
            "checkin_date": today.isoformat(),
            "expected": {
                "shared_child_trainings_left_before": 5,
                "shared_child_trainings_left_after": 4,
                "cascade_event_count": 7,
                "earning_count": 0,
            },
            "created_at": now.isoformat(),
        }

    def _create_student(
        self,
        *,
        club: Club,
        first_name: str,
        last_name: str,
        phone: str,
        guardian_phone: str = "",
        is_child: bool = False,
        tariff: Tariff,
        grade_system: GradeSystem,
        grade: Grade,
        schedule: Schedule | None,
        enrollment_status: str | None,
        trainings_left_before: int,
        today,
        subscription_expired: bool = False,
        student_status: str = Student.Status.ACTIVE,
    ) -> dict:
        student = Student.objects.create(
            club=club,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            guardian_phone=guardian_phone,
            email="",
            is_child=is_child,
            status=student_status,
            source=Student.Source.OTHER,
        )
        StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )
        subscription_expires_at = (
            timezone.now() - timedelta(days=1)
            if subscription_expired
            else timezone.now() + timedelta(days=30)
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=trainings_left_before,
            trainings_used=0,
            expires_at=subscription_expires_at,
            scope=tariff.scope,
            location=None,
        )
        if schedule is not None and enrollment_status is not None:
            enroll_student_in_schedule(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                status=enrollment_status,
                starts_on=today,
            )
        return {
            "student_id": student.id,
            "subscription_id": subscription.id,
        }

    def _reconcile_additional_group_to_active(
        self,
        *,
        club: Club,
        actor_user_id: int,
        schedule_ids: list[int],
        canonical_name: str,
        responsible_trainer_id: int,
        idempotency_prefix: str,
    ) -> dict:
        """Map one further exact schedule after the isolated fixture first reaches active."""
        state = TrainingGroupRolloutState.objects.for_club(club).get()
        if state.mode != TrainingGroupRolloutState.Mode.ACTIVE:
            raise CommandError("Additional canonical fixture group requires active rollout state.")

        transition_training_group_rollout_for_owner(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
            actor_id=actor_user_id,
            rationale="Isolated shared-guardian fixture containment transition.",
            idempotency_key=f"{idempotency_prefix}-containment",
            rollout_gate_digest="",
        )
        transition_training_group_rollout_for_owner(
            club_id=club.id,
            target_mode=TrainingGroupRolloutState.Mode.RECONCILING,
            actor_id=actor_user_id,
            rationale="Isolated shared-guardian fixture second-group mapping.",
            idempotency_key=f"{idempotency_prefix}-reconciling",
            rollout_gate_digest="",
        )
        preview = build_training_group_reconciliation_preview(
            club=club,
            schedule_ids=schedule_ids,
            canonical_name=canonical_name,
            responsible_trainer_id=responsible_trainer_id,
            start_dates=[],
        )
        if preview["conflicts"]:
            raise CommandError("Additional canonical fixture group preview has unresolved conflicts.")
        applied = apply_training_group_reconciliation(
            club=club,
            schedule_ids=schedule_ids,
            canonical_name=canonical_name,
            responsible_trainer_id=responsible_trainer_id,
            start_dates=[],
            preview_digest=preview["digest"],
            actor_user_id=actor_user_id,
            rationale="Isolated shared-guardian fixture second-group mapping.",
            idempotency_key=f"{idempotency_prefix}-apply",
        )
        if not audit_training_groups(club=club)["valid"]:
            raise CommandError("Additional canonical fixture group audit is not clean.")
        for target_mode, suffix in (
            (TrainingGroupRolloutState.Mode.SHADOW, "shadow"),
            (TrainingGroupRolloutState.Mode.ACTIVE, "active"),
        ):
            transition_training_group_rollout_for_owner(
                club_id=club.id,
                target_mode=target_mode,
                actor_id=actor_user_id,
                rationale="Isolated shared-guardian fixture second-group activation.",
                idempotency_key=f"{idempotency_prefix}-{suffix}",
                rollout_gate_digest=applied["rollout_gate_digest"],
            )
        state.refresh_from_db()
        if state.mode != TrainingGroupRolloutState.Mode.ACTIVE:
            raise CommandError("Additional canonical fixture group did not reach active rollout.")
        return {
            "training_group_id": applied["training_group_id"],
            "selected_schedule_ids": applied["selected_schedule_ids"],
            "rollout_gate_digest": applied["rollout_gate_digest"],
            "mode": state.mode,
        }

    def _create_user(self, *, fixture_id: str, role: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@kiosk-negative-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=None)
