from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment
from apps.attendance.services import create_checkin, generate_kiosk_pin
from apps.billing.models import Debt, Discount, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.pipelines.models import Pipeline, PipelineExecution, PipelineStep
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated deterministic fixture for owner/admin payment confirmation E2E."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        output_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared payment confirmation E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        fixture_id = f"payment-confirm-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"PaymentConfirmE2E-{unique}-pass"
        trainer_password = f"PaymentConfirmTrainerE2E-{unique}-pass"
        numeric_suffix = f"{Club.objects.count() + 1:04d}"

        club = Club.objects.create(
            name=f"Jaguar Payment Confirm E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Payment Confirm E2E",
            unified_client_journey_enabled=False,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V1,
        )
        location = Location.objects.create(
            club=club,
            name=f"Payment Hall {fixture_id}",
            address="Payment confirmation E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Payment",
            last_name="Seller",
            phone=f"+155560{numeric_suffix}",
            user=trainer_user,
        )

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Group Payment E2E {fixture_id}",
            slug=f"group-payment-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Payment Confirm 8-Pack {fixture_id}",
            price=Decimal("4000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        target_start_date = club_localdate(club) + timedelta(days=1)
        target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Payment Permanent Group {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        second_target_start_date = target_start_date + timedelta(days=2)
        second_target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=second_target_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=target_schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=owner_user.id,
            schedule_ids=[target_schedule.id, second_target_schedule.id],
            canonical_name=target_schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-canonical-group",
            require_manual_operational_admission=True,
        )
        discount = Discount.objects.create(
            club=club,
            name=f"Стартовая скидка 10% {fixture_id}",
            discount_type=Discount.Type.PERCENT,
            value=Decimal("10.00"),
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Payment",
            last_name="Student",
            phone=f"+155570{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.WEBSITE,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=trainer,
        )
        child = Student.objects.create(
            club=club,
            first_name="Payment",
            last_name="Child",
            phone="",
            email="",
            is_child=True,
            date_of_birth=today.replace(year=today.year - 10),
            guardian_phone=f"+155571{numeric_suffix}",
            status=Student.Status.LEAD,
            source=Student.Source.WEBSITE,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=trainer,
        )
        retained_trial_type = TrainingType.objects.create(
            club=club,
            name=f"Completed Trial E2E {fixture_id}",
            slug=f"completed-trial-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            trial_free=True,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=retained_trial_type,
            percent=Decimal("25.00"),
        )
        retained_trial_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(15, 0),
            end_time=time(16, 0),
            group_name=f"Completed Trial {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=retained_trial_type,
            is_active=True,
        )
        retained_student = Student.objects.create(
            club=club,
            first_name="Payment",
            last_name="Retained",
            phone=f"+155572{numeric_suffix}",
            email="",
            is_child=False,
            status=Student.Status.TRIAL,
            source=Student.Source.WEBSITE,
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
            assigned_trainer=trainer,
            trial_date=now,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=retained_student,
            schedule=retained_trial_schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=today,
            ends_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=retained_student.id,
                schedule_id=retained_trial_schedule.id,
                training_type_id=retained_trial_type.id,
                source=Checkin.Source.MANUAL,
                checkin_date=today,
            )
        retained_student.refresh_from_db()
        if retained_student.lead_status != Student.LeadStatus.TRIAL_DONE:
            raise CommandError("Retained fixture must have an exact completed trial check-in.")
        pipeline = Pipeline.objects.create(
            club=club,
            name=f"Payment Follow-up {fixture_id}",
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
        )
        pipeline_step = PipelineStep.objects.create(
            club=club,
            pipeline=pipeline,
            order=1,
            delay_hours=0,
            action_type=PipelineStep.ActionType.CREATE_TASK,
            action_config={
                "task_type": RetentionTask.TaskType.POST_TRIAL,
                "level": RetentionTask.Level.YELLOW,
            },
        )
        pipeline_execution = PipelineExecution.objects.create(
            club=club,
            pipeline=pipeline,
            student=student,
            current_step=pipeline_step,
            next_step_at=now + timedelta(days=7),
        )
        post_trial_task = RetentionTask.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=today,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        )
        retained_pipeline_execution = PipelineExecution.objects.create(
            club=club,
            pipeline=pipeline,
            student=retained_student,
            current_step=pipeline_step,
            next_step_at=now + timedelta(days=7),
        )
        retained_post_trial_task = RetentionTask.objects.create(
            club=club,
            student=retained_student,
            trainer=trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=today,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        )
        retained_debt_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Retained Payment Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        retained_debt_checkin = Checkin.objects.create(
            club=club,
            student=retained_student,
            schedule=retained_debt_schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        retained_debt = Debt.objects.create(
            club=club,
            student=retained_student,
            checkin=retained_debt_checkin,
            tariff_price=Decimal("1200.00"),
            reason="no_subscription",
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)
        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "trainer_id": trainer.id,
            "student": {
                "id": student.id,
                "name": str(student),
            },
            "student_id": student.id,
            "child": {
                "id": child.id,
                "name": str(child),
                "parent_phone_input": child.guardian_phone,
            },
            "retained": {
                "id": retained_student.id,
                "name": str(retained_student),
                "debt_id": retained_debt.id,
                "debt_checkin_id": retained_debt_checkin.id,
                "pipeline_execution_id": retained_pipeline_execution.id,
                "post_trial_task_id": retained_post_trial_task.id,
            },
            "training_type_id": training_type.id,
            "tariff": {
                "id": tariff.id,
                "name": tariff.name,
                "price": str(tariff.price),
            },
            "tariff_id": tariff.id,
            "target_group": {
                "training_group_id": canonical_group["training_group_id"],
                "rollout_mode": canonical_group["mode"],
                "new_writes_enabled": canonical_group["new_writes_enabled"],
                "manual_operational_admission_enabled": canonical_group[
                    "manual_operational_admission_enabled"
                ],
                "schedule_id": target_schedule.id,
                "name": target_schedule.group_name,
                "start_date": target_start_date.isoformat(),
                "second_schedule_id": second_target_schedule.id,
                "second_start_date": second_target_start_date.isoformat(),
            },
            "target_schedule_id": target_schedule.id,
            "target_start_date": target_start_date.isoformat(),
            "kiosk": {"activation_pin": kiosk_pin},
            "discount": {
                "id": discount.id,
                "name": discount.name,
                "discount_type": discount.discount_type,
                "value": str(discount.value),
            },
            "discount_id": discount.id,
            "pipeline_execution_id": pipeline_execution.id,
            "post_trial_task_id": post_trial_task.id,
            "expected": {
                "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
                "payment_original_amount": "4000.00",
                "payment_amount": "4000.00",
                "payment_method": "cash",
                "sale_rate_percent": "25.00",
                "sale_earning_amount": "1000.00",
                "student_status_before_confirm": Student.Status.LEAD,
                "lead_status_before_confirm": Student.LeadStatus.NEW,
                "student_status_after_confirm": Student.Status.ACTIVE,
                "lead_status_after_confirm": None,
                "post_trial_task_status_after_confirm": RetentionTask.TaskStatus.CLOSED,
                "post_trial_task_resolution_after_confirm": RetentionTask.Resolution.AUTO_SUBSCRIPTION,
                "subscription_trainings_left_before_confirm": 8,
                "subscription_trainings_left_after_confirm": 7,
                "subscription_trainings_used_after_confirm": 1,
                "retained_payment_original_amount": "4000.00",
                "retained_payment_amount": "3600.00",
                "retained_sale_earning_amount": "900.00",
                "retained_student_status_before_confirm": Student.Status.TRIAL,
                "retained_lead_status_before_confirm": Student.LeadStatus.TRIAL_DONE,
                "retained_subscription_trainings_left_after_confirm": 7,
                "retained_subscription_trainings_used_after_confirm": 1,
                "combined_income": "3600.00",
                "combined_salary_expenses": "900.00",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@payment-confirm-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
