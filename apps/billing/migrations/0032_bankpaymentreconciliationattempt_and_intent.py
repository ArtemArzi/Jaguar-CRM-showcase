# Generated manually for the additive P0 reconciliation/intent contract.

import django.db.models.deletion
from django.db import migrations, models


def backfill_dispatched_tochka_links(apps, schema_editor):
    BankPaymentOrder = apps.get_model("billing", "BankPaymentOrder")
    BankPaymentReconciliationAttempt = apps.get_model("billing", "BankPaymentReconciliationAttempt")
    candidates = BankPaymentOrder.objects.filter(provider="tochka").filter(
        models.Q(provider_payment_link_id__gt="")
        | models.Q(provider_payment_url__gt="")
        | models.Q(provider_operation_id__gt="")
        | models.Q(status__in=["created", "pending", "authorized", "manual_review"])
    )
    candidates.update(
        link_creation_state="dispatched",
        link_creation_claimed_at=models.F("created_at"),
        link_creation_dispatched_at=models.F("created_at"),
    )
    for order in candidates.iterator():
        has_operation = bool(order.provider_operation_id)
        is_manual = order.status == "manual_review"
        BankPaymentReconciliationAttempt.objects.get_or_create(
            order_id=order.id,
            defaults={
                "club_id": order.club_id,
                "status": (
                    "manual_review"
                    if is_manual
                    else "pending"
                    if has_operation
                    else "retry"
                ),
                "retry_at": None if is_manual else order.created_at,
                "last_error_code": (
                    "tochka_legacy_manual_review"
                    if is_manual and has_operation
                    else ""
                    if has_operation
                    else "tochka_operation_id_missing"
                ),
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0031_providerwebhookdelivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankpaymentorder",
            name="link_creation_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="link_creation_dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="link_creation_state",
            field=models.CharField(
                choices=[
                    ("ready", "Ready"),
                    ("claimed", "Claimed"),
                    ("dispatched", "Dispatched"),
                    ("unknown", "Unknown"),
                ],
                default="ready",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="bankpaymentorder",
            name="payment_intent_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.CreateModel(
            name="BankPaymentReconciliationAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("retry", "Retry"),
                            ("completed", "Completed"),
                            ("manual_review", "Manual review"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("lease_token", models.CharField(blank=True, default="", max_length=64)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("retry_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=120)),
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
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_attempt",
                        to="billing.bankpaymentorder",
                    ),
                ),
                (
                    "provider_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_attempts",
                        to="billing.bankpaymentproviderevent",
                    ),
                ),
            ],
        ),
        migrations.RunPython(backfill_dispatched_tochka_links, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="bankpaymentorder",
            constraint=models.UniqueConstraint(
                condition=models.Q(("payment_intent_key", ""), _negated=True)
                & models.Q(
                    ("status__in", ["created", "pending", "authorized", "manual_review"])
                ),
                fields=("club", "payment_intent_key"),
                name="uniq_bank_order_payment_intent",
            ),
        ),
        migrations.AddIndex(
            model_name="bankpaymentreconciliationattempt",
            index=models.Index(fields=["club", "status", "retry_at"], name="billing_reconcile_due_idx"),
        ),
    ]
