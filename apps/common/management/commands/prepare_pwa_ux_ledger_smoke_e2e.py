from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    Schedule,
    ScheduleEnrollment,
    TrainingGroupRolloutState,
)
from apps.attendance.services import enroll_student_in_schedule
from apps.billing.models import BankPaymentOrder, Debt, Payment, Subscription, Tariff, TrainingType
from apps.billing.services import create_bank_payment_order
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate

APP_TIME_ZONE = "Asia/Yekaterinburg"
APP_ZONE = ZoneInfo(APP_TIME_ZONE)
FIXTURE_CLUB_TIME_ZONE = "Europe/Moscow"
FIXTURE_CLUB_ZONE = ZoneInfo(FIXTURE_CLUB_TIME_ZONE)


class Command(BaseCommand):
    help = "Prepare an isolated fixture for PWA UX ledger real-stack E2E."

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
                "Prepared PWA UX ledger E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, control_club_id={fixture['control_club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localtime(now, FIXTURE_CLUB_ZONE).date()
        slot_date = today + timedelta(days=1)
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"pwa-ux-ledger-e2e-{now:%Y%m%d%H%M%S}-{unique}"

        club = self._create_club(fixture_id=fixture_id)
        location = Location.objects.create(
            club=club,
            name=f"Ledger Hall {fixture_id}",
            address="PWA UX ledger E2E fixture",
        )
        control_club = self._create_club(fixture_id=f"{fixture_id}-control", display="Control")
        control_location = Location.objects.create(
            club=control_club,
            name=f"Control Hall {fixture_id}",
            address="PWA UX ledger control fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=f"LedgerTrainer-{unique}-pass",
        )
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=f"LedgerStudent-{unique}-pass",
        )
        package_student_user = self._create_user(
            fixture_id=fixture_id,
            role="package-student",
            password=f"LedgerPackageStudent-{unique}-pass",
        )
        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=f"LedgerParent-{unique}-pass",
        )
        for user, role in [
            (trainer_user, ClubMembership.Role.TRAINER),
            (student_user, ClubMembership.Role.STUDENT),
            (package_student_user, ClubMembership.Role.STUDENT),
            (parent_user, ClubMembership.Role.PARENT),
        ]:
            ClubMembership.objects.create(user=user, club=club, role=role)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Ledger",
            last_name="Trainer",
            phone=f"+15560{phone_seed}0",
            user=trainer_user,
        )
        grade_system = GradeSystem.objects.create(club=club, discipline=f"Ledger Muay Thai {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        group_type = TrainingType.objects.create(
            club=club,
            name=f"Ledger Group {fixture_id}",
            slug=f"ledger-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Ledger Personal {fixture_id}",
            slug=f"ledger-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        blocked_personal_type = TrainingType.objects.create(
            club=club,
            name=f"Ledger Locked Personal {fixture_id}",
            slug=f"ledger-locked-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        for training_type, percent in [
            (group_type, Decimal("30.00")),
            (personal_type, Decimal("40.00")),
            (blocked_personal_type, Decimal("40.00")),
        ]:
            TrainerRate.objects.create(
                club=club,
                trainer=trainer,
                location=location,
                training_type=training_type,
                percent=percent,
            )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        group_tariff = Tariff.objects.create(
            club=club,
            training_type=group_type,
            name=f"Ledger Group Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        personal_tariff = Tariff.objects.create(
            club=club,
            training_type=personal_type,
            name=f"Ledger Personal Single {fixture_id}",
            price=Decimal("2500.00"),
            trainings_limit=1,
            duration_days=14,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )

        student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Self",
            phone=f"+15560{phone_seed}1",
            user=student_user,
            grade_system=grade_system,
            grade=grade,
        )
        student.assigned_trainer = trainer
        student.save(update_fields=["assigned_trainer"])
        package_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Package",
            phone=f"+15560{phone_seed}2",
            user=package_student_user,
            grade_system=grade_system,
            grade=grade,
        )
        child = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Child",
            phone=f"+15560{phone_seed}3",
            parent_user=parent_user,
            is_child=True,
            grade_system=grade_system,
            grade=grade,
        )
        roster_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Roster",
            phone=f"+15560{phone_seed}4",
            grade_system=grade_system,
            grade=grade,
        )

        renewal_subscription = self._create_subscription(
            club=club,
            student=student,
            tariff=group_tariff,
            now=now,
            status=Subscription.Status.EXPIRED,
            expires_at=now - timedelta(days=1),
            trainings_left=0,
        )
        package_subscription = self._create_subscription(
            club=club,
            student=package_student,
            tariff=personal_tariff,
            now=now,
            status=Subscription.Status.ACTIVE,
            expires_at=now + timedelta(days=30),
            trainings_left=1,
        )
        self._create_subscription(
            club=club,
            student=roster_student,
            tariff=group_tariff,
            now=now,
            status=Subscription.Status.ACTIVE,
            expires_at=now + timedelta(days=30),
            trainings_left=4,
        )
        self._create_subscription(
            club=club,
            student=child,
            tariff=group_tariff,
            now=now,
            status=Subscription.Status.ACTIVE,
            expires_at=now + timedelta(days=30),
            trainings_left=4,
        )
        renewal_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=group_tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=student_user.id,
        )

        group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=slot_date.weekday(),
            start_time=time(9, 0),
            end_time=time(10, 0),
            group_name=f"Ledger Already Enrolled {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=group_type,
            is_active=True,
        )
        trainer_recovery_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=personal_tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=trainer_user.id,
            seller_trainer_id=trainer.id,
            package_owner_trainer_id=trainer.id,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=group_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=slot_date,
        )

        trainer_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(0, 45),
            group_name=f"Ledger Trainer Session {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=group_type,
            one_time_date=today,
            is_active=True,
        )
        roster_enrollment = enroll_student_in_schedule(
            club_id=club.id,
            student_id=roster_student.id,
            schedule_id=trainer_schedule.id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
        )

        slots = {
            "student_primary": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=personal_type,
                slot_date=slot_date,
                start=time(11, 0),
                end=time(12, 0),
            ),
            "student_second": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=personal_type,
                slot_date=slot_date,
                start=time(12, 30),
                end=time(13, 30),
            ),
            "parent_primary": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=personal_type,
                slot_date=slot_date,
                start=time(18, 0),
                end=time(19, 0),
            ),
            "package_primary": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=personal_type,
                slot_date=slot_date,
                start=time(15, 30),
                end=time(16, 30),
            ),
            "blocked": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=blocked_personal_type,
                slot_date=slot_date,
                start=time(17, 0),
                end=time(18, 0),
            ),
            "trainer_payment": self._create_slot(
                club=club,
                trainer=trainer,
                location=location,
                training_type=personal_type,
                slot_date=slot_date,
                start=time(14, 0),
                end=time(15, 0),
            ),
        }

        baseline = {
            "attendance_empty_student_checkins": Checkin.objects.for_club(club).filter(student=student).count(),
            "roster_checkins": list(
                Checkin.objects.for_club(club)
                .filter(schedule=trainer_schedule, date=today)
                .order_by("id")
                .values("id", "student_id", "source", "cancelled_at")
            ),
            "roster_debt_count": Debt.objects.for_club(club).filter(student=roster_student).count(),
            "trainer_open_task_count": RetentionTask.objects.for_club(club)
            .filter(trainer=trainer, resolved_at__isnull=True)
            .count(),
        }

        control_data = self._create_control_decoys(
            fixture_id=fixture_id,
            club=control_club,
            location=control_location,
            now=now,
            slot_date=slot_date,
            unique=unique,
        )

        return {
            "fixture_id": fixture_id,
            "app_time_zone": APP_TIME_ZONE,
            "club_id": club.id,
            "control_club_id": control_club.id,
            "location": {
                "id": location.id,
                "name": location.name,
            },
            "trainer": {
                "email": trainer_user.email,
                "password": f"LedgerTrainer-{unique}-pass",
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
            },
            "student": {
                "email": student_user.email,
                "password": f"LedgerStudent-{unique}-pass",
                "user_id": student_user.id,
                "student_id": student.id,
                "name": str(student),
            },
            "package_student": {
                "email": package_student_user.email,
                "password": f"LedgerPackageStudent-{unique}-pass",
                "user_id": package_student_user.id,
                "student_id": package_student.id,
                "name": str(package_student),
                "subscription_id": package_subscription.id,
            },
            "parent": {
                "email": parent_user.email,
                "password": f"LedgerParent-{unique}-pass",
                "user_id": parent_user.id,
                "child_id": child.id,
                "child_name": str(child),
            },
            "tariffs": {
                "group_id": group_tariff.id,
                "group_name": group_tariff.name,
                "personal_id": personal_tariff.id,
                "personal_training_type_id": personal_type.id,
                "personal_name": personal_tariff.name,
                "personal_price": "2500.00",
            },
            "renewal": {
                "subscription_id": renewal_subscription.id,
                "tariff_id": group_tariff.id,
                "order_id": renewal_order.id,
            },
            "trainer_recovery": {
                "order_id": trainer_recovery_order.id,
                "subscription_id": trainer_recovery_order.subscription_id,
            },
            "booking_date": slot_date.isoformat(),
            "group_schedule": {
                "id": group_schedule.id,
                "name": group_schedule.group_name,
            },
            "trainer_session": {
                "schedule_id": trainer_schedule.id,
                "date": today.isoformat(),
                "group_name": trainer_schedule.group_name,
                "roster_student_id": roster_student.id,
                "roster_student_name": str(roster_student),
                "roster_enrollment_id": roster_enrollment.id,
            },
            "slots": {
                name: {
                    "id": slot.id,
                    "starts_at": slot.starts_at.isoformat(),
                    "ends_at": slot.ends_at.isoformat(),
                    "time_label": self._slot_time_label(slot),
                }
                for name, slot in slots.items()
            },
            "baseline": baseline,
            "control": control_data,
            "created_at": now.isoformat(),
        }

    def _create_club(self, *, fixture_id: str, display: str = "Ledger") -> Club:
        club = Club.objects.create(
            name=f"Jaguar {display} E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone=FIXTURE_CLUB_TIME_ZONE,
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display=f"Jaguar {display} E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        return club

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@pwa-ux-ledger-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)

    def _create_student(
        self,
        *,
        club: Club,
        fixture_id: str,
        label: str,
        phone: str,
        grade_system: GradeSystem,
        grade: Grade,
        user=None,
        parent_user=None,
        is_child: bool = False,
    ) -> Student:
        student = Student.objects.create(
            club=club,
            first_name=label,
            last_name=f"Ledger{fixture_id[-4:]}",
            phone=phone,
            email="",
            is_child=is_child,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=user,
            parent_user=parent_user,
        )
        StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=grade,
            trainings_since_last_grade=0,
        )
        return student

    def _create_subscription(
        self,
        *,
        club: Club,
        student: Student,
        tariff: Tariff,
        now,
        status: str,
        expires_at,
        trainings_left: int,
    ) -> Subscription:
        return Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=status,
            paid_amount=tariff.price,
            trainings_left=trainings_left,
            trainings_used=max((tariff.trainings_limit or 0) - trainings_left, 0),
            expires_at=expires_at,
            scope=tariff.scope,
            location=None,
        )

    def _slot_at(self, *, slot_date, value: time):
        naive = datetime.combine(slot_date, value)
        return timezone.make_aware(naive, timezone.get_current_timezone())

    def _create_slot(
        self,
        *,
        club: Club,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        slot_date,
        start: time,
        end: time,
    ) -> PersonalAvailabilitySlot:
        return PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=self._slot_at(slot_date=slot_date, value=start),
            ends_at=self._slot_at(slot_date=slot_date, value=end),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

    def _create_control_decoys(
        self,
        *,
        fixture_id: str,
        club: Club,
        location: Location,
        now,
        slot_date,
        unique: str,
    ) -> dict:
        user = self._create_user(
            fixture_id=f"{fixture_id}-control",
            role="trainer",
            password=f"ControlTrainer-{unique}-pass",
        )
        ClubMembership.objects.create(user=user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Control",
            last_name="Trainer",
            phone=f"+15561{str(int(unique, 16) % 10_000_000).zfill(7)}",
            user=user,
        )
        grade_system = GradeSystem.objects.create(club=club, discipline=f"Control Discipline {fixture_id}")
        grade = Grade.objects.create(club=club, grade_system=grade_system, name="Start", order=0, min_trainings=0)
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Control Personal {fixture_id}",
            slug=f"control-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("10.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Control Tariff {fixture_id}",
            price=Decimal("999.00"),
            trainings_limit=1,
            duration_days=7,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Control",
            phone=f"+15562{str(int(unique, 16) % 10_000_000).zfill(7)}",
            grade_system=grade_system,
            grade=grade,
        )
        subscription = self._create_subscription(
            club=club,
            student=student,
            tariff=tariff,
            now=now,
            status=Subscription.Status.PENDING,
            expires_at=now + timedelta(days=7),
            trainings_left=1,
        )
        payment = Payment.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=tariff.price,
            original_amount=tariff.price,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=user,
        )
        order = BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.STUDENT,
            status=BankPaymentOrder.Status.PENDING,
            amount_snapshot=tariff.price,
            purpose_snapshot=tariff.name,
            provider_payment_link_id=f"control-{unique}",
            provider_payment_url="https://pay.example.invalid/control",
            expires_at=now + timedelta(hours=1),
            created_by=user,
        )
        slot = self._create_slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            slot_date=slot_date,
            start=time(19, 0),
            end=time(20, 0),
        )
        reservation = PersonalBookingPaymentReservation.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            location=location,
            training_type=training_type,
            tariff=tariff,
            availability_slot=slot,
            payment=payment,
            bank_payment_order=order,
            subscription=subscription,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            expires_at=now + timedelta(hours=1),
            created_by=user,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=slot_date.weekday(),
            start_time=time(19, 0),
            end_time=time(20, 0),
            group_name=f"Control Session {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=slot_date,
            is_active=True,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=slot_date,
            source=Checkin.Source.MANUAL,
            subscription=subscription,
        )
        debt = Debt.objects.create(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=tariff.price,
            reason="control_decoy",
        )
        task = RetentionTask.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            level=RetentionTask.Level.YELLOW,
            status=RetentionTask.TaskStatus.OPEN,
            due_date=slot_date,
            task_type=RetentionTask.TaskType.RETENTION,
        )
        return {
            "order_id": order.id,
            "order_status": order.status,
            "order_payment_id": payment.id,
            "order_subscription_id": subscription.id,
            "payment_status": payment.status,
            "subscription_status": subscription.status,
            "reservation_id": reservation.id,
            "reservation_status": reservation.status,
            "reservation_order_id": order.id,
            "reservation_payment_id": payment.id,
            "reservation_subscription_id": subscription.id,
            "slot_id": slot.id,
            "slot_status": slot.status,
            "checkin_id": checkin.id,
            "checkin_cancelled": checkin.cancelled_at is not None,
            "debt_id": debt.id,
            "debt_tariff_price": str(debt.tariff_price),
            "debt_reason": debt.reason,
            "debt_settlement_payment_id": debt.settlement_payment_id,
            "debt_resolved": debt.resolved_at is not None,
            "debt_resolution_type": debt.resolution_type,
            "task_id": task.id,
            "task_status": task.status,
            "task_resolved": task.resolved_at is not None,
        }

    def _slot_time_label(self, slot: PersonalAvailabilitySlot) -> str:
        starts_at = timezone.localtime(slot.starts_at, APP_ZONE)
        ends_at = timezone.localtime(slot.ends_at, APP_ZONE)
        return f"{starts_at:%H:%M}-{ends_at:%H:%M}"
