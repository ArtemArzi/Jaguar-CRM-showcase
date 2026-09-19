from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("attendance", "0016_personal_booking_payment_reservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="groupsession",
            name="close_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("trainer_review", "Trainer review"),
                    ("batch", "Batch correction"),
                    ("system", "System"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="groupsession",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="groupsession",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="closed_group_sessions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="groupsession",
            index=models.Index(fields=["club", "date", "closed_at"], name="att_gsession_closed_idx"),
        ),
    ]
