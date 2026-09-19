from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0002_leadlifecycleevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="leadintakeevent",
            name="requires_owner_review",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
