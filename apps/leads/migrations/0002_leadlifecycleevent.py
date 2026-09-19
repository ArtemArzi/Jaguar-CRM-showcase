# Generated manually for lead lifecycle audit events.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0001_initial"),
        ("trainers", "0009_trainer_package_compensation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LeadLifecycleEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("lead_claimed", "Lead claimed"),
                            ("lead_released", "Lead released"),
                            ("lead_assigned", "Lead assigned"),
                            ("lead_reassigned", "Lead reassigned"),
                            ("status_changed", "Status changed"),
                            ("trial_booked", "Trial booked"),
                            ("trial_done", "Trial done"),
                            ("lead_lost", "Lead lost"),
                            ("lead_converted", "Lead converted"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                ("old_lead_status", models.CharField(blank=True, default="", max_length=20)),
                ("new_lead_status", models.CharField(blank=True, default="", max_length=20)),
                ("reason", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="lead_lifecycle_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "club",
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)ss",
                        to="clubs.club",
                    ),
                ),
                (
                    "new_trainer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="new_lead_lifecycle_events",
                        to="trainers.trainer",
                    ),
                ),
                (
                    "old_trainer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="old_lead_lifecycle_events",
                        to="trainers.trainer",
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lead_lifecycle_events",
                        to="students.student",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="leadlifecycleevent",
            index=models.Index(
                fields=["club", "student", "created_at"],
                name="leadlife_club_student_created",
            ),
        ),
        migrations.AddIndex(
            model_name="leadlifecycleevent",
            index=models.Index(
                fields=["club", "event_type", "created_at"],
                name="leadlife_club_type_created",
            ),
        ),
    ]
