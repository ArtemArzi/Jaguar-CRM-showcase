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

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalDropInBooking,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    TrainingGroupRolloutState,
)
from apps.attendance.services import generate_kiosk_pin
from apps.billing.models import Tariff, TariffComponent, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated trainer personal drop-in E2E fixture."

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
                "Prepared trainer personal drop-in E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, trainer_id={fixture['trainer']['trainer_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-personal-drop-in-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerPersonalDropIn-{unique}-pass"
        owner_password = f"OwnerPersonalDropIn-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Personal Drop-in E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Personal Drop-in E2E",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        location = Location.objects.create(
            club=club,
            name=f"Personal Drop-in Hall {fixture_id}",
            address="Trainer personal drop-in E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="DropIn",
            last_name="Trainer",
            phone=f"+155570{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="Other",
            last_name="Trainer",
            phone=f"+155571{phone_seed}",
        )

        personal_training_type = TrainingType.objects.create(
            club=club,
            name=f"Personal Drop-in {fixture_id}",
            slug=f"personal-drop-in-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("2500.00"),
            trial_free=True,
        )
        personal_tariff = Tariff.objects.create(
            club=club,
            training_type=personal_training_type,
            name=f"Разовая персоналка {fixture_id}",
            price=Decimal("2500.00"),
            trainings_limit=1,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        TariffComponent.objects.create(
            club=club,
            tariff=personal_tariff,
            name=f"Разовая персоналка компонент {fixture_id}",
            training_type=personal_training_type,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            scope=Tariff.Scope.CLUB,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis=personal_tariff.price,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_training_type,
            percent=Decimal("50.00"),
        )

        assigned_lead = Student.objects.create(
            club=club,
            first_name="Assigned",
            last_name="DropInLead",
            phone=f"+155572{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.CONTACTED,
            assigned_trainer=trainer,
        )
        unrelated_lead = Student.objects.create(
            club=club,
            first_name="Unrelated",
            last_name="DropInLead",
            phone=f"+155573{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.LEAD,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=other_trainer,
        )
        availability_student = Student.objects.create(
            club=club,
            first_name="Availability",
            last_name="Client",
            phone=f"+155574{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        past_student = Student.objects.create(
            club=club,
            first_name="Past",
            last_name="NoShow",
            phone=f"+155575{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        grandfathered_trial = Student.objects.create(
            club=club,
            first_name="Grandfathered",
            last_name="PersonalTrial",
            phone=f"+155576{phone_seed}",
            email="",
            is_child=False,
            status=Student.Status.TRIAL,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
            assigned_trainer=trainer,
        )

        today = club_localdate(club)
        primary_day = today + timedelta(days=7)
        availability_day = today + timedelta(days=8)
        past_day = today - timedelta(days=2)
        legacy_trial_day = today + timedelta(days=9)
        zoneinfo = club_zoneinfo(club)
        availability_start = timezone.make_aware(datetime.combine(availability_day, time(14, 0)), zoneinfo)
        availability_end = timezone.make_aware(datetime.combine(availability_day, time(15, 0)), zoneinfo)
        availability_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_training_type,
            starts_at=availability_start,
            ends_at=availability_end,
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        past_booking = self._create_unattended_drop_in(
            club=club,
            student=past_student,
            trainer=trainer,
            location=location,
            training_type=personal_training_type,
            tariff=personal_tariff,
            actor=trainer_user,
            target_day=past_day,
            fixture_id=fixture_id,
        )
        legacy_trial_enrollment = self._create_grandfathered_personal_trial(
            club=club,
            student=grandfathered_trial,
            trainer=trainer,
            location=location,
            training_type=personal_training_type,
            actor=trainer_user,
            target_day=legacy_trial_day,
            fixture_id=fixture_id,
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "kiosk_pin": kiosk_pin,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
            },
            "owner": {
                "user_id": owner_user.id,
                "email": owner_user.email,
                "password": owner_password,
            },
            "assigned_lead": {
                "student_id": assigned_lead.id,
                "name": str(assigned_lead),
            },
            "unrelated_lead": {"student_id": unrelated_lead.id},
            "location": {"id": location.id, "name": location.name},
            "personal_training_type": {
                "id": personal_training_type.id,
                "name": personal_training_type.name,
            },
            "personal_tariff": {
                "id": personal_tariff.id,
                "name": personal_tariff.name,
                "price": str(personal_tariff.price),
            },
            "booking": {
                "date": primary_day.isoformat(),
                "start_time": "10:00",
                "end_time": "11:00",
            },
            "availability_slot": {
                "id": availability_slot.id,
                "date": availability_day.isoformat(),
                "client_search": availability_student.first_name,
                "client_name": str(availability_student),
                "student_id": availability_student.id,
            },
            "past_booking": {
                "booking_id": past_booking.id,
                "student_id": past_student.id,
            },
            "grandfathered_personal_trial": {
                "student_id": grandfathered_trial.id,
                "enrollment_id": legacy_trial_enrollment.id,
                "schedule_id": legacy_trial_enrollment.schedule_id,
                "checkin_date": legacy_trial_day.isoformat(),
            },
            "expected": {
                "availability_slot_id": availability_slot.id,
                "past_booking_id": past_booking.id,
                "price_snapshot": str(personal_tariff.price),
            },
            "created_at": now.isoformat(),
        }

    def _create_unattended_drop_in(
        self,
        *,
        club: Club,
        student: Student,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        tariff: Tariff,
        actor,
        target_day,
        fixture_id: str,
    ) -> PersonalDropInBooking:
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_day.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name=f"Past drop-in {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_day,
            is_active=True,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_day,
            ends_on=target_day,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        booking = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            created_by=actor,
            idempotency_key=f"past-drop-in-{fixture_id}",
        )
        ScheduleBookingEvent.objects.create(
            club=club,
            enrollment=enrollment,
            schedule=schedule,
            student=student,
            actor=actor,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            effective_date=target_day,
            metadata={"booking_kind": "drop_in", "personal_drop_in_booking_id": booking.id},
        )
        return booking

    def _create_grandfathered_personal_trial(
        self,
        *,
        club: Club,
        student: Student,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        actor,
        target_day,
        fixture_id: str,
    ) -> ScheduleEnrollment:
        starts_at = timezone.make_aware(datetime.combine(target_day, time(16, 0)), club_zoneinfo(club))
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_day.weekday(),
            start_time=time(16, 0),
            end_time=time(17, 0),
            group_name=f"Grandfathered personal trial {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_day,
            is_active=True,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=target_day,
            ends_on=target_day,
            trial_at=starts_at,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        ScheduleBookingEvent.objects.create(
            club=club,
            enrollment=enrollment,
            schedule=schedule,
            student=student,
            actor=actor,
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_BOOKED,
            origin=ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION,
            effective_date=target_day,
            metadata={"mode": "personal", "legacy_personal_trial": True},
        )
        return enrollment

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-personal-drop-in-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
