import importlib

import pytest
from django.apps import apps as django_apps
from django_q.models import Schedule as QSchedule

from apps.clubs.tests.factories import ClubFactory
from apps.notifications.models import NotificationTemplate
from apps.notifications.tests.factories import NotificationTemplateFactory


@pytest.mark.django_db
def test_occurrence_delivery_migration_registers_all_hourly_jobs_idempotently():
    migration = importlib.import_module(
        "apps.notifications.migrations.0010_occurrence_notification_delivery"
    )
    tracked_names = {
        "hourly_training_reminders",
        "hourly_training_reminders_24h",
        "hourly_missed_training_check",
        "daily_missed_training_check",
    }
    assert set(
        QSchedule.objects.filter(name__in=tracked_names).values_list("name", flat=True)
    ) == {
        "hourly_training_reminders",
        "hourly_training_reminders_24h",
        "hourly_missed_training_check",
    }

    for _ in range(2):
        migration.register_reminder_tasks(django_apps, None)

    schedules = {
        schedule.name: schedule
        for schedule in QSchedule.objects.filter(
            name__in=tracked_names
        )
    }
    assert set(schedules) == {
        "hourly_training_reminders",
        "hourly_training_reminders_24h",
        "hourly_missed_training_check",
    }
    assert all(schedule.schedule_type == QSchedule.HOURLY for schedule in schedules.values())


@pytest.mark.django_db
def test_occurrence_delivery_migration_backfills_distinct_24h_template_idempotently():
    club = ClubFactory()
    NotificationTemplateFactory(
        club=club,
        trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER,
        is_enabled=False,
    )
    migration = importlib.import_module(
        "apps.notifications.migrations.0010_occurrence_notification_delivery"
    )

    for _ in range(2):
        migration.seed_twenty_four_hour_templates(django_apps, None)

    template = NotificationTemplate.objects.for_club(club).get(
        trigger_type=NotificationTemplate.TriggerType.TRAINING_REMINDER_24H,
    )
    assert template.is_enabled is False
    assert "завтра" in template.body_template
