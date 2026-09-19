import django.db.models.deletion
from django.db import migrations, models

REMINDER_JOBS = {
    "hourly_training_reminders": "apps.notifications.tasks.send_training_reminders",
    "hourly_training_reminders_24h": "apps.notifications.tasks.send_training_reminders_24h",
    "hourly_missed_training_check": "apps.notifications.tasks.check_missed_trainings",
}


def register_reminder_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    for name, func in REMINDER_JOBS.items():
        Schedule.objects.update_or_create(
            name=name,
            defaults={
                "func": func,
                "schedule_type": "H",
                "minutes": None,
            },
        )
    Schedule.objects.filter(name="daily_missed_training_check").delete()


def unregister_reminder_tasks(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(
        name__in=[
            "hourly_training_reminders_24h",
            "hourly_missed_training_check",
        ]
    ).delete()
    Schedule.objects.update_or_create(
        name="daily_missed_training_check",
        defaults={
            "func": "apps.notifications.tasks.check_missed_trainings",
            "schedule_type": "D",
            "minutes": 0,
        },
    )


def seed_twenty_four_hour_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    one_hour_templates = NotificationTemplate.objects.filter(
        trigger_type="training_reminder"
    ).order_by("club_id", "id")
    for one_hour in one_hour_templates.iterator():
        NotificationTemplate.objects.get_or_create(
            club_id=one_hour.club_id,
            trigger_type="training_reminder_24h",
            defaults={
                "title_template": "{name}, завтра тренировка",
                "body_template": "{group} завтра в {time}",
                "is_enabled": one_hour.is_enabled,
            },
        )


def unseed_twenty_four_hour_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(trigger_type="training_reminder_24h").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0019_personal_reservation_manual_review_active"),
        ("django_q", "0001_initial"),
        ("notifications", "0009_alter_notificationtemplate_trigger_type"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="sentnotification",
            name="unique_notification_per_student_per_day",
        ),
        migrations.AddField(
            model_name="sentnotification",
            name="delivery_stage",
            field=models.CharField(
                blank=True,
                choices=[
                    ("one_hour", "One hour"),
                    ("twenty_four_hour", "Twenty-four hours"),
                    ("missed", "Missed training"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="sentnotification",
            name="delivery_state",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("queued", "Queued"),
                    ("failed", "Failed"),
                ],
                default="queued",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="sentnotification",
            name="occurrence_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sentnotification",
            name="occurrence_schedule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sent_occurrence_notifications",
                to="attendance.schedule",
            ),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="trigger_type",
            field=models.CharField(
                choices=[
                    ("sub_expiry_7d", "Абонемент истекает через 7 дней"),
                    ("sub_expiry_3d", "Абонемент истекает через 3 дня"),
                    ("sub_expiry_1d", "Абонемент истекает завтра"),
                    ("trainings_left_2", "Осталось 2 занятия"),
                    ("trainings_last", "Последнее занятие"),
                    ("parent_checkin", "Родитель: ребёнок на тренировке"),
                    ("parent_sub_expiry", "Родитель: абонемент ребёнка истекает"),
                    ("parent_grade_up", "Родитель: повышение грейда"),
                    ("trial_feedback", "Отзыв после пробной тренировки"),
                    ("training_reminder", "Напоминание о тренировке (за 1 час)"),
                    ("training_reminder_24h", "Напоминание о тренировке (за 24 часа)"),
                    ("missed_training", "Пропуск привычной тренировки"),
                    ("follow_up", "Напоминание по задаче"),
                    ("churned_survey", "Опрос ушедшего ученика"),
                ],
                max_length=30,
            ),
        ),
        migrations.AddConstraint(
            model_name="sentnotification",
            constraint=models.UniqueConstraint(
                condition=models.Q(("occurrence_schedule__isnull", True)),
                fields=("club", "student", "notification_type", "sent_date"),
                name="unique_notification_per_student_per_day",
            ),
        ),
        migrations.AddConstraint(
            model_name="sentnotification",
            constraint=models.UniqueConstraint(
                condition=models.Q(("occurrence_schedule__isnull", False)),
                fields=(
                    "club",
                    "student",
                    "notification_type",
                    "occurrence_schedule",
                    "occurrence_date",
                    "delivery_stage",
                ),
                name="unique_occurrence_stage_notification",
            ),
        ),
        migrations.AddConstraint(
            model_name="sentnotification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("delivery_stage", ""),
                        ("occurrence_date__isnull", True),
                        ("occurrence_schedule__isnull", True),
                    ),
                    models.Q(
                        ("occurrence_date__isnull", False),
                        ("occurrence_schedule__isnull", False),
                        models.Q(("delivery_stage", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="valid_notification_occurrence_identity",
            ),
        ),
        migrations.RunPython(
            seed_twenty_four_hour_templates,
            unseed_twenty_four_hour_templates,
        ),
        migrations.RunPython(register_reminder_tasks, unregister_reminder_tasks),
    ]
