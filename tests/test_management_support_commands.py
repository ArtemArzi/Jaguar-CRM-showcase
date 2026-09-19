import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_q.models import Schedule as QSchedule

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import Subscription, Tariff
from apps.clubs.tests.factories import ClubFactory
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.grades.services import GRADE_TEMPLATES
from apps.notifications.tasks import (
    check_missed_trainings,
    check_subscription_expiry,
    send_training_reminders,
    send_training_reminders_24h,
)
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
def test_seed_trainer_testdata_seeds_portal_support_data_without_open_task_conflict():
    club = ClubFactory()
    trainer = TrainerFactory(club=club)

    call_command(
        "seed_trainer_testdata",
        club_id=club.id,
        trainer_id=trainer.id,
        force=True,
        stdout=io.StringIO(),
    )
    call_command(
        "seed_trainer_testdata",
        club_id=club.id,
        trainer_id=trainer.id,
        force=True,
        stdout=io.StringIO(),
    )

    assert Student.objects.for_club(club).filter(phone__startswith="+7900100000").count() == 5
    assert GradeSystem.objects.for_club(club).filter(discipline="Муай Тай").exists()
    assert Grade.objects.for_club(club).filter(grade_system__discipline="Муай Тай").count() == 5
    assert StudentGrade.objects.for_club(club).count() == 3
    assert Schedule.objects.for_club(club).filter(trainer=trainer, is_active=True).exists()
    assert Tariff.objects.for_club(club).filter(is_active=True).exists()
    assert Subscription.objects.for_club(club).filter(status=Subscription.Status.ACTIVE).count() == 2
    assert Checkin.objects.for_club(club).count() > 0

    open_tasks = RetentionTask.objects.for_club(club).filter(resolved_at__isnull=True)
    assert set(open_tasks.values_list("level", flat=True)) == {
        RetentionTask.Level.YELLOW,
        RetentionTask.Level.RED,
        RetentionTask.Level.CHURNED,
    }
    assert open_tasks.count() == open_tasks.values("student_id", "task_type").distinct().count()


@pytest.mark.django_db
def test_seed_trainer_testdata_skips_populated_club_without_force():
    club = ClubFactory()
    trainer = TrainerFactory(club=club)
    for index in range(4):
        StudentFactory(club=club, phone=f"+7900200000{index}")

    stdout = io.StringIO()

    call_command(
        "seed_trainer_testdata",
        club_id=club.id,
        trainer_id=trainer.id,
        force=False,
        stdout=stdout,
    )

    assert "Use --force to seed anyway" in stdout.getvalue()
    assert Student.objects.for_club(club).filter(phone__startswith="+7900100000").count() == 0


@pytest.mark.django_db
def test_seed_trainer_testdata_rejects_trainer_from_another_club():
    club = ClubFactory()
    other_trainer = TrainerFactory(club=ClubFactory())

    with pytest.raises(CommandError, match="not found in club"):
        call_command(
            "seed_trainer_testdata",
            club_id=club.id,
            trainer_id=other_trainer.id,
            force=True,
            stdout=io.StringIO(),
        )


@pytest.mark.django_db
def test_seed_grade_templates_command_seeds_selected_disciplines_idempotently():
    club = ClubFactory()

    call_command(
        "seed_grade_templates",
        club_id=club.id,
        disciplines="BJJ, Бокс",
        stdout=io.StringIO(),
    )
    call_command(
        "seed_grade_templates",
        club_id=club.id,
        disciplines="BJJ, Бокс",
        stdout=io.StringIO(),
    )

    systems = GradeSystem.objects.for_club(club).order_by("discipline")
    assert list(systems.values_list("discipline", flat=True)) == ["BJJ", "Бокс"]
    assert Grade.objects.for_club(club).filter(grade_system__discipline="BJJ").count() == 5
    assert Grade.objects.for_club(club).filter(grade_system__discipline="Бокс").count() == 4


@pytest.mark.django_db
def test_seed_grade_templates_command_requires_disciplines_or_all():
    club = ClubFactory()

    with pytest.raises(CommandError, match="Specify --disciplines or --all"):
        call_command("seed_grade_templates", club_id=club.id, stdout=io.StringIO())


@pytest.mark.django_db
def test_seed_grade_templates_command_all_seeds_every_supported_discipline():
    club = ClubFactory()

    call_command("seed_grade_templates", club_id=club.id, all=True, stdout=io.StringIO())

    assert set(GradeSystem.objects.for_club(club).values_list("discipline", flat=True)) == set(GRADE_TEMPLATES)


@pytest.mark.django_db
def test_register_scheduled_tasks_upserts_notification_and_billing_jobs():
    for _ in range(2):
        call_command("register_scheduled_tasks", stdout=io.StringIO())

    schedules = {
        schedule.name: schedule
        for schedule in QSchedule.objects.filter(
            name__in=[
                "daily_subscription_expiry_notifications",
                "auto_unfreeze_expired_subscriptions",
                "expire_pending_bank_payment_orders",
                "hourly_training_reminders",
                "hourly_training_reminders_24h",
                "hourly_missed_training_check",
            ]
        )
    }

    assert set(schedules) == {
        "daily_subscription_expiry_notifications",
        "auto_unfreeze_expired_subscriptions",
        "expire_pending_bank_payment_orders",
        "hourly_training_reminders",
        "hourly_training_reminders_24h",
        "hourly_missed_training_check",
    }
    assert schedules["daily_subscription_expiry_notifications"].func == (
        f"{check_subscription_expiry.__module__}.{check_subscription_expiry.__name__}"
    )
    assert schedules["daily_subscription_expiry_notifications"].schedule_type == QSchedule.DAILY
    assert schedules["daily_subscription_expiry_notifications"].minutes is None
    assert schedules["auto_unfreeze_expired_subscriptions"].func == "apps.billing.tasks.auto_unfreeze_expired"
    assert schedules["auto_unfreeze_expired_subscriptions"].schedule_type == QSchedule.HOURLY
    assert schedules["auto_unfreeze_expired_subscriptions"].minutes is None
    assert schedules["expire_pending_bank_payment_orders"].func == (
        "apps.billing.tasks.expire_pending_bank_payment_orders"
    )
    assert schedules["expire_pending_bank_payment_orders"].schedule_type == QSchedule.HOURLY
    assert schedules["expire_pending_bank_payment_orders"].minutes is None
    assert schedules["hourly_training_reminders"].func == (
        f"{send_training_reminders.__module__}.{send_training_reminders.__name__}"
    )
    assert schedules["hourly_training_reminders"].schedule_type == QSchedule.HOURLY
    assert schedules["hourly_training_reminders_24h"].func == (
        f"{send_training_reminders_24h.__module__}.{send_training_reminders_24h.__name__}"
    )
    assert schedules["hourly_training_reminders_24h"].schedule_type == QSchedule.HOURLY
    assert schedules["hourly_missed_training_check"].func == (
        f"{check_missed_trainings.__module__}.{check_missed_trainings.__name__}"
    )
    assert schedules["hourly_missed_training_check"].schedule_type == QSchedule.HOURLY
