from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0039_bankpaymentorder_creation_recovery_backoff"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bankpaymentorderreviewevent",
            name="resolution",
            field=models.CharField(
                choices=[
                    ("confirm_paid", "Confirm paid"),
                    ("reject", "Reject"),
                    ("mark_refunded", "Mark refunded"),
                    ("mark_refunded_partially", "Mark refunded partially"),
                    ("retry_reconciliation", "Retry reconciliation"),
                ],
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="personal_booking_reservation_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="personal_drop_in_booking_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.UniqueConstraint(
                condition=models.Q(personal_booking_reservation_id_snapshot__isnull=False),
                fields=("club", "personal_booking_reservation_id_snapshot"),
                name="uniq_live_bank_order_personal_reservation",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.UniqueConstraint(
                condition=models.Q(personal_drop_in_booking_id_snapshot__isnull=False),
                fields=("club", "personal_drop_in_booking_id_snapshot"),
                name="uniq_live_bank_order_personal_dropin",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(personal_booking_reservation_id_snapshot__isnull=True)
                    | models.Q(personal_drop_in_booking_id_snapshot__isnull=True)
                ),
                name="bank_order_single_personal_origin",
            ),
        ),
        migrations.AddIndex(
            model_name="paymentreturnstate",
            index=models.Index(
                fields=["session_handle_hash", "session_expires_at"],
                name="billing_return_session_idx",
            ),
        ),
    ]
