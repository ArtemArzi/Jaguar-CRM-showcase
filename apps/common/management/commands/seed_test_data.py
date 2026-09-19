"""
Comprehensive seed command to populate test data for the entire CRM Jaguar system.
This is a local/demo support command. It intentionally depends on test factories
and is not part of the production deploy path.

Usage:
    python manage.py seed_test_data --club-id=1
    python manage.py seed_test_data --club-id=1 --students=20 --schedules=10
    python manage.py seed_test_data --new-club
    python manage.py seed_test_data --new-club --name="Test Club" --city="Moscow"
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
)
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import Club, Location
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    ClubSettingsFactory,
    LocationFactory,
    UserFactory,
)
from apps.grades.tests.factories import GradeFactory, GradeSystemFactory, StudentGradeFactory
from apps.onboarding.tests.factories import OnboardingDraftFactory
from apps.students.models import Student
from apps.students.tests.factories import ParentInviteFactory, StudentFactory, StudentNoteFactory
from apps.trainers.models import Trainer
from apps.trainers.tests.factories import TrainerEarningFactory, TrainerFactory, TrainerLocationFactory

User = get_user_model()


class SeedDataManager:
    """Manager class to coordinate seeding all test data."""

    def __init__(self, club: Club, verbose: bool = False):
        self.club = club
        self.verbose = verbose
        self.stats = {
            "users": 0,
            "trainers": 0,
            "students": 0,
            "schedules": 0,
            "checkins": 0,
            "subscriptions": 0,
            "grade_systems": 0,
            "payments": 0,
        }

    def log(self, msg: str):
        """Print message if verbose mode is enabled."""
        if self.verbose:
            print(f"  → {msg}")

    def _role_user(self, role: str):
        """Create deterministic, globally unique role users for this club."""
        return UserFactory(
            username=f"club{self.club.id}_{role}",
            email=f"{role}.club{self.club.id}@example.com",
        )

    def seed_users(self) -> dict:
        """Create club owner, admin, trainer, student, and parent users."""
        self.log("Creating users...")

        owner = self._role_user("owner")
        admin = self._role_user("admin")
        trainer = self._role_user("trainer")
        student = self._role_user("student")
        parent = self._role_user("parent")

        # Create club memberships with different roles
        ClubMembershipFactory(user=owner, club=self.club, role="owner")
        ClubMembershipFactory(user=admin, club=self.club, role="admin")
        ClubMembershipFactory(user=trainer, club=self.club, role="trainer")
        ClubMembershipFactory(user=student, club=self.club, role="student")
        ClubMembershipFactory(user=parent, club=self.club, role="parent")

        self.stats["users"] = 5

        return {
            "owner": owner,
            "admin": admin,
            "trainer": trainer,
            "student": student,
            "parent": parent,
        }

    def seed_locations(self, count: int = 3) -> list[Location]:
        """Create multiple club locations."""
        self.log(f"Creating {count} locations...")
        locations = [
            LocationFactory(
                club=self.club,
                name=f"{self.club.name} - Location {i+1}",
                address=f"Address {i+1}, {self.club.city}"
            )
            for i in range(count)
        ]
        return locations

    def seed_trainers(
        self,
        locations: list[Location],
        count: int = 5,
        users: dict | None = None,
    ) -> list[Trainer]:
        """Create trainers and assign them to locations with rates."""
        self.log(f"Creating {count} trainers with locations...")
        trainers = []

        for i in range(count):
            trainer_kwargs = {}
            if i == 0 and users is not None:
                trainer_kwargs["user"] = users["trainer"]
            trainer = TrainerFactory(
                club=self.club,
                first_name=f"Trainer{i+1}",
                last_name="Coach",
                is_active=True,
                **trainer_kwargs,
            )

            # Assign trainer to locations with varying rates
            for location in locations[:2]:  # Assign to first 2 locations
                TrainerLocationFactory(
                    club=self.club,
                    trainer=trainer,
                    location=location,
                    rate_group=Decimal("20.00") + Decimal(i),
                    rate_personal=Decimal("50.00") + Decimal(i * 5),
                    rate_mini_group=Decimal("40.00") + Decimal(i),
                )

            trainers.append(trainer)

        self.stats["trainers"] = count
        return trainers

    def seed_training_types(self, count: int = 3) -> list[TrainingType]:
        """Create training types (disciplines)."""
        self.log(f"Creating {count} training types...")
        kinds = [
            TrainingType.Kind.GROUP,
            TrainingType.Kind.PERSONAL,
            TrainingType.Kind.MINI_GROUP,
        ]
        types = [
            TrainingTypeFactory(
                club=self.club,
                name=f"Training Type {i+1}",
                slug=f"type-{i+1}",
                kind=kinds[i % len(kinds)],
                is_active=True,
            )
            for i in range(count)
        ]
        return types

    def seed_tariffs(self, training_types: list[TrainingType], locations: list[Location]):
        """Create tariffs for each training type."""
        self.log("Creating tariffs...")
        tariffs = []

        for tt in training_types:
            # Club-level tariff
            tariff = TariffFactory(
                club=self.club,
                training_type=tt,
                name=f"{tt.name} - 8 trainings",
                price=Decimal("5000.00"),
                trainings_limit=8,
                duration_days=30,
                scope="club",
                location=None,
            )
            tariffs.append(tariff)

            # Location-specific tariff
            for loc in locations[:1]:  # One location-specific tariff
                loc_tariff = TariffFactory(
                    club=self.club,
                    training_type=tt,
                    name=f"{tt.name} - {loc.name} - 4 trainings",
                    price=Decimal("3000.00"),
                    trainings_limit=4,
                    duration_days=30,
                    scope="location",
                    location=loc,
                )
                tariffs.append(loc_tariff)

        return tariffs

    def seed_students(self, count: int = 15, users: dict = None) -> list[Student]:
        """Create students with varying statuses and subscriptions."""
        self.log(f"Creating {count} students...")
        students = []
        statuses = ["lead", "trial", "active", "at_risk", "churned"]
        parent_child_assigned = False
        student_user_assigned = False

        for i in range(count):
            status = statuses[i % len(statuses)]
            is_child = i % 3 == 0
            parent_user = users["parent"] if is_child and not parent_child_assigned else None
            parent_child_assigned = parent_child_assigned or parent_user is not None
            student_user = None
            if (
                users is not None
                and not student_user_assigned
                and parent_user is None
                and status in ["active", "trial"]
            ):
                student_user = users["student"]
                student_user_assigned = True
            student = StudentFactory(
                club=self.club,
                first_name=f"Student{i+1}",
                last_name="Test",
                phone=f"+7900{i:07d}",
                status=status,
                email=f"student{i+1}@test.com",
                is_child=is_child,  # Every 3rd student is a child
                user=student_user,
                parent_user=parent_user,
            )
            if parent_user is not None:
                student.status = "active"
                student.last_visit_date = date.today() - timedelta(days=2)
                student.save(update_fields=["status", "last_visit_date", "updated_at"])
            students.append(student)

        self.stats["students"] = count
        return students

    def seed_subscriptions(
        self,
        students: list[Student],
        tariffs: list[Tariff],
        trainers: list[Trainer] | None = None,
        users: dict | None = None,
    ):
        """Create subscriptions for students."""
        self.log(f"Creating subscriptions for {len(students)} students...")

        import random

        from apps.billing.models import TrainingType

        active_trainers = [t for t in (trainers or []) if t.is_active]

        for i, student in enumerate(students):
            if student.status in ["active", "trial"]:
                tariff = tariffs[i % len(tariffs)]
                sub = SubscriptionFactory(
                    club=self.club,
                    student=student,
                    tariff=tariff,
                    status="active",
                    trainings_left=tariff.trainings_limit - (i % 3),
                    trainings_used=i % 3,
                    expires_at=timezone.now() + timedelta(days=30 - (i % 20)),
                )
                self.stats["subscriptions"] += 1

                # Add payment for subscription
                if i % 2 == 0:
                    # T1: GROUP subs get a random active seller-trainer so the
                    # sale-earning path is exercised in seeded data.
                    seller = None
                    if (
                        active_trainers
                        and tariff.training_type.kind == TrainingType.Kind.GROUP
                    ):
                        seller = random.choice(active_trainers)
                    payment_kwargs = {}
                    if users is not None:
                        payment_kwargs["recorded_by"] = users["admin"]
                    PaymentFactory(
                        club=self.club,
                        student=student,
                        tariff=tariff,
                        subscription=sub,
                        amount=tariff.price,
                        status="confirmed",
                        seller_trainer=seller,
                        **payment_kwargs,
                    )
                    self.stats["payments"] += 1

    def seed_schedules(
        self,
        trainers: list[Trainer],
        locations: list[Location],
        training_types: list[TrainingType],
        count: int = 10
    ):
        """Create training schedules."""
        self.log(f"Creating {count} schedules...")
        schedules = []
        days = [0, 1, 2, 3, 4, 5, 6]  # Monday to Sunday

        for i in range(count):
            trainer = trainers[i % len(trainers)]
            location = locations[i % len(locations)]
            training_type = training_types[i % len(training_types)]
            day = days[i % len(days)]

            schedule = ScheduleFactory(
                club=self.club,
                day_of_week=day,
                start_time=time(10 + (i % 8), 0),
                end_time=time(11 + (i % 8), 0),
                group_name=f"Class {i+1}",
                trainer=trainer,
                location=location,
                training_type=training_type,
                is_active=True,
            )
            schedules.append(schedule)

        self.stats["schedules"] = count
        return schedules

    def seed_checkins(
        self,
        students: list[Student],
        schedules: list[Schedule],
        subscriptions_by_student: dict,
        count: int = 30
    ):
        """Create check-ins (attendance records)."""
        self.log(f"Creating {count} check-ins...")

        active_students = [s for s in students if s.status in ["active", "trial"]]

        for i in range(count):
            student = active_students[i % len(active_students)]
            schedule = schedules[i % len(schedules)]

            checkin = CheckinFactory(
                club=self.club,
                student=student,
                schedule=schedule,
                training_type=schedule.training_type,
                trainer=schedule.trainer,
                location=schedule.location,
                date=date.today() - timedelta(days=i % 30),
                source="batch",
                subscription=subscriptions_by_student.get(student.id),
            )

            # Create trainer earning record
            if i % 3 != 0:
                TrainerEarningFactory(
                    club=self.club,
                    trainer=schedule.trainer,
                    checkin=checkin,
                    earning_type=schedule.training_type.kind,
                    amount=Decimal("500.00"),
                    rate_percent=Decimal("20.00"),
                )

            self.stats["checkins"] += 1

    def seed_grade_systems(self, count: int = 2):
        """Create grade systems (belt ranks, etc.)."""
        self.log(f"Creating {count} grade systems...")

        disciplines = ["Karate", "BJJ", "Boxing", "Muay Thai"][:count]

        for discipline in disciplines:
            gs = GradeSystemFactory(
                club=self.club,
                discipline=discipline,
                is_active=True,
            )

            # Create grades
            grades_data = [
                ("White", 0),
                ("Yellow", 3),
                ("Orange", 6),
                ("Green", 9),
                ("Blue", 12),
                ("Brown", 15),
                ("Black", 18),
            ]

            for name, min_trainings in grades_data[:4]:  # Create 4 grades per system
                GradeFactory(
                    club=self.club,
                    grade_system=gs,
                    name=name,
                    min_trainings=min_trainings,
                )

            self.stats["grade_systems"] += 1

    def seed_schedule_exceptions(self, schedules: list[Schedule]):
        """Create schedule exceptions (cancellations, reschedules)."""
        self.log("Creating schedule exceptions...")

        for i, schedule in enumerate(schedules[:len(schedules) // 3]):  # 1/3 have exceptions
            ScheduleExceptionFactory(
                club=self.club,
                schedule=schedule,
                date=date.today() + timedelta(days=(i % 7) + 1),
                exception_type="cancelled" if i % 2 == 0 else "rescheduled",
            )

    def seed_group_sessions(self, schedules: list[Schedule]):
        """Create group session records."""
        self.log("Creating group sessions...")

        for i, schedule in enumerate(schedules[:len(schedules) // 2]):
            GroupSessionFactory(
                club=self.club,
                schedule=schedule,
                date=date.today() - timedelta(days=i),
                trainer=schedule.trainer,
                attendee_count=5 + (i % 8),
                topic_tags=["technique", "sparring"] if i % 2 == 0 else ["conditioning"],
            )

    def enrich_parent_demo_child(self, students: list[Student]):
        """Ensure the demo parent account opens an informative single-child portal."""
        parent_child = next((student for student in students if student.parent_user_id), None)
        if not parent_child:
            return

        grade_system = GradeSystemFactory(
            club=self.club,
            discipline="Muay Thai Kids",
            is_active=True,
        )
        white = GradeFactory(
            club=self.club,
            grade_system=grade_system,
            name="White",
            order=0,
            min_trainings=0,
        )
        yellow = GradeFactory(
            club=self.club,
            grade_system=grade_system,
            name="Yellow",
            order=1,
            min_trainings=8,
        )
        StudentGradeFactory(
            club=self.club,
            student=parent_child,
            grade_system=grade_system,
            current_grade=white,
            trainings_since_last_grade=5,
        )
        self.log(f"Parent demo child enriched: {parent_child} -> next grade {yellow.name}")

    def enrich_student_demo_account(self, students: list[Student]):
        """Ensure the demo student account has grade progress in the PWA."""
        student = next((candidate for candidate in students if candidate.user_id), None)
        if not student:
            return

        grade_system = GradeSystemFactory(
            club=self.club,
            discipline="Muay Thai Students",
            is_active=True,
        )
        white = GradeFactory(
            club=self.club,
            grade_system=grade_system,
            name="White",
            order=0,
            min_trainings=0,
        )
        yellow = GradeFactory(
            club=self.club,
            grade_system=grade_system,
            name="Yellow",
            order=1,
            min_trainings=8,
        )
        StudentGradeFactory(
            club=self.club,
            student=student,
            grade_system=grade_system,
            current_grade=white,
            trainings_since_last_grade=4,
        )
        self.log(f"Student demo account enriched: {student} -> next grade {yellow.name}")

    def seed_parent_invites(self, students: list[Student]):
        """Create parent invite records for child students."""
        self.log("Creating parent invites...")

        for student in students[:len(students) // 3]:
            if student.is_child:
                ParentInviteFactory(
                    club=self.club,
                    student=student,
                    expires_at=timezone.now() + timedelta(days=7),
                )

    def seed_student_notes(self, students: list[Student], users: dict):
        """Create student notes."""
        self.log("Creating student notes...")

        for i, student in enumerate(students[:len(students) // 2]):
            author = users["trainer"] if i % 2 == 0 else users["admin"]
            StudentNoteFactory(
                club=self.club,
                student=student,
                author=author,
                text=f"Note {i+1}: Progress is {['good', 'needs work', 'excellent'][i % 3]}",
            )

    def seed_onboarding_draft(self):
        """Create an onboarding draft for the club."""
        self.log("Creating onboarding draft...")

        OnboardingDraftFactory(
            club=self.club,
            current_step=3,  # Schedule step
            data={"_schema_version": 2, "_skipped_steps": [1]},
            is_completed=False,
        )

    def run_seed(
        self,
        students: int = 15,
        schedules: int = 10,
        checkins: int = 30,
        locations: int = 3,
        trainers: int = 5,
        training_types: int = 3,
        grade_systems: int = 2,
    ):
        """Main method to run all seeding operations."""
        with transaction.atomic():
            users = self.seed_users()
            locs = self.seed_locations(locations)
            # Training types must exist BEFORE trainers so that
            # TrainerLocationFactory can fan rates out into TrainerRate.
            tt_list = self.seed_training_types(training_types)
            trainers_list = self.seed_trainers(locs, trainers, users=users)
            tariffs = self.seed_tariffs(tt_list, locs)
            students_list = self.seed_students(students, users)

            # Create subscriptions and collect mapping
            self.seed_subscriptions(students_list, tariffs, trainers_list, users=users)
            subscriptions_by_student = {
                sub.student_id: sub
                for sub in Subscription.objects.for_club(self.club).select_related("student")
            }

            schedules_list = self.seed_schedules(trainers_list, locs, tt_list, schedules)
            self.seed_checkins(students_list, schedules_list, subscriptions_by_student, checkins)
            self.seed_grade_systems(grade_systems)
            self.enrich_parent_demo_child(students_list)
            self.enrich_student_demo_account(students_list)
            self.seed_schedule_exceptions(schedules_list)
            self.seed_group_sessions(schedules_list)
            self.seed_parent_invites(students_list)
            self.seed_student_notes(students_list, users)
            self.seed_onboarding_draft()

        return self.stats


class Command(BaseCommand):
    help = "Seed comprehensive test data for the entire CRM system"

    def add_arguments(self, parser):
        parser.add_argument(
            "--club-id",
            type=int,
            help="ID of existing club to seed data for",
        )
        parser.add_argument(
            "--new-club",
            action="store_true",
            help="Create a new club and seed data for it",
        )
        parser.add_argument(
            "--name",
            type=str,
            default="Test Club",
            help="Name for new club (used with --new-club)",
        )
        parser.add_argument(
            "--city",
            type=str,
            default="Moscow",
            help="City for new club (used with --new-club)",
        )
        parser.add_argument(
            "--students",
            type=int,
            default=15,
            help="Number of students to create (default: 15)",
        )
        parser.add_argument(
            "--schedules",
            type=int,
            default=10,
            help="Number of schedules to create (default: 10)",
        )
        parser.add_argument(
            "--checkins",
            type=int,
            default=30,
            help="Number of check-ins to create (default: 30)",
        )
        parser.add_argument(
            "--locations",
            type=int,
            default=3,
            help="Number of locations to create (default: 3)",
        )
        parser.add_argument(
            "--trainers",
            type=int,
            default=5,
            help="Number of trainers to create (default: 5)",
        )
        parser.add_argument(
            "--training-types",
            type=int,
            default=3,
            help="Number of training types to create (default: 3)",
        )
        parser.add_argument(
            "--grade-systems",
            type=int,
            default=2,
            help="Number of grade systems to create (default: 2)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed progress messages",
        )

    def handle(self, *args, **options):
        count_requirements = {
            "students": ("--students", 1),
            "schedules": ("--schedules", 1),
            "locations": ("--locations", 1),
            "trainers": ("--trainers", 1),
            "training_types": ("--training-types", 1),
            "checkins": ("--checkins", 0),
            "grade_systems": ("--grade-systems", 0),
        }
        for option_name, (flag, minimum) in count_requirements.items():
            if options[option_name] < minimum:
                raise CommandError(f"{flag} must be at least {minimum}")

        # Determine target club
        club_id = options["club_id"]
        new_club = options["new_club"]

        if not club_id and not new_club:
            raise CommandError("Specify either --club-id or --new-club")

        if club_id and new_club:
            raise CommandError("Cannot specify both --club-id and --new-club")

        if new_club:
            club = ClubFactory(
                name=options["name"],
                city=options["city"],
                disciplines=["boxing", "kickboxing"],
            )
            ClubSettingsFactory(club=club)
            self.stdout.write(
                self.style.SUCCESS(f"✓ Created new club: {club.name} (ID: {club.id})")
            )
        else:
            try:
                club = Club.objects.get(id=club_id)
            except Club.DoesNotExist as exc:
                raise CommandError(f"Club with ID {club_id} not found") from exc

            seeded_usernames = [
                f"club{club.id}_{role}"
                for role in ("owner", "admin", "trainer", "student", "parent")
            ]
            if User.objects.filter(username__in=seeded_usernames).exists():
                raise CommandError(
                    "Club already appears to contain seed_test_data demo users; "
                    "create a new club for a fresh dataset."
                )

        # Run seeding
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"Seeding test data for club: {club.name}")
        self.stdout.write(f"{'='*60}\n")

        manager = SeedDataManager(club, verbose=options["verbose"])
        stats = manager.run_seed(
            students=options["students"],
            schedules=options["schedules"],
            checkins=options["checkins"],
            locations=options["locations"],
            trainers=options["trainers"],
            training_types=options["training_types"],
            grade_systems=options["grade_systems"],
        )

        # Print summary
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(self.style.SUCCESS("✓ Seeding completed successfully!"))
        self.stdout.write(f"{'='*60}\n")

        for key, value in stats.items():
            self.stdout.write(f"  {key:.<30} {value}")

        self.stdout.write(f"\n✓ Club ID: {club.id}")
        self.stdout.write(f"✓ Admin URL: /admin/clubs/club/{club.id}/change/")
