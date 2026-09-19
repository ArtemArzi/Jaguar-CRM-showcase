from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("clubs", "0009_commercial_journey_protocol_version"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CommercialJourneyProtocolTransition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("previous_version", models.CharField(choices=[("v1", "Commercial journey protocol v1"), ("v2", "Commercial journey protocol v2")], max_length=2)),
                ("target_version", models.CharField(choices=[("v1", "Commercial journey protocol v1"), ("v2", "Commercial journey protocol v2")], max_length=2)),
                ("rationale", models.CharField(max_length=500)),
                ("idempotency_key", models.CharField(max_length=120)),
                ("readiness_snapshot", models.JSONField(default=dict)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="commercial_journey_protocol_transitions", to=settings.AUTH_USER_MODEL)),
                ("club", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="commercial_journey_protocol_transitions", to="clubs.club")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("club", "idempotency_key"), name="uniq_club_commercial_protocol_transition_key")],
            },
        ),
    ]
