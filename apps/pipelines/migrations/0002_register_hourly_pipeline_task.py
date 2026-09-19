from django.db import migrations


def register_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name="hourly_advance_pipelines",
        defaults={
            "func": "apps.pipelines.tasks.advance_all_pipelines",
            "schedule_type": "H",
        },
    )


def unregister_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name="hourly_advance_pipelines").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pipelines", "0001_initial"),
        ("django_q", "0001_initial"),
    ]
    operations = [migrations.RunPython(register_tasks, unregister_tasks)]
