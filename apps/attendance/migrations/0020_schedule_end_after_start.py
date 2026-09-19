from django.db import migrations, models


def assert_valid_schedule_time_ranges(apps, schema_editor):
    Schedule = apps.get_model("attendance", "Schedule")
    invalid_ids = list(
        Schedule.objects.filter(end_time__lte=models.F("start_time"))
        .order_by("id")
        .values_list("id", flat=True)[:20]
    )
    if invalid_ids:
        raise RuntimeError(
            "Cannot add att_schedule_end_after_start: existing schedules have "
            f"end_time <= start_time. Correct schedule IDs first: {invalid_ids}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0019_personal_reservation_manual_review_active"),
    ]

    operations = [
        migrations.RunPython(
            assert_valid_schedule_time_ranges,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="schedule",
            constraint=models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="att_schedule_end_after_start",
            ),
        ),
    ]
