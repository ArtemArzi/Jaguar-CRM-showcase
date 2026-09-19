from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0025_personal_staff_command_attempt_snapshot"),
        ("billing", "0043_payment_command_identity_and_live_personal_origins"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonalSelfServiceCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("command_key", models.CharField(max_length=120)),
                ("command_fingerprint", models.CharField(max_length=64)),
                ("source", models.CharField(choices=[("student", "Student"), ("parent", "Parent")], max_length=20)),
                ("action", models.CharField(choices=[("book", "Book from entitlement"), ("pay", "Pay by SBP")], max_length=20)),
                ("offer_digest", models.CharField(blank=True, default="", max_length=64)),
                ("enrollment_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("reservation_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("result_bound_at", models.DateTimeField(blank=True, null=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="personal_self_service_commands", to=settings.AUTH_USER_MODEL)),
                ("availability_slot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="self_service_commands", to="attendance.personalavailabilityslot")),
                ("club", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(class)ss", to="clubs.club")),
                ("enrollment", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="self_service_personal_command", to="attendance.scheduleenrollment")),
                ("entitlement_subscription", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="personal_self_service_commands", to="billing.subscription")),
                ("entitlement_component", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="personal_self_service_commands", to="billing.subscriptioncomponent")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="personal_self_service_commands", to="students.student")),
            ],
        ),
        migrations.AddConstraint(
            model_name="personalselfservicecommand",
            constraint=models.UniqueConstraint(fields=("club", "command_key"), name="uniq_personal_self_service_command_key"),
        ),
        migrations.AddConstraint(
            model_name="personalselfservicecommand",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        result_bound_at__isnull=True,
                        enrollment_id_snapshot__isnull=True,
                        reservation_id_snapshot__isnull=True,
                        enrollment__isnull=True,
                        entitlement_subscription__isnull=True,
                        entitlement_component__isnull=True,
                    )
                    | models.Q(
                        result_bound_at__isnull=False,
                        enrollment_id_snapshot__isnull=False,
                        reservation_id_snapshot__isnull=True,
                        enrollment__isnull=False,
                        entitlement_subscription__isnull=False,
                    )
                    | models.Q(
                        result_bound_at__isnull=False,
                        enrollment_id_snapshot__isnull=True,
                        reservation_id_snapshot__isnull=False,
                        enrollment__isnull=True,
                        entitlement_subscription__isnull=True,
                        entitlement_component__isnull=True,
                    )
                ),
                name="personal_self_service_command_binding",
            ),
        ),
        migrations.AddIndex(
            model_name="personalselfservicecommand",
            index=models.Index(fields=["club", "student", "source", "created_at"], name="att_self_service_cmd_actor_idx"),
        ),
    ]
