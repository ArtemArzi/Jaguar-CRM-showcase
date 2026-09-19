from django.db import migrations


def register_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name="hourly_training_reminders",
        defaults={
            "func": "apps.notifications.tasks.send_training_reminders",
            "schedule_type": "H",
        },
    )
    Schedule.objects.update_or_create(
        name="daily_missed_training_check",
        defaults={
            "func": "apps.notifications.tasks.check_missed_trainings",
            "schedule_type": "D",
            "minutes": 0,
        },
    )


def unregister_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(
        name__in=["hourly_training_reminders", "daily_missed_training_check"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0006_add_training_reminder_triggers"),
        ("django_q", "0001_initial"),
    ]
    operations = [migrations.RunPython(register_tasks, unregister_tasks)]
