from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0023_personal_service_terms_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonalStaffIntentCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("command_key", models.CharField(max_length=120)),
                ("command_fingerprint", models.CharField(max_length=64)),
                ("payment_method", models.CharField(max_length=20)),
                ("command_shape", models.JSONField(default=dict)),
                ("booking_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("reservation_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("enrollment_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("club", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(class)ss", to="clubs.club")),
            ],
        ),
        migrations.AddConstraint(
            model_name="personalstaffintentcommand",
            constraint=models.UniqueConstraint(fields=("club", "command_key"), name="uniq_personal_staff_intent_command_key"),
        ),
        migrations.AddIndex(
            model_name="personalstaffintentcommand",
            index=models.Index(fields=["club", "created_at"], name="att_personal_cmd_created_idx"),
        ),
    ]
