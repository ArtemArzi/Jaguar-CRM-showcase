from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0035_bankpaymentorder_creation_absence")]

    operations = [
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_recovery_claim_token",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_recovery_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
