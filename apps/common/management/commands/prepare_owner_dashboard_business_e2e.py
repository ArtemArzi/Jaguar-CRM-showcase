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

from apps.attendance.models import (
    Checkin,
    Schedule,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroupRolloutState,
)
from apps.billing.models import Debt, Expense, Payment, Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.retention.models import RetentionTask, TaskComment
from apps.students.models import Student
from apps.trainers.models import (
    Trainer,
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerLocation,
    TrainerRate,
)


class Command(BaseCommand):
    help = "Prepare an isolated owner/admin dashboard business picture E2E fixture."

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
                "Prepared owner dashboard business E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        enrollment_session_date = week_start + timedelta(days=1)
        enrollment_transfer_date = enrollment_session_date + timedelta(days=1)
        enrollment_cancel_date = enrollment_transfer_date + timedelta(days=7)
        report_from = today - timedelta(days=7)
        report_to = today + timedelta(days=7)
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"owner-dashboard-business-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"OwnerDashboardBusiness-{unique}-pass"
        admin_password = f"OwnerDashboardBusinessAdmin-{unique}-pass"
        schedule_admin_week_start = week_start + timedelta(weeks=1)
        schedule_admin_create_date = schedule_admin_week_start + timedelta(days=1)
        schedule_admin_cancel_date = schedule_admin_week_start + timedelta(days=2)
        schedule_admin_reschedule_old_date = schedule_admin_week_start + timedelta(days=3)
        schedule_admin_reschedule_new_date = schedule_admin_week_start + timedelta(days=4)
        schedule_admin_substitute_date = schedule_admin_week_start + timedelta(days=5)
        schedule_admin_revert_date = schedule_admin_week_start

        club = Club.objects.create(
            name=f"Jaguar Owner Dashboard Business E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Owner Dashboard Business E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Dashboard Hall {fixture_id}",
            address="Owner dashboard business E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        admin_user = self._create_user(fixture_id=fixture_id, role="admin", password=admin_password)
        ClubMembership.objects.create(user=admin_user, club=club, role=ClubMembership.Role.ADMIN)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Dashboard",
            last_name="Trainer",
            phone=f"+155510{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        substitute_trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="substitute-trainer",
            password=None,
        )
        ClubMembership.objects.create(
            user=substitute_trainer_user,
            club=club,
            role=ClubMembership.Role.TRAINER,
        )
        substitute_trainer = Trainer.objects.create(
            club=club,
            first_name="Schedule",
            last_name="Substitute",
            phone=f"+155511{phone_seed}",
            user=substitute_trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=substitute_trainer, location=location)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Dashboard Group {fixture_id}",
            slug=f"dashboard-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("500.00"),
            trial_free=False,
        )
        schedule_admin_training_type = TrainingType.objects.create(
            club=club,
            name=f"Dashboard Schedule Edited {fixture_id}",
            slug=f"dashboard-schedule-edited-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("650.00"),
            trial_free=False,
        )
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
            name=f"Dashboard 4-Pack {fixture_id}",
            price=Decimal("1200.00"),
            trainings_limit=4,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="Dashboard",
            last_name="Student",
            phone=f"+155520{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Dashboard Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        debt_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 1),
            end_time=time(23, 58),
            group_name=f"Dashboard Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        enrollment_source_schedule = Schedule.objects.create(
            club=club,
            day_of_week=enrollment_session_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name=f"Dashboard Enrollment Source {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=enrollment_session_date,
            is_active=True,
        )
        enrollment_target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=enrollment_transfer_date.weekday(),
            start_time=time(12, 0),
            end_time=time(13, 0),
            group_name=f"Dashboard Enrollment Target {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        enrollment_consistency_schedule = Schedule.objects.create(
            club=club,
            day_of_week=enrollment_session_date.weekday(),
            start_time=time(14, 0),
            end_time=time(15, 0),
            group_name=f"Dashboard Enrollment Consistency {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=enrollment_session_date,
            is_active=True,
        )
        schedule_admin_cancel_schedule = Schedule.objects.create(
            club=club,
            day_of_week=schedule_admin_cancel_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name=f"Dashboard Schedule Cancel {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=schedule_admin_cancel_date,
            is_active=True,
        )
        schedule_admin_reschedule_schedule = Schedule.objects.create(
            club=club,
            day_of_week=schedule_admin_reschedule_old_date.weekday(),
            start_time=time(11, 30),
            end_time=time(12, 30),
            group_name=f"Dashboard Schedule Reschedule {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=schedule_admin_reschedule_old_date,
            is_active=True,
        )
        schedule_admin_substitute_schedule = Schedule.objects.create(
            club=club,
            day_of_week=schedule_admin_substitute_date.weekday(),
            start_time=time(13, 0),
            end_time=time(14, 0),
            group_name=f"Dashboard Schedule Substitute {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=schedule_admin_substitute_date,
            is_active=True,
        )
        schedule_admin_revert_schedule = Schedule.objects.create(
            club=club,
            day_of_week=schedule_admin_revert_date.weekday(),
            start_time=time(15, 0),
            end_time=time(16, 0),
            group_name=f"Dashboard Schedule Revert {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=schedule_admin_revert_date,
            is_active=True,
        )
        reconciliation_first_schedule = Schedule.objects.create(
            club=club,
            day_of_week=(today + timedelta(days=1)).weekday(),
            start_time=time(17, 0),
            end_time=time(18, 0),
            group_name=f"Dashboard Canonical Mapping {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        reconciliation_second_schedule = Schedule.objects.create(
            club=club,
            day_of_week=(today + timedelta(days=3)).weekday(),
            start_time=time(17, 0),
            end_time=time(18, 0),
            group_name=reconciliation_first_schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        ScheduleException.objects.create(
            club=club,
            schedule=schedule_admin_revert_schedule,
            date=schedule_admin_revert_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
            substitute_trainer=substitute_trainer,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=Decimal("1200.00"),
            trainings_left=3,
            trainings_used=1,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        confirmed_payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("1200.00"),
            original_amount=Decimal("1200.00"),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
            seller_trainer=trainer,
            verified_by=owner_user,
            verified_at=now,
        )
        pending_payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=None,
            amount=Decimal("700.00"),
            original_amount=Decimal("700.00"),
            payment_method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            seller_trainer=trainer,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=subscription,
            is_debt=False,
        )
        debt_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=debt_schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        debt = Debt.objects.create(
            club=club,
            student=student,
            checkin=debt_checkin,
            tariff_price=Decimal("500.00"),
            reason="no_subscription",
        )
        earning = TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            checkin=checkin,
            payment=None,
            earning_source=TrainerEarning.Source.CHECKIN,
            earning_type=TrainerEarning.EarningType.GROUP,
            amount=Decimal("300.00"),
            rate_percent=Decimal("25.00"),
            subscription_price=Decimal("1200.00"),
            cancelled=False,
        )
        sale_earning = TrainerEarning.objects.create(
            club=club,
            trainer=trainer,
            checkin=None,
            payment=confirmed_payment,
            earning_source=TrainerEarning.Source.SALE,
            earning_type=TrainerEarning.EarningType.GROUP,
            amount=Decimal("120.00"),
            rate_percent=Decimal("10.00"),
            subscription_price=confirmed_payment.amount,
            cancelled=False,
        )
        payroll_adjustment = TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=trainer,
            amount_basis_snapshot=Decimal("0.00"),
            payable_amount_delta=Decimal("80.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.CREDIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=today,
            reason="Owner dashboard salary helper proof",
        )
        expense = Expense.objects.create(
            club=club,
            name=f"Dashboard Rent {fixture_id}",
            amount=Decimal("200.00"),
            date=today,
            category="rent",
            is_recurring=False,
        )
        retention_task = RetentionTask.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            level=RetentionTask.Level.RED,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=today - timedelta(days=1),
            notes="Owner dashboard retention queue proof",
            task_type=RetentionTask.TaskType.RETENTION,
        )
        retention_comment = TaskComment.objects.create(
            club=club,
            task=retention_task,
            author=owner_user,
            text="Retention queue comment proof",
        )
        enrollment_student = Student.objects.create(
            club=club,
            first_name="Enrollment",
            last_name="Admin",
            phone=f"+155530{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        transferred_enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=enrollment_student,
            schedule=enrollment_consistency_schedule,
            status=ScheduleEnrollment.Status.TRANSFERRED,
            starts_on=enrollment_session_date - timedelta(days=7),
            ends_on=enrollment_session_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        active_enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=enrollment_student,
            schedule=enrollment_consistency_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=enrollment_session_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        reconciliation_student = Student.objects.create(
            club=club,
            first_name="Reconciliation",
            last_name="Source",
            phone=f"+155540{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        reconciliation_source = ScheduleEnrollment.objects.create(
            club=club,
            student=reconciliation_student,
            schedule=reconciliation_first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "admin": {
                "user_id": admin_user.id,
                "email": admin_user.email,
                "password": admin_password,
            },
            "trainer": {
                "trainer_id": trainer.id,
                "name": str(trainer),
            },
            "substitute_trainer": {
                "trainer_id": substitute_trainer.id,
                "name": str(substitute_trainer),
            },
            "student": {
                "student_id": student.id,
                "name": str(student),
                "phone": student.phone,
            },
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "tariff_id": tariff.id,
            "subscription_id": subscription.id,
            "checkin_id": checkin.id,
            "debt_checkin_id": debt_checkin.id,
            "debt_id": debt.id,
            "earning_id": earning.id,
            "sale_earning_id": sale_earning.id,
            "payroll_adjustment_id": payroll_adjustment.id,
            "expense_id": expense.id,
            "retention_admin": {
                "task_id": retention_task.id,
                "comment_id": retention_comment.id,
                "comment_text": retention_comment.text,
                "student_name": str(student),
                "trainer_id": trainer.id,
                "trainer_name": str(trainer),
                "status": retention_task.status,
                "level": retention_task.level,
                "due_date": retention_task.due_date.isoformat(),
            },
            "report_range": {
                "date_from": report_from.isoformat(),
                "date_to": report_to.isoformat(),
            },
            "browser_expense": {
                "name": f"Browser Expense {fixture_id}",
                "amount": "125.00",
                "date": today.isoformat(),
                "category": "browser",
            },
            "enrollment_admin": {
                "student_id": enrollment_student.id,
                "student_name": str(enrollment_student),
                "source_schedule_id": enrollment_source_schedule.id,
                "source_group_name": enrollment_source_schedule.group_name,
                "source_week_offset": 0,
                "session_date": enrollment_session_date.isoformat(),
                "target_schedule_id": enrollment_target_schedule.id,
                "target_group_name": enrollment_target_schedule.group_name,
                "target_cancel_week_offset": 1,
                "transfer_date": enrollment_transfer_date.isoformat(),
                "cancel_date": enrollment_cancel_date.isoformat(),
            },
            "enrollment_consistency": {
                "student_id": enrollment_student.id,
                "student_name": str(enrollment_student),
                "schedule_id": enrollment_consistency_schedule.id,
                "group_name": enrollment_consistency_schedule.group_name,
                "week_offset": 0,
                "session_date": enrollment_session_date.isoformat(),
                "transferred_enrollment_id": transferred_enrollment.id,
                "active_enrollment_id": active_enrollment.id,
            },
            "schedule_admin": {
                "week_offset": 1,
                "create": {
                    "date": schedule_admin_create_date.isoformat(),
                    "created_group_name": f"Dashboard Schedule Created {fixture_id}",
                    "edited_group_name": f"Dashboard Schedule Edited {fixture_id}",
                    "created_start_time": "09:15",
                    "created_end_time": "10:15",
                    "edited_start_time": "09:45",
                    "edited_end_time": "10:45",
                    "created_training_type_id": training_type.id,
                    "edited_training_type_id": schedule_admin_training_type.id,
                    "trainer_id": trainer.id,
                    "location_id": location.id,
                },
                "cancel": {
                    "schedule_id": schedule_admin_cancel_schedule.id,
                    "group_name": schedule_admin_cancel_schedule.group_name,
                    "date": schedule_admin_cancel_date.isoformat(),
                    "reason": "Owner dashboard browser cancel proof",
                },
                "reschedule": {
                    "schedule_id": schedule_admin_reschedule_schedule.id,
                    "group_name": schedule_admin_reschedule_schedule.group_name,
                    "old_date": schedule_admin_reschedule_old_date.isoformat(),
                    "new_date": schedule_admin_reschedule_new_date.isoformat(),
                    "new_start_time": "16:00",
                    "new_end_time": "17:00",
                },
                "substitute": {
                    "schedule_id": schedule_admin_substitute_schedule.id,
                    "group_name": schedule_admin_substitute_schedule.group_name,
                    "date": schedule_admin_substitute_date.isoformat(),
                    "substitute_trainer_id": substitute_trainer.id,
                    "substitute_trainer_name": str(substitute_trainer),
                },
                "revert": {
                    "schedule_id": schedule_admin_revert_schedule.id,
                    "group_name": schedule_admin_revert_schedule.group_name,
                    "date": schedule_admin_revert_date.isoformat(),
                    "week_offset": 1,
                    "original_trainer_id": trainer.id,
                    "substitute_trainer_name": str(substitute_trainer),
                },
            },
            "training_group_reconciliation": {
                "schedule_ids": [reconciliation_first_schedule.id, reconciliation_second_schedule.id],
                "canonical_name": reconciliation_first_schedule.group_name,
                "responsible_trainer_id": trainer.id,
                "source_student_id": reconciliation_student.id,
                "source_enrollment_id": reconciliation_source.id,
                "source_schedule_id": reconciliation_first_schedule.id,
                "projection_schedule_id": reconciliation_second_schedule.id,
                "initial_rollout_mode": TrainingGroupRolloutState.Mode.OFF,
                "new_writes_enabled": True,
                "manual_operational_admission_enabled": True,
                "rationale": "Owner browser reconciliation confirmation.",
                "idempotency_key": f"{fixture_id}-owner-reconciliation",
            },
            "payment_ids": {
                "confirmed": confirmed_payment.id,
                "pending": pending_payment.id,
            },
            "expected": {
                "dashboard_revenue": "1200.00",
                "dashboard_salary": "500.00",
                "pnl_income": "1200.00",
                "manual_expense": "200.00",
                "pnl_salary": "500.00",
                "pnl_margin": "500.00",
                "debt_amount": "500.00",
                "pending_amount": "700.00",
                "confirmed_amount": "1200.00",
                "kiosk_active_count": 0,
                "kiosk_device_count": 1,
                "group_name": schedule.group_name,
                "debt_group_name": debt_schedule.group_name,
                "tariff_name": tariff.name,
                "expense_name": expense.name,
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@owner-dashboard-business-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
