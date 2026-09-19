# Generated manually for P1 bank payment manual-review resolution.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0023_subscription_renewal_chain_id_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BankPaymentOrderReviewEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "resolution",
                    models.CharField(
                        choices=[
                            ("confirm_paid", "Confirm paid"),
                            ("reject", "Reject"),
                            ("mark_refunded", "Mark refunded"),
                            ("mark_refunded_partially", "Mark refunded partially"),
                        ],
                        max_length=40,
                    ),
                ),
                (
                    "previous_status",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("authorized", "Authorized"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                            ("manual_review", "Manual review"),
                            ("refunded", "Refunded"),
                            ("refunded_partially", "Refunded partially"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "new_status",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("authorized", "Authorized"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                            ("manual_review", "Manual review"),
                            ("refunded", "Refunded"),
                            ("refunded_partially", "Refunded partially"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "previous_payment_status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("confirmed", "Подтверждён"),
                            ("rejected", "Отклонён"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "new_payment_status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("confirmed", "Подтверждён"),
                            ("rejected", "Отклонён"),
                        ],
                        max_length=20,
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                ("evidence_metadata", models.JSONField(blank=True, default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bank_payment_order_review_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)ss",
                        to="clubs.club",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="review_events",
                        to="billing.bankpaymentorder",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["club", "order", "created_at"], name="billing_bank_review_order_idx"),
                    models.Index(fields=["club", "resolution", "created_at"], name="billing_bank_review_res_idx"),
                    models.Index(fields=["club", "actor", "created_at"], name="billing_bank_review_actor_idx"),
                ],
            },
        ),
    ]
