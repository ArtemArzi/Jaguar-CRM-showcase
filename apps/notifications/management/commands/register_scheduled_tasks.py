from django.core.management.base import BaseCommand
from django_q.models import Schedule


class Command(BaseCommand):
    help = "Register scheduled notification and billing tasks"

    def handle(self, *args, **options):
        Schedule.objects.update_or_create(
            name="cleanup_expired_student_import_files",
            defaults={
                "func": "apps.students.imports.tasks.cleanup_expired_import_files",
                "schedule_type": Schedule.HOURLY,
                "minutes": None,
            },
        )
        Schedule.objects.update_or_create(
            name="daily_subscription_expiry_notifications",
            defaults={
                "func": "apps.notifications.tasks.check_subscription_expiry",
                "schedule_type": Schedule.DAILY,
                "minutes": None,
            },
        )
        Schedule.objects.update_or_create(
            name="auto_unfreeze_expired_subscriptions",
            defaults={
                "func": "apps.billing.tasks.auto_unfreeze_expired",
                "schedule_type": Schedule.HOURLY,
                "minutes": None,
            },
        )
        Schedule.objects.update_or_create(
            name="expire_pending_bank_payment_orders",
            defaults={
                "func": "apps.billing.tasks.expire_pending_bank_payment_orders",
                "schedule_type": Schedule.HOURLY,
                "minutes": None,
            },
        )
        Schedule.objects.update_or_create(
            name="process_due_bank_payment_provider_work",
            defaults={
                "func": "apps.billing.tasks.process_due_bank_payment_provider_work",
                "schedule_type": Schedule.MINUTES,
                "minutes": 1,
            },
        )
        Schedule.objects.update_or_create(
            name="refresh_tochka_payment_readiness",
            defaults={
                "func": "apps.billing.tasks.refresh_tochka_payment_readiness_task",
                "schedule_type": Schedule.MINUTES,
                "minutes": 15,
            },
        )
        reminder_jobs = {
            "hourly_training_reminders": "apps.notifications.tasks.send_training_reminders",
            "hourly_training_reminders_24h": "apps.notifications.tasks.send_training_reminders_24h",
            "hourly_missed_training_check": "apps.notifications.tasks.check_missed_trainings",
        }
        for name, func in reminder_jobs.items():
            Schedule.objects.update_or_create(
                name=name,
                defaults={
                    "func": func,
                    "schedule_type": Schedule.HOURLY,
                    "minutes": None,
                },
            )
        Schedule.objects.filter(name="daily_missed_training_check").delete()
        self.stdout.write(self.style.SUCCESS("Scheduled tasks registered"))
