from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0036_bankpaymentorder_creation_recovery_claim")]

    operations = [
        migrations.CreateModel(
            name="PaymentProviderReadinessSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider",
                    models.CharField(
                        choices=[("mock", "Mock"), ("tochka", "Tochka")],
                        max_length=20,
                    ),
                ),
                ("customer_code_hash", models.CharField(max_length=64)),
                ("merchant_id_hash", models.CharField(max_length=64)),
                ("retailer_status", models.CharField(max_length=40)),
                ("is_active", models.BooleanField(default=False)),
                ("payment_modes", models.JSONField(blank=True, default=list)),
                ("cashbox_ready", models.BooleanField(default=False)),
                ("checked_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
            ],
        ),
        migrations.AddIndex(
            model_name="paymentproviderreadinesssnapshot",
            index=models.Index(
                fields=["provider", "customer_code_hash", "merchant_id_hash", "-checked_at"],
                name="billing_provider_ready_idx",
            ),
        ),
    ]
