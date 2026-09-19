from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0017_groupsession_close_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalavailabilityslot",
            name="block_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="personalavailabilityslot",
            name="status",
            field=models.CharField(
                choices=[
                    ("published", "Published"),
                    ("held", "Held"),
                    ("booked", "Booked"),
                    ("blocked", "Blocked"),
                    ("cancelled", "Cancelled"),
                ],
                default="published",
                max_length=20,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="personalavailabilityslot",
            name="uniq_active_personal_availability_trainer_slot",
        ),
        migrations.AddConstraint(
            model_name="personalavailabilityslot",
            constraint=models.UniqueConstraint(
                condition=Q(status__in=["published", "held", "booked", "blocked"]),
                fields=("club", "trainer", "starts_at", "ends_at"),
                name="uniq_active_personal_availability_trainer_slot",
            ),
        ),
    ]
