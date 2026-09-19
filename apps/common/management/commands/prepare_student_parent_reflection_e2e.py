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

from apps.attendance.models import Checkin, Schedule, TrainingGroupRolloutState
from apps.attendance.services import enroll_student_in_schedule, generate_kiosk_pin
from apps.billing.models import Debt, Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student, StudentNote
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for student/parent reflection E2E."

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
                "Prepared student/parent reflection E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, schedule_id={fixture['schedule_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        checkin_date = today
        debt_date = today - timedelta(days=2)
        upcoming_date = today + timedelta(days=2)
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"student-parent-reflection-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"ReflectionTrainer-{unique}-pass"
        student_password = f"ReflectionStudent-{unique}-pass"
        parent_password = f"ReflectionParent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Reflection E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Reflection E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Reflection Hall {fixture_id}",
            address="Student parent reflection E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=student_password,
        )
        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=parent_password,
        )
        foreign_parent_user = self._create_user(
            fixture_id=fixture_id,
            role="foreign-parent",
            password=f"ReflectionForeignParent-{unique}-pass",
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        ClubMembership.objects.create(user=foreign_parent_user, club=club, role=ClubMembership.Role.PARENT)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Reflection",
            last_name="Trainer",
            phone=f"+15557{phone_seed}0",
            user=trainer_user,
        )
        grade_system = GradeSystem.objects.create(club=club, discipline=f"Muay Thai {fixture_id}")
        start_grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        next_grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Next",
            order=1,
            min_trainings=10,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Reflection Group {fixture_id}",
            slug=f"reflection-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        debt_training_type = TrainingType.objects.create(
            club=club,
            name=f"Reflection Drop-in Debt {fixture_id}",
            slug=f"reflection-debt-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=None,
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
            name=f"Reflection 5-Pack {fixture_id}",
            price=Decimal("6000.00"),
            trainings_limit=5,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=checkin_date.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Reflection Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=checkin_date,
            is_active=True,
        )
        upcoming_schedule = Schedule.objects.create(
            club=club,
            day_of_week=upcoming_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Reflection Upcoming {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=upcoming_date,
            is_active=True,
        )
        debt_schedule = Schedule.objects.create(
            club=club,
            day_of_week=debt_date.weekday(),
            start_time=time(0, 1),
            end_time=time(0, 59),
            group_name=f"Reflection Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=debt_training_type,
            one_time_date=debt_date,
            is_active=False,
        )

        staff_only_note = f"STAFF_ONLY_REFLECTION_NOTE_{unique}"
        private_medical_marker = f"PRIVATE_MEDICAL_REFLECTION_{unique}"
        student = Student.objects.create(
            club=club,
            first_name="Reflection",
            last_name="Student",
            phone=f"+15558{phone_seed}1",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
            parent_user=parent_user,
            contraindications=private_medical_marker,
        )
        foreign_child = Student.objects.create(
            club=club,
            first_name="Foreign",
            last_name="Child",
            phone=f"+15558{phone_seed}2",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=foreign_parent_user,
        )
        attention_child = Student.objects.create(
            club=club,
            first_name="Attention",
            last_name="Child",
            phone=f"+15558{phone_seed}3",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        StudentNote.objects.create(
            club=club,
            student=student,
            author=trainer_user,
            text=staff_only_note,
        )
        student_grade = StudentGrade.objects.create(
            club=club,
            student=student,
            grade_system=grade_system,
            current_grade=start_grade,
            trainings_since_last_grade=0,
        )
        StudentGrade.objects.create(
            club=club,
            student=attention_child,
            grade_system=grade_system,
            current_grade=next_grade,
            trainings_since_last_grade=0,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=5,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        attention_subscription = Subscription.objects.create(
            club=club,
            student=attention_child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=1,
            trainings_used=4,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            starts_on=checkin_date,
            ends_on=checkin_date,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=student.id,
            schedule_id=upcoming_schedule.id,
            starts_on=upcoming_date,
        )
        enroll_student_in_schedule(
            club_id=club.id,
            student_id=attention_child.id,
            schedule_id=upcoming_schedule.id,
            starts_on=upcoming_date,
        )
        debt_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=debt_schedule,
            training_type=debt_training_type,
            trainer=trainer,
            location=location,
            date=debt_date,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        debt = Debt.objects.create(
            club=club,
            student=student,
            checkin=debt_checkin,
            tariff_price=debt_training_type.drop_in_price,
            reason="no_subscription",
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "phone_suffix": student.phone[-4:],
            "trainer": {
                "email": trainer_user.email,
                "password": trainer_password,
                "user_id": trainer_user.id,
            },
            "student": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
                "student_id": student.id,
                "name": str(student),
            },
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
            },
            "foreign_child_id": foreign_child.id,
            "schedule_id": schedule.id,
            "upcoming_schedule_id": upcoming_schedule.id,
            "debt_schedule_id": debt_schedule.id,
            "training_type_id": training_type.id,
            "debt_training_type_id": debt_training_type.id,
            "subscription_id": subscription.id,
            "attention_subscription_id": attention_subscription.id,
            "student_grade_id": student_grade.id,
            "debt_checkin_id": debt_checkin.id,
            "debt_id": debt.id,
            "checkin_date": checkin_date.isoformat(),
            "debt_checkin_date": debt_date.isoformat(),
            "upcoming_date": upcoming_date.isoformat(),
            "attention_child": {
                "student_id": attention_child.id,
                "name": str(attention_child),
                "first_name": attention_child.first_name,
                "last_name": attention_child.last_name,
            },
            "expected": {
                "attendance_count_after": 2,
                "open_debt_count": 1,
                "trainings_left_before": 5,
                "trainings_left_after": 4,
                "trainings_used_after": 1,
                "trainings_total": tariff.trainings_limit,
                "staff_only_note": staff_only_note,
                "private_medical_marker": private_medical_marker,
                "foreign_child_name": str(foreign_child),
                "group_name": schedule.group_name,
                "tariff_name": tariff.name,
                "training_type_name": training_type.name,
                "debt_group_name": debt_schedule.group_name,
                "debt_training_type_name": debt_training_type.name,
                "debt_amount": f"{debt_training_type.drop_in_price:.2f}",
                "grade_system_name": grade_system.discipline,
                "current_grade_name": start_grade.name,
                "next_grade_name": next_grade.name,
                "grade_trainings_since_last_after": 1,
                "grade_trainings_to_next_after": 9,
                "next_grade_min_trainings": next_grade.min_trainings,
                "upcoming_group_name": upcoming_schedule.group_name,
                "upcoming_start_time": upcoming_schedule.start_time.strftime("%H:%M:%S"),
                "upcoming_end_time": upcoming_schedule.end_time.strftime("%H:%M:%S"),
                "upcoming_trainer_name": f"{trainer.first_name} {trainer.last_name}",
                "upcoming_location_name": location.name,
                "attention_child_name": str(attention_child),
                "attention_child_first_name": attention_child.first_name,
                "attention_grade_name": next_grade.name,
                "attention_trainings_left": attention_subscription.trainings_left,
                "attention_trainings_used": attention_subscription.trainings_used,
                "attention_trainings_total": tariff.trainings_limit,
                "trainer_roster_name": f"{student.last_name} {student.first_name}",
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@student-parent-reflection-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
