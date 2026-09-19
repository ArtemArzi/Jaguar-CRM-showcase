from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Debt, Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.feedback.models import FeedbackAnswer, FeedbackForm, FeedbackQuestion, FeedbackResponse
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student, StudentNote
from apps.trainers.models import Trainer, TrainerLocation, TrainerPackageAllocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for trainer student cockpit scope E2E."

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
                "Prepared trainer student cockpit scope E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-student-cockpit-scope-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerCockpitScope-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Cockpit Scope E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Trainer Cockpit Scope E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Cockpit Scope Hall {fixture_id}",
            address="Trainer student cockpit scope E2E fixture",
        )
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Cockpit",
            last_name="Trainer",
            phone=f"+15557{phone_seed}0",
            user=trainer_user,
        )
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="OtherCockpit",
            last_name="Trainer",
            phone=f"+15557{phone_seed}1",
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        grade_system = GradeSystem.objects.create(club=club, discipline=f"Cockpit BJJ {fixture_id}")
        white = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="White",
            order=1,
            min_trainings=0,
        )
        blue = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Blue",
            order=2,
            min_trainings=3,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Cockpit Group {fixture_id}",
            slug=f"cockpit-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Cockpit Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=timezone.datetime.strptime("10:00", "%H:%M").time(),
            end_time=timezone.datetime.strptime("11:00", "%H:%M").time(),
            group_name=f"Cockpit Scope Group {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
        )
        other_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=timezone.datetime.strptime("12:00", "%H:%M").time(),
            end_time=timezone.datetime.strptime("13:00", "%H:%M").time(),
            group_name=f"Other Cockpit Scope Group {fixture_id}",
            trainer=other_trainer,
            location=location,
            training_type=training_type,
        )

        assigned = self._create_student(
            club=club,
            first_name="Scoped",
            last_name="Student",
            phone=f"+15558{phone_seed}1",
            trainer=trainer,
        )
        unassigned = self._create_student(
            club=club,
            first_name="Hidden",
            last_name="Unassigned",
            phone=f"+15558{phone_seed}2",
            trainer=None,
        )
        package_owned = self._create_student(
            club=club,
            first_name="PackageOwned",
            last_name="Student",
            phone=f"+15558{phone_seed}5",
            trainer=None,
        )
        other_trainer_student = self._create_student(
            club=club,
            first_name="Hidden",
            last_name="OtherTrainer",
            phone=f"+15558{phone_seed}3",
            trainer=other_trainer,
        )
        foreign_club = Club.objects.create(
            name=f"Foreign Cockpit Scope E2E {fixture_id}",
            city="E2E",
            disciplines=["boxing"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=foreign_club,
            primary_color="#222222",
            accent_color="#00AA88",
            club_name_display="Foreign Cockpit Scope E2E",
        )
        foreign_student = self._create_student(
            club=foreign_club,
            first_name="Foreign",
            last_name="Student",
            phone=f"+15558{phone_seed}4",
            trainer=None,
        )

        assigned_subscription = self._create_subscription(
            club=club,
            student=assigned,
            tariff=tariff,
            now=now,
        )
        unassigned_subscription = self._create_subscription(
            club=club,
            student=unassigned,
            tariff=tariff,
            now=now,
        )
        package_owned_subscription = self._create_subscription(
            club=club,
            student=package_owned,
            tariff=tariff,
            now=now,
        )
        package_owned_allocation = TrainerPackageAllocation.objects.create(
            club=club,
            subscription=package_owned_subscription,
            student=package_owned,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=trainer,
            source=TrainerPackageAllocation.Source.MANUAL_SUBSCRIPTION,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=tariff.trainings_limit,
            amount_snapshot=tariff.price,
            activated_at=now,
            created_by=trainer_user,
        )
        unassigned_checkin = Checkin.objects.create(
            club=club,
            student=unassigned,
            schedule=other_schedule,
            training_type=training_type,
            trainer=other_trainer,
            location=location,
            date=today,
            source=Checkin.Source.BATCH,
            is_debt=True,
        )
        unassigned_debt = Debt.objects.create(
            club=club,
            student=unassigned,
            checkin=unassigned_checkin,
            tariff_price=training_type.drop_in_price,
            reason="no_subscription",
        )
        assigned_grade = StudentGrade.objects.create(
            club=club,
            student=assigned,
            grade_system=grade_system,
            current_grade=white,
            trainings_since_last_grade=3,
        )
        unassigned_grade = StudentGrade.objects.create(
            club=club,
            student=unassigned,
            grade_system=grade_system,
            current_grade=white,
            trainings_since_last_grade=3,
        )
        StudentNote.objects.create(
            club=club,
            student=unassigned,
            author=trainer_user,
            text=f"Hidden unassigned note {fixture_id}",
        )
        assigned_note = StudentNote.objects.create(
            club=club,
            student=assigned,
            author=trainer_user,
            text=f"Visible assigned note {fixture_id}",
        )
        history_form = FeedbackForm.objects.create(
            club=club,
            name=f"Cockpit Scope History Survey {fixture_id}",
            trigger_type="manual",
            is_active=False,
        )
        history_question = FeedbackQuestion.objects.create(
            club=club,
            form=history_form,
            question_type="rating",
            text=f"Cockpit history rating {fixture_id}",
            order=1,
            is_required=False,
        )
        assigned_feedback_response = FeedbackResponse.objects.create(
            club=club,
            form=history_form,
            student=assigned,
        )
        FeedbackAnswer.objects.create(
            club=club,
            response=assigned_feedback_response,
            question=history_question,
            rating_value=4,
        )

        form = FeedbackForm.objects.create(
            club=club,
            name=f"Cockpit Scope Survey {fixture_id}",
            trigger_type="trial",
            is_active=True,
        )
        question = FeedbackQuestion.objects.create(
            club=club,
            form=form,
            question_type="rating",
            text="Rate training",
            order=1,
            is_required=False,
        )
        feedback_response = FeedbackResponse.objects.create(
            club=club,
            form=form,
            student=unassigned,
        )
        FeedbackAnswer.objects.create(
            club=club,
            response=feedback_response,
            question=question,
            rating_value=5,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "foreign_club_id": foreign_club.id,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "assigned_student": self._student_fixture(assigned),
            "unassigned_student": self._student_fixture(unassigned),
            "package_owned_student": self._student_fixture(package_owned),
            "other_trainer_student": self._student_fixture(other_trainer_student),
            "foreign_student": self._student_fixture(foreign_student),
            "ids": {
                "assigned_subscription_id": assigned_subscription.id,
                "assigned_note_id": assigned_note.id,
                "unassigned_subscription_id": unassigned_subscription.id,
                "package_owned_subscription_id": package_owned_subscription.id,
                "package_owned_allocation_id": package_owned_allocation.id,
                "unassigned_debt_id": unassigned_debt.id,
                "tariff_id": tariff.id,
                "assigned_student_grade_id": assigned_grade.id,
                "unassigned_student_grade_id": unassigned_grade.id,
                "grade_system_id": grade_system.id,
                "current_grade_id": white.id,
                "next_grade_id": blue.id,
                "feedback_form_id": form.id,
                "feedback_question_id": question.id,
                "assigned_feedback_form_id": history_form.id,
                "assigned_feedback_question_id": history_question.id,
                "assigned_feedback_response_id": assigned_feedback_response.id,
            },
            "expected": {
                "assigned_feedback_question": history_question.text,
                "assigned_feedback_rating_text": "Оценка 4/5",
                "assigned_note_text": assigned_note.text,
                "assigned_tariff_name": tariff.name,
                "assigned_trainings_left_text": (
                    f"Осталось {tariff.trainings_limit}/{tariff.trainings_limit} тренировок"
                ),
                "assigned_access_eligibility_text": "Можно открыть кабинет",
                "package_owned_tariff_name": tariff.name,
                "package_owned_trainings_left_text": (
                    f"Осталось {tariff.trainings_limit}/{tariff.trainings_limit} тренировок"
                ),
            },
            "created_at": now.isoformat(),
        }

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-student-cockpit-scope-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)

    def _create_student(
        self,
        *,
        club: Club,
        first_name: str,
        last_name: str,
        phone: str,
        trainer: Trainer | None,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )

    def _create_subscription(self, *, club: Club, student: Student, tariff: Tariff, now) -> Subscription:
        return Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=tariff.trainings_limit,
            trainings_used=0,
            paid_amount=tariff.price,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
        )

    def _student_fixture(self, student: Student) -> dict:
        return {
            "id": student.id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "full_name": f"{student.first_name} {student.last_name}".strip(),
            "phone": student.phone,
        }
