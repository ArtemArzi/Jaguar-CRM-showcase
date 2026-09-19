from django.db import migrations


def mark_retryable_legacy_manual_attempts(apps, schema_editor):
    BankPaymentReconciliationAttempt = apps.get_model(
        "billing",
        "BankPaymentReconciliationAttempt",
    )
    BankPaymentReconciliationAttempt.objects.filter(
        status="manual_review",
        last_error_code="",
        order__provider="tochka",
        order__status="manual_review",
        order__provider_operation_id__gt="",
    ).update(last_error_code="tochka_legacy_manual_review")


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0040_bankpaymentorder_personal_origin"),
    ]

    operations = [
        migrations.RunPython(
            mark_retryable_legacy_manual_attempts,
            migrations.RunPython.noop,
        ),
    ]
