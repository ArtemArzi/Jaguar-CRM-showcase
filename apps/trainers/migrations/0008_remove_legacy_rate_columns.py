"""Remove legacy rate_* columns from TrainerLocation.

Reverse is data-preserving: on rollback, the columns are re-added and
then populated from TrainerRate (matching TrainingType.kind), so
`migrate trainers 0006` does not destroy rate configuration.
"""
from django.db import migrations


def noop_forward(apps, schema_editor):
    pass


def restore_legacy_columns_on_reverse(apps, schema_editor):
    """Runs on reverse AFTER AddField has re-added the columns (because
    RunPython is placed before the RemoveFields in this file, so in
    reverse order it runs AFTER them, which is when the columns exist again).
    """
    TrainerLocation = apps.get_model("trainers", "TrainerLocation")
    TrainerRate = apps.get_model("trainers", "TrainerRate")
    TrainingType = apps.get_model("billing", "TrainingType")

    kind_to_column = {
        "group": "rate_group",
        "personal": "rate_personal",
        "mini_group": "rate_mini_group",
    }

    for tl in TrainerLocation.objects.all():
        rates = TrainerRate.objects.filter(
            club_id=tl.club_id,
            trainer_id=tl.trainer_id,
            location_id=tl.location_id,
        ).select_related("training_type")
        for rate in rates:
            column = kind_to_column.get(rate.training_type.kind)
            if column:
                setattr(tl, column, rate.percent)
        tl.save()


class Migration(migrations.Migration):

    dependencies = [
        ("trainers", "0007_backfill_trainer_rates"),
    ]

    operations = [
        # Placed BEFORE the RemoveField ops: forward order runs noop first,
        # then drops columns. Reverse order runs column re-creation first
        # (via RemoveField reverse), then this RunPython reverse populates
        # them from TrainerRate.
        migrations.RunPython(noop_forward, restore_legacy_columns_on_reverse),
        migrations.RemoveField(
            model_name="trainerlocation",
            name="rate_group",
        ),
        migrations.RemoveField(
            model_name="trainerlocation",
            name="rate_mini_group",
        ),
        migrations.RemoveField(
            model_name="trainerlocation",
            name="rate_personal",
        ),
    ]
