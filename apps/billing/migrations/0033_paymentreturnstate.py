import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0032_bankpaymentreconciliationattempt_and_intent")]

    operations = [
        migrations.CreateModel(
            name="PaymentReturnState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("state_hash", models.CharField(max_length=64, unique=True)),
                ("purpose", models.CharField(default="payment_return", max_length=32)),
                ("version", models.PositiveSmallIntegerField(default=1)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("session_handle_hash", models.CharField(blank=True, default="", max_length=64)),
                ("session_expires_at", models.DateTimeField(blank=True, null=True)),
                ("club", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(class)ss", to="clubs.club")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="return_states", to="billing.bankpaymentorder")),
            ],
        ),
        migrations.AddIndex(
            model_name="paymentreturnstate",
            index=models.Index(fields=["state_hash", "expires_at"], name="billing_return_state_idx"),
        ),
    ]
