from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0038_paymentreturnstate_browser_binding"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_recovery_failure_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="creation_recovery_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="bankpaymentorder",
            index=models.Index(
                fields=[
                    "provider",
                    "link_creation_state",
                    "creation_recovery_retry_at",
                ],
                name="billing_creation_recover_idx",
            ),
        ),
    ]
