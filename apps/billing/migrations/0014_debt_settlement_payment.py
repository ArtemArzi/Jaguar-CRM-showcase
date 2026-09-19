import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0013_subscription_paid_amount_freeze_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="debt",
            name="settlement_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="settled_debts",
                to="billing.payment",
            ),
        ),
        migrations.AddIndex(
            model_name="debt",
            index=models.Index(fields=["club", "settlement_payment"], name="billing_deb_club_id_d97685_idx"),
        ),
    ]
