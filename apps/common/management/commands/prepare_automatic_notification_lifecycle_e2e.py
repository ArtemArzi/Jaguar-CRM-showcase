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

from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.feedback.models import FeedbackForm
from apps.notifications.models import NotificationPreference, NotificationTemplate, PushSubscription
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for automatic notification lifecycle E2E."

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
                "Prepared automatic notification lifecycle E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, student_id={fixture['student']['student_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        numeric = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"automatic-notification-lifecycle-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        student_password = f"AutoNotifStudent-{unique}-pass"
        parent_password = f"AutoNotifParent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Auto Notifications E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        today = club_localdate(club, now)
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Auto Notifications E2E",
            max_push_per_week=20,
            quiet_hours_start=time(0, 0),
            quiet_hours_end=time(0, 0),
        )
        location = Location.objects.create(
            club=club,
            name=f"Auto Notification Hall {fixture_id}",
            address="Automatic notification lifecycle E2E fixture",
        )

        trainer_user = self._create_user(fixture_id=fixture_id, role="trainer", password=None)
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="AutoNotif",
            last_name="Trainer",
            phone=f"+155590{numeric}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        student_user = self._create_user(fixture_id=fixture_id, role="student", password=student_password)
        parent_user = self._create_user(fixture_id=fixture_id, role="parent", password=parent_password)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Auto Notification Group {fixture_id}",
            slug=f"auto-notification-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1200.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("30.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Auto Notification Tariff {fixture_id}",
            price=Decimal("6000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        student = Student.objects.create(
            club=club,
            first_name="AutoNotif",
            last_name="Student",
            phone=f"+155591{numeric}",
            email="",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            is_child=True,
            user=student_user,
            parent_user=parent_user,
            last_visit_date=today,
        )
        subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=2,
            trainings_used=6,
            expires_at=now + timedelta(days=7),
            scope=tariff.scope,
            location=None,
        )
        Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=4,
            trainings_used=4,
            expires_at=now + timedelta(days=3),
            scope=tariff.scope,
            location=None,
        )
        Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=1,
            trainings_used=7,
            expires_at=now + timedelta(days=1),
            scope=tariff.scope,
            location=None,
        )
        last_training_subscription = Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=0,
            trainings_used=8,
            expires_at=now + timedelta(days=14),
            scope=tariff.scope,
            location=None,
        )
        reminder_schedule = Schedule.objects.create(
            club=club,
            day_of_week=tomorrow.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Auto Reminder Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        reminder_occurrence_at = timezone.make_aware(
            datetime.combine(tomorrow, reminder_schedule.start_time),
            club_zoneinfo(club),
        )
        missed_schedule = Schedule.objects.create(
            club=club,
            day_of_week=yesterday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Auto Missed Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        for schedule, starts_on in ((reminder_schedule, today), (missed_schedule, yesterday - timedelta(days=14))):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=starts_on,
            )
        trainings_left_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=reminder_schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=subscription,
            is_debt=False,
        )
        last_training_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=missed_schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=last_training_subscription,
            is_debt=False,
        )

        self._create_templates(club=club)
        trial_feedback_form = FeedbackForm.objects.create(
            club=club,
            name=f"Auto Trial Feedback {fixture_id}",
            trigger_type=FeedbackForm.TriggerType.TRIAL,
            is_active=True,
        )
        churned_survey_form = FeedbackForm.objects.create(
            club=club,
            name=f"Auto Churned Survey {fixture_id}",
            trigger_type=FeedbackForm.TriggerType.CHURNED,
            is_active=True,
        )
        NotificationPreference.objects.create(
            user=parent_user,
            disabled_categories=["feedback_surveys", "child_checkin"],
        )
        student_push = PushSubscription.objects.create(
            user=student_user,
            endpoint=f"https://push.example.invalid/auto-notifications/student/{fixture_id}",
            key_p256dh=f"student-key-{unique}",
            key_auth=f"student-auth-{unique}",
        )
        parent_push = PushSubscription.objects.create(
            user=parent_user,
            endpoint=f"https://push.example.invalid/auto-notifications/parent/{fixture_id}",
            key_p256dh=f"parent-key-{unique}",
            key_auth=f"parent-auth-{unique}",
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "student": {
                "user_id": student_user.id,
                "student_id": student.id,
                "email": student_user.email,
                "password": student_password,
                "name": str(student),
            },
            "parent": {
                "user_id": parent_user.id,
                "email": parent_user.email,
                "password": parent_password,
            },
            "subscription_id": subscription.id,
            "reminder_schedule_id": reminder_schedule.id,
            "reminder_occurrence_date": tomorrow.isoformat(),
            "reminder_occurrence_at": reminder_occurrence_at.isoformat(),
            "trainings_left_checkin_id": trainings_left_checkin.id,
            "last_training_checkin_id": last_training_checkin.id,
            "feedback_form_ids": {
                "trial": trial_feedback_form.id,
                "churned": churned_survey_form.id,
            },
            "push_subscription_ids": {
                "student": student_push.id,
                "parent": parent_push.id,
            },
            "expected": {
                "student_notification_types": [
                    NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
                    NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
                    NotificationTemplate.TriggerType.SUB_EXPIRY_1D,
                    NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
                    NotificationTemplate.TriggerType.TRAININGS_LAST,
                    NotificationTemplate.TriggerType.TRAINING_REMINDER,
                    NotificationTemplate.TriggerType.TRAINING_REMINDER_24H,
                    NotificationTemplate.TriggerType.MISSED_TRAINING,
                ],
                "parent_notification_types": [
                    NotificationTemplate.TriggerType.PARENT_SUB_EXPIRY,
                ],
                "feedback_disabled_categories": ["feedback_surveys"],
                "parent_disabled_categories": ["feedback_surveys", "child_checkin"],
                "child_checkin_expected_queued_pushes": 0,
                "child_checkin_suppressed_notification_types": [
                    f"parent_checkin:{trainings_left_checkin.id}",
                    f"parent_checkin_cancelled:{last_training_checkin.id}",
                ],
                "trial_feedback_push_expected": False,
                "churned_survey_push_expected": False,
                "feedback_expected_queued_pushes": 0,
                "reminder_group_name": reminder_schedule.group_name,
                "missed_group_name": missed_schedule.group_name,
            },
            "created_at": now.isoformat(),
        }

    def _create_templates(self, *, club: Club) -> None:
        templates = [
            (
                NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
                "{name}, абонемент истекает",
                "Через {days} дн. Осталось {trainings_left} тренировок.",
                7,
            ),
            (
                NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
                "{name}, абонемент истекает",
                "Через {days} дн. Осталось {trainings_left} тренировок.",
                3,
            ),
            (
                NotificationTemplate.TriggerType.SUB_EXPIRY_1D,
                "{name}, абонемент истекает",
                "Через {days} дн. Осталось {trainings_left} тренировок.",
                1,
            ),
            (
                NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
                "{name}, осталось 2 тренировки",
                "Осталось {trainings_left} тренировки.",
                None,
            ),
            (
                NotificationTemplate.TriggerType.TRAININGS_LAST,
                "{name}, последнее занятие",
                "Это было последнее занятие по абонементу.",
                None,
            ),
            (
                NotificationTemplate.TriggerType.TRAINING_REMINDER,
                "{name}, тренировка скоро",
                "{group} в {time}",
                None,
            ),
            (
                NotificationTemplate.TriggerType.TRAINING_REMINDER_24H,
                "{name}, завтра тренировка",
                "{group} завтра в {time}",
                None,
            ),
            (
                NotificationTemplate.TriggerType.MISSED_TRAINING,
                "{name}, пропустили тренировку?",
                "{group} {missed_day}",
                None,
            ),
            (
                NotificationTemplate.TriggerType.TRIAL_FEEDBACK,
                "{name}, как тренировка?",
                "Расскажите о первой тренировке {name}.",
                None,
            ),
            (
                NotificationTemplate.TriggerType.CHURNED_SURVEY,
                "{name}, расскажите почему",
                "Помогите понять, почему вы перестали заниматься.",
                None,
            ),
        ]
        for trigger_type, title, body, days_before in templates:
            NotificationTemplate.objects.create(
                club=club,
                trigger_type=trigger_type,
                title_template=title,
                body_template=body,
                days_before=days_before,
                is_enabled=True,
            )

    def _create_user(self, *, fixture_id: str, role: str, password: str | None):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@automatic-notification-lifecycle-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
