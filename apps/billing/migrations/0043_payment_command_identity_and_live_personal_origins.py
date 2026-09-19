from django.db import migrations, models


LIVE_PERSONAL_ORDER_STATUSES = ["created", "pending", "authorized", "manual_review"]


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0042_tariff_personal_booking_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="command_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="payment",
            name="command_idempotency_key",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                condition=models.Q(command_idempotency_key__isnull=False),
                fields=("club", "command_idempotency_key"),
                name="uniq_payment_command_idempotency",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="bankpaymentorder",
            name="uniq_live_bank_order_personal_reservation",
        ),
        migrations.RemoveConstraint(
            model_name="bankpaymentorder",
            name="uniq_live_bank_order_personal_dropin",
        ),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(personal_booking_reservation_id_snapshot__isnull=False)
                    & models.Q(status__in=LIVE_PERSONAL_ORDER_STATUSES)
                ),
                fields=("club", "personal_booking_reservation_id_snapshot"),
                name="uniq_live_bank_order_personal_reservation",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(personal_drop_in_booking_id_snapshot__isnull=False)
                    & models.Q(status__in=LIVE_PERSONAL_ORDER_STATUSES)
                ),
                fields=("club", "personal_drop_in_booking_id_snapshot"),
                name="uniq_live_bank_order_personal_dropin",
            ),
        ),
    ]
