"""Backfill TrainerRate rows from legacy TrainerLocation.rate_* columns.

For each existing TrainerLocation, iterate all TrainingType rows in the
same club and create one TrainerRate per kind (group/personal/mini_group),
reading the percent from the corresponding legacy column.
"""
from django.db import migrations


def backfill_rates(apps, schema_editor):
    TrainerLocation = apps.get_model("trainers", "TrainerLocation")
    TrainerRate = apps.get_model("trainers", "TrainerRate")
    TrainingType = apps.get_model("billing", "TrainingType")

    KIND_TO_COLUMN = {
        "group": "rate_group",
        "personal": "rate_personal",
        "mini_group": "rate_mini_group",
    }

    created = 0
    clubs_without_types = 0

    for tl in TrainerLocation.objects.all().select_related("trainer", "location"):
        club_id = tl.club_id
        types = list(TrainingType.objects.filter(club_id=club_id))
        if not types:
            clubs_without_types += 1
            continue

        for tt in types:
            column = KIND_TO_COLUMN.get(tt.kind)
            if not column:
                continue
            percent = getattr(tl, column)
            _, was_created = TrainerRate.objects.get_or_create(
                club_id=club_id,
                trainer=tl.trainer,
                location=tl.location,
                training_type=tt,
                defaults={"percent": percent},
            )
            if was_created:
                created += 1

    print(
        f"  TrainerRate backfill: created={created}, "
        f"clubs_without_training_types={clubs_without_types}"
    )


def reverse_backfill(apps, schema_editor):
    TrainerRate = apps.get_model("trainers", "TrainerRate")
    TrainerRate.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("trainers", "0006_add_trainer_rate"),
        ("billing", "0009_payment_seller_trainer"),
    ]

    operations = [
        migrations.RunPython(backfill_rates, reverse_backfill),
    ]
