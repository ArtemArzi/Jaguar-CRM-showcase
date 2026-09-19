from django.db import migrations


def register_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name="daily_subscription_expiry_notifications",
        defaults={
            "func": "apps.notifications.tasks.check_subscription_expiry",
            "schedule_type": "D",
            "minutes": 30,
        },
    )


def unregister_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name="daily_subscription_expiry_notifications").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_pushsubscription_is_active_notificationtemplate_and_more"),
        ("django_q", "0001_initial"),
    ]
    operations = [migrations.RunPython(register_tasks, unregister_tasks)]
