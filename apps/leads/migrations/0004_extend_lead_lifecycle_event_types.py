from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0003_leadintakeevent_requires_owner_review"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leadlifecycleevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("lead_claimed", "Lead claimed"),
                    ("lead_released", "Lead released"),
                    ("lead_assigned", "Lead assigned"),
                    ("lead_reassigned", "Lead reassigned"),
                    ("status_changed", "Status changed"),
                    ("trial_booked", "Trial booked"),
                    ("trial_done", "Trial done"),
                    ("contact_outcome_recorded", "Contact outcome recorded"),
                    ("lead_lost", "Lead lost"),
                    ("lead_converted", "Lead converted"),
                    ("lead_reopened", "Lead reopened"),
                    ("lead_restored", "Lead restored"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
