from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import PersonalAvailabilitySlot, Schedule
from apps.billing.models import Subscription, SubscriptionComponent, Tariff, TariffComponent, TrainingType
from apps.billing.services import create_tariff
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for student/parent self-booking E2E."

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
                "Prepared student/parent self-booking E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(legacy_club_id={fixture['legacy']['club_id']}, "
                f"unified_club_id={fixture['unified']['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        booking_date = today + timedelta(days=1)
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"student-parent-self-booking-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        student_password = f"SelfBookingStudent-{unique}-pass"
        parent_password = f"SelfBookingParent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Self Booking E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Self Booking E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Self Booking Hall {fixture_id}",
            address="Student parent self-booking E2E fixture",
        )
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=f"SelfBookingTrainer-{unique}-pass",
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
            password=f"SelfBookingForeignParent-{unique}-pass",
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        ClubMembership.objects.create(user=foreign_parent_user, club=club, role=ClubMembership.Role.PARENT)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Self",
            last_name="Trainer",
            phone=f"+15559{phone_seed}0",
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
        group_type = TrainingType.objects.create(
            club=club,
            name=f"Self Booking Group {fixture_id}",
            slug=f"self-booking-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Self Booking Personal {fixture_id}",
            slug=f"self-booking-personal-{unique}",
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
            training_type=group_type,
            percent=Decimal("25.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            percent=Decimal("35.00"),
        )

        group_tariff = Tariff.objects.create(
            club=club,
            training_type=group_type,
            name=f"Self Booking Group Pack {fixture_id}",
            price=Decimal("7000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        personal_tariff = Tariff.objects.create(
            club=club,
            training_type=personal_type,
            name=f"Self Booking Personal Pack {fixture_id}",
            price=Decimal("12000.00"),
            trainings_limit=4,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )

        student = Student.objects.create(
            club=club,
            first_name="Self",
            last_name="Student",
            phone=f"+15550{phone_seed}1",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
        )
        child = Student.objects.create(
            club=club,
            first_name="Self",
            last_name="Child",
            phone=f"+15550{phone_seed}2",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        foreign_child = Student.objects.create(
            club=club,
            first_name="Foreign",
            last_name="SelfBookingChild",
            phone=f"+15550{phone_seed}3",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=foreign_parent_user,
        )
        for target_student in [student, child, foreign_child]:
            StudentGrade.objects.create(
                club=club,
                student=target_student,
                grade_system=grade_system,
                current_grade=grade,
                trainings_since_last_grade=0,
            )
        self._create_subscriptions(
            club=club,
            student=student,
            group_tariff=group_tariff,
            personal_tariff=personal_tariff,
            now=now,
        )
        self._create_subscriptions(
            club=club,
            student=child,
            group_tariff=group_tariff,
            personal_tariff=personal_tariff,
            now=now,
        )

        group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=booking_date.weekday(),
            start_time=time(8, 0),
            end_time=time(9, 0),
            group_name=f"Self Booking Group Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=group_type,
            one_time_date=None,
            is_active=True,
        )
        student_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=self._slot_at(today=booking_date, value=time(10, 0)),
            ends_at=self._slot_at(today=booking_date, value=time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        child_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=self._slot_at(today=booking_date, value=time(14, 0)),
            ends_at=self._slot_at(today=booking_date, value=time(15, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        legacy = {
            "fixture_id": fixture_id,
            "club_id": club.id,
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
                "child_id": child.id,
                "child_name": str(child),
            },
            "foreign_child_id": foreign_child.id,
            "booking_date": booking_date.isoformat(),
            "group_schedule_id": group_schedule.id,
            "student_personal_slot_id": student_slot.id,
            "parent_personal_slot_id": child_slot.id,
            "expected": {
                "group_name": group_schedule.group_name,
                "personal_training_type_name": personal_type.name,
                "student_personal_group_name": f"Персоналка: {student.last_name} {student.first_name}",
                "parent_personal_group_name": f"Персоналка: {child.last_name} {child.first_name}",
                "student_personal_time_label": "10:00-11:00",
                "parent_personal_time_label": "14:00-15:00",
                "trainer_name": f"{trainer.first_name} {trainer.last_name}",
                "foreign_child_name": str(foreign_child),
            },
            "created_at": now.isoformat(),
        }
        return {
            "fixture_id": fixture_id,
            # The shared real-stack runner requires a root club marker even
            # when --all-clubs also initializes the fixture's second club.
            "club_id": legacy["club_id"],
            "legacy": legacy,
            "unified": self._create_unified_fixture(now=now, unique=unique, phone_seed=phone_seed),
        }

    def _create_unified_fixture(self, *, now, unique: str, phone_seed: str) -> dict:
        """Create a separate club so one browser pack proves both flag states."""

        fixture_id = f"unified-self-service-e2e-{unique}"
        booking_date = timezone.localdate() + timedelta(days=1)
        payment_date = booking_date + timedelta(days=1)
        club = Club.objects.create(
            name=f"Jaguar Unified Self Service E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Unified Self Service E2E",
            unified_client_journey_enabled=True,
        )
        location = Location.objects.create(
            club=club,
            name=f"Unified Self Service Hall {fixture_id}",
            address="Unified student parent self-booking E2E fixture",
        )
        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=f"UnifiedTrainer-{unique}-pass",
        )
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=f"UnifiedStudent-{unique}-pass",
        )
        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=f"UnifiedParent-{unique}-pass",
        )
        payer_user = self._create_user(
            fixture_id=fixture_id,
            role="payer",
            password=f"UnifiedPayer-{unique}-pass",
        )
        other_user = self._create_user(
            fixture_id=fixture_id,
            role="other",
            password=f"UnifiedOther-{unique}-pass",
        )
        for user, role in [
            (trainer_user, ClubMembership.Role.TRAINER),
            (student_user, ClubMembership.Role.STUDENT),
            (parent_user, ClubMembership.Role.PARENT),
            (payer_user, ClubMembership.Role.STUDENT),
            (other_user, ClubMembership.Role.STUDENT),
        ]:
            ClubMembership.objects.create(user=user, club=club, role=role)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Unified",
            last_name="Trainer",
            phone=f"+155570{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        grade_system = GradeSystem.objects.create(club=club, discipline=f"Unified Muay Thai {fixture_id}")
        grade = Grade.objects.create(
            club=club,
            grade_system=grade_system,
            name="Start",
            order=0,
            min_trainings=0,
        )
        personal_type = TrainingType.objects.create(
            club=club,
            name=f"Unified Personal {fixture_id}",
            slug=f"unified-personal-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            grade_system=grade_system,
            drop_in_price=None,
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            percent=Decimal("35.00"),
        )
        tariff = create_tariff(
            club_id=club.id,
            name=f"Unified Personal Offer {fixture_id}",
            training_type_id=personal_type.id,
            price=Decimal("2700.00"),
            trainings_limit=1,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            location_id=None,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            components=[
                {
                    "name": f"Unified Personal Credit {fixture_id}",
                    "training_type_id": personal_type.id,
                    "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
                    "credits_total": 1,
                    "scope": Tariff.Scope.CLUB,
                    "location_id": None,
                    "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
                    "paid_amount_basis": Decimal("2700.00"),
                }
            ],
            is_personal_booking_default=True,
        )
        tariff_component = TariffComponent.objects.for_club(club).get(tariff=tariff)

        student = Student.objects.create(
            club=club,
            first_name="Unified",
            last_name="Student",
            phone=f"+155571{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
        )
        child = Student.objects.create(
            club=club,
            first_name="Unified",
            last_name="Child",
            phone=f"+155572{phone_seed}",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        payer = Student.objects.create(
            club=club,
            first_name="Unified",
            last_name="Payer",
            phone=f"+155573{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=payer_user,
        )
        other_actor = Student.objects.create(
            club=club,
            first_name="Unified",
            last_name="Other",
            phone=f"+155574{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=other_user,
        )
        for target_student in [student, child, payer, other_actor]:
            StudentGrade.objects.create(
                club=club,
                student=target_student,
                grade_system=grade_system,
                current_grade=grade,
                trainings_since_last_grade=0,
            )
        student_subscription = self._create_unified_entitlement(
            club=club,
            student=student,
            tariff=tariff,
            tariff_component=tariff_component,
            now=now,
        )
        child_subscription = self._create_unified_entitlement(
            club=club,
            student=child,
            tariff=tariff,
            tariff_component=tariff_component,
            now=now,
        )
        student_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=self._slot_at(today=booking_date, value=time(10, 0)),
            ends_at=self._slot_at(today=booking_date, value=time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        child_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=self._slot_at(today=booking_date, value=time(14, 0)),
            ends_at=self._slot_at(today=booking_date, value=time(15, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        payer_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=self._slot_at(today=payment_date, value=time(10, 0)),
            ends_at=self._slot_at(today=payment_date, value=time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        return {
            "club_id": club.id,
            "booking_date": booking_date.isoformat(),
            "payment_date": payment_date.isoformat(),
            "student": {
                "email": student_user.email,
                "password": f"UnifiedStudent-{unique}-pass",
                "user_id": student_user.id,
                "student_id": student.id,
                "subscription_id": student_subscription.id,
            },
            "parent": {
                "email": parent_user.email,
                "password": f"UnifiedParent-{unique}-pass",
                "user_id": parent_user.id,
                "child_id": child.id,
                "child_name": str(child),
                "subscription_id": child_subscription.id,
            },
            "payer": {
                "email": payer_user.email,
                "password": f"UnifiedPayer-{unique}-pass",
                "user_id": payer_user.id,
                "student_id": payer.id,
            },
            "other_actor": {
                "email": other_user.email,
                "password": f"UnifiedOther-{unique}-pass",
                "user_id": other_user.id,
                "student_id": other_actor.id,
            },
            "personal_tariff": {"id": tariff.id, "price": str(tariff.price)},
            "student_slot_id": student_slot.id,
            "parent_slot_id": child_slot.id,
            "payer_slot_id": payer_slot.id,
            "expected": {
                "training_type_name": personal_type.name,
                "student_time_label": "10:00",
                "parent_time_label": "14:00",
                "payer_time_label": "10:00",
            },
        }

    def _create_unified_entitlement(
        self,
        *,
        club: Club,
        student: Student,
        tariff: Tariff,
        tariff_component: TariffComponent,
        now,
    ) -> Subscription:
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=tariff.trainings_limit,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        SubscriptionComponent.objects.create(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            name_snapshot=tariff_component.name,
            training_type=tariff_component.training_type,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            credits_left=1,
            credits_used=0,
            scope=Tariff.Scope.CLUB,
            location=None,
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis_snapshot=tariff.price,
            unit_amount_basis_snapshot=tariff.price,
            is_active=True,
        )
        return subscription

    def _create_subscriptions(
        self,
        *,
        club: Club,
        student: Student,
        group_tariff: Tariff,
        personal_tariff: Tariff,
        now,
    ) -> None:
        for tariff in [group_tariff, personal_tariff]:
            Subscription.objects.create(
                club=club,
                student=student,
                tariff=tariff,
                status=Subscription.Status.ACTIVE,
                paid_amount=tariff.price,
                trainings_left=tariff.trainings_limit,
                trainings_used=0,
                expires_at=now + timedelta(days=30),
                scope=tariff.scope,
                location=None,
            )

    def _slot_at(self, *, today, value: time):
        naive = datetime.combine(today, value)
        return timezone.make_aware(naive, timezone.get_current_timezone())

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@student-parent-self-booking-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
