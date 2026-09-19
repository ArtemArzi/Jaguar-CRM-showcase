# Generated manually for same-browser concurrent return exchange convergence.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0037_paymentproviderreadinesssnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentreturnstate",
            name="browser_binding_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
