from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("retention", "0006_add_auto_subscription_resolution"),
    ]

    operations = [
        migrations.AlterField(
            model_name="retentiontask",
            name="resolution",
            field=models.CharField(
                blank=True,
                choices=[
                    ("auto_checkin", "Student returned"),
                    ("auto_subscription", "Subscription created"),
                    ("manual_contacted", "Contacted, won't come"),
                    ("manual_other", "Other"),
                    ("called_will_come", "Called, will come"),
                    ("no_answer", "No answer"),
                    ("quit", "Quit"),
                    ("lead_closed", "Lead closed"),
                ],
                default="",
                max_length=30,
            ),
        ),
    ]
