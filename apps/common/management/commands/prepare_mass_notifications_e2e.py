from __future__ import annotations

import json
import uuid
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Schedule, ScheduleEnrollment
from apps.billing.models import TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.notifications.models import NotificationPreference, PushSubscription
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for mass notification E2E."

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
                "Prepared mass notifications E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, schedule_id={fixture['owned_schedule_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"mass-notifications-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"MassNotifyOwner-{unique}-pass"
        trainer_password = f"MassNotifyTrainer-{unique}-pass"
        owner_message = f"Owner group notice {fixture_id}"
        trainer_message = f"Trainer group notice {fixture_id}"
        trainer_club_message = f"Trainer denied club notice {fixture_id}"
        trainer_foreign_message = f"Trainer denied foreign group notice {fixture_id}"

        club = Club.objects.create(
            name=f"Jaguar Mass Notifications E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Asia/Yekaterinburg",
        )
        today = club_localdate(club, now)
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Mass Notifications E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Mass Hall {fixture_id}",
            address="Mass notification E2E fixture",
        )

        owner_user = self._create_user(fixture_id=fixture_id, role="owner", password=owner_password)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=trainer_password)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        other_trainer_user = self._create_user(fixture_id=fixture_id, role="other-trainer", password=None)
        ClubMembership.objects.create(user=other_trainer_user, club=club, role=ClubMembership.Role.TRAINER)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Mass",
            last_name="Trainer",
            phone=f"+155570{phone_seed}",
            user=trainer_user,
        )
        other_trainer = Trainer.objects.create(
            club=club,
            first_name="Other",
            last_name="Trainer",
            phone=f"+155571{phone_seed}",
            user=other_trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerLocation.objects.create(club=club, trainer=other_trainer, location=location)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Mass Group {fixture_id}",
            slug=f"mass-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1000.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("40.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=other_trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("40.00"),
        )
        owned_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Mass Owned Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        foreign_schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 1),
            end_time=time(23, 58),
            group_name=f"Mass Foreign Proof {fixture_id}",
            trainer=other_trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )

        target_student, target_user = self._create_student_user(
            club=club,
            fixture_id=fixture_id,
            role="target-student",
            first_name="MassTarget",
            phone=f"+155572{phone_seed}",
        )
        non_target_student, non_target_user = self._create_student_user(
            club=club,
            fixture_id=fixture_id,
            role="non-target-student",
            first_name="MassOther",
            phone=f"+155573{phone_seed}",
        )
        opted_out_student, opted_out_user = self._create_student_user(
            club=club,
            fixture_id=fixture_id,
            role="opted-out-student",
            first_name="MassOptOut",
            phone=f"+155574{phone_seed}",
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=target_student,
            schedule=owned_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=non_target_student,
            schedule=foreign_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=opted_out_student,
            schedule=owned_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )

        target_subscription = PushSubscription.objects.create(
            user=target_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/target",
            key_p256dh=f"p256dh-target-{unique}",
            key_auth=f"auth-target-{unique}",
        )
        target_inactive_subscription = PushSubscription.objects.create(
            user=target_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/target-inactive",
            key_p256dh=f"p256dh-target-inactive-{unique}",
            key_auth=f"auth-target-inactive-{unique}",
            is_active=False,
        )
        target_second_subscription = PushSubscription.objects.create(
            user=target_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/target-second",
            key_p256dh=f"p256dh-target-second-{unique}",
            key_auth=f"auth-target-second-{unique}",
        )
        non_target_subscription = PushSubscription.objects.create(
            user=non_target_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/non-target",
            key_p256dh=f"p256dh-non-target-{unique}",
            key_auth=f"auth-non-target-{unique}",
        )
        opted_out_subscription = PushSubscription.objects.create(
            user=opted_out_user,
            endpoint=f"https://push-e2e.invalid/{fixture_id}/opted-out",
            key_p256dh=f"p256dh-opted-out-{unique}",
            key_auth=f"auth-opted-out-{unique}",
        )
        NotificationPreference.objects.create(
            user=opted_out_user,
            disabled_categories=["training_reminders"],
        )

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
                "email": trainer_user.email,
                "password": trainer_password,
                "trainer_id": trainer.id,
            },
            "owned_schedule_id": owned_schedule.id,
            "foreign_schedule_id": foreign_schedule.id,
            "target_student": {
                "student_id": target_student.id,
                "user_id": target_user.id,
                "name": str(target_student),
            },
            "non_target_student": {
                "student_id": non_target_student.id,
                "user_id": non_target_user.id,
                "name": str(non_target_student),
            },
            "opted_out_student": {
                "student_id": opted_out_student.id,
                "user_id": opted_out_user.id,
                "name": str(opted_out_student),
            },
            "push_subscriptions": {
                "target_active_id": target_subscription.id,
                "target_second_active_id": target_second_subscription.id,
                "target_inactive_id": target_inactive_subscription.id,
                "non_target_active_id": non_target_subscription.id,
                "opted_out_active_id": opted_out_subscription.id,
            },
            "expected": {
                "owner_message": owner_message,
                "trainer_message": trainer_message,
                "trainer_club_message": trainer_club_message,
                "trainer_foreign_message": trainer_foreign_message,
                "raw_recipient_count": 2,
                "recipient_count": 2,
                "mass_notification_category": "staff_announcement",
                "disabled_categories": ["training_reminders"],
            },
            "created_at": now.isoformat(),
        }

    def _create_student_user(
        self,
        *,
        club: Club,
        fixture_id: str,
        role: str,
        first_name: str,
        phone: str,
    ):
        user = self._create_user(fixture_id=fixture_id, role=role, password=None)
        ClubMembership.objects.create(user=user, club=club, role=ClubMembership.Role.STUDENT)
        student = Student.objects.create(
            club=club,
            first_name=first_name,
            last_name="Student",
            phone=phone,
            email=user.email,
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=user,
        )
        return student, user

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@mass-notifications-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
