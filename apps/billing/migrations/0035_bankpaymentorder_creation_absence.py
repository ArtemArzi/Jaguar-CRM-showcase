from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0034_paymentreturnstate_terminal_grace")]

    operations = [
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_absence_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_last_absence_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
