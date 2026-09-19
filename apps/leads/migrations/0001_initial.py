# Generated manually for landing lead intake events.

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("clubs", "0007_min_trainings_to_freeze"),
        ("students", "0006_task_type_attempt_count"),
    ]

    operations = [
        migrations.CreateModel(
            name="LeadIntakeEvent",
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
                ("goal", models.CharField(max_length=500)),
                (
                    "preferred_format",
                    models.CharField(
                        choices=[
                            ("group", "Группа"),
                            ("hybrid", "Группа + персонально"),
                            ("personal", "Персонально"),
                            ("unsure", "Нужна помощь"),
                        ],
                        max_length=20,
                    ),
                ),
                ("source_page", models.CharField(blank=True, default="", max_length=200)),
                ("utm_source", models.CharField(blank=True, default="", max_length=120)),
                ("utm_medium", models.CharField(blank=True, default="", max_length=120)),
                ("utm_campaign", models.CharField(blank=True, default="", max_length=120)),
                ("utm_content", models.CharField(blank=True, default="", max_length=120)),
                ("utm_term", models.CharField(blank=True, default="", max_length=120)),
                ("privacy_policy_version", models.CharField(max_length=50)),
                ("consent_text_hash", models.CharField(max_length=128)),
                ("consent_accepted_at", models.DateTimeField(default=timezone.now)),
                (
                    "request_id",
                    models.CharField(blank=True, db_index=True, default="", max_length=128),
                ),
                ("client_ip_hash", models.CharField(blank=True, default="", max_length=64)),
                ("user_agent_hash", models.CharField(blank=True, default="", max_length=64)),
                ("idempotency_key", models.UUIDField(blank=True, null=True)),
                ("is_repeat_submission", models.BooleanField(default=False)),
                (
                    "telegram_status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("sent", "Отправлено"),
                            ("failed", "Ошибка"),
                            ("skipped", "Пропущено"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("telegram_attempt_count", models.PositiveIntegerField(default=0)),
                ("telegram_sent_at", models.DateTimeField(blank=True, null=True)),
                ("telegram_error_code", models.CharField(blank=True, default="", max_length=80)),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(class)ss",
                        to="clubs.club",
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lead_intake_events",
                        to="students.student",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="leadintakeevent",
            index=models.Index(fields=["club", "created_at"], name="leads_leadi_club_id_bc418f_idx"),
        ),
        migrations.AddIndex(
            model_name="leadintakeevent",
            index=models.Index(
                fields=["club", "student", "created_at"],
                name="leads_leadi_club_id_2c3245_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="leadintakeevent",
            constraint=models.UniqueConstraint(
                condition=Q(("idempotency_key__isnull", False)),
                fields=("club", "idempotency_key"),
                name="unique_lead_intake_idempotency_key_per_club",
            ),
        ),
    ]
