from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0024_personal_staff_intent_command"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalstaffintentcommand",
            name="payment_link_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="personalstaffintentcommand",
            name="result_bound_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
