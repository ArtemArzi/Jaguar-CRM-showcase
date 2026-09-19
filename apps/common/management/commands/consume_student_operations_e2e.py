from datetime import date

from django.core.management.base import BaseCommand

from apps.attendance.services.student_corrections import preview_student_attendance, record_student_attendance
from apps.common.management.commands.prepare_student_operations_e2e import load_owned_fixture


class Command(BaseCommand):
    help = "Record one synthetic competing attendance while the browser correction form remains open."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)

    def handle(self, *args, **options):
        fixture = load_owned_fixture(options["fixture"])
        args = dict(
            club_id=fixture["club_id"],
            actor_user_id=fixture["owner"]["id"],
            student_id=fixture["student"]["id"],
            subscription_id=fixture["subscription_id"],
            component_id=fixture["component_id"],
            schedule_id=fixture["concurrent_schedule"]["id"],
            checkin_date=date.fromisoformat(fixture["concurrent_schedule"]["date"]),
        )
        preview = preview_student_attendance(**args)
        record_student_attendance(
            **args,
            expected_fingerprint=preview["fingerprint"],
            command_key=f"{fixture['fixture_id']}:concurrent",
            reason="Синтетическая параллельная отметка",
            channel="cli",
        )
        self.stdout.write('{"ok": true}')
