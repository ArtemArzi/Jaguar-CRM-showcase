# Generated manually for bank payment manual-review subscription snapshots.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0025_subscriptionfreeze_pending_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankpaymentorderreviewevent",
            name="previous_subscription_status",
            field=models.CharField(
                choices=[
                    ("active", "Активен"),
                    ("expired", "Истёк"),
                    ("pending", "Ожидает"),
                    ("frozen", "Заморожен"),
                ],
                blank=True,
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="bankpaymentorderreviewevent",
            name="new_subscription_status",
            field=models.CharField(
                choices=[
                    ("active", "Активен"),
                    ("expired", "Истёк"),
                    ("pending", "Ожидает"),
                    ("frozen", "Заморожен"),
                ],
                blank=True,
                max_length=20,
                null=True,
            ),
        ),
    ]
