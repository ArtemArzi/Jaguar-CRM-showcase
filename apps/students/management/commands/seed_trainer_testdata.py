"""Seed realistic test data for trainer PWA: students, grades, checkins, subscriptions, retention tasks."""

import random
from datetime import date, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer

# Synthetic fixtures only: generated names and sequential test phone numbers.
STUDENTS_DATA = [
    {"first_name": "Демо", "last_name": f"Ученик{index}", "phone": f"+7900100000{index}", "status": status}
    for index, status in enumerate(
        [Student.Status.ACTIVE, Student.Status.TRIAL, Student.Status.AT_RISK,
         Student.Status.LEAD, Student.Status.CHURNED], start=1
    )
]


GRADE_NAMES = [
    ("Белый", 0),
    ("Жёлтый", 10),
    ("Зелёный", 30),
    ("Синий", 60),
    ("Коричневый", 100),
]


class Command(BaseCommand):
    help = "Seed test data for trainer PWA (students, grades, checkins, subscriptions, tasks)"

    def add_arguments(self, parser):
        parser.add_argument("--club-id", type=int, required=True, help="Club ID to seed data for")
        parser.add_argument("--trainer-id", type=int, required=True, help="Trainer ID to assign")
        parser.add_argument("--force", action="store_true", help="Seed even if club already has >3 students")

    def handle(self, *args, **options):
        club_id: int = options["club_id"]
        trainer_id: int = options["trainer_id"]
        force: bool = options["force"]

        try:
            club = Club.objects.get(pk=club_id)
        except Club.DoesNotExist:
            raise CommandError(f"Club with id={club_id} not found")

        try:
            trainer = Trainer.objects.for_club(club).get(pk=trainer_id)
        except Trainer.DoesNotExist:
            raise CommandError(f"Trainer with id={trainer_id} not found in club {club_id}")

        existing_count = Student.objects.for_club(club).count()
        if existing_count > 3 and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"Club already has {existing_count} students. Use --force to seed anyway."
                )
            )
            return

        with transaction.atomic():
            self._seed(club=club, trainer=trainer)

        self.stdout.write(self.style.SUCCESS("Done! Test data seeded successfully."))

    def _seed(self, *, club: Club, trainer: Trainer) -> None:
        location = self._ensure_location(club=club)
        training_type = self._ensure_training_type(club=club)
        students = self._create_students(club=club)
        grade_system, grades = self._create_grades(club=club)
        self._assign_grades(club=club, students=students, grade_system=grade_system, grades=grades)
        schedule = self._ensure_schedule(club=club, trainer=trainer, location=location, training_type=training_type)
        tariff = self._ensure_tariff(club=club, training_type=training_type)
        self._create_subscriptions(club=club, students=students, tariff=tariff, location=location)
        self._create_checkins(
            club=club,
            students=students,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
        )
        self._create_retention_tasks(club=club, students=students, trainer=trainer)

    def _ensure_location(self, *, club: Club) -> Location:
        location = Location.objects.filter(club=club).first()
        if location:
            self.stdout.write(f"  Using existing location: {location.name}")
            return location
        location = Location.objects.create(club=club, name="Основной зал", address="ул. Спортивная, 1")
        self.stdout.write(f"  Created location: {location.name}")
        return location

    def _ensure_training_type(self, *, club: Club) -> TrainingType:
        tt = TrainingType.objects.for_club(club).filter(is_active=True).first()
        if tt:
            self.stdout.write(f"  Using existing training type: {tt.name}")
            return tt
        tt = TrainingType.objects.create(
            club=club, name="Муай Тай", slug="muay-thai", kind=TrainingType.Kind.GROUP, is_active=True,
        )
        self.stdout.write(f"  Created training type: {tt.name}")
        return tt

    def _create_students(self, *, club: Club) -> list[Student]:
        created = []
        for data in STUDENTS_DATA:
            student, is_new = Student.objects.get_or_create(
                club=club,
                phone=data["phone"],
                defaults={
                    "first_name": data["first_name"],
                    "last_name": data["last_name"],
                    "status": data["status"],
                    "lead_status": (
                        Student.LeadStatus.NEW if data["status"] == Student.Status.LEAD else None
                    ),
                    "source": Student.Source.OTHER,
                },
            )
            tag = "Created" if is_new else "Exists"
            self.stdout.write(f"  [{tag}] Student: {student.first_name} {student.last_name} ({student.status})")
            created.append(student)
        return created

    def _create_grades(self, *, club: Club) -> tuple[GradeSystem, list[Grade]]:
        gs, _ = GradeSystem.objects.get_or_create(
            club=club, discipline="Муай Тай", defaults={"is_active": True},
        )
        self.stdout.write(f"  Grade system: {gs.discipline}")

        grades = []
        for order, (name, min_tr) in enumerate(GRADE_NAMES, start=1):
            grade, _ = Grade.objects.get_or_create(
                club=club,
                grade_system=gs,
                order=order,
                defaults={"name": name, "min_trainings": min_tr},
            )
            grades.append(grade)
        self.stdout.write(f"  Grades: {len(grades)} levels")
        return gs, grades

    def _assign_grades(
        self,
        *,
        club: Club,
        students: list[Student],
        grade_system: GradeSystem,
        grades: list[Grade],
    ) -> None:
        # Assign grades to active, trial, at_risk students (indices 0, 1, 2)
        assignments = [
            (students[0], grades[2], 35),  # active -> green, 35 trainings
            (students[1], grades[0], 3),   # trial -> white, 3 trainings
            (students[2], grades[1], 15),  # at_risk -> yellow, 15 trainings
        ]
        for student, grade, trainings in assignments:
            sg, created = StudentGrade.objects.get_or_create(
                student=student,
                grade_system=grade_system,
                defaults={
                    "club": club,
                    "current_grade": grade,
                    "trainings_since_last_grade": trainings,
                },
            )
            if not created:
                sg.current_grade = grade
                sg.trainings_since_last_grade = trainings
                sg.save(update_fields=["current_grade", "trainings_since_last_grade"])
            self.stdout.write(f"  Grade assigned: {student.first_name} -> {grade.name}")

    def _ensure_schedule(
        self,
        *,
        club: Club,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
    ) -> Schedule:
        schedule = Schedule.objects.for_club(club).filter(trainer=trainer, is_active=True).first()
        if schedule:
            self.stdout.write(f"  Using existing schedule: {schedule.group_name}")
            return schedule
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=0,  # Monday
            start_time=time(18, 0),
            end_time=time(19, 30),
            group_name="Муай Тай (взрослые)",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        self.stdout.write(f"  Created schedule: {schedule.group_name}")
        return schedule

    def _ensure_tariff(self, *, club: Club, training_type: TrainingType) -> Tariff:
        tariff = Tariff.objects.for_club(club).filter(is_active=True).first()
        if tariff:
            self.stdout.write(f"  Using existing tariff: {tariff.name}")
            return tariff
        tariff = Tariff.objects.create(
            club=club,
            name="Абонемент 12 тренировок",
            training_type=training_type,
            price=4500,
            trainings_limit=12,
            duration_days=30,
            is_active=True,
        )
        self.stdout.write(f"  Created tariff: {tariff.name}")
        return tariff

    def _create_subscriptions(
        self,
        *,
        club: Club,
        students: list[Student],
        tariff: Tariff,
        location: Location,
    ) -> None:
        now = timezone.now()
        # Subscriptions for active and trial students (indices 0, 1)
        for student in [students[0], students[1]]:
            if Subscription.objects.for_club(club).filter(student=student, status="active").exists():
                self.stdout.write(f"  Subscription exists: {student.first_name}")
                continue
            Subscription.objects.create(
                club=club,
                student=student,
                tariff=tariff,
                status=Subscription.Status.ACTIVE,
                trainings_left=max(1, random.randint(3, 10)),
                trainings_used=random.randint(2, 8),
                expires_at=now + timedelta(days=random.randint(5, 25)),
                scope="club",
                location=location,
            )
            self.stdout.write(f"  Subscription created: {student.first_name}")

    def _create_checkins(
        self,
        *,
        club: Club,
        students: list[Student],
        schedule: Schedule,
        training_type: TrainingType,
        trainer: Trainer,
        location: Location,
    ) -> None:
        today = date.today()
        # Checkins for active, trial, at_risk (indices 0, 1, 2)
        checkin_counts = [15, 5, 10]
        for student, count in zip([students[0], students[1], students[2]], checkin_counts):
            existing = Checkin.objects.for_club(club).filter(student=student).count()
            if existing >= count:
                self.stdout.write(f"  Checkins exist ({existing}): {student.first_name}")
                continue

            need = count - existing
            created = 0
            for i in range(need):
                checkin_date = today - timedelta(days=random.randint(1, 60))
                # Skip if duplicate
                if (
                    Checkin.objects.for_club(club)
                    .filter(student=student, schedule=schedule, date=checkin_date)
                    .exists()
                ):
                    continue
                Checkin.objects.create(
                    club=club,
                    student=student,
                    schedule=schedule,
                    training_type=training_type,
                    trainer=trainer,
                    location=location,
                    date=checkin_date,
                    source=Checkin.Source.BATCH,
                )
                created += 1

            # Update last_visit_date
            last_checkin = (
                Checkin.objects.for_club(club)
                .filter(student=student, deleted_at__isnull=True)
                .order_by("-date")
                .first()
            )
            if last_checkin:
                student.last_visit_date = last_checkin.date
                student.save(update_fields=["last_visit_date"])

            self.stdout.write(f"  Checkins created ({created}): {student.first_name}")

    def _create_retention_tasks(
        self,
        *,
        club: Club,
        students: list[Student],
        trainer: Trainer,
    ) -> None:
        today = date.today()
        tasks_data = [
            (students[1], RetentionTask.Level.YELLOW, today + timedelta(days=1)),   # trial follow-up
            (students[2], RetentionTask.Level.RED, today + timedelta(days=3)),       # at_risk escalated
            (students[4], RetentionTask.Level.CHURNED, today),                        # churned
        ]
        for student, level, due in tasks_data:
            existing = RetentionTask.objects.for_club(club).filter(
                student=student, level=level, resolved_at__isnull=True,
            ).exists()
            if existing:
                self.stdout.write(f"  Retention task exists: {student.first_name} ({level})")
                continue
            RetentionTask.objects.create(
                club=club,
                student=student,
                trainer=trainer,
                level=level,
                due_date=due,
            )
            self.stdout.write(f"  Retention task created: {student.first_name} ({level})")
