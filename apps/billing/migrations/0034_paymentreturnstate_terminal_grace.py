from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0033_paymentreturnstate")]

    operations = [
        migrations.AddField(
            model_name="paymentreturnstate",
            name="terminal_grace_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
